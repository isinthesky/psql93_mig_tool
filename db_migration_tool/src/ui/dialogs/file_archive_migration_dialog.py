"""PostgreSQL ↔ File archive migration dialog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from psycopg import sql
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDateEdit,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.archive_manifest import ArchiveManifestStore
from src.core.file_archive_workers import FileToPostgresArchiveWorker, PostgresToFileArchiveWorker
from src.core.partition_discovery import PartitionDiscovery
from src.core.table_types import TABLE_TYPE_CONFIG, TableType, get_all_table_types
from src.database.postgres_utils import PostgresOptimizer
from src.models.history import CheckpointManager, HistoryManager, MigrationHistoryItem
from src.models.profile import ENDPOINT_KIND_FILE, ENDPOINT_KIND_POSTGRES, ConnectionProfile
from src.utils.enhanced_logger import log_emitter
from src.utils.validators import ConnectionValidator


@dataclass
class PartitionSummary:
    table_name: str
    row_count: int
    table_type: TableType


def to_qdate(py_date):
    from PySide6.QtCore import QDate

    return QDate(py_date.year, py_date.month, py_date.day)


class FileArchiveMigrationDialog(QDialog):
    def __init__(self, parent, profile: ConnectionProfile):
        super().__init__(parent)
        self.profile = profile
        self.history_manager = HistoryManager()
        self.checkpoint_manager = CheckpointManager()

        self.worker = None
        self.history_id: int | None = None
        self.resume_mode = False
        self._incomplete_history: MigrationHistoryItem | None = None
        self._frozen_selection: list[str] = []
        self._worker_had_error = False

        self.source_connected = False
        self.target_connected = False
        self.source_status_message = "확인 전"
        self.target_status_message = "확인 전"
        self.selected_table_types = [TableType.POINT_HISTORY]
        self.error_strategy = "stop"
        self.discovered_partitions: list[PartitionSummary] = []
        self._target_has_data: dict[str, bool] = {}

        self.setWindowTitle(self._build_title())
        self.resize(980, 760)
        self._build_ui()
        self._check_incomplete_migration()
        self.check_connections()

    def _build_title(self) -> str:
        if self.profile.migration_mode == "postgres_to_file":
            return "파일 아카이브 마이그레이션 - PostgreSQL → File Archive"
        return "파일 아카이브 마이그레이션 - File Archive → PostgreSQL"

    def _source_label(self) -> str:
        return "소스 DB" if self.profile.source_kind == ENDPOINT_KIND_POSTGRES else "소스 Archive"

    def _target_label(self) -> str:
        return "대상 DB" if self.profile.target_kind == ENDPOINT_KIND_POSTGRES else "대상 Archive"

    def _build_ui(self):
        root = QVBoxLayout(self)

        self.step_title = QLabel("1/3 연결 확인")
        self.step_title.setStyleSheet("font-size: 18px; font-weight: bold;")
        self.step_hint = QLabel(
            f"{self._source_label()}와 {self._target_label()} 연결/경로를 확인하고, 파티션 단위로 내보내기/가져오기를 진행합니다."
        )
        self.step_hint.setStyleSheet("color: #888888;")
        root.addWidget(self.step_title)
        root.addWidget(self.step_hint)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_page_connections())
        self.pages.addWidget(self._build_page_scope())
        self.pages.addWidget(self._build_page_run())
        root.addWidget(self.pages, 1)

        nav = QHBoxLayout()
        self.back_btn = QPushButton("이전")
        self.back_btn.clicked.connect(self.go_back)
        self.next_btn = QPushButton("다음")
        self.next_btn.clicked.connect(self.go_next)
        self.close_btn = QPushButton("닫기")
        self.close_btn.clicked.connect(self.reject)
        nav.addWidget(self.back_btn)
        nav.addStretch(1)
        nav.addWidget(self.next_btn)
        nav.addWidget(self.close_btn)
        root.addLayout(nav)

        self._update_nav_state()

    def _build_page_connections(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._create_connection_status_widget())

        self.recheck_btn = QPushButton("연결 다시 확인")
        self.recheck_btn.clicked.connect(self.check_connections)
        layout.addWidget(self.recheck_btn)

        self.incomplete_group = QGroupBox("이전 작업 이어서 진행")
        ig = QVBoxLayout(self.incomplete_group)
        self.incomplete_label = QLabel("")
        self.incomplete_label.setWordWrap(True)
        ig.addWidget(self.incomplete_label)
        row = QHBoxLayout()
        self.resume_btn = QPushButton("이어서 진행")
        self.resume_btn.clicked.connect(self._on_resume_clicked)
        self.new_run_btn = QPushButton("새 작업으로 진행")
        self.new_run_btn.clicked.connect(self._on_new_run_clicked)
        row.addWidget(self.resume_btn)
        row.addWidget(self.new_run_btn)
        row.addStretch(1)
        ig.addLayout(row)
        layout.addWidget(self.incomplete_group)

        tip = QLabel(
            "1) 연결 확인 → 2) 범위/파티션 선택 → 3) 실행 순서로 진행합니다.\n"
            "PG→File / File→PG 는 파티션 단위 재개(MVP)만 지원합니다."
        )
        tip.setStyleSheet("color: #888888;")
        tip.setWordWrap(True)
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

        summary_group = QGroupBox("3/3 실행 요약")
        sg = QVBoxLayout(summary_group)
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        sg.addWidget(self.summary_label)
        layout.addWidget(summary_group)

        progress_group = QGroupBox("진행 상황")
        pg = QVBoxLayout(progress_group)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("전체:"))
        self.total_progress = QProgressBar()
        row1.addWidget(self.total_progress)
        self.total_label = QLabel("0 / 0")
        row1.addWidget(self.total_label)
        pg.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("현재:"))
        self.current_progress = QProgressBar()
        row2.addWidget(self.current_progress)
        self.current_label = QLabel("대기중")
        row2.addWidget(self.current_label)
        pg.addLayout(row2)

        row3 = QHBoxLayout()
        self.speed_label = QLabel("처리 속도: 0 rows/sec")
        self.data_rate_label = QLabel("전송 속도: 0 MB/sec")
        self.eta_label = QLabel("예상 완료: 계산중...")
        self.elapsed_label = QLabel("경과 시간: 00:00:00")
        row3.addWidget(self.speed_label)
        row3.addWidget(self.data_rate_label)
        row3.addWidget(self.eta_label)
        row3.addWidget(self.elapsed_label)
        row3.addStretch(1)
        pg.addLayout(row3)
        layout.addWidget(progress_group)

        log_group = QGroupBox("실행 로그")
        lg = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        lg.addWidget(self.log_text)
        layout.addWidget(log_group, 1)

        controls = QHBoxLayout()
        self.start_btn = QPushButton("시작")
        self.start_btn.clicked.connect(self.start_migration)
        self.pause_btn = QPushButton("일시정지")
        self.pause_btn.clicked.connect(self.pause_migration)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setVisible(False)
        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.clicked.connect(self.cancel_migration)
        controls.addWidget(self.start_btn)
        controls.addWidget(self.pause_btn)
        controls.addStretch(1)
        controls.addWidget(self.cancel_btn)
        layout.addLayout(controls)
        return page

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
            return title_label, icon, text

        s_title, self.source_status_icon, self.source_status_text = mk_block(self._source_label())
        t_title, self.target_status_icon, self.target_status_text = mk_block(self._target_label())
        layout.addWidget(s_title)
        layout.addWidget(self.source_status_icon)
        layout.addWidget(self.source_status_text)
        layout.addSpacing(30)
        layout.addWidget(t_title)
        layout.addWidget(self.target_status_icon)
        layout.addWidget(self.target_status_text)
        layout.addStretch(1)
        return widget

    def _create_scope_group(self) -> QGroupBox:
        group = QGroupBox("2/3 범위 선택")
        layout = QVBoxLayout(group)

        settings_group = QGroupBox("마이그레이션 설정")
        sg = QVBoxLayout(settings_group)
        row = QHBoxLayout()
        row.addWidget(QLabel("마이그레이션 항목:"))
        self.table_type_checkboxes = {}
        for table_type in get_all_table_types():
            config = TABLE_TYPE_CONFIG[table_type]
            checkbox = QCheckBox(f"{config.display_name} ({table_type.value})")
            checkbox.setChecked(table_type == TableType.POINT_HISTORY)
            checkbox.stateChanged.connect(self._on_table_type_changed)
            self.table_type_checkboxes[table_type] = checkbox
            row.addWidget(checkbox)
        row.addStretch(1)
        sg.addLayout(row)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("에러 처리:"))
        self.stop_on_error_radio = QRadioButton("중단")
        self.stop_on_error_radio.setChecked(True)
        self.skip_on_error_radio = QRadioButton("건너뛰기(파티션 단위)")
        bg = QButtonGroup(self)
        bg.addButton(self.stop_on_error_radio)
        bg.addButton(self.skip_on_error_radio)
        self.stop_on_error_radio.toggled.connect(self._on_error_strategy_changed)
        self.skip_on_error_radio.toggled.connect(self._on_error_strategy_changed)
        opts.addWidget(self.stop_on_error_radio)
        opts.addWidget(self.skip_on_error_radio)
        opts.addStretch(1)
        sg.addLayout(opts)
        layout.addWidget(settings_group)

        date_group = QGroupBox("날짜 범위")
        dg = QVBoxLayout(date_group)
        row_dates = QHBoxLayout()
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.end_date_edit = QDateEdit()
        self.end_date_edit.setCalendarPopup(True)
        today = datetime.now().date()
        self.start_date_edit.setDate(to_qdate(today))
        self.end_date_edit.setDate(to_qdate(today))
        row_dates.addWidget(QLabel("시작"))
        row_dates.addWidget(self.start_date_edit)
        row_dates.addWidget(QLabel("종료"))
        row_dates.addWidget(self.end_date_edit)
        row_dates.addStretch(1)
        dg.addLayout(row_dates)

        presets = QHBoxLayout()
        for label, days, yesterday in [
            ("오늘", 0, False),
            ("어제", 1, True),
            ("최근 7일", 7, False),
            ("최근 30일", 30, False),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(
                lambda _checked=False, d=days, y=yesterday: self._set_preset_days(d, y)
            )
            presets.addWidget(btn)
        presets.addStretch(1)
        dg.addLayout(presets)
        layout.addWidget(date_group)

        action_group = QGroupBox("파티션 탐색 및 선택")
        ag = QVBoxLayout(action_group)
        row_actions = QHBoxLayout()
        self.discover_btn = QPushButton("파티션 찾기")
        self.discover_btn.clicked.connect(self.discover_partitions)
        self.check_completed_btn = QPushButton("완료여부 확인")
        self.check_completed_btn.clicked.connect(self.check_target_completed)
        row_actions.addWidget(self.discover_btn)
        row_actions.addWidget(self.check_completed_btn)
        self.discover_status = QLabel("날짜/항목을 선택하고 ‘파티션 찾기’를 눌러주세요.")
        row_actions.addWidget(self.discover_status)
        row_actions.addStretch(1)
        ag.addLayout(row_actions)

        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        ag.addWidget(separator)

        row_select = QHBoxLayout()
        self.select_all_btn = QPushButton("전체 선택")
        self.select_all_btn.clicked.connect(lambda: self._bulk_check(True))
        self.select_none_btn = QPushButton("전체 해제")
        self.select_none_btn.clicked.connect(lambda: self._bulk_check(False))
        self.select_pending_btn = QPushButton("완료 제외 선택")
        self.select_pending_btn.clicked.connect(self._select_excluding_completed)
        row_select.addWidget(self.select_all_btn)
        row_select.addWidget(self.select_none_btn)
        row_select.addWidget(self.select_pending_btn)
        row_select.addStretch(1)
        ag.addLayout(row_select)
        layout.addWidget(action_group)
        return group

    def _create_partition_group(self) -> QGroupBox:
        group = QGroupBox("발견된 파티션")
        layout = QVBoxLayout(group)
        self.partition_list = QListWidget()
        self.partition_list.itemChanged.connect(
            lambda _item: (self._update_counts(), self._update_nav_state())
        )
        layout.addWidget(self.partition_list)
        info = QHBoxLayout()
        self.partition_count_label = QLabel("총 0개")
        self.partition_selected_label = QLabel("선택 0개")
        self.partition_rows_label = QLabel("총 0 rows")
        info.addWidget(self.partition_count_label)
        info.addSpacing(20)
        info.addWidget(self.partition_selected_label)
        info.addSpacing(20)
        info.addWidget(self.partition_rows_label)
        info.addStretch(1)
        layout.addLayout(info)
        return group

    def _set_preset_days(self, days: int, yesterday: bool = False):
        today = datetime.now().date()
        if yesterday:
            target = today - timedelta(days=1)
            self.start_date_edit.setDate(to_qdate(target))
            self.end_date_edit.setDate(to_qdate(target))
        elif days == 0:
            self.start_date_edit.setDate(to_qdate(today))
            self.end_date_edit.setDate(to_qdate(today))
        else:
            start = today - timedelta(days=days - 1)
            self.start_date_edit.setDate(to_qdate(start))
            self.end_date_edit.setDate(to_qdate(today))

    def _on_table_type_changed(self, _state: int):
        selected = [tt for tt, cb in self.table_type_checkboxes.items() if cb.isChecked()]
        if not selected:
            sender = self.sender()
            if isinstance(sender, QCheckBox):
                sender.setChecked(True)
            QMessageBox.warning(self, "선택 오류", "최소 1개의 테이블 타입을 선택해야 합니다.")
            return
        self.selected_table_types = selected

    def _on_error_strategy_changed(self, _checked: bool):
        self.error_strategy = "stop" if self.stop_on_error_radio.isChecked() else "skip"

    def _check_incomplete_migration(self):
        incomplete = self.history_manager.get_incomplete_history(self.profile.id)
        self._incomplete_history = incomplete
        if not incomplete:
            self.incomplete_group.setVisible(False)
            return
        self.incomplete_group.setVisible(True)
        self.incomplete_label.setText(
            "이전에 중단된 작업이 있습니다.\n"
            f"날짜: {incomplete.start_date} ~ {incomplete.end_date}\n"
            f"진행: {incomplete.processed_rows:,} rows\n\n"
            "이어서 진행하면 미완료 파티션만 다시 실행합니다."
        )

    def _on_resume_clicked(self):
        if not self._incomplete_history:
            return
        self.resume_mode = True
        self.history_id = self._incomplete_history.id
        pending = self.checkpoint_manager.get_pending_checkpoints(self.history_id)
        self._frozen_selection = [cp.partition_name for cp in pending]
        try:
            start_d = datetime.strptime(self._incomplete_history.start_date, "%Y-%m-%d").date()
            end_d = datetime.strptime(self._incomplete_history.end_date, "%Y-%m-%d").date()
            self.start_date_edit.setDate(to_qdate(start_d))
            self.end_date_edit.setDate(to_qdate(end_d))
        except Exception:
            pass
        self.pages.setCurrentIndex(2)
        self._refresh_summary()
        self._update_step_text()
        self._update_nav_state()
        self.add_log(f"재개 모드: 미완료 파티션 {len(self._frozen_selection)}개", "INFO")

    def _on_new_run_clicked(self):
        self.resume_mode = False
        self.history_id = None
        self._frozen_selection = []
        QMessageBox.information(self, "안내", "새 작업으로 진행합니다.")

    def check_connections(self):
        self._update_connection_ui(True, True, "확인 중...", "확인 중...")

        if self.profile.source_kind == ENDPOINT_KIND_POSTGRES:
            s_ok, s_msg = PostgresOptimizer.check_connection_quick(self.profile.source_config)
        else:
            s_ok, s_msg = ConnectionValidator.validate_file_archive_config(
                self.profile.source_config,
                must_exist=True,
            )
            s_msg = "경로 확인 완료" if s_ok else s_msg

        if self.profile.target_kind == ENDPOINT_KIND_POSTGRES:
            t_ok, t_msg = PostgresOptimizer.check_connection_quick(self.profile.target_config)
        else:
            t_ok, t_msg = ConnectionValidator.validate_file_archive_config(
                self.profile.target_config,
                must_exist=False,
            )
            t_msg = "출력 경로 사용 가능" if t_ok else t_msg

        self._update_connection_ui(s_ok, t_ok, s_msg, t_msg)

    def _update_connection_ui(self, source_ok: bool, target_ok: bool, source_msg: str, target_msg: str):
        self.source_connected = source_ok
        self.target_connected = target_ok
        self.source_status_message = source_msg
        self.target_status_message = target_msg

        self.source_status_icon.setStyleSheet(
            f"color: {'#00FF00' if source_ok else '#FF0000'}; font-size: 16px;"
        )
        self.target_status_icon.setStyleSheet(
            f"color: {'#00FF00' if target_ok else '#FF0000'}; font-size: 16px;"
        )
        self.source_status_text.setText(source_msg)
        self.target_status_text.setText(target_msg)
        self._update_nav_state()

    def discover_partitions(self):
        if not (self.source_connected and self.target_connected):
            QMessageBox.warning(self, "연결 필요", "먼저 양쪽 엔드포인트를 확인하세요.")
            return

        start_date = self.start_date_edit.date().toPython()
        end_date = self.end_date_edit.date().toPython()
        if start_date > end_date:
            QMessageBox.warning(self, "날짜 오류", "시작 날짜가 종료 날짜보다 늦습니다.")
            return

        self.discover_status.setText("파티션 탐색 중...")
        if self.profile.source_kind == ENDPOINT_KIND_POSTGRES:
            discovery = PartitionDiscovery(self.profile.source_config)
            partitions = discovery.discover_partitions(start_date, end_date, self.selected_table_types)
        else:
            store = ArchiveManifestStore(self.profile.source_config["archive_path"])
            partitions = store.filter_partitions(
                start_date=start_date,
                end_date=end_date,
                table_types=self.selected_table_types,
            )

        self.discovered_partitions = [
            PartitionSummary(
                table_name=item["table_name"],
                row_count=int(item.get("row_count") or 0),
                table_type=item["table_type"],
            )
            for item in partitions
        ]
        self._target_has_data = {}
        self._render_partition_list()
        self.discover_status.setText(f"완료: {len(self.discovered_partitions)}개 파티션, 대상 확인 중...")
        self._update_counts()
        self._update_nav_state()

        if self.discovered_partitions:
            self.check_target_completed()
            self.discover_status.setText(f"완료: {len(self.discovered_partitions)}개 파티션")

    def _render_partition_list(self):
        self.partition_list.clear()
        gray = QColor("#888888")
        for summary in self.discovered_partitions:
            cfg = TABLE_TYPE_CONFIG[summary.table_type]
            suffix = " [완료/존재]" if self._target_has_data.get(summary.table_name) else ""
            item = QListWidgetItem(
                f"[{cfg.display_name}] {summary.table_name} ({summary.row_count:,} rows){suffix}"
            )
            item.setData(Qt.UserRole, summary.table_name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Unchecked if self._target_has_data.get(summary.table_name) else Qt.Checked
            )
            if self._target_has_data.get(summary.table_name):
                item.setForeground(gray)
            self.partition_list.addItem(item)

    def _bulk_check(self, checked: bool):
        for idx in range(self.partition_list.count()):
            item = self.partition_list.item(idx)
            if item.data(Qt.UserRole):
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self._update_counts()
        self._update_nav_state()

    def _select_excluding_completed(self):
        for idx in range(self.partition_list.count()):
            item = self.partition_list.item(idx)
            name = item.data(Qt.UserRole)
            if name:
                item.setCheckState(Qt.Unchecked if self._target_has_data.get(name) else Qt.Checked)
        self._update_counts()
        self._update_nav_state()

    def get_selected_partition_names(self) -> list[str]:
        selected = []
        for idx in range(self.partition_list.count()):
            item = self.partition_list.item(idx)
            name = item.data(Qt.UserRole)
            if name and item.checkState() == Qt.Checked:
                selected.append(str(name))
        return selected

    def _update_counts(self):
        total = len(self.discovered_partitions)
        selected = len(self.get_selected_partition_names())
        rows = sum(part.row_count for part in self.discovered_partitions)
        self.partition_count_label.setText(f"총 {total}개")
        self.partition_selected_label.setText(f"선택 {selected}개")
        self.partition_rows_label.setText(f"총 {rows:,} rows")

    def check_target_completed(self):
        names = [part.table_name for part in self.discovered_partitions]
        if not names:
            return

        if self.profile.target_kind == ENDPOINT_KIND_FILE:
            store = ArchiveManifestStore(self.profile.target_config["archive_path"])
            try:
                self._target_has_data = store.get_completed_status(names)
            except FileNotFoundError:
                self._target_has_data = {name: False for name in names}
        else:
            self._target_has_data = self._check_target_postgres_partitions(names)

        self._render_partition_list()
        self._update_counts()
        self._update_nav_state()
        self.add_log("완료 여부 확인 완료", "INFO")

    def _check_target_postgres_partitions(self, names: list[str]) -> dict[str, bool]:
        conn_params = {
            "host": self.profile.target_config.get("host"),
            "port": self.profile.target_config.get("port"),
            "dbname": self.profile.target_config.get("database"),
            "user": self.profile.target_config.get("username"),
            "password": self.profile.target_config.get("password"),
        }
        if self.profile.target_config.get("ssl"):
            conn_params["sslmode"] = "require"

        results = {name: False for name in names}
        conn = psycopg.connect(**conn_params)
        try:
            with conn.cursor() as cur:
                for name in names:
                    cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_schema = 'public' AND table_name = %s
                        )
                        """,
                        (name,),
                    )
                    exists = bool(cur.fetchone()[0])
                    if not exists:
                        continue
                    try:
                        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(name)))
                        results[name] = bool(cur.fetchone()[0] > 0)
                    except Exception:
                        results[name] = False
        finally:
            conn.close()
        return results

    def go_back(self):
        idx = self.pages.currentIndex()
        if idx > 0:
            self.pages.setCurrentIndex(idx - 1)
        self._update_step_text()
        self._update_nav_state()

    def go_next(self):
        idx = self.pages.currentIndex()
        if idx == 0:
            self.pages.setCurrentIndex(1)
        elif idx == 1:
            selected = self.get_selected_partition_names()
            if not selected:
                QMessageBox.warning(self, "선택 오류", "실행할 파티션을 1개 이상 선택하세요.")
                return
            self._frozen_selection = selected
            self.pages.setCurrentIndex(2)
            self._refresh_summary()
        self._update_step_text()
        self._update_nav_state()

    def _update_step_text(self):
        idx = self.pages.currentIndex()
        titles = ["1/3 연결 확인", "2/3 범위/파티션 선택", "3/3 실행"]
        hints = [
            "양쪽 엔드포인트를 확인합니다.",
            "날짜/항목을 선택하고 파티션을 고릅니다.",
            "파일 아카이브 마이그레이션을 실행합니다.",
        ]
        self.step_title.setText(titles[idx])
        self.step_hint.setText(hints[idx])

    def _update_nav_state(self):
        idx = self.pages.currentIndex()
        running = bool(self.worker and getattr(self.worker, "is_running", False))
        self.back_btn.setEnabled(idx > 0 and not running)
        if idx == 0:
            self.next_btn.setVisible(True)
            self.next_btn.setEnabled(self.source_connected and self.target_connected)
        elif idx == 1:
            self.next_btn.setVisible(True)
            self.next_btn.setEnabled(len(self.get_selected_partition_names()) > 0)
        else:
            self.next_btn.setVisible(False)

    def _refresh_summary(self):
        parts = self._frozen_selection or self.get_selected_partition_names()
        mode_text = {
            "postgres_to_file": "PostgreSQL → File Archive",
            "file_to_postgres": "File Archive → PostgreSQL",
        }.get(self.profile.migration_mode, self.profile.migration_mode)
        lines = [
            f"프로필: {self.profile.name}",
            f"모드: {mode_text}",
            f"에러 처리: {'중단' if self.error_strategy == 'stop' else '건너뛰기'}",
            f"파티션: {len(parts)}개",
        ]
        if self.resume_mode:
            lines.append("재개: 미완료 파티션만 재실행")
        self.summary_label.setText("\n".join(lines))

    def start_migration(self):
        if self.worker and self.worker.isRunning():
            return

        # 이전 실행이 중단된 상태에서 다시 시작 → resume 모드로 전환
        if self.history_id and not self.resume_mode:
            pending = self.checkpoint_manager.get_pending_checkpoints(self.history_id)
            if pending:
                self.resume_mode = True
                self._frozen_selection = [cp.partition_name for cp in pending]
                self.add_log(f"중단된 작업 재개: 미완료 파티션 {len(pending)}개", "INFO")

        partitions = self._frozen_selection or self.get_selected_partition_names()
        if not partitions:
            QMessageBox.warning(self, "파티션 없음", "실행할 파티션이 없습니다.")
            return

        if not self.resume_mode:
            start_date = self.start_date_edit.date().toPython()
            end_date = self.end_date_edit.date().toPython()
            history = self.history_manager.create_history(
                self.profile.id,
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
                source_status=self.source_status_message,
                target_status=self.target_status_message,
            )
            self.history_id = history.id
            for partition in partitions:
                self.checkpoint_manager.create_checkpoint(self.history_id, partition)

        if self.profile.migration_mode == "postgres_to_file":
            self.worker = PostgresToFileArchiveWorker(
                self.profile,
                partitions,
                self.history_id,
                resume=self.resume_mode,
            )
        else:
            self.worker = FileToPostgresArchiveWorker(
                self.profile,
                partitions,
                self.history_id,
                resume=self.resume_mode,
            )

        self._worker_had_error = False
        self.worker.skip_on_error = self.error_strategy == "skip"
        self.worker.progress.connect(self.on_progress)
        self.worker.log.connect(self.add_log)
        self.worker.error.connect(self.on_error)
        self.worker.performance.connect(self.on_performance_update)
        self.worker.finished.connect(self.on_worker_finished)

        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.close_btn.setEnabled(False)
        self.add_log(f"마이그레이션 시작 - 파티션 {len(partitions)}개", "INFO")
        self.worker.start()
        self._update_nav_state()

    def on_truncate_requested(self, table_name: str, row_count: int):
        reply = QMessageBox.question(
            self,
            "기존 데이터 발견",
            f"대상 테이블 {table_name}에 {row_count:,}개의 데이터가 있습니다.\n\n"
            "삭제(TRUNCATE)하고 다시 적재할까요?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if self.worker:
            self.worker.truncate_permission = reply == QMessageBox.Yes
            if reply == QMessageBox.No and self.error_strategy == "stop":
                self.worker.stop(reason="user_cancel_existing_data")

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
                "작업을 취소하시겠습니까? 완료된 파티션은 유지됩니다.",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self.worker.stop(reason="user_cancel")
                self.add_log("사용자가 작업을 취소했습니다", "WARNING")
        else:
            self.reject()

    def on_progress(self, data: dict):
        if "total_progress" in data:
            self.total_progress.setValue(int(data["total_progress"]))
            self.total_label.setText(
                f"{data.get('completed_partitions', 0)} / {data.get('total_partitions', 0)}"
            )
        if "current_progress" in data:
            self.current_progress.setValue(int(data["current_progress"]))
            self.current_label.setText(
                f"{data.get('current_partition', '')} ({int(data.get('current_rows', 0) or 0):,} rows)"
            )
        if "speed" in data:
            self.speed_label.setText(f"처리 속도: {int(data['speed']):,} rows/sec")

    def on_performance_update(self, stats: dict):
        self.speed_label.setText(
            f"처리 속도: {float(stats.get('instant_rows_per_sec', 0) or 0):,.0f} rows/sec"
        )
        self.data_rate_label.setText(
            f"전송 속도: {float(stats.get('instant_mb_per_sec', 0.0) or 0.0):.1f} MB/sec"
        )
        self.eta_label.setText(f"예상 완료: {stats.get('eta_time', '계산중...')}")
        self.elapsed_label.setText(f"경과 시간: {stats.get('elapsed_time', '00:00:00')}")

    def on_worker_finished(self):
        was_normal_completion = bool(self.worker and getattr(self.worker, "is_running", False))
        if self.worker:
            self.worker.is_running = False

        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.close_btn.setEnabled(True)

        rows_processed = 0
        if self.worker and hasattr(self.worker, "get_stats"):
            try:
                rows_processed = int(self.worker.get_stats().get("total_rows") or 0)
            except Exception:
                rows_processed = 0

        if self._worker_had_error:
            self._update_nav_state()
            return

        if not was_normal_completion:
            if self.history_id:
                self.history_manager.update_history_status(
                    self.history_id,
                    "running",
                    processed_rows=rows_processed,
                )
            self.add_log("작업이 중단되었습니다. 나중에 이어서 진행할 수 있습니다.", "WARNING")
            self._update_nav_state()
            return

        self.total_progress.setValue(100)
        self.current_progress.setValue(100)
        total = len(self.worker.partitions) if self.worker else 0
        self.total_label.setText(f"{total} / {total}")

        has_failures = bool(self.worker and getattr(self.worker, "partition_failures", []))
        if has_failures:
            failed_count = len(self.worker.partition_failures)
            if self.history_id:
                self.history_manager.update_history_status(
                    self.history_id,
                    "running",
                    processed_rows=rows_processed,
                )
            self.add_log(
                f"작업 완료 (일부 실패: {failed_count}건). 이어서 진행(resume)할 수 있습니다.",
                "WARNING",
            )
            QMessageBox.warning(
                self,
                "부분 완료",
                f"마이그레이션이 완료되었지만 {failed_count}개 파티션이 실패했습니다.\n"
                "다음 실행 시 이어서 진행할 수 있습니다.",
            )
        else:
            if self.history_id:
                self.history_manager.update_history_status(
                    self.history_id,
                    "completed",
                    processed_rows=rows_processed,
                )
            self.add_log("마이그레이션이 완료되었습니다", "SUCCESS")
            QMessageBox.information(self, "완료", "마이그레이션이 성공적으로 완료되었습니다.")
        self._update_nav_state()

    def on_error(self, error_msg: str):
        self._worker_had_error = True
        if self.worker:
            self.worker.is_running = False
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        if self.history_id:
            self.history_manager.update_history_status(self.history_id, "failed")
        self.add_log(f"오류 발생: {error_msg}", "ERROR")
        QMessageBox.critical(self, "오류", error_msg)
        self._update_nav_state()

    def add_log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {level}: {message}"
        self.log_text.append(entry)
        self.log_text.moveCursor(QTextCursor.End)
        log_emitter.emit_log(level, message)
