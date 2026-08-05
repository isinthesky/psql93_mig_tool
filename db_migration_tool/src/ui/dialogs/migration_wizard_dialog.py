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

import html
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import cast

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
from src.core.scan_workers import (
    PartitionScanWorker,
    RowCountVerifyWorker,
    TargetCompletedScanWorker,
)
from src.core.table_types import TABLE_TYPE_CONFIG, TableType, get_all_table_types
from src.models.history import CheckpointManager, HistoryManager, MigrationHistoryItem
from src.models.profile import ConnectionProfile
from src.ui.dialogs.scan_host import (
    PARTITION_DISPLAY_LIMIT,
    ScanHostMixin,
    build_log_retention_hint,
    format_row_count,
)
from src.ui.theme import LOG_MAX_BLOCKS, TEXT_DANGER, TEXT_MUTED, log_color
from src.ui.widgets import MetricReadout, StatusLamp, StepRail
from src.utils.enhanced_logger import log_emitter


def to_qdate(d: date) -> QDate:
    return QDate(d.year, d.month, d.day)


@dataclass
class PartitionSummary:
    table_name: str
    row_count: int
    table_type: TableType
    # PostgreSQL 소스의 행 수는 플래너 통계에서 온 추정치다.
    # 아카이브 소스는 내보낼 때 실제로 센 값이라 정확하다.
    row_count_estimated: bool = False


class MigrationWizardDialog(ScanHostMixin, QDialog):
    """COPY 중심 단계형 마이그레이션 마법사"""

    # 실행 상태 → (램프, 표시 문구). 실행 페이지의 모든 버튼이 여기서 갈린다.
    RUN_STATES = {
        "idle": ("idle", "대기 중"),
        "running": ("ok", "실행 중"),
        "paused": ("busy", "일시정지"),
        "done": ("ok", "완료"),
        "stopped": ("busy", "중단됨"),
        "failed": ("error", "오류"),
    }

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
        self._init_scan_host()

        self.history_id: int | None = None
        self.resume_mode: bool = False
        self.run_state: str = "idle"
        self._frozen_selection: list[str] = []

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
        self.copy_mode = "auto"  # "python" | "server" | "auto"

        # 탐색 결과
        self.discovered_partitions: list[PartitionSummary] = []
        self._completed_from_history: set[str] = set()
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

        self.step_rail = StepRail(["연결 확인", "범위/파티션 선택", "실행"])
        root.addWidget(self.step_rail)

        self.step_title = QLabel()
        self.step_title.setProperty("role", "stepTitle")
        root.addWidget(self.step_title)

        self.step_hint = QLabel()
        self.step_hint.setProperty("role", "hint")
        self.step_hint.setWordWrap(True)
        root.addWidget(self.step_hint)

        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)

        self.page_connection = self._build_page_connection()
        self.page_scope = self._build_page_scope()
        self.page_run = self._build_page_run()

        self.pages.addWidget(self.page_connection)
        self.pages.addWidget(self.page_scope)
        self.pages.addWidget(self.page_run)

        # 확인(다음)을 오른쪽 끝에 두는 Windows 대화상자 관례를 따른다.
        nav = QHBoxLayout()
        self.close_btn = QPushButton("닫기")
        self.back_btn = QPushButton("이전")
        self.next_btn = QPushButton("다음")

        self.back_btn.clicked.connect(self.go_back)
        self.next_btn.clicked.connect(self.go_next)
        self.close_btn.clicked.connect(self.close)

        self.close_btn.setToolTip("마법사를 닫습니다. 실행 중에는 닫을 수 없습니다.")
        self.back_btn.setToolTip("이전 단계로 돌아갑니다.")
        self.next_btn.setToolTip("다음 단계로 이동합니다.")

        # Enter가 엉뚱한 버튼을 누르지 않도록 기본 버튼을 '다음'으로 고정한다.
        for btn in (self.close_btn, self.back_btn, self.next_btn):
            btn.setAutoDefault(False)
        self.next_btn.setAutoDefault(True)
        self.next_btn.setDefault(True)

        nav.addWidget(self.close_btn)
        nav.addStretch(1)
        nav.addWidget(self.back_btn)
        nav.addWidget(self.next_btn)

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

        self.connection_hint = QLabel("")
        self.connection_hint.setProperty("role", "hint")
        self.connection_hint.setWordWrap(True)
        layout.addWidget(self.connection_hint)

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

        tip = QLabel("소스와 대상이 모두 연결되어야 다음 단계로 넘어갈 수 있습니다.")
        tip.setWordWrap(True)
        tip.setProperty("role", "hint")
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

        layout.addWidget(self._create_run_state_rail())
        layout.addWidget(self._create_run_summary_group())
        layout.addWidget(self._create_progress_group())
        layout.addWidget(self._create_log_group(), 1)
        layout.addWidget(self._create_run_controls())

        return page

    def _create_run_state_rail(self) -> QWidget:
        """실행 상태를 연결 상태와 같은 램프 언어로 읽게 한다."""
        rail = QFrame()
        rail.setObjectName("statusRail")
        layout = QHBoxLayout(rail)
        layout.setContentsMargins(12, 10, 12, 10)

        self.run_lamp = StatusLamp("작업 상태")
        self.run_lamp.set_state("idle", "대기 중")
        layout.addWidget(self.run_lamp)
        layout.addStretch(1)

        self.run_detail_label = QLabel("")
        self.run_detail_label.setProperty("role", "muted")
        layout.addWidget(self.run_detail_label)
        return rail

    # ----------------------------
    # Common widgets
    # ----------------------------

    def _create_connection_status_widget(self) -> QWidget:
        # QFrame + objectName 선택자를 쓴다. `QWidget {...}` 선택자는 자식 QLabel까지
        # 매칭되어 라벨마다 테두리가 그려지므로 쓰지 않는다.
        rail = QFrame()
        rail.setObjectName("statusRail")
        layout = QHBoxLayout(rail)
        layout.setContentsMargins(12, 10, 12, 10)

        self.source_lamp = StatusLamp("소스 DB")
        self.target_lamp = StatusLamp("대상 DB")
        self.source_lamp.set_state("busy", "확인 중...")
        self.target_lamp.set_state("busy", "확인 중...")

        layout.addWidget(self.source_lamp)
        layout.addSpacing(30)
        layout.addWidget(self.target_lamp)
        layout.addStretch(1)
        return rail

    # ----------------------------
    # Step 2 widgets
    # ----------------------------

    def _create_scope_group(self) -> QGroupBox:
        group = QGroupBox("2/3 범위 선택")
        layout = QVBoxLayout(group)

        # ── 마이그레이션 설정 ──
        settings_group = QGroupBox("마이그레이션 설정")
        settings_layout = QVBoxLayout(settings_group)

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
        settings_layout.addLayout(row_types)

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
        row_opts.addSpacing(20)
        row_opts.addWidget(QLabel("COPY 모드:"))
        self.copy_mode_combo = QComboBox()
        # 항목은 이름만, 설명은 툴팁으로. 문장을 넣으면 콤보가 한 줄을 다 먹는다.
        self.copy_mode_combo.addItems(
            [
                "AUTO (권장)",
                "Python COPY (재개 가능)",
                "Server-side COPY (빠름, 재개 불가)",
            ]
        )
        self.copy_mode_combo.setToolTip(
            "AUTO: server-side COPY가 가능한지 실제로 시도해보고, 실패하면 Python COPY로 자동 전환합니다.\n"
            "Python: 청크/체크포인트 기반 재개(resume) 가능\n"
            "Server-side: OS 파이프 기반 스트리밍(가장 빠름), 중단 시 해당 파티션 재개 불가"
        )

        self.copy_mode_combo.setCurrentIndex(0)  # 기본값: AUTO
        self.copy_mode_combo.currentIndexChanged.connect(self._on_copy_mode_changed)
        row_opts.addWidget(self.copy_mode_combo)
        row_opts.addStretch(1)
        settings_layout.addLayout(row_opts)

        self.server_copy_warning = QLabel("⚠ Server-side COPY는 재개 모드에서 사용할 수 없습니다.")
        self.server_copy_warning.setProperty("role", "lampText")
        self.server_copy_warning.setProperty("state", "busy")
        self.server_copy_warning.setVisible(False)
        settings_layout.addWidget(self.server_copy_warning)

        layout.addWidget(settings_group)

        # ── 날짜 범위 ──
        date_group = QGroupBox("날짜 범위")
        date_layout = QVBoxLayout(date_group)

        row_dates = QHBoxLayout()
        row_dates.addWidget(QLabel("시작"))
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        # 날짜가 바뀌면 지금 목록은 무효다. 프리셋 버튼과 재개 모드의
        # setDate()도 이 시그널을 타므로 한 곳만 걸면 전부 덮인다.
        self.start_date_edit.dateChanged.connect(self._on_scan_condition_changed)
        row_dates.addWidget(self.start_date_edit)
        row_dates.addWidget(QLabel("종료"))
        self.end_date_edit = QDateEdit()
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.dateChanged.connect(self._on_scan_condition_changed)
        row_dates.addWidget(self.end_date_edit)
        row_dates.addStretch(1)
        date_layout.addLayout(row_dates)

        preset = QHBoxLayout()
        self.preset_today_btn = QPushButton("오늘")
        self.preset_yesterday_btn = QPushButton("어제")
        self.preset_7d_btn = QPushButton("최근 7일")
        self.preset_30d_btn = QPushButton("최근 30일")
        self.preset_today_btn.clicked.connect(lambda: self._set_preset_days(0))
        self.preset_yesterday_btn.clicked.connect(lambda: self._set_preset_days(1, yesterday=True))
        self.preset_7d_btn.clicked.connect(lambda: self._set_preset_days(7))
        self.preset_30d_btn.clicked.connect(lambda: self._set_preset_days(30))
        for btn in (
            self.preset_today_btn,
            self.preset_yesterday_btn,
            self.preset_7d_btn,
            self.preset_30d_btn,
        ):
            btn.setProperty("variant", "chip")
            btn.setAutoDefault(False)
            preset.addWidget(btn)
        preset.addStretch(1)
        date_layout.addLayout(preset)

        layout.addWidget(date_group)

        # ── 파티션 탐색 및 선택 ──
        action_group = QGroupBox("파티션 탐색 및 선택")
        action_layout = QVBoxLayout(action_group)

        row_actions = QHBoxLayout()
        self.discover_btn = QPushButton("파티션 찾기")
        self.discover_btn.setObjectName("primaryAction")
        self.discover_btn.setToolTip("선택한 항목과 날짜 범위에 해당하는 소스 파티션을 찾습니다.")
        self.discover_btn.setAutoDefault(False)
        self.discover_btn.clicked.connect(self.discover_partitions)
        self.check_completed_btn = QPushButton("완료여부 확인(대상 DB)")
        self.check_completed_btn.setToolTip("대상 DB에 이미 데이터가 있는 파티션을 표시합니다.")
        self.check_completed_btn.setAutoDefault(False)
        self.check_completed_btn.clicked.connect(self.check_target_completed)
        self.check_completed_btn.setEnabled(False)
        row_actions.addWidget(self.discover_btn)
        row_actions.addWidget(self.check_completed_btn)
        self.discover_status = QLabel("날짜/항목을 선택하고 ‘파티션 찾기’를 눌러주세요.")
        self.discover_status.setProperty("role", "hint")
        row_actions.addWidget(self.discover_status)
        row_actions.addStretch(1)
        action_layout.addLayout(row_actions)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        action_layout.addWidget(separator)

        row_select = QHBoxLayout()
        self.select_all_btn = QPushButton("전체 선택")
        self.select_none_btn = QPushButton("전체 해제")
        self.select_pending_btn = QPushButton("완료 제외 선택")
        self.select_all_btn.clicked.connect(lambda: self._bulk_check(True))
        self.select_none_btn.clicked.connect(lambda: self._bulk_check(False))
        self.select_pending_btn.clicked.connect(self._select_excluding_completed)
        for btn in (self.select_all_btn, self.select_none_btn, self.select_pending_btn):
            btn.setProperty("variant", "chip")
            btn.setAutoDefault(False)
            row_select.addWidget(btn)

        row_select.addSpacing(16)
        self.partition_filter = QLineEdit()
        self.partition_filter.setPlaceholderText("이름으로 거르기 (예: 20260801)")
        self.partition_filter.setToolTip(
            "입력한 문자열이 들어간 파티션만 목록에 보여줍니다. "
            "선택 상태는 그대로 유지되고, 전체 선택/해제는 보이는 항목에만 적용됩니다."
        )
        self.partition_filter.setClearButtonEnabled(True)
        self.partition_filter.textChanged.connect(self._apply_partition_filter)
        row_select.addWidget(self.partition_filter, 1)
        action_layout.addLayout(row_select)

        layout.addWidget(action_group)

        return group

    def _create_partition_group(self) -> QGroupBox:
        group = QGroupBox("발견된 파티션(체크해서 일부만 실행 가능)")
        layout = QVBoxLayout(group)

        self.partition_list = QListWidget()
        self.partition_list.setObjectName("partitionList")
        self.partition_list.setUniformItemSizes(True)
        layout.addWidget(self.partition_list)

        info = QHBoxLayout()
        self.partition_count_label = QLabel("총 0개")
        self.partition_rows_label = QLabel("총 0 rows")
        self.partition_selected_label = QLabel("선택 0개")
        self.partition_count_label.setProperty("role", "fieldTitle")
        self.partition_selected_label.setProperty("role", "fieldTitle")
        self.partition_rows_label.setProperty("role", "muted")

        info.addWidget(self.partition_count_label)
        info.addSpacing(20)
        info.addWidget(self.partition_selected_label)
        info.addSpacing(20)
        info.addWidget(self.partition_rows_label)
        info.addStretch(1)

        # 색이 아니라 모양으로 구분한다(흑백 화면·색약 환경에서도 읽힌다).
        legend = QLabel("●  실행 대상        ✓  이미 완료된 것으로 보임(기본 해제)")
        legend.setProperty("role", "hint")
        info.addWidget(legend)

        layout.addLayout(info)
        return group

    # ----------------------------
    # Step 3 widgets
    # ----------------------------

    def _create_run_summary_group(self) -> QGroupBox:
        group = QGroupBox("실행 요약")
        layout = QVBoxLayout(group)
        self.summary_label = QLabel("")
        self.summary_label.setProperty("role", "summary")
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
        self.total_label.setProperty("role", "metricValue")
        total_row.addWidget(self.total_label)
        layout.addLayout(total_row)

        cur_row = QHBoxLayout()
        cur_row.addWidget(QLabel("현재:"))
        self.current_progress = QProgressBar()
        cur_row.addWidget(self.current_progress)
        self.current_label = QLabel("대기 중")
        self.current_label.setProperty("role", "metricValue")
        cur_row.addWidget(self.current_label)
        layout.addLayout(cur_row)

        # 계기 묶음: 값은 고정폭이라 실행 중에도 자릿수가 흔들리지 않는다.
        info = QHBoxLayout()
        info.setSpacing(28)
        self.speed_metric = MetricReadout("처리 속도")
        self.data_rate_metric = MetricReadout("전송 속도")
        self.eta_metric = MetricReadout("예상 완료")
        self.elapsed_metric = MetricReadout("경과 시간", "00:00:00")
        for metric in (
            self.speed_metric,
            self.data_rate_metric,
            self.eta_metric,
            self.elapsed_metric,
        ):
            info.addWidget(metric)
        info.addStretch(1)
        layout.addLayout(info)

        return group

    def _create_log_group(self) -> QGroupBox:
        group = QGroupBox("실행 로그")
        layout = QVBoxLayout(group)
        self.log_text = QTextEdit()
        self.log_text.setObjectName("runLog")
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("작업을 시작하면 실행 로그가 여기에 표시됩니다.")
        # 장시간 실행에서 로그가 메모리를 계속 먹지 않도록 상한을 둔다.
        self.log_text.document().setMaximumBlockCount(LOG_MAX_BLOCKS)
        layout.addWidget(self.log_text)
        layout.addWidget(build_log_retention_hint(LOG_MAX_BLOCKS))
        return group

    def _create_run_controls(self) -> QWidget:
        # 한 줄에 '채워진' 버튼은 시작 하나뿐이다. 나머지는 조용히 둔다.
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)

        self.start_btn = QPushButton("시작")
        self.start_btn.setObjectName("startButton")
        self.start_btn.setToolTip("선택한 파티션으로 COPY 마이그레이션을 시작합니다.")
        self.start_btn.clicked.connect(self.start_migration)

        self.pause_btn = QPushButton("일시정지")
        self.pause_btn.setToolTip("진행 중인 작업을 잠시 멈추거나 다시 진행합니다.")
        self.pause_btn.clicked.connect(self.pause_migration)
        self.pause_btn.setEnabled(False)

        layout.addWidget(self.start_btn)
        layout.addWidget(self.pause_btn)

        self.verify_btn = QPushButton("검증 실행")
        self.verify_btn.setToolTip(
            "실행한 파티션의 소스/대상 행 수를 COUNT(*)로 비교합니다. 시간이 오래 걸릴 수 있습니다."
        )
        self.verify_btn.clicked.connect(self.run_rowcount_verification)
        self.verify_btn.setEnabled(False)
        layout.addWidget(self.verify_btn)

        # 파괴적 액션 분리
        layout.addStretch(1)

        self.cancel_btn = QPushButton("작업 취소")
        self.cancel_btn.setObjectName("dangerAction")
        self.cancel_btn.setToolTip("진행 중인 작업을 멈춥니다. 완료된 파티션은 그대로 남습니다.")
        self.cancel_btn.clicked.connect(self.cancel_migration)
        self.cancel_btn.setEnabled(False)
        layout.addWidget(self.cancel_btn)

        for btn in (self.start_btn, self.pause_btn, self.verify_btn, self.cancel_btn):
            btn.setAutoDefault(False)

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
            # 재개 모드의 실행 대상은 미완료 체크포인트지 목록의 체크 상태가 아니다.
            # 여기서 목록 선택을 요구하면 재개 → 이전 → 다음에서 실행 페이지로
            # 영영 못 돌아간다(목록은 탐색한 적이 없어 비어 있다).
            if self.resume_mode and self._frozen_selection:
                self.pages.setCurrentIndex(2)
                self._update_step_ui()
                self._update_nav_state()
                return

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
        titles = ["연결 확인", "범위/파티션 선택", "실행"]
        hints = [
            "소스와 대상 DB에 접속할 수 있는지 확인합니다.",
            "날짜와 항목을 고른 뒤 파티션을 찾고, 실행할 파티션만 체크합니다.",
            "COPY 마이그레이션을 실행하고 진행 상황과 로그를 확인합니다.",
        ]
        self.step_rail.set_current(idx)
        self.step_title.setText(titles[idx])
        self.step_hint.setText(hints[idx])

        if idx == 2:
            self._refresh_summary()

    def _on_scan_condition_changed(self, *_args) -> None:
        """탐색 조건(날짜·항목)이 바뀌었다.

        여기서 세대를 올리지 않으면, 옛 조건으로 시작한 탐색 결과가 현재
        세대로 통과해 화면을 채운다. 그 목록을 그대로 실행하면 이력에는
        위젯의 **새** 날짜가, 실제로는 **옛** 범위의 파티션이 기록된다.
        """
        self._bump_generation()

    def _on_scan_generation_changed(self) -> None:
        """조건이 바뀌면 지금 목록은 더 이상 그 조건의 결과가 아니다.

        늦은 결과를 버리는 것만으로는 부족하다. 이미 그려진 목록이 남아
        있으면 사용자는 그게 새 조건의 결과라고 믿고 실행한다.
        """
        if not self.discovered_partitions:
            return
        self.discovered_partitions = []
        self._target_has_data = {}
        self._render_partition_list()
        self._update_counts()
        self.discover_status.setText("조건이 바뀌었습니다. 파티션을 다시 찾으세요.")
        self._sync_scan_buttons()
        self._update_nav_state()

    def _sync_scan_buttons(self) -> None:
        """조회 버튼 활성화를 한 곳에서 정한다.

        각 핸들러가 따로 켜고 끄면 순서에 따라 어긋난다. 예전에는 탐색이
        한 번 실패하면 '완료여부 확인'이 영구 비활성으로 남았다.
        """
        if self.resume_mode:
            # 재개 모드는 옵션이 잠겨 있다.
            return
        busy = self._is_scan_inflight("discover") or self._is_scan_inflight("target")
        self.discover_btn.setEnabled(not busy)
        self.check_completed_btn.setEnabled(not busy and bool(self.discovered_partitions))

    def _set_scan_status(self, text: str) -> None:
        self.discover_status.setText(text)

    def _on_scans_abandoned(self) -> None:
        self.add_log("조회 작업이 멈추지 않아 분리하고 창을 닫습니다", "WARNING")

    def _is_running(self) -> bool:
        if self.run_state in ("running", "paused"):
            return True
        return bool(self.worker and getattr(self.worker, "is_running", False))

    def _update_nav_state(self):
        idx = self.pages.currentIndex()
        running = self._is_running()

        # 한 번 실행을 시작한 마법사는 그 실행에 묶인다. 여기서 범위를 다시 고르면
        # '되돌아가서 고른 선택'과 '이미 만들어진 작업 이력'이 어긋난다.
        run_page_locked = idx == 2 and self.run_state != "idle"
        self.back_btn.setEnabled(idx > 0 and not running and not run_page_locked)
        self.back_btn.setToolTip(
            "이미 실행한 작업입니다. 다른 범위로 실행하려면 창을 닫고 다시 여세요."
            if run_page_locked
            else "이전 단계로 돌아갑니다."
        )

        if idx == 0:
            self.next_btn.setText("다음")
            self.next_btn.setVisible(True)
            self.next_btn.setEnabled(self.source_connected and self.target_connected)
        elif idx == 1:
            self.next_btn.setText("다음")
            self.next_btn.setVisible(True)
            # 재개 모드는 목록이 아니라 미완료 체크포인트가 실행 대상이다.
            has_target = bool(self.resume_mode and self._frozen_selection) or bool(
                self.get_selected_partition_names()
            )
            self.next_btn.setEnabled(has_target)
        else:
            # 실행 페이지에서는 네비게이션 "다음"이 필요 없다.
            self.next_btn.setVisible(False)

        self.close_btn.setEnabled(not running)

    def _set_run_state(self, state: str, detail: str = ""):
        """실행 페이지의 상태를 한 곳에서 정한다.

        버튼 활성화를 여기저기서 따로 건드리면 '취소했는데 시작도 못 누르는'
        막다른 상태가 생긴다. 상태 하나가 램프·버튼·안내를 모두 결정하게 한다.
        """
        lamp, text = self.RUN_STATES.get(state, self.RUN_STATES["idle"])
        self.run_state = state
        self.run_lamp.set_state(lamp, text)
        self.run_detail_label.setText(detail)

        running = state in ("running", "paused")
        has_history = self.history_id is not None

        start_labels = {"stopped": "이어서 시작", "failed": "다시 시도"}
        self.start_btn.setText(start_labels.get(state, "시작"))
        self.start_btn.setEnabled(state in ("idle", "stopped", "failed"))

        self.pause_btn.setEnabled(running)
        self.pause_btn.setText("재개" if state == "paused" else "일시정지")

        self.cancel_btn.setEnabled(running)
        self.verify_btn.setEnabled(has_history and not running)

        if state == "done":
            self.start_btn.setToolTip(
                "완료된 작업입니다. 새로 실행하려면 창을 닫고 다시 시작하세요."
            )
        elif state == "stopped":
            self.start_btn.setToolTip("중단된 지점부터 미완료 파티션만 이어서 실행합니다.")
        else:
            self.start_btn.setToolTip("선택한 파티션으로 COPY 마이그레이션을 시작합니다.")

        self._update_nav_state()

    # ============================
    # Connection check
    # ============================

    def check_connections(self):
        # 생성자에서 QTimer로 예약되므로, 창이 뜨자마자 Esc를 누르면
        # 닫힌 뒤에 이 호출이 도착할 수 있다.
        if self._closing or self._is_scan_inflight("conn"):
            return

        checker = CopyMigrationWorker(
            profile=self.profile,
            partitions=[],
            history_id=0,
            resume=False,
        )
        checker.connection_checking.connect(self._on_connection_checking)
        checker.source_connection_status.connect(self._on_source_connection_status)
        checker.target_connection_status.connect(self._on_target_connection_status)
        checker.finished.connect(self._on_connection_check_finished)

        checker.check_connections_only = True
        self.connection_checker = checker
        # 조회 워커로 등록해야 창을 닫을 때 함께 정리된다. 등록하지 않으면
        # 실행 중인 QThread의 참조가 다이얼로그와 함께 사라져 프로세스가 죽는다.
        self._scan_workers["conn"] = checker
        self._mark_scan_started("conn")
        checker.start()

    def _on_connection_check_finished(self):
        self._mark_scan_finished("conn")
        self._update_nav_state()

    def _on_connection_checking(self):
        self.source_lamp.set_state("busy", "확인 중...")
        self.target_lamp.set_state("busy", "확인 중...")
        self.connection_hint.setText("")

    def _on_source_connection_status(self, connected: bool, message: str):
        self.source_connected = connected
        self.source_status_message = message
        if connected:
            self.source_lamp.set_state("ok", "연결됨")
        else:
            self.source_lamp.set_state("error", message)
        self._update_connection_hint()
        self._update_nav_state()

    def _on_target_connection_status(self, connected: bool, message: str):
        self.target_connected = connected
        self.target_status_message = message
        if connected:
            self.target_lamp.set_state("ok", "연결됨")
        else:
            self.target_lamp.set_state("error", message)
        self._update_connection_hint()
        self._update_nav_state()

    def _update_connection_hint(self):
        """연결이 막혔을 때 다음에 뭘 해야 하는지를 그 자리에서 알려준다."""
        failed = []
        if not self.source_connected:
            failed.append("소스")
        if not self.target_connected:
            failed.append("대상")

        if not failed:
            self.connection_hint.setText("")
            return

        self.connection_hint.setText(
            f"{' · '.join(failed)} 연결에 실패했습니다. "
            "주소·포트·계정을 확인한 뒤 '연결 다시 확인'을 누르세요. "
            "연결 정보는 메인 창의 '편집'에서 고칠 수 있습니다."
        )

    # ============================
    # Step 1: resume
    # ============================

    def _check_incomplete_migration(self):
        incomplete = (
            self.history_manager.get_incomplete_history(self.profile.id)
            if self.profile.id is not None
            else None
        )
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
        self.copy_mode_combo.setEnabled(False)
        self.start_date_edit.setEnabled(False)
        self.end_date_edit.setEnabled(False)
        self.discover_btn.setEnabled(False)
        self.check_completed_btn.setEnabled(False)
        self.select_all_btn.setEnabled(False)
        self.select_none_btn.setEnabled(False)
        self.select_pending_btn.setEnabled(False)

    def _unlock_options_for_new_run(self):
        """재개용 잠금을 되돌린다.

        '이어서 진행'을 눌렀다가 '새 작업으로 진행'으로 마음을 바꾸면
        옵션이 잠긴 채로 남아 범위를 다시 고를 수 없게 된다.
        """
        self.stop_on_error_radio.setEnabled(True)
        self.skip_on_error_radio.setEnabled(True)
        self.copy_mode_combo.setEnabled(True)
        self.start_date_edit.setEnabled(True)
        self.end_date_edit.setEnabled(True)
        # 조회 버튼은 직접 켜지 않는다. 탐색이 아직 도는 중일 수 있고,
        # 그러면 눌러도 반응 없는 죽은 버튼이 된다.
        self._sync_scan_buttons()
        self.select_all_btn.setEnabled(True)
        self.select_none_btn.setEnabled(True)
        self.select_pending_btn.setEnabled(True)
        # 배치 크기는 COPY 모드에 따라 달라지므로 그 규칙에 맡긴다.
        self._on_copy_mode_changed(self.copy_mode_combo.currentIndex())
        # 체크박스는 '마지막 하나' 규칙이 다시 정한다.
        self._guard_last_table_type()

    def _force_python_copy_for_resume(self):
        """재개는 Python COPY만 가능하다(server-side는 중단 지점을 못 잡는다)."""
        self.copy_mode_combo.setCurrentIndex(1)  # Python COPY (재개 가능)
        self.copy_mode = "python"
        self.server_copy_warning.setVisible(False)

    def _on_resume_clicked(self):
        if not self._incomplete_history or self._incomplete_history.id is None:
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

        self._force_python_copy_for_resume()

        pending = self.checkpoint_manager.get_pending_checkpoints(self.history_id)
        pending_names = [cp.partition_name for cp in pending]

        # 선택 고정(재개는 pending만)
        self._frozen_selection = pending_names

        # 실행 페이지로 이동
        self.pages.setCurrentIndex(2)
        self._update_step_ui()
        self._set_run_state("idle", f"재개 대기 · 미완료 파티션 {len(pending_names)}개")

        self.add_log(f"재개 모드: 미완료 파티션 {len(pending_names)}개", "INFO")

    def _on_new_run_clicked(self):
        # 아무것도 알려주지 않는 확인 창 대신, 그 자리에서 상태를 바꾼다.
        self.resume_mode = False
        self.history_id = None
        self._frozen_selection = []
        self._unlock_options_for_new_run()
        self._set_run_state("idle")
        self.incomplete_group.setVisible(False)
        self.connection_hint.setText(
            "새 작업으로 진행합니다. 다음 단계에서 날짜와 파티션을 선택하세요."
        )

    # ============================
    # Step 2: discovery & completed checks
    # ============================

    def _load_last_completed_partitions_cache(self):
        """프로필의 모든 완료 이력에서 completed 파티션 캐시"""
        if self.profile.id is None:
            self._completed_from_history = set()
            return
        try:
            completed_histories = self.history_manager.get_completed_histories(self.profile.id)
            if not completed_histories:
                self._completed_from_history = set()
                return

            history_ids = [h.id for h in completed_histories if h.id is not None]
            self._completed_from_history = self.checkpoint_manager.get_completed_partition_names(
                history_ids
            )
        except Exception as e:
            self._completed_from_history = set()
            self.add_log(f"완료 파티션 캐시 로드 실패: {e}", "WARNING")

    def _on_table_type_changed(self, _state: int):
        self.selected_table_types = [
            tt for tt, cb in self.table_type_checkboxes.items() if cb.isChecked()
        ]
        self._guard_last_table_type()
        # 항목도 탐색 조건이다. 날짜와 같이 취급한다.
        self._on_scan_condition_changed()

    def _guard_last_table_type(self):
        """마지막 하나 남은 항목은 아예 끌 수 없게 한다.

        경고 창을 띄워 되돌리는 대신, 잘못된 상태로 갈 수 없게 막는다.
        """
        only_one = sum(1 for cb in self.table_type_checkboxes.values() if cb.isChecked()) == 1
        for table_type, cb in self.table_type_checkboxes.items():
            lock = only_one and cb.isChecked()
            cb.setEnabled(not lock and not self.resume_mode)
            cb.setToolTip(
                "최소 1개 항목은 선택되어 있어야 합니다."
                if lock
                else TABLE_TYPE_CONFIG[table_type].description
            )

    def _on_error_strategy_changed(self, _checked: bool):
        self.error_strategy = "stop" if self.stop_on_error_radio.isChecked() else "skip"

    def _on_copy_mode_changed(self, _idx: int):
        label = self.copy_mode_combo.currentText() if hasattr(self, "copy_mode_combo") else ""

        if label.startswith("Server-side"):
            self.copy_mode = "server"
            self.batch_size_spin.setEnabled(False)  # 서버 모드면 배치 크기 무의미
        elif label.startswith("Python"):
            self.copy_mode = "python"
            self.batch_size_spin.setEnabled(True)
        else:
            self.copy_mode = "auto"
            # auto는 fallback 시 배치 크기 의미가 있으므로 활성화
            self.batch_size_spin.setEnabled(True)

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

        # QDate.toPython()의 스텁 반환형은 object라 비교/전달 전에 좁혀 준다.
        start_date = cast(date, self.start_date_edit.date().toPython())
        end_date = cast(date, self.end_date_edit.date().toPython())
        if start_date > end_date:
            QMessageBox.warning(self, "날짜 오류", "시작 날짜가 종료 날짜보다 늦습니다.")
            return

        if self._closing or self._is_scan_inflight("discover"):
            return

        gen = self._bump_generation()

        self.discover_status.setText("파티션 탐색 중...")
        self.partition_list.clear()
        self.discovered_partitions = []
        self._target_has_data = {}
        self._load_last_completed_partitions_cache()

        worker = PartitionScanWorker(
            gen,
            self.profile.source_kind,
            dict(self.profile.source_config or {}),
            start_date,
            end_date,
            list(self.selected_table_types),
        )
        # 람다로 연결하지 않는다. 수신자 QObject가 없어 자동 해제되지 않는다.
        worker.result.connect(self._on_discovery_result)
        worker.failed.connect(self._on_discovery_error)
        worker.progress.connect(self._on_discovery_progress)
        worker.finished.connect(self._on_discovery_finished)

        self._scan_workers["discover"] = worker
        self._mark_scan_started("discover")
        self._sync_scan_buttons()
        worker.start()

    def _on_discovery_progress(self, gen: int, done: int, total: int):
        if not self._is_current_generation(gen):
            return
        self.discover_status.setText(f"파티션 탐색 중... ({done}/{total})")

    def _on_discovery_error(self, gen: int, msg: str):
        if not self._is_current_generation(gen):
            return
        # 실패했는데 이전 목록이 남아 있으면 그걸 실행 대상으로 착각한다.
        self.discovered_partitions = []
        self._target_has_data = {}
        self._render_partition_list()
        self.discover_status.setText("파티션 탐색 실패 — 다시 시도하세요")
        self.add_log(f"파티션 탐색 오류: {msg}", "ERROR")
        self._update_counts()
        self._update_nav_state()

    def _on_discovery_finished(self):
        """성공·실패·취소 공통 정리.

        결과 슬롯이 아니라 여기서 버튼을 되돌린다. 예전에는 실패 시
        '완료여부 확인'이 영구 비활성으로 남았다.
        """
        self._mark_scan_finished("discover")
        self._sync_scan_buttons()

        worker = self.sender()
        gen = getattr(worker, "generation", self._scan_gen)
        if self._is_current_generation(gen) and "탐색 중" in self.discover_status.text():
            self.discover_status.setText("파티션 탐색이 중단되었습니다")

        self._update_nav_state()

    def _on_discovery_result(self, gen: int, payload: object):
        if not self._is_current_generation(gen):
            return
        partitions = cast(list, payload)
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
                        row_count_estimated=bool(p.get("row_count_estimated")),
                    )
                )
            except Exception:
                continue

        self.discovered_partitions = summaries
        self._render_partition_list()

        if summaries:
            self.discover_status.setText(f"완료: {len(summaries)}개 파티션")
            self.add_log(f"파티션 {len(summaries)}개 발견", "INFO")
        else:
            self.discover_status.setText("조건에 해당하는 파티션이 없습니다")
            self.add_log("선택한 조건에 해당하는 파티션이 없습니다", "WARNING")

        self._update_counts()
        # 버튼은 _sync_scan_buttons가 정한다(직접 켜면 순서에 따라 어긋난다).
        self._sync_scan_buttons()
        self._update_nav_state()

    def _is_completed_like(self, table_name: str) -> bool:
        return (table_name in self._completed_from_history) or bool(
            self._target_has_data.get(table_name)
        )

    def _render_partition_list(self):
        self.partition_list.clear()
        muted = QColor(TEXT_MUTED)

        # addItem마다 itemChanged가 튀면 항목 수의 제곱만큼 갱신이 돈다.
        # 수천 개를 그릴 때 창이 멈추는 원인이므로 그리는 동안은 막아둔다.
        self.partition_list.blockSignals(True)
        try:
            self._fill_partition_items(muted)
        finally:
            self.partition_list.blockSignals(False)

        self._apply_partition_filter(self.partition_filter.text())

    def _fill_partition_items(self, muted: QColor):
        for s in self.discovered_partitions[:PARTITION_DISPLAY_LIMIT]:
            cfg = TABLE_TYPE_CONFIG.get(s.table_type)
            type_name = cfg.display_name if cfg else s.table_type

            completed_like = self._is_completed_like(s.table_name)
            # 상태는 색이 아니라 맨 앞의 모양으로 먼저 읽힌다.
            marker = "✓" if completed_like else "●"
            rows_text = format_row_count(s.row_count, s.row_count_estimated)
            text = f"{marker}  {s.table_name}  ·  {rows_text}  ·  {type_name}"

            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, s.table_name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)

            # 기본 체크: 완료로 판단되면 기본 해제, 그 외 체크
            item.setCheckState(Qt.CheckState.Unchecked if completed_like else Qt.CheckState.Checked)

            if completed_like:
                reasons = []
                if s.table_name in self._completed_from_history:
                    reasons.append("이전 작업에서 완료됨")
                if self._target_has_data.get(s.table_name):
                    reasons.append("대상 DB에 데이터 있음")
                item.setForeground(muted)
                item.setToolTip(f"{' · '.join(reasons)}\n다시 실행하려면 체크하세요.")
            else:
                item.setToolTip(f"{s.table_name} · {rows_text}")

            self.partition_list.addItem(item)

        # 잘린 파티션은 체크할 수 없어 실행 대상에서도 빠진다. 조용히 넘어가지 않는다.
        hidden = len(self.discovered_partitions) - PARTITION_DISPLAY_LIMIT
        if hidden > 0:
            overflow = QListWidgetItem(
                f"⚠ {hidden:,}개는 목록에 표시되지 않아 이번 실행에서 제외됩니다. "
                "날짜 범위를 좁혀서 나눠 실행하세요."
            )
            overflow.setForeground(QColor(TEXT_DANGER))
            overflow.setFlags(Qt.ItemFlag.NoItemFlags)
            self.partition_list.addItem(overflow)

    def _apply_partition_filter(self, text: str):
        """이름으로 목록을 거른다. 선택 상태는 건드리지 않는다."""
        needle = (text or "").strip().lower()
        for i in range(self.partition_list.count()):
            item = self.partition_list.item(i)
            name = item.data(Qt.ItemDataRole.UserRole)
            if not name:
                continue
            item.setHidden(bool(needle) and needle not in str(name).lower())
        self._update_counts()

    def _visible_partition_items(self):
        for i in range(self.partition_list.count()):
            item = self.partition_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) and not item.isHidden():
                yield item

    def _bulk_check(self, checked: bool):
        # 거른 상태에서는 '보이는 것'에만 적용한다(안 보이는 걸 몰래 바꾸지 않는다).
        for item in self._visible_partition_items():
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self._update_counts()
        self._update_nav_state()

    def _select_excluding_completed(self):
        for item in self._visible_partition_items():
            name = item.data(Qt.ItemDataRole.UserRole)
            item.setCheckState(
                Qt.CheckState.Unchecked if self._is_completed_like(name) else Qt.CheckState.Checked
            )
        self._update_counts()
        self._update_nav_state()

    def get_selected_partition_names(self) -> list[str]:
        selected: list[str] = []
        for i in range(self.partition_list.count()):
            item = self.partition_list.item(i)
            name = item.data(Qt.ItemDataRole.UserRole)
            if not name:
                continue
            if item.checkState() == Qt.CheckState.Checked:
                selected.append(str(name))
        return selected

    def _update_counts(self):
        total = len(self.discovered_partitions)
        visible = 0
        selected_names: set[str] = set()
        hidden_selected = 0

        for i in range(self.partition_list.count()):
            item = self.partition_list.item(i)
            name = item.data(Qt.ItemDataRole.UserRole)
            if not name:
                continue
            hidden = item.isHidden()
            if not hidden:
                visible += 1
            if item.checkState() == Qt.CheckState.Checked:
                selected_names.add(str(name))
                if hidden:
                    hidden_selected += 1

        selected_rows, rows_estimated = self._row_total(selected_names)

        if visible != min(total, PARTITION_DISPLAY_LIMIT):
            self.partition_count_label.setText(f"표시 {visible}개 / 총 {total}개")
        else:
            self.partition_count_label.setText(f"총 {total}개")

        # 걸러서 안 보이는 항목도 체크되어 있으면 실행 대상이다. 숨기지 않고 말한다.
        selected_text = f"선택 {len(selected_names)}개"
        if hidden_selected:
            selected_text += f" (필터 밖 {hidden_selected}개 포함)"
        self.partition_selected_label.setText(selected_text)
        self.partition_selected_label.setToolTip(
            "필터는 보기만 거릅니다. 체크된 항목은 걸러져 보이지 않아도 실행 대상입니다."
            if hidden_selected
            else ""
        )
        # 총합보다 '지금 실행하면 옮겨질 양'이 결정에 필요한 숫자다.
        self.partition_rows_label.setText(f"선택 {format_row_count(selected_rows, rows_estimated)}")

    def check_target_completed(self):
        if not self.discovered_partitions:
            return

        if self._closing or self._is_scan_inflight("target"):
            return

        names = [s.table_name for s in self.discovered_partitions]
        gen = self._scan_gen
        self.discover_status.setText("대상 DB 확인 중...")

        worker = TargetCompletedScanWorker(
            gen,
            self.profile.target_kind,
            dict(self.profile.target_config or {}),
            names,
        )
        worker.progress.connect(self._on_target_check_progress)
        worker.result.connect(self._on_target_check_result)
        worker.failed.connect(self._on_target_check_error)
        worker.finished.connect(self._on_target_check_finished)

        self._scan_workers["target"] = worker
        self._mark_scan_started("target")
        self._sync_scan_buttons()
        worker.start()

    def _on_target_check_progress(self, gen: int, done: int, total: int):
        if not self._is_current_generation(gen):
            return
        self.discover_status.setText(f"대상 DB 확인 중... ({done}/{total})")

    def _on_target_check_error(self, gen: int, msg: str):
        if not self._is_current_generation(gen):
            return
        # 확인에 실패하면 완료 표시를 믿을 수 없다. 비워서 '전부 미확인'으로 둔다.
        self._target_has_data = {}
        self._render_partition_list()
        self._update_counts()
        self.add_log(f"대상 DB 완료여부 확인 오류: {msg}", "ERROR")
        self.discover_status.setText("대상 DB 확인 실패 — 다시 시도하세요")
        self._update_nav_state()

    def _on_target_check_result(self, gen: int, payload: object):
        if not self._is_current_generation(gen):
            return
        result = cast(dict, payload)
        self._target_has_data = {str(k): bool(v) for k, v in (result or {}).items()}
        self.add_log("대상 DB 완료여부 확인 완료", "INFO")
        self.discover_status.setText("대상 DB 확인 완료")
        self._render_partition_list()
        self._update_counts()
        self._update_nav_state()

    def _on_target_check_finished(self):
        """성공·실패·취소 공통 정리."""
        self._mark_scan_finished("target")
        self._sync_scan_buttons()

        worker = self.sender()
        gen = getattr(worker, "generation", self._scan_gen)
        if self._is_current_generation(gen) and "확인 중" in self.discover_status.text():
            self.discover_status.setText("대상 DB 확인이 중단되었습니다")

        self._update_nav_state()

    # ============================
    # Step 3: run
    # ============================

    def _row_total(self, names) -> tuple[int, bool]:
        """선택분의 행 수 합계와, 그 합계가 추정치를 포함하는지 돌려준다.

        하나라도 추정치가 섞이면 합계도 추정치다. 정확한 값처럼 보이면
        사용자가 그 숫자로 용량이나 시간을 계산한다.
        """
        wanted = set(names)
        picked = [s for s in self.discovered_partitions if s.table_name in wanted]
        total = sum(s.row_count for s in picked)
        return total, any(s.row_count_estimated for s in picked)

    def _refresh_summary(self):
        error_text = "중단" if self.error_strategy == "stop" else "건너뛰기"

        if self.resume_mode:
            parts = self._frozen_selection
            lines = [
                f"프로필      {self.profile.name}",
                "방식        COPY (고성능) · 재개 모드(옵션 잠김)",
                f"파티션      {len(parts):,}개 (미완료만)",
                f"에러 처리   {error_text}",
            ]
            self.summary_label.setText("\n".join(lines))
            return

        start_date = self.start_date_edit.date().toPython()
        end_date = self.end_date_edit.date().toPython()
        types_text = ", ".join(TABLE_TYPE_CONFIG[t].display_name for t in self.selected_table_types)
        parts = self._frozen_selection or self.get_selected_partition_names()

        lines = [
            f"프로필      {self.profile.name}",
            "방식        COPY (고성능)",
            f"날짜        {start_date} ~ {end_date}",
            f"항목        {types_text}",
            f"파티션      {len(parts):,}개 · {format_row_count(*self._row_total(parts))}",
            f"에러 처리   {error_text} · 배치 {int(self.batch_size_spin.value()):,} rows",
        ]
        self.summary_label.setText("\n".join(lines))

    def start_migration(self):
        if self.worker and self.worker.isRunning():
            return

        if not (self.source_connected and self.target_connected):
            QMessageBox.warning(self, "연결 필요", "소스/대상 DB가 모두 연결되어야 합니다.")
            return

        # 중단된 작업을 다시 시작하는 경우: 새 이력을 만들지 않고 재개로 넘긴다.
        # (그러지 않으면 이력이 하나 더 생기고 이미 옮긴 파티션을 다시 복사한다.)
        if self.history_id is not None and not self.resume_mode:
            pending = self.checkpoint_manager.get_pending_checkpoints(self.history_id)
            if pending:
                self.resume_mode = True
                self._frozen_selection = [cp.partition_name for cp in pending]
                self._force_python_copy_for_resume()
                self.add_log(
                    f"중단된 작업을 이어서 진행합니다 - 미완료 파티션 {len(pending)}개", "INFO"
                )
                self._refresh_summary()

        partitions = self._frozen_selection
        if not partitions:
            QMessageBox.warning(self, "파티션 없음", "실행할 파티션이 없습니다.")
            return

        if self.resume_mode:
            # 재개 모드: history_id 필수
            if self.history_id is None:
                QMessageBox.critical(self, "오류", "재개 이력 ID가 없습니다.")
                return
        else:
            if self.profile.id is None:
                QMessageBox.critical(self, "오류", "저장되지 않은 프로필로는 실행할 수 없습니다.")
                return

            # 새 이력 생성 + 체크포인트 생성
            start_date = cast(date, self.start_date_edit.date().toPython())
            end_date = cast(date, self.end_date_edit.date().toPython())

            source_status = "연결 성공" if self.source_connected else self.source_status_message
            target_status = "연결 성공" if self.target_connected else self.target_status_message

            history = self.history_manager.create_history(
                self.profile.id,
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
                source_status=source_status,
                target_status=target_status,
            )
            if history.id is None:
                QMessageBox.critical(self, "오류", "작업 이력을 만들지 못했습니다.")
                return
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
            copy_mode=self.copy_mode,
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

        self._set_run_state("running", f"파티션 {len(partitions):,}개")

        mode_label = (
            "Server-side"
            if self.copy_mode == "server"
            else ("AUTO" if self.copy_mode == "auto" else "COPY")
        )
        self.add_log(
            f"마이그레이션 시작({mode_label}) - 파티션 {len(partitions)}개, 배치 {self.batch_size:,}",
            "INFO",
        )

        self.worker.start()

    def on_truncate_requested(self, table_name: str, row_count: int):
        reply = QMessageBox.question(
            self,
            "기존 데이터 발견",
            f"대상 테이블 {table_name}에 {row_count:,}개의 데이터가 있습니다.\n\n"
            "삭제(TRUNCATE)하고 계속 진행할까요?\n\n"
            "Yes: 삭제 후 진행\n"
            "No: 해당 파티션 실패 처리(에러 전략에 따라 중단/스킵)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if self.worker:
            self.worker.truncate_permission = reply == QMessageBox.StandardButton.Yes
            if reply == QMessageBox.StandardButton.No and not self.worker.skip_on_error:
                # 중단 모드면 즉시 stop(대화상자 반환 후 워커가 예외 처리)
                self.worker.stop()

    def pause_migration(self):
        if not self.worker:
            return

        if self.run_state == "running":
            self.worker.pause()
            self._set_run_state("paused", self._pause_detail())
            self.add_log("일시정지 요청", "INFO")
        else:
            self.worker.resume()
            self._set_run_state("running", self.run_detail_label.text())
            self.add_log("재개 요청", "INFO")

    def _pause_detail(self) -> str:
        """일시정지가 언제 실제로 걸리는지 말한다.

        Python COPY는 배치 사이에서 바로 멈춘다. server-side COPY는 파티션
        하나가 단일 명령이라 그 파티션이 끝나야 멈춘다. 그냥 '일시정지'라고만
        쓰면 사용자는 이미 멈춘 줄 알고 창을 닫거나 DB를 만진다.
        """
        mode = getattr(self.worker, "copy_mode", self.copy_mode)
        if mode == "python":
            return "일시정지"
        return "일시정지 요청됨 · 진행 중인 파티션이 끝나면 멈춥니다"

    def cancel_migration(self):
        """진행 중인 작업만 멈춘다. 창을 닫는 일은 '닫기'가 한다."""
        if not self.worker or not self._is_running():
            return

        reply = QMessageBox.question(
            self,
            "작업 취소",
            "마이그레이션을 취소하시겠습니까?\n완료된 파티션은 유지되며, 나중에 이어서 진행할 수 있습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.worker.stop()
            self.add_log("사용자가 마이그레이션을 취소했습니다", "WARNING")

    def run_rowcount_verification(self):
        """수동 row_count 검증 실행 (옵션3)"""
        if self.worker and getattr(self.worker, "is_running", False):
            QMessageBox.information(
                self, "안내", "마이그레이션 실행 중에는 검증을 시작할 수 없습니다."
            )
            return

        if not self.history_id:
            QMessageBox.warning(
                self, "이력 없음", "검증할 작업 이력이 없습니다. 먼저 마이그레이션을 실행하세요."
            )
            return

        table_names = self._frozen_selection or self.get_selected_partition_names()
        if not table_names:
            QMessageBox.warning(self, "파티션 없음", "검증할 파티션이 없습니다.")
            return

        reply = QMessageBox.question(
            self,
            "검증 실행",
            "COUNT(*) 기반 검증은 시간이 오래 걸릴 수 있습니다.\n\n계속 진행할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.add_log(f"row_count 검증 시작 - 파티션 {len(table_names)}개", "INFO")

        self.verify_btn.setEnabled(False)

        if self._is_scan_inflight("verify"):
            return

        worker = RowCountVerifyWorker(
            self._scan_gen,
            dict(self.profile.source_config or {}),
            dict(self.profile.target_config or {}),
            list(table_names),
        )
        worker.progress.connect(self._on_verify_progress)
        worker.result.connect(self._on_verify_result)
        worker.failed.connect(self._on_verify_error)
        worker.finished.connect(self._on_verify_finished)

        self._scan_workers["verify"] = worker
        self._mark_scan_started("verify")
        worker.start()

    def _on_verify_progress(self, gen: int, done: int, total: int):
        if not self._is_current_generation(gen):
            return
        self.add_log(f"검증 진행: {done}/{total}", "INFO")

    def _on_verify_error(self, gen: int, msg: str):
        if not self._is_current_generation(gen):
            return
        self.add_log(f"검증 오류: {msg}", "ERROR")
        QMessageBox.critical(self, "검증 오류", msg)

    def _on_verify_finished(self):
        """성공·실패·취소 공통 정리. 버튼 활성화는 상태 머신이 정한다."""
        self._mark_scan_finished("verify")
        self._set_run_state(self.run_state, self.run_detail_label.text())

    def _on_verify_result(self, gen: int, payload: object):
        if not self._is_current_generation(gen):
            return
        results = cast(list, payload)
        mismatches = [r for r in (results or []) if not r.get("ok")]

        # mismatch는 해당 파티션만 failed로 마킹하고 history는 completed 유지
        if self.history_id is None:
            return
        try:
            cps = self.checkpoint_manager.get_checkpoints(self.history_id)
            by_name = {cp.partition_name: cp for cp in cps}

            for r in mismatches:
                name = r.get("table")
                cp = by_name.get(name)
                if not cp or cp.id is None:
                    continue
                self.checkpoint_manager.update_checkpoint_status(
                    cp.id,
                    "failed",
                    error_message=(
                        f"row_count mismatch: source={r.get('source_count')}, target={r.get('target_count')}"
                    ),
                )
        except Exception as e:
            self.add_log(f"검증 결과 반영 실패: {e}", "WARNING")

        if not mismatches:
            self.add_log("검증 완료: 모든 파티션 row_count 일치", "SUCCESS")
            QMessageBox.information(self, "검증 완료", "모든 파티션이 row_count 일치합니다.")
            return

        self.add_log(
            f"검증 완료: 불일치 {len(mismatches)}개 (해당 파티션은 failed로 표시됨)",
            "WARNING",
        )
        details = "\n".join(
            [
                f"- {m['table']}: source={m['source_count']:,}, target={m['target_count']:,}"
                for m in mismatches
            ]
        )
        QMessageBox.warning(
            self,
            "검증 불일치",
            f"불일치 파티션 {len(mismatches)}개가 발견되었습니다.\n\n{details}\n\n"
            "이력은 완료(completed)로 유지되며, 불일치 파티션만 failed로 표시했습니다.",
        )

    def on_progress(self, data: dict):
        if "total_progress" in data:
            self.total_progress.setValue(int(data["total_progress"]))
            done = data.get("completed_partitions", 0)
            total = data.get("total_partitions", 0)
            self.total_label.setText(f"{done} / {total}")
            if self.run_state in ("running", "paused"):
                self.run_detail_label.setText(f"파티션 {done} / {total} 완료")

        if "current_progress" in data:
            self.current_progress.setValue(int(data["current_progress"]))
            part = data.get("current_partition", "")
            rows = int(data.get("current_rows", 0) or 0)
            self.current_label.setText(f"{part} ({rows:,} rows)")

        if "speed" in data:
            self.speed_metric.set_value(f"{int(data['speed']):,} rows/s")

    def on_performance_update(self, stats: dict):
        rows_per_sec = float(stats.get("instant_rows_per_sec", 0) or 0)
        if rows_per_sec >= 1_000_000:
            speed_text = f"{rows_per_sec / 1_000_000:.1f}M rows/s"
        elif rows_per_sec >= 1_000:
            speed_text = f"{rows_per_sec / 1_000:.1f}K rows/s"
        else:
            speed_text = f"{rows_per_sec:.0f} rows/s"
        self.speed_metric.set_value(speed_text)

        mb_per_sec = float(stats.get("instant_mb_per_sec", 0.0) or 0.0)
        self.data_rate_metric.set_value(f"{mb_per_sec:.1f} MB/s")

        self.eta_metric.set_value(str(stats.get("eta_time", "계산 중")))
        self.elapsed_metric.set_value(str(stats.get("elapsed_time", "00:00:00")))

    def on_worker_thread_finished(self):
        """워커 스레드 종료 핸들러

        주의: QThread.finished는 "완료"가 아니라 "종료"이므로,
        - 오류: on_error에서 처리
        - 사용자 중단/취소: worker.is_running == False
        - 정상 완료: worker.is_running == True
        로 판정한다.

        is_running 판정 후 즉시 False로 초기화하여,
        이후 _update_nav_state()가 "실행 중"으로 오판하지 않도록 한다.
        """
        # 완료 유형 판정용 스냅샷 (is_running: True=정상완료, False=취소)
        was_normal_completion = self.worker and getattr(self.worker, "is_running", False)
        if self.worker:
            self.worker.is_running = False

        # 오류 케이스는 on_error에서 status=failed 처리하므로 여기선 건드리지 않는다.
        if getattr(self, "_worker_had_error", False):
            self._set_run_state("failed", self.run_detail_label.text())
            return

        if not self.worker:
            self._set_run_state("idle")
            return

        rows_processed = self._get_processed_rows()

        # 중단/취소: 재개 가능해야 하므로 completed로 마킹하지 않는다.
        if not was_normal_completion:
            if self.history_id:
                self.history_manager.update_history_status(
                    self.history_id, "running", processed_rows=rows_processed
                )
            self.add_log(
                "마이그레이션이 중단되었습니다. '이어서 시작'을 누르면 남은 파티션부터 재개합니다.",
                "WARNING",
            )
            self._set_run_state("stopped", f"{rows_processed:,} rows 처리 후 중단")
            return

        # 정상 완료: 프로그래스바 100%로 갱신
        self.total_progress.setValue(100)
        self.current_progress.setValue(100)
        total_partitions = len(self.worker.partitions)
        self.total_label.setText(f"{total_partitions} / {total_partitions}")
        self.current_label.setText("완료")

        if self.history_id:
            self.history_manager.update_history_status(
                self.history_id, "completed", processed_rows=rows_processed
            )

        self.add_log("마이그레이션이 완료되었습니다", "SUCCESS")
        self._set_run_state("done", f"파티션 {total_partitions:,}개 · {rows_processed:,} rows")
        QMessageBox.information(
            self,
            "완료",
            f"마이그레이션이 완료되었습니다.\n\n"
            f"파티션 {total_partitions:,}개 · {rows_processed:,} rows\n\n"
            "행 수가 정확히 맞는지 확인하려면 '검증 실행'을 누르세요.",
        )

    def on_error(self, error_msg: str):
        self._worker_had_error = True
        if self.worker:
            self.worker.is_running = False

        if self.history_id:
            rows_processed = self._get_processed_rows()
            self.history_manager.update_history_status(
                self.history_id, "failed", processed_rows=rows_processed
            )

        self.add_log(f"오류 발생: {error_msg}", "ERROR")
        self._set_run_state("failed", "실행 로그에서 원인을 확인하세요")
        QMessageBox.critical(self, "오류", f"마이그레이션 중 오류가 발생했습니다:\n\n{error_msg}")

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
        # 메시지에 <, & 같은 문자가 있어도 그대로 보이도록 이스케이프한다.
        safe = html.escape(message).replace("\n", "<br>")
        color = log_color(level)
        self.log_text.append(
            f'<span style="color:{TEXT_MUTED}">[{timestamp}]</span> '
            f'<span style="color:{color}">{level:<7} {safe}</span>'
        )
        self.log_text.moveCursor(QTextCursor.MoveOperation.End)
        log_emitter.emit_log(level, message)

    # ============================
    # Bind
    # ============================

    def _bind_ui(self):
        self.pages.currentChanged.connect(lambda _i: self._update_nav_state())

        # 파티션 체크 변경 시 카운트/네비 갱신
        self.partition_list.itemChanged.connect(
            lambda _item: (self._update_counts(), self._update_nav_state())
        )

        self.pages.setCurrentIndex(0)
        self._guard_last_table_type()
        self._update_step_ui()
        self._set_run_state("idle")

    # ============================
    # Qt lifecycle
    # ============================

    def _block_close_while_running(self) -> bool:
        if not self._is_running():
            return False
        QMessageBox.warning(
            self,
            "진행 중",
            "마이그레이션 진행 중에는 닫을 수 없습니다.\n먼저 '작업 취소'로 작업을 멈추세요.",
        )
        return True

    def reject(self):
        # Esc 키는 closeEvent를 거치지 않을 수 있으므로 여기서도 막는다.
        if self._block_close_while_running():
            return
        if not self._prepare_close():
            QTimer.singleShot(self.SCAN_SHUTDOWN_STEP_MS, self.reject)
            return
        super().reject()

    def closeEvent(self, event):
        if self._block_close_while_running():
            event.ignore()
            return
        # 조회 워커는 닫기를 막지 않지만, 정리하지 않고 닫으면 실행 중인
        # QThread가 파괴돼 프로세스가 죽는다(main_window가 deleteLater를 건다).
        if not self._prepare_close():
            event.ignore()
            QTimer.singleShot(self.SCAN_SHUTDOWN_STEP_MS, self.close)
            return
        event.accept()
