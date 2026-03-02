"""단계형 마이그레이션 마법사 다이얼로그 (COPY 중심)

요구사항 (사용자 결정 반영)
- COPY 중심(대용량)
- 3단계 마법사
- 파티션은 "일부만 선택"하여 실행 가능
- 파티션 리스트에 "이미 완료된(과거 완료 이력/대상 DB에 데이터 존재)" 표시/확인
- 기존 데이터 존재 시: 경고+확인(C) (빈 테이블이면 자동 진행)
- 에러 처리: 파티션 단위 skip 지원
- 재개(resume): 원클릭 + 옵션 잠금
- 기본 날짜 범위: 최근 7일
- 파티션 탐색/완료여부 확인: UI 멈춤 방지를 위해 워커 스레드로 수행
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import psycopg2
from psycopg2 import sql
from PySide6.QtCore import QDate, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDateEdit,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.copy_migration_worker import CopyMigrationWorker
from src.core.partition_discovery import PartitionDiscovery
from src.core.table_types import TableType, TABLE_TYPE_CONFIG, get_all_table_types
from src.models.history import CheckpointManager, HistoryManager, MigrationHistoryItem
from src.models.profile import ConnectionProfile
from src.utils.enhanced_logger import log_emitter


def to_qdate(d: date) -> QDate:
    return QDate(d.year, d.month, d.day)


@dataclass
class PartitionSummary:
    table_name: str
    row_count: int
    table_type: TableType


class PartitionDiscoveryWorker(QThread):
    """파티션 탐색을 백그라운드에서 수행"""

    result = Signal(list)  # list[dict]
    error = Signal(str)

    def __init__(self, source_config: dict, start_date: date, end_date: date, table_types: list[TableType]):
        super().__init__()
        self.source_config = source_config
        self.start_date = start_date
        self.end_date = end_date
        self.table_types = table_types

    def run(self):
        try:
            discovery = PartitionDiscovery(self.source_config)
            partitions = discovery.discover_partitions(
                self.start_date,
                self.end_date,
                table_types=self.table_types,
            )
            self.result.emit(partitions or [])
        except Exception as e:
            self.error.emit(str(e))


class TargetCompletedCheckWorker(QThread):
    """대상 DB에 테이블/데이터가 이미 존재하는지(완료 후보) 확인"""

    progress = Signal(int, int)  # done, total
    result = Signal(dict)  # {table_name: bool}
    error = Signal(str)

    def __init__(self, target_config: dict, table_names: list[str]):
        super().__init__()
        self.target_config = target_config
        self.table_names = table_names

    def run(self):
        try:
            conn_params = {
                "host": self.target_config.get("host"),
                "port": self.target_config.get("port"),
                "database": self.target_config.get("database"),
                "user": self.target_config.get("username"),
                "password": self.target_config.get("password"),
            }
            if self.target_config.get("ssl"):
                conn_params["sslmode"] = "require"

            conn = psycopg2.connect(**conn_params)
            conn.autocommit = True

            results: dict[str, bool] = {}
            total = len(self.table_names)

            with conn.cursor() as cur:
                for i, table_name in enumerate(self.table_names, start=1):
                    # table exists?
                    cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_schema = 'public'
                            AND table_name = %s
                        )
                        """,
                        (table_name,),
                    )
                    exists = bool(cur.fetchone()[0])
                    has_data = False

                    if exists:
                        # cheap-ish check: any row?
                        q = sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(table_name))
                        try:
                            cur.execute(q)
                            has_data = cur.fetchone() is not None
                        except Exception:
                            # 권한/테이블 타입 이슈 등은 "있음"으로 보지 않고 False
                            has_data = False

                    results[table_name] = has_data
                    self.progress.emit(i, total)

            conn.close()
            self.result.emit(results)
        except Exception as e:
            self.error.emit(str(e))


class MigrationWizardDialog(QDialog):
    """COPY 중심 단계형 마이그레이션 마법사"""

    def __init__(self, parent=None, profile: ConnectionProfile | None = None):
        super().__init__(parent)
        if profile is None:
            raise ValueError("profile is required")

        self.profile = profile
        self.history_manager = HistoryManager()
        self.checkpoint_manager = CheckpointManager()

        # 실행 상태
        self.worker: CopyMigrationWorker | None = None
        self.connection_checker: CopyMigrationWorker | None = None
        self.discovery_worker: PartitionDiscoveryWorker | None = None
        self.completed_check_worker: TargetCompletedCheckWorker | None = None

        self.history_id: int | None = None
        self.resume_mode: bool = False

        # 연결 상태
        self.source_connected = False
        self.target_connected = False
        self.source_status_message = "확인 중..."
        self.target_status_message = "확인 중..."

        # 선택 상태 (Step 2)
        self.selected_table_types: list[TableType] = [TableType.POINT_HISTORY]
        self.table_type_checkboxes: dict[TableType, QCheckBox] = {}
        self.error_strategy = "stop"  # stop|skip
        # COPY 청크(배치) 크기 기본값: 250k (대용량 환경 튜닝 결과)
        self.batch_size = 250000

        # 탐색 결과
        self.discovered_partitions: list[PartitionSummary] = []
        self._completed_from_last_history: set[str] = set()
        self._target_has_data: dict[str, bool] = {}

        # UI
        self.setup_ui()
        self._bind_ui()

        # 기본 날짜: 최근 7일
        today = datetime.now().date()
        self.start_date_edit.setDate(to_qdate(today - timedelta(days=6)))
        self.end_date_edit.setDate(to_qdate(today))

        # 미완료 작업 확인 + 연결 체크
        self._check_incomplete_migration()
        QTimer.singleShot(50, self.check_connections)

    # ============================
    # UI
    # ============================

    def setup_ui(self):
        self.setWindowTitle(f"마이그레이션 마법사 - {self.profile.name}")
        self.setModal(True)
        self.resize(1000, 850)

        root = QVBoxLayout(self)

        self.step_title = QLabel()
        self.step_title.setStyleSheet("font-size: 18px; font-weight: bold;")
        root.addWidget(self.step_title)

        self.step_hint = QLabel()
        self.step_hint.setStyleSheet("color: #BBBBBB;")
        root.addWidget(self.step_hint)

        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)

        self.page_connection = self._build_page_connection()
        self.page_scope = self._build_page_scope()
        self.page_run = self._build_page_run()

        self.pages.addWidget(self.page_connection)
        self.pages.addWidget(self.page_scope)
        self.pages.addWidget(self.page_run)

        nav = QHBoxLayout()
        self.back_btn = QPushButton("이전")
        self.next_btn = QPushButton("다음")
        self.close_btn = QPushButton("닫기")

        self.back_btn.clicked.connect(self.go_back)
        self.next_btn.clicked.connect(self.go_next)
        self.close_btn.clicked.connect(self.close)

        nav.addWidget(self.back_btn)
        nav.addWidget(self.next_btn)
        nav.addStretch(1)
        nav.addWidget(self.close_btn)

        root.addLayout(nav)

        self._update_step_ui()
        self._update_nav_state()

    def _build_page_connection(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(self._create_connection_status_widget())

        actions = QHBoxLayout()
        self.recheck_btn = QPushButton("연결 다시 확인")
        self.recheck_btn.clicked.connect(self.check_connections)
        actions.addWidget(self.recheck_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        # 미완료 작업(있을 때만)
        self.incomplete_group = QGroupBox("미완료 작업")
        self.incomplete_group.setVisible(False)
        ig = QVBoxLayout(self.incomplete_group)

        self.incomplete_label = QLabel("")
        self.incomplete_label.setWordWrap(True)
        ig.addWidget(self.incomplete_label)

        ig_actions = QHBoxLayout()
        self.resume_btn = QPushButton("이어서 진행")
        self.resume_btn.clicked.connect(self._on_resume_clicked)
        self.new_run_btn = QPushButton("새 작업으로 진행")
        self.new_run_btn.clicked.connect(self._on_new_run_clicked)
        ig_actions.addWidget(self.resume_btn)
        ig_actions.addWidget(self.new_run_btn)
        ig_actions.addStretch(1)
        ig.addLayout(ig_actions)

        layout.addWidget(self.incomplete_group)

        tip = QLabel(
            "1) 연결 확인 → 2) 범위/파티션 선택 → 3) 실행 순서로 진행합니다.\n"
            "연결이 둘 다 성공해야 다음 단계로 넘어갈 수 있습니다."
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #AAAAAA;")
        layout.addWidget(tip)

        layout.addStretch(1)
        return page

    def _build_page_scope(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(self._create_scope_group())
        layout.addWidget(self._create_partition_group(), 1)

        return page

    def _build_page_run(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(self._create_run_summary_group())
        layout.addWidget(self._create_progress_group())
        layout.addWidget(self._create_log_group(), 1)
        layout.addWidget(self._create_run_controls())

        return page

    # ----------------------------
    # Common widgets
    # ----------------------------

    def _create_connection_status_widget(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(10, 8, 10, 8)

        def mk_block(title: str):
            title_label = QLabel(f"{title}:")
            title_label.setStyleSheet("font-weight: bold;")
            icon = QLabel("●")
            icon.setStyleSheet("color: #FFFF00; font-size: 16px;")
            text = QLabel("확인 중...")
            text.setStyleSheet("color: #DDDDDD;")
            return title_label, icon, text

        s_title, self.source_status_icon, self.source_status_text = mk_block("소스 DB")
        t_title, self.target_status_icon, self.target_status_text = mk_block("대상 DB")

        layout.addWidget(s_title)
        layout.addWidget(self.source_status_icon)
        layout.addWidget(self.source_status_text)
        layout.addSpacing(30)
        layout.addWidget(t_title)
        layout.addWidget(self.target_status_icon)
        layout.addWidget(self.target_status_text)
        layout.addStretch(1)

        widget.setStyleSheet(
            """
            QWidget {
                background-color: #2b2b2b;
                border: 1px solid #444;
                border-radius: 6px;
            }
            """
        )
        return widget

    # ----------------------------
    # Step 2 widgets
    # ----------------------------

    def _create_scope_group(self) -> QGroupBox:
        group = QGroupBox("2/3 범위 선택")
        layout = QVBoxLayout(group)

        # 테이블 타입
        row_types = QHBoxLayout()
        row_types.addWidget(QLabel("마이그레이션 항목:"))

        for table_type in get_all_table_types():
            config = TABLE_TYPE_CONFIG[table_type]
            checkbox = QCheckBox(f"{config.display_name} ({table_type.value})")
            checkbox.setToolTip(config.description)
            if table_type == TableType.POINT_HISTORY:
                checkbox.setChecked(True)
            checkbox.stateChanged.connect(self._on_table_type_changed)
            self.table_type_checkboxes[table_type] = checkbox
            row_types.addWidget(checkbox)

        row_types.addStretch(1)
        layout.addLayout(row_types)

        # 에러 + 배치 크기
        row_opts = QHBoxLayout()
        row_opts.addWidget(QLabel("에러 처리:"))

        self.stop_on_error_radio = QRadioButton("중단")
        self.stop_on_error_radio.setChecked(True)
        self.skip_on_error_radio = QRadioButton("건너뛰기(파티션 단위)")

        bg = QButtonGroup(self)
        bg.addButton(self.stop_on_error_radio)
        bg.addButton(self.skip_on_error_radio)

        self.stop_on_error_radio.toggled.connect(self._on_error_strategy_changed)
        self.skip_on_error_radio.toggled.connect(self._on_error_strategy_changed)

        row_opts.addWidget(self.stop_on_error_radio)
        row_opts.addWidget(self.skip_on_error_radio)
        row_opts.addSpacing(20)

        row_opts.addWidget(QLabel("배치 크기:"))
        self.batch_size_spin = QSpinBox()
        self.batch_size_spin.setMinimum(1000)
        self.batch_size_spin.setMaximum(500000)
        self.batch_size_spin.setSingleStep(10000)
        self.batch_size_spin.setValue(self.batch_size)
        self.batch_size_spin.setSuffix(" rows")
        self.batch_size_spin.setToolTip("COPY 청크 단위 LIMIT (1,000 ~ 500,000)")
        row_opts.addWidget(self.batch_size_spin)

        row_opts.addStretch(1)
        layout.addLayout(row_opts)

        # 날짜 + 프리셋
        row_dates = QHBoxLayout()
        row_dates.addWidget(QLabel("날짜 범위:"))

        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.end_date_edit = QDateEdit()
        self.end_date_edit.setCalendarPopup(True)

        row_dates.addWidget(QLabel("시작"))
        row_dates.addWidget(self.start_date_edit)
        row_dates.addWidget(QLabel("종료"))
        row_dates.addWidget(self.end_date_edit)
        row_dates.addStretch(1)
        layout.addLayout(row_dates)

        preset = QHBoxLayout()
        self.preset_today_btn = QPushButton("오늘")
        self.preset_yesterday_btn = QPushButton("어제")
        self.preset_7d_btn = QPushButton("최근 7일")
        self.preset_30d_btn = QPushButton("최근 30일")

        self.preset_today_btn.clicked.connect(lambda: self._set_preset_days(0))
        self.preset_yesterday_btn.clicked.connect(lambda: self._set_preset_days(1, yesterday=True))
        self.preset_7d_btn.clicked.connect(lambda: self._set_preset_days(7))
        self.preset_30d_btn.clicked.connect(lambda: self._set_preset_days(30))

        preset.addWidget(self.preset_today_btn)
        preset.addWidget(self.preset_yesterday_btn)
        preset.addWidget(self.preset_7d_btn)
        preset.addWidget(self.preset_30d_btn)
        preset.addStretch(1)
        layout.addLayout(preset)

        # 탐색/확인 버튼
        row_actions = QHBoxLayout()
        self.discover_btn = QPushButton("파티션 찾기")
        self.discover_btn.clicked.connect(self.discover_partitions)

        self.check_completed_btn = QPushButton("완료여부 확인(대상 DB)")
        self.check_completed_btn.clicked.connect(self.check_target_completed)
        self.check_completed_btn.setEnabled(False)

        row_actions.addWidget(self.discover_btn)
        row_actions.addWidget(self.check_completed_btn)

        self.discover_status = QLabel("날짜/항목을 선택하고 ‘파티션 찾기’를 눌러주세요.")
        self.discover_status.setStyleSheet("color: #AAAAAA;")
        row_actions.addWidget(self.discover_status)
        row_actions.addStretch(1)

        layout.addLayout(row_actions)

        # 선택 편의
        row_select = QHBoxLayout()
        self.select_all_btn = QPushButton("전체 선택")
        self.select_none_btn = QPushButton("전체 해제")
        self.select_pending_btn = QPushButton("완료 제외 선택")

        self.select_all_btn.clicked.connect(lambda: self._bulk_check(True))
        self.select_none_btn.clicked.connect(lambda: self._bulk_check(False))
        self.select_pending_btn.clicked.connect(self._select_excluding_completed)

        row_select.addWidget(self.select_all_btn)
        row_select.addWidget(self.select_none_btn)
        row_select.addWidget(self.select_pending_btn)
        row_select.addStretch(1)
        layout.addLayout(row_select)

        return group

    def _create_partition_group(self) -> QGroupBox:
        group = QGroupBox("발견된 파티션(체크해서 일부만 실행 가능)")
        layout = QVBoxLayout(group)

        self.partition_list = QListWidget()
        layout.addWidget(self.partition_list)

        info = QHBoxLayout()
        self.partition_count_label = QLabel("총 0개")
        self.partition_rows_label = QLabel("총 0 rows")
        self.partition_selected_label = QLabel("선택 0개")
        self.partition_count_label.setStyleSheet("font-weight: bold;")
        self.partition_selected_label.setStyleSheet("font-weight: bold;")

        info.addWidget(self.partition_count_label)
        info.addSpacing(20)
        info.addWidget(self.partition_selected_label)
        info.addSpacing(20)
        info.addWidget(self.partition_rows_label)
        info.addStretch(1)

        layout.addLayout(info)
        return group

    # ----------------------------
    # Step 3 widgets
    # ----------------------------

    def _create_run_summary_group(self) -> QGroupBox:
        group = QGroupBox("3/3 실행 요약")
        layout = QVBoxLayout(group)
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        return group

    def _create_progress_group(self) -> QGroupBox:
        group = QGroupBox("진행 상황")
        layout = QVBoxLayout(group)

        total_row = QHBoxLayout()
        total_row.addWidget(QLabel("전체:"))
        self.total_progress = QProgressBar()
        total_row.addWidget(self.total_progress)
        self.total_label = QLabel("0 / 0")
        total_row.addWidget(self.total_label)
        layout.addLayout(total_row)

        cur_row = QHBoxLayout()
        cur_row.addWidget(QLabel("현재:"))
        self.current_progress = QProgressBar()
        cur_row.addWidget(self.current_progress)
        self.current_label = QLabel("대기중")
        cur_row.addWidget(self.current_label)
        layout.addLayout(cur_row)

        info = QHBoxLayout()
        self.speed_label = QLabel("처리 속도: 0 rows/sec")
        self.data_rate_label = QLabel("전송 속도: 0 MB/sec")
        self.eta_label = QLabel("예상 완료: 계산중...")
        self.elapsed_label = QLabel("경과 시간: 00:00:00")
        info.addWidget(self.speed_label)
        info.addWidget(self.data_rate_label)
        info.addWidget(self.eta_label)
        info.addWidget(self.elapsed_label)
        info.addStretch(1)
        layout.addLayout(info)

        return group

    def _create_log_group(self) -> QGroupBox:
        group = QGroupBox("실행 로그")
        layout = QVBoxLayout(group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)
        return group

    def _create_run_controls(self) -> QWidget:
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)

        self.start_btn = QPushButton("시작")
        self.start_btn.clicked.connect(self.start_migration)

        self.pause_btn = QPushButton("일시정지")
        self.pause_btn.clicked.connect(self.pause_migration)
        self.pause_btn.setEnabled(False)

        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.clicked.connect(self.cancel_migration)

        layout.addWidget(self.start_btn)
        layout.addWidget(self.pause_btn)
        layout.addWidget(self.cancel_btn)
        layout.addStretch(1)
        return w

    # ============================
    # Navigation
    # ============================

    def go_back(self):
        idx = self.pages.currentIndex()
        if idx > 0:
            self.pages.setCurrentIndex(idx - 1)
        self._update_step_ui()
        self._update_nav_state()

    def go_next(self):
        idx = self.pages.currentIndex()
        if idx == 0:
            self.pages.setCurrentIndex(1)
            self._load_last_completed_partitions_cache()
        elif idx == 1:
            selected = self.get_selected_partition_names()
            if not selected:
                QMessageBox.warning(self, "선택 필요", "실행할 파티션을 1개 이상 선택하세요.")
                return
            # 선택을 고정
            self._frozen_selection = selected
            self.pages.setCurrentIndex(2)
        self._update_step_ui()
        self._update_nav_state()

    def _update_step_ui(self):
        idx = self.pages.currentIndex()
        titles = ["1/3 연결 확인", "2/3 범위/파티션 선택", "3/3 실행"]
        hints = [
            "소스/대상 DB 연결 상태를 확인합니다.",
            "날짜/항목을 선택하고 파티션을 탐색한 후, 일부만 체크해서 실행할 수 있습니다.",
            "COPY 마이그레이션을 실행하고 진행/로그를 확인합니다.",
        ]
        self.step_title.setText(titles[idx])
        self.step_hint.setText(hints[idx])

        if idx == 2:
            self._refresh_summary()

    def _update_nav_state(self):
        idx = self.pages.currentIndex()
        self.back_btn.setEnabled(idx > 0)

        if idx == 0:
            self.next_btn.setText("다음")
            self.next_btn.setEnabled(self.source_connected and self.target_connected)
        elif idx == 1:
            self.next_btn.setText("실행")
            self.next_btn.setEnabled(len(self.get_selected_partition_names()) > 0)
        else:
            self.next_btn.setText("다음")
            self.next_btn.setEnabled(False)

        # 실행 중: 이동/닫기 제한
        if self.worker and getattr(self.worker, "is_running", False):
            self.back_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            self.close_btn.setEnabled(False)
        else:
            self.close_btn.setEnabled(True)

    # ============================
    # Connection check
    # ============================

    def check_connections(self):
        if self.connection_checker and self.connection_checker.isRunning():
            return

        self.connection_checker = CopyMigrationWorker(
            profile=self.profile,
            partitions=[],
            history_id=0,
            resume=False,
        )
        self.connection_checker.connection_checking.connect(self._on_connection_checking)
        self.connection_checker.source_connection_status.connect(self._on_source_connection_status)
        self.connection_checker.target_connection_status.connect(self._on_target_connection_status)
        self.connection_checker.finished.connect(lambda: self._update_nav_state())

        self.connection_checker.check_connections_only = True
        self.connection_checker.start()

    def _on_connection_checking(self):
        self.source_status_icon.setStyleSheet("color: #FFFF00; font-size: 16px;")
        self.source_status_text.setText("확인 중...")
        self.target_status_icon.setStyleSheet("color: #FFFF00; font-size: 16px;")
        self.target_status_text.setText("확인 중...")

    def _on_source_connection_status(self, connected: bool, message: str):
        self.source_connected = connected
        self.source_status_message = message
        if connected:
            self.source_status_icon.setStyleSheet("color: #00FF00; font-size: 16px;")
            self.source_status_text.setText("연결됨")
        else:
            self.source_status_icon.setStyleSheet("color: #FF0000; font-size: 16px;")
            self.source_status_text.setText(message)
        self._update_nav_state()

    def _on_target_connection_status(self, connected: bool, message: str):
        self.target_connected = connected
        self.target_status_message = message
        if connected:
            self.target_status_icon.setStyleSheet("color: #00FF00; font-size: 16px;")
            self.target_status_text.setText("연결됨")
        else:
            self.target_status_icon.setStyleSheet("color: #FF0000; font-size: 16px;")
            self.target_status_text.setText(message)
        self._update_nav_state()

    # ============================
    # Step 1: resume
    # ============================

    def _check_incomplete_migration(self):
        incomplete = self.history_manager.get_incomplete_history(self.profile.id)
        self._incomplete_history: MigrationHistoryItem | None = incomplete

        if not incomplete:
            self.incomplete_group.setVisible(False)
            return

        self.incomplete_group.setVisible(True)
        self.incomplete_label.setText(
            "이전에 중단된 마이그레이션이 있습니다.\n"
            f"날짜: {incomplete.start_date} ~ {incomplete.end_date}\n"
            f"진행: {incomplete.processed_rows:,} / {incomplete.total_rows:,} (rows)\n\n"
            "‘이어서 진행’을 선택하면 옵션 변경 없이 미완료 파티션만 재개합니다."
        )

    def _lock_options_for_resume(self):
        # 재개 모드에서는 옵션 변경 불가 (결정 4:A)
        for cb in self.table_type_checkboxes.values():
            cb.setEnabled(False)
        self.stop_on_error_radio.setEnabled(False)
        self.skip_on_error_radio.setEnabled(False)
        self.batch_size_spin.setEnabled(False)
        self.start_date_edit.setEnabled(False)
        self.end_date_edit.setEnabled(False)
        self.discover_btn.setEnabled(False)
        self.check_completed_btn.setEnabled(False)
        self.select_all_btn.setEnabled(False)
        self.select_none_btn.setEnabled(False)
        self.select_pending_btn.setEnabled(False)

    def _on_resume_clicked(self):
        if not self._incomplete_history:
            return

        self.resume_mode = True
        self.history_id = self._incomplete_history.id

        # 날짜/옵션 UI에 반영(표시용)
        try:
            start_d = datetime.strptime(self._incomplete_history.start_date, "%Y-%m-%d").date()
            end_d = datetime.strptime(self._incomplete_history.end_date, "%Y-%m-%d").date()
            self.start_date_edit.setDate(to_qdate(start_d))
            self.end_date_edit.setDate(to_qdate(end_d))
        except Exception:
            pass

        # 재개 모드에서는 옵션 변경 불가
        self._lock_options_for_resume()

        pending = self.checkpoint_manager.get_pending_checkpoints(self.history_id)
        pending_names = [cp.partition_name for cp in pending]

        # 선택 고정(재개는 pending만)
        self._frozen_selection = pending_names

        # 실행 페이지로 이동
        self.pages.setCurrentIndex(2)
        self._update_step_ui()
        self._update_nav_state()

        self.add_log(f"재개 모드: 미완료 파티션 {len(pending_names)}개", "INFO")

    def _on_new_run_clicked(self):
        self.resume_mode = False
        self.history_id = None
        QMessageBox.information(self, "안내", "새 작업으로 진행합니다. 다음 단계에서 범위를 선택해주세요.")

    # ============================
    # Step 2: discovery & completed checks
    # ============================

    def _load_last_completed_partitions_cache(self):
        """마지막 완료 이력(완료 status) 기준으로 completed 파티션 캐시"""
        try:
            histories = [h for h in self.history_manager.get_all_history() if h.profile_id == self.profile.id]
            completed = [h for h in histories if h.status == "completed"]
            if not completed:
                self._completed_from_last_history = set()
                return

            # 최신 completed 1개만
            latest = completed[0]
            cps = self.checkpoint_manager.get_checkpoints(latest.id)
            self._completed_from_last_history = {c.partition_name for c in cps if c.status == "completed"}
        except Exception:
            self._completed_from_last_history = set()

    def _on_table_type_changed(self, _state: int):
        self.selected_table_types = [
            tt for tt, cb in self.table_type_checkboxes.items() if cb.isChecked()
        ]
        if not self.selected_table_types:
            sender = self.sender()
            if isinstance(sender, QCheckBox):
                sender.setChecked(True)
            QMessageBox.warning(self, "선택 오류", "최소 1개의 테이블 타입을 선택해야 합니다.")

    def _on_error_strategy_changed(self, _checked: bool):
        self.error_strategy = "stop" if self.stop_on_error_radio.isChecked() else "skip"

    def _set_preset_days(self, days: int, yesterday: bool = False):
        today = datetime.now().date()
        if yesterday:
            d = today - timedelta(days=1)
            self.start_date_edit.setDate(to_qdate(d))
            self.end_date_edit.setDate(to_qdate(d))
            return

        if days == 0:
            self.start_date_edit.setDate(to_qdate(today))
            self.end_date_edit.setDate(to_qdate(today))
            return

        start = today - timedelta(days=days - 1)
        self.start_date_edit.setDate(to_qdate(start))
        self.end_date_edit.setDate(to_qdate(today))

    def discover_partitions(self):
        if not (self.source_connected and self.target_connected):
            QMessageBox.warning(self, "연결 필요", "먼저 소스/대상 DB 연결이 모두 성공해야 합니다.")
            return

        start_date = self.start_date_edit.date().toPython()
        end_date = self.end_date_edit.date().toPython()
        if start_date > end_date:
            QMessageBox.warning(self, "날짜 오류", "시작 날짜가 종료 날짜보다 늦습니다.")
            return

        if self.discovery_worker and self.discovery_worker.isRunning():
            return

        self.discover_status.setText("파티션 탐색 중...")
        self.discover_btn.setEnabled(False)
        self.check_completed_btn.setEnabled(False)
        self.partition_list.clear()
        self.discovered_partitions = []
        self._target_has_data = {}

        self.discovery_worker = PartitionDiscoveryWorker(
            self.profile.source_config,
            start_date,
            end_date,
            self.selected_table_types,
        )
        self.discovery_worker.result.connect(self._on_discovery_result)
        self.discovery_worker.error.connect(self._on_discovery_error)
        self.discovery_worker.finished.connect(lambda: self.discover_btn.setEnabled(True))
        self.discovery_worker.start()

    def _on_discovery_error(self, msg: str):
        self.discover_status.setText("파티션 탐색 오류")
        self.add_log(f"파티션 탐색 오류: {msg}", "ERROR")
        self._update_counts()
        self._update_nav_state()

    def _on_discovery_result(self, partitions: list):
        summaries: list[PartitionSummary] = []
        for p in partitions or []:
            if not isinstance(p, dict):
                continue
            try:
                tt: TableType = p["table_type"]
                name = p.get("table_name")
                if not name:
                    continue
                summaries.append(
                    PartitionSummary(
                        table_name=name,
                        row_count=int(p.get("row_count") or 0),
                        table_type=tt,
                    )
                )
            except Exception:
                continue

        self.discovered_partitions = summaries
        self._render_partition_list()

        if summaries:
            self.discover_status.setText(f"완료: {len(summaries)}개 파티션")
            self.add_log(f"파티션 {len(summaries)}개 발견", "INFO")
            self.check_completed_btn.setEnabled(True)
        else:
            self.discover_status.setText("조건에 해당하는 파티션이 없습니다")
            self.add_log("선택한 조건에 해당하는 파티션이 없습니다", "WARNING")
            self.check_completed_btn.setEnabled(False)

        self._update_counts()
        self._update_nav_state()

    def _render_partition_list(self):
        self.partition_list.clear()
        display_limit = 300

        for s in self.discovered_partitions[:display_limit]:
            cfg = TABLE_TYPE_CONFIG.get(s.table_type)
            prefix = f"[{cfg.display_name}] " if cfg else ""

            status_tags: list[str] = []
            if s.table_name in self._completed_from_last_history:
                status_tags.append("이전완료")
            if self._target_has_data.get(s.table_name):
                status_tags.append("대상데이터")

            tag_text = f" [{'|'.join(status_tags)}]" if status_tags else ""
            text = f"{prefix}{s.table_name} ({s.row_count:,} rows){tag_text}"

            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, s.table_name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)

            # 기본 체크: 완료로 판단되면 기본 해제, 그 외 체크
            is_completed_like = (s.table_name in self._completed_from_last_history) or bool(
                self._target_has_data.get(s.table_name)
            )
            item.setCheckState(Qt.Unchecked if is_completed_like else Qt.Checked)

            if is_completed_like:
                item.setForeground(Qt.gray)
                item.setToolTip("이미 완료된 것으로 보입니다. 다시 실행하려면 체크하세요.")

            self.partition_list.addItem(item)

        if len(self.discovered_partitions) > display_limit:
            self.partition_list.addItem(
                QListWidgetItem(f"... 외 {len(self.discovered_partitions) - display_limit}개")
            )

    def _bulk_check(self, checked: bool):
        for i in range(self.partition_list.count()):
            item = self.partition_list.item(i)
            name = item.data(Qt.UserRole)
            if not name:
                continue
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self._update_counts()
        self._update_nav_state()

    def _select_excluding_completed(self):
        for i in range(self.partition_list.count()):
            item = self.partition_list.item(i)
            name = item.data(Qt.UserRole)
            if not name:
                continue
            is_completed_like = (name in self._completed_from_last_history) or bool(
                self._target_has_data.get(name)
            )
            item.setCheckState(Qt.Unchecked if is_completed_like else Qt.Checked)
        self._update_counts()
        self._update_nav_state()

    def get_selected_partition_names(self) -> list[str]:
        selected: list[str] = []
        for i in range(self.partition_list.count()):
            item = self.partition_list.item(i)
            name = item.data(Qt.UserRole)
            if not name:
                continue
            if item.checkState() == Qt.Checked:
                selected.append(str(name))
        return selected

    def _update_counts(self):
        total = len(self.discovered_partitions)
        selected = len(self.get_selected_partition_names())
        total_rows = sum(s.row_count for s in self.discovered_partitions)

        self.partition_count_label.setText(f"총 {total}개")
        self.partition_selected_label.setText(f"선택 {selected}개")
        self.partition_rows_label.setText(f"총 {total_rows:,} rows")

    def check_target_completed(self):
        if not self.discovered_partitions:
            return

        if self.completed_check_worker and self.completed_check_worker.isRunning():
            return

        names = [s.table_name for s in self.discovered_partitions]
        self.check_completed_btn.setEnabled(False)
        self.discover_status.setText("대상 DB 확인 중...")

        self.completed_check_worker = TargetCompletedCheckWorker(self.profile.target_config, names)
        self.completed_check_worker.progress.connect(self._on_target_check_progress)
        self.completed_check_worker.result.connect(self._on_target_check_result)
        self.completed_check_worker.error.connect(self._on_target_check_error)
        self.completed_check_worker.finished.connect(lambda: self.check_completed_btn.setEnabled(True))
        self.completed_check_worker.start()

    def _on_target_check_progress(self, done: int, total: int):
        self.discover_status.setText(f"대상 DB 확인 중... ({done}/{total})")

    def _on_target_check_error(self, msg: str):
        self.add_log(f"대상 DB 완료여부 확인 오류: {msg}", "ERROR")
        self.discover_status.setText("대상 DB 확인 오류")
        self._update_nav_state()

    def _on_target_check_result(self, result: dict):
        self._target_has_data = {str(k): bool(v) for k, v in (result or {}).items()}
        self.add_log("대상 DB 완료여부 확인 완료", "INFO")
        self.discover_status.setText("대상 DB 확인 완료")
        self._render_partition_list()
        self._update_counts()
        self._update_nav_state()

    # ============================
    # Step 3: run
    # ============================

    def _refresh_summary(self):
        if self.resume_mode:
            parts = getattr(self, "_frozen_selection", [])
            error_text = "중단" if self.error_strategy == "stop" else "건너뛰기"
            lines = [
                f"프로필: {self.profile.name}",
                "방식: COPY (고성능)",
                "모드: 재개(옵션 잠금)",
                f"파티션: {len(parts)}개(미완료만)",
                f"에러 처리: {error_text}",
            ]
            self.summary_label.setText("\n".join(lines))
            return

        start_date = self.start_date_edit.date().toPython()
        end_date = self.end_date_edit.date().toPython()
        types_text = ", ".join([t.value for t in self.selected_table_types])
        error_text = "중단" if self.error_strategy == "stop" else "건너뛰기"
        parts = getattr(self, "_frozen_selection", self.get_selected_partition_names())

        lines = [
            f"프로필: {self.profile.name}",
            "방식: COPY (고성능)",
            f"에러 처리: {error_text}",
            f"배치 크기: {int(self.batch_size_spin.value()):,} rows",
            f"날짜: {start_date} ~ {end_date}",
            f"테이블 타입: {types_text}",
            f"파티션: {len(parts)}개 (선택된 항목만)",
        ]
        self.summary_label.setText("\n".join(lines))

    def start_migration(self):
        if self.worker and self.worker.isRunning():
            return

        if not (self.source_connected and self.target_connected):
            QMessageBox.warning(self, "연결 필요", "소스/대상 DB가 모두 연결되어야 합니다.")
            return

        partitions = getattr(self, "_frozen_selection", [])
        if not partitions:
            QMessageBox.warning(self, "파티션 없음", "실행할 파티션이 없습니다.")
            return

        if self.resume_mode:
            # 재개 모드: history_id 필수
            if self.history_id is None:
                QMessageBox.critical(self, "오류", "재개 이력 ID가 없습니다.")
                return
        else:
            # 새 이력 생성 + 체크포인트 생성
            start_date = self.start_date_edit.date().toPython()
            end_date = self.end_date_edit.date().toPython()

            source_status = "연결 성공" if self.source_connected else self.source_status_message
            target_status = "연결 성공" if self.target_connected else self.target_status_message

            history = self.history_manager.create_history(
                self.profile.id,
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
                source_status=source_status,
                target_status=target_status,
            )
            self.history_id = history.id

            for p in partitions:
                self.checkpoint_manager.create_checkpoint(self.history_id, p)

        self.batch_size = int(self.batch_size_spin.value())

        self.worker = CopyMigrationWorker(
            self.profile,
            partitions,
            self.history_id,
            resume=self.resume_mode,
            batch_size=self.batch_size,
        )

        # 에러 처리 전략
        self.worker.skip_on_error = self.error_strategy == "skip"

        # 기존 데이터 처리(C): row_count>0이면 UI에서 확인
        self.worker.truncate_requested.connect(self.on_truncate_requested)

        self._worker_had_error = False

        self.worker.progress.connect(self.on_progress)
        self.worker.log.connect(self.add_log)
        # QThread.finished는 "스레드 종료" 의미(성공/중단/오류 모두)라서,
        # 여기서는 종료 이벤트로 받고, 성공 여부는 worker.is_running / _worker_had_error로 판정한다.
        self.worker.finished.connect(self.on_worker_thread_finished)
        self.worker.error.connect(self.on_error)
        self.worker.performance.connect(self.on_performance_update)

        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.close_btn.setEnabled(False)

        self.add_log(
            f"마이그레이션 시작(COPY) - 파티션 {len(partitions)}개, 배치 {self.batch_size:,}",
            "INFO",
        )

        self.worker.start()
        self._update_nav_state()

    def on_truncate_requested(self, table_name: str, row_count: int):
        reply = QMessageBox.question(
            self,
            "기존 데이터 발견",
            f"대상 테이블 {table_name}에 {row_count:,}개의 데이터가 있습니다.\n\n"
            "삭제(TRUNCATE)하고 계속 진행할까요?\n\n"
            "Yes: 삭제 후 진행\n"
            "No: 해당 파티션 실패 처리(에러 전략에 따라 중단/스킵)",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if self.worker:
            self.worker.truncate_permission = reply == QMessageBox.Yes
            if reply == QMessageBox.No and not self.worker.skip_on_error:
                # 중단 모드면 즉시 stop(대화상자 반환 후 워커가 예외 처리)
                self.worker.stop()

    def pause_migration(self):
        if not self.worker:
            return

        if self.pause_btn.text() == "일시정지":
            self.worker.pause()
            self.pause_btn.setText("재개")
            self.add_log("일시정지 요청", "INFO")
        else:
            self.worker.resume()
            self.pause_btn.setText("일시정지")
            self.add_log("재개 요청", "INFO")

    def cancel_migration(self):
        if self.worker and getattr(self.worker, "is_running", False):
            reply = QMessageBox.question(
                self,
                "확인",
                "마이그레이션을 취소하시겠습니까?\n완료된 파티션은 유지되며, 나중에 이어서 진행할 수 있습니다.",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self.worker.stop()
                self.add_log("사용자가 마이그레이션을 취소했습니다", "WARNING")
        else:
            self.close()

    def on_progress(self, data: dict):
        if "total_progress" in data:
            self.total_progress.setValue(int(data["total_progress"]))
            self.total_label.setText(
                f"{data.get('completed_partitions', 0)} / {data.get('total_partitions', 0)}"
            )

        if "current_progress" in data:
            self.current_progress.setValue(int(data["current_progress"]))
            part = data.get("current_partition", "")
            rows = int(data.get("current_rows", 0) or 0)
            self.current_label.setText(f"{part} ({rows:,} rows)")

        if "speed" in data:
            self.speed_label.setText(f"처리 속도: {int(data['speed']):,} rows/sec")

    def on_performance_update(self, stats: dict):
        rows_per_sec = float(stats.get("instant_rows_per_sec", 0) or 0)
        if rows_per_sec >= 1_000_000:
            speed_text = f"{rows_per_sec / 1_000_000:.1f}M rows/sec"
        elif rows_per_sec >= 1_000:
            speed_text = f"{rows_per_sec / 1_000:.1f}K rows/sec"
        else:
            speed_text = f"{rows_per_sec:.0f} rows/sec"
        self.speed_label.setText(f"처리 속도: {speed_text}")

        mb_per_sec = float(stats.get("instant_mb_per_sec", 0.0) or 0.0)
        self.data_rate_label.setText(f"전송 속도: {mb_per_sec:.1f} MB/sec")

        self.eta_label.setText(f"예상 완료: {stats.get('eta_time', '계산중...')}")
        self.elapsed_label.setText(f"경과 시간: {stats.get('elapsed_time', '00:00:00')}")

    def on_worker_thread_finished(self):
        """워커 스레드 종료 핸들러

        주의: QThread.finished는 "완료"가 아니라 "종료"이므로,
        - 오류: on_error에서 처리
        - 사용자 중단/취소: worker.is_running == False
        - 정상 완료: worker.is_running == True
        로 판정한다.
        """

        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)

        # 오류 케이스는 on_error에서 status=failed 처리하므로 여기선 건드리지 않는다.
        if getattr(self, "_worker_had_error", False):
            self._update_nav_state()
            return

        if not self.worker:
            self._update_nav_state()
            return

        rows_processed = self._get_processed_rows()

        # 중단/취소: 재개 가능해야 하므로 completed로 마킹하지 않는다.
        if not getattr(self.worker, "is_running", True):
            if self.history_id:
                self.history_manager.update_history_status(
                    self.history_id, "running", processed_rows=rows_processed
                )
            self.add_log(
                "마이그레이션이 중단되었습니다. 나중에 '이어서 진행'으로 재개할 수 있습니다.",
                "WARNING",
            )
            self._update_nav_state()
            return

        # 정상 완료
        if self.history_id:
            self.history_manager.update_history_status(
                self.history_id, "completed", processed_rows=rows_processed
            )

        self.add_log("마이그레이션이 완료되었습니다", "SUCCESS")
        QMessageBox.information(self, "완료", "마이그레이션이 성공적으로 완료되었습니다.")
        self._update_nav_state()

    def on_error(self, error_msg: str):
        self._worker_had_error = True

        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)

        if self.history_id:
            rows_processed = self._get_processed_rows()
            self.history_manager.update_history_status(
                self.history_id, "failed", processed_rows=rows_processed
            )

        self.add_log(f"오류 발생: {error_msg}", "ERROR")
        QMessageBox.critical(self, "오류", f"마이그레이션 중 오류가 발생했습니다:\n\n{error_msg}")
        self._update_nav_state()

    def _get_processed_rows(self) -> int:
        if self.worker and hasattr(self.worker, "get_stats"):
            try:
                stats = self.worker.get_stats()
                return int(stats.get("total_rows") or 0)
            except Exception:
                pass

        if self.history_id:
            history = self.history_manager.get_history(self.history_id)
            if history:
                return int(history.processed_rows or 0)

        return 0

    # ============================
    # Logging
    # ============================

    def add_log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {level}: {message}"
        self.log_text.append(entry)
        self.log_text.moveCursor(QTextCursor.End)
        log_emitter.emit_log(level, message)

    # ============================
    # Bind
    # ============================

    def _bind_ui(self):
        self.pages.currentChanged.connect(lambda _i: self._update_nav_state())

        # 파티션 체크 변경 시 카운트/네비 갱신
        self.partition_list.itemChanged.connect(lambda _item: (self._update_counts(), self._update_nav_state()))

        self.pages.setCurrentIndex(0)
        self._update_step_ui()
        self._update_nav_state()

    # ============================
    # Qt lifecycle
    # ============================

    def closeEvent(self, event):
        if self.worker and getattr(self.worker, "is_running", False):
            QMessageBox.warning(self, "진행 중", "마이그레이션 진행 중에는 닫을 수 없습니다.")
            event.ignore()
            return
        event.accept()
