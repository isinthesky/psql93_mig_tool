"""PostgreSQL ↔ File archive migration dialog."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import cast

from PySide6.QtCore import Qt, QTimer
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
    QLineEdit,
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

from src.core.file_archive_workers import (
    ArchiveMigrationWorkerBase,
    FileToPostgresArchiveWorker,
    PostgresToFileArchiveWorker,
)
from src.core.scan_workers import (
    ConnectionCheckWorker,
    EndpointCheckSpec,
    PartitionScanWorker,
    TargetCompletedScanWorker,
)
from src.core.table_types import TABLE_TYPE_CONFIG, TableType, get_all_table_types
from src.models.history import CheckpointManager, HistoryManager, MigrationHistoryItem
from src.models.profile import ENDPOINT_KIND_POSTGRES, ConnectionProfile
from src.ui.dialogs.scan_host import (
    PARTITION_DISPLAY_LIMIT,
    ScanHostMixin,
    build_log_retention_hint,
    format_row_count,
)
from src.ui.theme import LOG_MAX_BLOCKS, TEXT_DANGER, TEXT_MUTED, log_color
from src.ui.widgets import MetricReadout, StatusLamp, StepRail
from src.utils.enhanced_logger import log_emitter


@dataclass
class PartitionSummary:
    table_name: str
    row_count: int
    table_type: TableType
    # 내보내기(DB→파일)의 소스는 PostgreSQL이라 행 수가 추정치다.
    # 가져오기(파일→DB)의 소스는 매니페스트라 내보낼 때 실제로 센 값이다.
    row_count_estimated: bool = False


def to_qdate(py_date):
    from PySide6.QtCore import QDate

    return QDate(py_date.year, py_date.month, py_date.day)


class FileArchiveMigrationDialog(ScanHostMixin, QDialog):
    # 실행 상태 → (램프, 표시 문구). 마이그레이션 마법사와 같은 언어를 쓴다.
    # 'paused'는 없다 — 아카이브 워커가 일시정지를 지원하지 않는다.
    RUN_STATES = {
        "idle": ("idle", "대기 중"),
        "running": ("ok", "실행 중"),
        "done": ("ok", "완료"),
        "partial": ("busy", "일부 실패"),
        "stopped": ("busy", "중단됨"),
        "failed": ("error", "오류"),
    }

    def __init__(self, parent, profile: ConnectionProfile):
        super().__init__(parent)
        self.profile = profile
        self.history_manager = HistoryManager()
        self.checkpoint_manager = CheckpointManager()

        self.worker: ArchiveMigrationWorkerBase | None = None
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
        self.run_state = "idle"

        # ── 조회(scan) 작업 상태 ────────────────────────────────────────
        # 탐색/확인을 워커 스레드로 옮기기 위한 공통 골격. 아직 워커는 없고,
        # 지금은 동기 경로가 이 상태를 갱신한다.
        #
        # _scan_gen: 요청 세대. 조건이 바뀌면 올라가고, 늦게 도착한 결과는
        #   자기 세대가 아니면 버린다.
        # _selection_verified: 지금 목록에 대해 '대상 완료 여부 확인'이 끝났는가.
        #   이게 False면 '다음'을 막는다. 확인 전에는 모든 파티션이 체크된 상태로
        #   보이는데, 그대로 실행하면 이미 완료된 파티션까지 TRUNCATE 후 재적재된다.
        self._init_scan_host()
        self._selection_verified: bool = False

        self.setWindowTitle(self._build_title())
        self.resize(980, 760)
        self._build_ui()
        self._guard_last_table_type()
        self._check_incomplete_migration()
        # 연결 확인은 최대 수 초 걸린다. 창을 먼저 그린 뒤 시작해야
        # '열자마자 멈춘 창'처럼 보이지 않는다.
        QTimer.singleShot(0, self.check_connections)

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

        self.step_rail = StepRail(["연결 확인", "범위/파티션 선택", "실행"])
        root.addWidget(self.step_rail)

        self.step_title = QLabel("연결 확인")
        self.step_title.setProperty("role", "stepTitle")
        self.step_hint = QLabel(
            f"{self._source_label()}와 {self._target_label()} 연결/경로를 확인하고, "
            "파티션 단위로 내보내기/가져오기를 진행합니다."
        )
        self.step_hint.setProperty("role", "hint")
        self.step_hint.setWordWrap(True)
        root.addWidget(self.step_title)
        root.addWidget(self.step_hint)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_page_connections())
        self.pages.addWidget(self._build_page_scope())
        self.pages.addWidget(self._build_page_run())
        root.addWidget(self.pages, 1)

        # 확인(다음)을 오른쪽 끝에 두는 Windows 대화상자 관례를 따른다.
        nav = QHBoxLayout()
        self.close_btn = QPushButton("닫기")
        self.close_btn.setToolTip("창을 닫습니다. 실행 중에는 닫을 수 없습니다.")
        self.close_btn.clicked.connect(self.reject)
        self.back_btn = QPushButton("이전")
        self.back_btn.setToolTip("이전 단계로 돌아갑니다.")
        self.back_btn.clicked.connect(self.go_back)
        self.next_btn = QPushButton("다음")
        self.next_btn.setToolTip("현재 설정을 확인하고 다음 단계로 이동합니다.")
        self.next_btn.clicked.connect(self.go_next)

        for btn in (self.close_btn, self.back_btn, self.next_btn):
            btn.setAutoDefault(False)
        self.next_btn.setAutoDefault(True)
        self.next_btn.setDefault(True)

        nav.addWidget(self.close_btn)
        nav.addStretch(1)
        nav.addWidget(self.back_btn)
        nav.addWidget(self.next_btn)
        root.addLayout(nav)

        self._update_nav_state()
        # 조회 버튼도 처음부터 규칙을 따르게 한다(아직 찾은 파티션이 없으므로
        # '완료여부 확인'은 꺼져 있어야 한다).
        self._sync_scan_buttons()

    def _build_page_connections(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._create_connection_status_widget())

        actions = QHBoxLayout()
        self.recheck_btn = QPushButton("연결 다시 확인")
        self.recheck_btn.setToolTip("소스와 대상 연결/경로 상태를 다시 확인합니다.")
        self.recheck_btn.setAutoDefault(False)
        self.recheck_btn.clicked.connect(self.check_connections)
        actions.addWidget(self.recheck_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.connection_hint = QLabel("")
        self.connection_hint.setProperty("role", "hint")
        self.connection_hint.setWordWrap(True)
        layout.addWidget(self.connection_hint)

        self.incomplete_group = QGroupBox("이전 작업 이어서 진행")
        ig = QVBoxLayout(self.incomplete_group)
        self.incomplete_label = QLabel("")
        self.incomplete_label.setWordWrap(True)
        ig.addWidget(self.incomplete_label)
        row = QHBoxLayout()
        self.resume_btn = QPushButton("이어서 진행")
        self.resume_btn.setObjectName("primaryAction")
        self.resume_btn.setToolTip("이전 미완료 작업의 진행 상태를 이어서 사용합니다.")
        self.resume_btn.setAutoDefault(False)
        self.resume_btn.clicked.connect(self._on_resume_clicked)
        self.new_run_btn = QPushButton("새 작업으로 진행")
        self.new_run_btn.setToolTip("이전 진행 상태를 사용하지 않고 새 작업으로 시작합니다.")
        self.new_run_btn.setAutoDefault(False)
        self.new_run_btn.clicked.connect(self._on_new_run_clicked)
        row.addWidget(self.resume_btn)
        row.addWidget(self.new_run_btn)
        row.addStretch(1)
        ig.addLayout(row)
        layout.addWidget(self.incomplete_group)

        tip = QLabel("파티션 단위로 재개할 수 있습니다. 중단해도 완료된 파티션은 그대로 남습니다.")
        tip.setProperty("role", "hint")
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

        state_rail = QFrame()
        state_rail.setObjectName("statusRail")
        sr = QHBoxLayout(state_rail)
        sr.setContentsMargins(12, 10, 12, 10)
        self.run_lamp = StatusLamp("작업 상태")
        self.run_lamp.set_state("idle", "대기 중")
        sr.addWidget(self.run_lamp)
        sr.addStretch(1)
        self.run_detail_label = QLabel("")
        self.run_detail_label.setProperty("role", "muted")
        sr.addWidget(self.run_detail_label)
        layout.addWidget(state_rail)

        summary_group = QGroupBox("실행 요약")
        sg = QVBoxLayout(summary_group)
        self.summary_label = QLabel("")
        self.summary_label.setProperty("role", "summary")
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
        self.total_label.setProperty("role", "metricValue")
        row1.addWidget(self.total_label)
        pg.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("현재:"))
        self.current_progress = QProgressBar()
        row2.addWidget(self.current_progress)
        self.current_label = QLabel("시작 대기 중")
        self.current_label.setProperty("role", "metricValue")
        row2.addWidget(self.current_label)
        pg.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(28)
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
            row3.addWidget(metric)
        row3.addStretch(1)
        pg.addLayout(row3)
        layout.addWidget(progress_group)

        log_group = QGroupBox("실행 로그")
        lg = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setObjectName("runLog")
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("작업을 시작하면 실행 로그가 여기에 표시됩니다.")
        self.log_text.document().setMaximumBlockCount(LOG_MAX_BLOCKS)
        lg.addWidget(self.log_text)
        lg.addWidget(build_log_retention_hint(LOG_MAX_BLOCKS))
        layout.addWidget(log_group, 1)

        controls = QHBoxLayout()
        self.start_btn = QPushButton("시작")
        self.start_btn.setObjectName("startButton")
        self.start_btn.setToolTip("선택한 범위와 파티션으로 마이그레이션을 시작합니다.")
        self.start_btn.clicked.connect(self.start_migration)
        # 일시정지 버튼은 없다. 아카이브 워커는 `_check_pause()`를 부르지 않아
        # `pause()`를 호출해도 계속 돈다. 눌러도 안 멈추는 버튼을 두느니
        # 없는 게 낫다 — 취소는 실제로 동작한다.
        self.cancel_btn = QPushButton("작업 취소")
        self.cancel_btn.setObjectName("dangerAction")
        self.cancel_btn.setToolTip("진행 중인 작업을 멈춥니다. 완료된 파티션은 그대로 남습니다.")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_migration)
        for btn in (self.start_btn, self.cancel_btn):
            btn.setAutoDefault(False)
        controls.addWidget(self.start_btn)
        controls.addStretch(1)
        controls.addWidget(self.cancel_btn)
        layout.addLayout(controls)
        return page

    def _set_run_state(self, state: str, detail: str = ""):
        """실행 페이지 상태를 한 곳에서 정한다(버튼 활성화 산발 방지)."""
        lamp, text = self.RUN_STATES.get(state, self.RUN_STATES["idle"])
        self.run_state = state
        self.run_lamp.set_state(lamp, text)
        self.run_detail_label.setText(detail)

        running = state == "running"
        start_labels = {
            "stopped": "이어서 시작",
            "partial": "실패분 다시 실행",
            "failed": "다시 시도",
        }
        self.start_btn.setText(start_labels.get(state, "시작"))
        self.start_btn.setEnabled(state in ("idle", "stopped", "partial", "failed"))
        self.cancel_btn.setEnabled(running)
        self._update_nav_state()

    def _create_connection_status_widget(self) -> QWidget:
        rail = QFrame()
        rail.setObjectName("statusRail")
        layout = QHBoxLayout(rail)
        layout.setContentsMargins(12, 10, 12, 10)

        self.source_lamp = StatusLamp(self._source_label())
        self.target_lamp = StatusLamp(self._target_label())
        self.source_lamp.set_state("busy", "확인 중...")
        self.target_lamp.set_state("busy", "확인 중...")

        layout.addWidget(self.source_lamp)
        layout.addSpacing(30)
        layout.addWidget(self.target_lamp)
        layout.addStretch(1)
        return rail

    def _create_scope_group(self) -> QGroupBox:
        group = QGroupBox("2/3 범위 선택")
        layout = QVBoxLayout(group)

        settings_group = QGroupBox("마이그레이션 설정")
        settings_group.setToolTip("마이그레이션할 항목과 오류 처리 방식을 선택합니다.")
        sg = QVBoxLayout(settings_group)
        row = QHBoxLayout()
        row.addWidget(QLabel("마이그레이션 항목:"))
        self.table_type_checkboxes = {}
        for table_type in get_all_table_types():
            config = TABLE_TYPE_CONFIG[table_type]
            checkbox = QCheckBox(f"{config.display_name} ({table_type.value})")
            checkbox.setToolTip(f"{config.display_name} 파티션을 마이그레이션 대상에 포함합니다.")
            checkbox.setChecked(table_type == TableType.POINT_HISTORY)
            checkbox.stateChanged.connect(self._on_table_type_changed)
            self.table_type_checkboxes[table_type] = checkbox
            row.addWidget(checkbox)
        row.addStretch(1)
        sg.addLayout(row)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("에러 처리:"))
        self.stop_on_error_radio = QRadioButton("중단")
        self.stop_on_error_radio.setToolTip("오류가 발생하면 작업을 중단합니다.")
        self.stop_on_error_radio.setChecked(True)
        self.skip_on_error_radio = QRadioButton("건너뛰기(파티션 단위)")
        self.skip_on_error_radio.setToolTip(
            "오류가 난 파티션은 건너뛰고 다음 파티션을 계속 처리합니다."
        )
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
        date_group.setToolTip("탐색할 파티션의 날짜 범위를 지정합니다.")
        dg = QVBoxLayout(date_group)
        row_dates = QHBoxLayout()
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setToolTip("파티션 탐색 시작 날짜를 선택합니다.")
        self.end_date_edit = QDateEdit()
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setToolTip("파티션 탐색 종료 날짜를 선택합니다.")
        today = datetime.now().date()
        self.start_date_edit.setDate(to_qdate(today))
        self.end_date_edit.setDate(to_qdate(today))
        # 초기값을 넣은 뒤에 연결한다(설정 자체로 세대가 올라가지 않도록).
        self.start_date_edit.dateChanged.connect(self._on_scan_condition_changed)
        self.end_date_edit.dateChanged.connect(self._on_scan_condition_changed)
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
            btn.setProperty("variant", "chip")
            btn.setAutoDefault(False)
            btn.setToolTip(f"날짜 범위를 '{label}' 기준으로 빠르게 설정합니다.")
            btn.clicked.connect(
                lambda _checked=False, d=days, y=yesterday: self._set_preset_days(d, y)
            )
            presets.addWidget(btn)
        presets.addStretch(1)
        dg.addLayout(presets)
        layout.addWidget(date_group)

        action_group = QGroupBox("파티션 탐색 및 선택")
        action_group.setToolTip("선택한 조건으로 파티션을 찾고 처리 대상을 고릅니다.")
        ag = QVBoxLayout(action_group)
        row_actions = QHBoxLayout()
        self.discover_btn = QPushButton("파티션 찾기")
        self.discover_btn.setObjectName("primaryAction")
        self.discover_btn.setToolTip("선택한 항목과 날짜 범위에 해당하는 파티션을 찾습니다.")
        self.discover_btn.setAutoDefault(False)
        self.discover_btn.clicked.connect(self.discover_partitions)
        self.check_completed_btn = QPushButton("완료여부 확인")
        self.check_completed_btn.setToolTip("대상에 이미 완료된 파티션이 있는지 확인합니다.")
        self.check_completed_btn.setAutoDefault(False)
        self.check_completed_btn.clicked.connect(self.check_target_completed)
        row_actions.addWidget(self.discover_btn)
        row_actions.addWidget(self.check_completed_btn)
        self.discover_status = QLabel("날짜/항목을 선택하고 ‘파티션 찾기’를 눌러주세요.")
        self.discover_status.setProperty("role", "hint")
        row_actions.addWidget(self.discover_status)
        row_actions.addStretch(1)
        ag.addLayout(row_actions)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        ag.addWidget(separator)

        row_select = QHBoxLayout()
        self.select_all_btn = QPushButton("전체 선택")
        self.select_all_btn.setToolTip("보이는 파티션을 모두 선택합니다.")
        self.select_all_btn.clicked.connect(lambda: self._bulk_check(True))
        self.select_none_btn = QPushButton("전체 해제")
        self.select_none_btn.setToolTip("보이는 파티션 선택을 모두 해제합니다.")
        self.select_none_btn.clicked.connect(lambda: self._bulk_check(False))
        self.select_pending_btn = QPushButton("완료 제외 선택")
        self.select_pending_btn.setToolTip("완료로 확인된 파티션을 제외하고 선택합니다.")
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
        ag.addLayout(row_select)
        layout.addWidget(action_group)
        return group

    def _create_partition_group(self) -> QGroupBox:
        group = QGroupBox("발견된 파티션")
        group.setToolTip("마이그레이션할 파티션을 체크박스로 선택합니다.")
        layout = QVBoxLayout(group)
        self.partition_list = QListWidget()
        self.partition_list.setObjectName("partitionList")
        self.partition_list.setUniformItemSizes(True)
        self.partition_list.setToolTip("처리할 파티션을 선택하거나 해제합니다.")
        self.partition_list.itemChanged.connect(
            lambda _item: (self._update_counts(), self._update_nav_state())
        )
        layout.addWidget(self.partition_list)
        info = QHBoxLayout()
        self.partition_count_label = QLabel("총 0개")
        self.partition_count_label.setProperty("role", "fieldTitle")
        self.partition_selected_label = QLabel("선택 0개")
        self.partition_selected_label.setProperty("role", "fieldTitle")
        self.partition_rows_label = QLabel("선택 0 rows")
        self.partition_rows_label.setProperty("role", "muted")
        info.addWidget(self.partition_count_label)
        info.addSpacing(20)
        info.addWidget(self.partition_selected_label)
        info.addSpacing(20)
        info.addWidget(self.partition_rows_label)
        info.addStretch(1)

        legend = QLabel("●  실행 대상        ✓  이미 완료된 것으로 보임(기본 해제)")
        legend.setProperty("role", "hint")
        info.addWidget(legend)

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
        if selected:
            self.selected_table_types = selected
        self._guard_last_table_type()
        # 항목이 바뀌면 지금 목록은 더 이상 이 조건의 결과가 아니다.
        self._bump_generation()

    def _on_scan_condition_changed(self, *_args):
        """날짜 등 탐색 조건이 바뀌면 확인 상태를 무효화한다.

        이름 필터(`partition_filter`)는 여기 연결하지 않는다. 보기만 거르고
        선택 상태는 건드리지 않으므로, 필터를 칠 때마다 재탐색을 강요하게 된다.
        """
        self._bump_generation()

    def _guard_last_table_type(self):
        """마지막 하나 남은 항목은 아예 끌 수 없게 한다(경고창 대신 예방)."""
        only_one = sum(1 for cb in self.table_type_checkboxes.values() if cb.isChecked()) == 1
        for table_type, cb in self.table_type_checkboxes.items():
            lock = only_one and cb.isChecked()
            cb.setEnabled(not lock)
            cb.setToolTip(
                "최소 1개 항목은 선택되어 있어야 합니다."
                if lock
                else f"{TABLE_TYPE_CONFIG[table_type].display_name} 파티션을 대상에 포함합니다."
            )

    def _on_error_strategy_changed(self, _checked: bool):
        self.error_strategy = "stop" if self.stop_on_error_radio.isChecked() else "skip"

    def _check_incomplete_migration(self):
        if self.profile.id is None:
            self.incomplete_group.setVisible(False)
            return
        incomplete = self.history_manager.get_incomplete_history(self.profile.id)
        self._incomplete_history = incomplete
        if not incomplete:
            self.incomplete_group.setVisible(False)
            return
        self.incomplete_group.setVisible(True)
        self.incomplete_label.setText(
            "이전에 중단된 작업이 있습니다.\n"
            f"날짜: {incomplete.start_date} ~ {incomplete.end_date}\n"
            f"진행: {self._describe_progress(incomplete)}\n\n"
            "이어서 진행하면 미완료 파티션만 다시 실행합니다."
        )

    def _describe_progress(self, history: MigrationHistoryItem) -> str:
        """중단된 작업이 어디까지 갔는지 한 줄로 말한다.

        이력에 저장된 `processed_rows`가 아니라 체크포인트 합계를 쓴다.
        워커의 카운터는 실행 1회분만 세므로, 한 번 재개한 뒤에는 이력의
        값이 실제보다 작다.
        """
        done = max(
            self.checkpoint_manager.get_processed_rows(history.id or 0),
            int(history.processed_rows or 0),
        )
        planned = int(history.total_rows or 0)
        if planned <= 0:
            return f"{done:,} rows 처리됨"
        percent = min(100, int(done / planned * 100))
        return f"{done:,} / 약 {planned:,} rows ({percent}%)"

    def _on_resume_clicked(self):
        if not self._incomplete_history or self._incomplete_history.id is None:
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
        self._set_run_state("idle", f"재개 대기 · 미완료 파티션 {len(self._frozen_selection)}개")
        self.add_log(f"재개 모드: 미완료 파티션 {len(self._frozen_selection)}개", "INFO")

    def _on_new_run_clicked(self):
        # 아무것도 알려주지 않는 확인 창 대신, 그 자리에서 상태를 바꾼다.
        self.resume_mode = False
        self.history_id = None
        self._frozen_selection = []
        self._set_run_state("idle")
        self.incomplete_group.setVisible(False)
        self.connection_hint.setText(
            "새 작업으로 진행합니다. 다음 단계에서 날짜와 파티션을 선택하세요."
        )

    def check_connections(self):
        """소스/대상 엔드포인트를 확인한다(워커 스레드).

        이 이름과 호출 위치는 유지해야 한다. 테스트 픽스처들이 이 메서드를
        패치해 생성자에서 실제 DB에 붙는 것을 막는다.
        """
        # 생성자에서 QTimer.singleShot으로 예약되므로, 창이 뜨자마자 Esc를 누르면
        # 닫힌 뒤에 이 호출이 도착할 수 있다.
        if self._closing:
            return
        if self._is_scan_inflight("conn"):
            return

        gen = self._scan_gen
        self.source_lamp.set_state("busy", "확인 중...")
        self.target_lamp.set_state("busy", "확인 중...")
        self.connection_hint.setText("")
        self.recheck_btn.setEnabled(False)

        worker = ConnectionCheckWorker(
            gen,
            EndpointCheckSpec(
                kind=self.profile.source_kind,
                config=dict(self.profile.source_config or {}),
                must_exist=True,
            ),
            EndpointCheckSpec(
                kind=self.profile.target_kind,
                config=dict(self.profile.target_config or {}),
                must_exist=False,
            ),
        )
        # 람다로 연결하지 않는다. 람다는 수신자 QObject가 없어 다이얼로그가
        # 먼저 사라지면 죽은 위젯을 건드린다.
        worker.result.connect(self._on_connection_check_result)
        worker.failed.connect(self._on_connection_check_failed)
        worker.finished.connect(self._on_connection_check_finished)

        self._scan_workers["conn"] = worker
        self._mark_scan_started("conn")
        worker.start()
        self._update_nav_state()

    def _on_connection_check_result(self, gen: int, payload: object):
        if not self._is_current_generation(gen):
            # 확인하는 동안 조건이 바뀌었다. 이 결과는 지금 상태의 것이 아니다.
            # (연결 상태 문구는 작업 이력에도 기록되므로 덮어쓰면 안 된다)
            return
        results = cast(dict, payload)
        source = results["source"]
        target = results["target"]
        self._update_connection_ui(source.ok, target.ok, source.message, target.message)

    def _on_connection_check_failed(self, gen: int, message: str):
        if not self._is_current_generation(gen):
            return
        # 램프가 '확인 중...'에 멈춰 있으면 안 된다.
        self._update_connection_ui(False, False, f"확인 실패: {message}", f"확인 실패: {message}")

    def _on_connection_check_finished(self):
        """성공·실패 공통 정리.

        결과 슬롯이 아니라 여기서 되돌린다. 성공 경로에만 복구를 두면
        실패했을 때 버튼이 영구 비활성으로 남는다.
        """
        self._mark_scan_finished("conn")
        self.recheck_btn.setEnabled(True)

        # 이 finished가 어느 세대의 것인지 확인한다. 낡은 워커가 끝나면서
        # 연결 상태 문구를 덮으면, 그 문구가 그대로 작업 이력에 기록된다.
        worker = self.sender()
        gen = getattr(worker, "generation", self._scan_gen)
        if not self._is_current_generation(gen):
            self._update_nav_state()
            return

        # 취소된 워커는 결과도 실패도 보내지 않는다. 그대로 두면 램프가
        # '확인 중...'에 영원히 멈춘다.
        # (result/failed는 run() 안에서 먼저 emit되고 finished는 그 뒤에
        #  전달되므로, 여기서 여전히 busy면 아무것도 도착하지 않은 것이다)
        if "busy" in (self.source_lamp.state, self.target_lamp.state):
            self._update_connection_ui(
                False, False, "확인이 중단되었습니다", "확인이 중단되었습니다"
            )
            return

        self._update_nav_state()

    def _update_connection_ui(
        self, source_ok: bool, target_ok: bool, source_msg: str, target_msg: str
    ):
        self.source_connected = source_ok
        self.target_connected = target_ok
        self.source_status_message = source_msg
        self.target_status_message = target_msg

        self.source_lamp.set_state("ok" if source_ok else "error", source_msg)
        self.target_lamp.set_state("ok" if target_ok else "error", target_msg)

        failed = []
        if not source_ok:
            failed.append(self._source_label())
        if not target_ok:
            failed.append(self._target_label())
        self.connection_hint.setText(
            ""
            if not failed
            else f"{' · '.join(failed)} 확인에 실패했습니다. "
            "연결 정보나 아카이브 경로를 메인 창의 '편집'에서 고친 뒤 다시 확인하세요."
        )
        self._update_nav_state()

    def discover_partitions(self):
        if not (self.source_connected and self.target_connected):
            QMessageBox.warning(self, "연결 필요", "먼저 양쪽 엔드포인트를 확인하세요.")
            return

        # QDate.toPython()의 스텁 반환형은 object라 비교/전달 전에 좁혀 준다.
        start_date = cast(date, self.start_date_edit.date().toPython())
        end_date = cast(date, self.end_date_edit.date().toPython())
        if start_date > end_date:
            QMessageBox.warning(self, "날짜 오류", "시작 날짜가 종료 날짜보다 늦습니다.")
            return

        if self._closing or self._is_scan_inflight("discover"):
            return

        # 새 탐색이 시작되면 지금 목록은 더 이상 '확인된' 목록이 아니다.
        gen = self._bump_generation()

        # 결과를 기다리는 동안 이전 목록이 체크된 채 남아 있으면 안 된다.
        self.discovered_partitions = []
        self._target_has_data = {}
        self._render_partition_list()
        self._update_counts()

        self.discover_status.setText("파티션 탐색 중...")

        worker = PartitionScanWorker(
            gen,
            self.profile.source_kind,
            dict(self.profile.source_config or {}),
            start_date,
            end_date,
            list(self.selected_table_types),
        )
        worker.result.connect(self._on_discovery_result)
        worker.failed.connect(self._on_discovery_failed)
        worker.progress.connect(self._on_discovery_progress)
        worker.finished.connect(self._on_discovery_finished)

        self._scan_workers["discover"] = worker
        self._mark_scan_started("discover")
        self._sync_scan_buttons()
        worker.start()
        self._update_nav_state()

    def _on_discovery_progress(self, gen: int, done: int, total: int):
        if not self._is_current_generation(gen):
            return
        self.discover_status.setText(f"파티션 탐색 중... ({done}/{total})")

    def _on_discovery_result(self, gen: int, payload: object):
        if not self._is_current_generation(gen):
            return

        self.discovered_partitions = [
            PartitionSummary(
                table_name=item["table_name"],
                row_count=int(item.get("row_count") or 0),
                table_type=item["table_type"],
                row_count_estimated=bool(item.get("row_count_estimated")),
            )
            for item in cast(list, payload)
        ]
        self._target_has_data = {}
        self._render_partition_list()
        self._update_counts()
        self._update_nav_state()

        if not self.discovered_partitions:
            self.discover_status.setText("조건에 해당하는 파티션이 없습니다")
            return

        self.add_log(f"파티션 {len(self.discovered_partitions)}개 발견", "INFO")
        # 대상 확인으로 이어진다. 상태 문구는 그쪽이 이어서 쓴다.
        # 같은 generation을 물려주므로, 이 결과가 낡았으면 확인도 시작되지 않는다.
        self.check_target_completed()

    def _on_discovery_failed(self, gen: int, message: str):
        if not self._is_current_generation(gen):
            return

        # 실패했는데 이전 목록이 남아 있으면 사용자가 그걸 실행 대상으로 착각한다.
        self.discovered_partitions = []
        self._target_has_data = {}
        self._render_partition_list()
        self._update_counts()
        self.discover_status.setText("파티션 탐색 실패 — 다시 시도하세요")
        self.add_log(f"파티션 탐색 실패: {message}", "ERROR")
        self._update_nav_state()

    def _on_discovery_finished(self):
        """성공·실패·취소 공통 정리."""
        self._mark_scan_finished("discover")
        self._sync_scan_buttons()

        worker = self.sender()
        gen = getattr(worker, "generation", self._scan_gen)
        if self._is_current_generation(gen) and "탐색 중" in self.discover_status.text():
            # 결과도 실패도 오지 않았다(취소).
            self.discover_status.setText("파티션 탐색이 중단되었습니다")

        self._update_nav_state()

    def _render_partition_list(self):
        self.partition_list.clear()
        # addItem마다 itemChanged가 튀면 항목 수의 제곱만큼 갱신이 돈다.
        self.partition_list.blockSignals(True)
        try:
            self._fill_partition_items(QColor(TEXT_MUTED))
        finally:
            self.partition_list.blockSignals(False)

        self._apply_partition_filter(self.partition_filter.text())

    def _fill_partition_items(self, muted: QColor):
        for summary in self.discovered_partitions[:PARTITION_DISPLAY_LIMIT]:
            cfg = TABLE_TYPE_CONFIG[summary.table_type]
            completed_like = bool(self._target_has_data.get(summary.table_name))
            # 상태는 색이 아니라 맨 앞의 모양으로 먼저 읽힌다.
            marker = "✓" if completed_like else "●"
            rows_text = format_row_count(summary.row_count, summary.row_count_estimated)
            item = QListWidgetItem(
                f"{marker}  {summary.table_name}  ·  {rows_text}  ·  {cfg.display_name}"
            )
            item.setData(Qt.ItemDataRole.UserRole, summary.table_name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if completed_like else Qt.CheckState.Checked)
            if completed_like:
                item.setForeground(muted)
                item.setToolTip("대상에 이미 데이터가 있습니다. 다시 실행하려면 체크하세요.")
            else:
                item.setToolTip(f"{summary.table_name} · {rows_text}")
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
        for idx in range(self.partition_list.count()):
            item = self.partition_list.item(idx)
            name = item.data(Qt.ItemDataRole.UserRole)
            if not name:
                continue
            item.setHidden(bool(needle) and needle not in str(name).lower())
        self._update_counts()

    def _visible_partition_items(self):
        for idx in range(self.partition_list.count()):
            item = self.partition_list.item(idx)
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
                Qt.CheckState.Unchecked
                if self._target_has_data.get(name)
                else Qt.CheckState.Checked
            )
        self._update_counts()
        self._update_nav_state()

    def get_selected_partition_names(self) -> list[str]:
        selected = []
        for idx in range(self.partition_list.count()):
            item = self.partition_list.item(idx)
            name = item.data(Qt.ItemDataRole.UserRole)
            if name and item.checkState() == Qt.CheckState.Checked:
                selected.append(str(name))
        return selected

    def _update_counts(self):
        total = len(self.discovered_partitions)
        visible = 0
        selected_names: set[str] = set()
        hidden_selected = 0

        for idx in range(self.partition_list.count()):
            item = self.partition_list.item(idx)
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
        names = [part.table_name for part in self.discovered_partitions]
        if not names:
            return

        if self._closing or self._is_scan_inflight("target"):
            return

        # 확인을 시작하는 순간 이전 확인 결과는 더 이상 유효하지 않다.
        # 여기서 내리지 않으면 '성공 → 재확인 실패' 순서에서 게이트가 열린 채
        # 남아, 확인되지 않은 목록(=전 파티션)이 실행 대상으로 굳는다.
        self._selection_verified = False

        gen = self._scan_gen
        self.discover_status.setText(f"대상 확인 중... (파티션 {len(names)}개)")

        worker = TargetCompletedScanWorker(
            gen,
            self.profile.target_kind,
            dict(self.profile.target_config or {}),
            names,
        )
        worker.result.connect(self._on_target_check_result)
        worker.failed.connect(self._on_target_check_failed)
        worker.progress.connect(self._on_target_check_progress)
        worker.finished.connect(self._on_target_check_finished)

        self._scan_workers["target"] = worker
        self._mark_scan_started("target")
        self._sync_scan_buttons()
        worker.start()
        self._update_nav_state()

    def _on_target_check_progress(self, gen: int, done: int, total: int):
        if not self._is_current_generation(gen):
            return
        self.discover_status.setText(f"대상 확인 중... ({done}/{total})")

    def _on_target_check_result(self, gen: int, payload: object):
        if not self._is_current_generation(gen):
            # 확인하는 동안 조건이 바뀌었다. 이 결과는 지금 목록의 것이 아니다.
            return

        self._target_has_data = cast(dict, payload)
        self._render_partition_list()
        self._update_counts()
        self._selection_verified = True
        self._update_nav_state()
        self.discover_status.setText("대상 확인 완료")
        self.add_log("완료 여부 확인 완료", "INFO")

    def _on_target_check_failed(self, gen: int, message: str):
        if not self._is_current_generation(gen):
            return

        # 확인에 실패하면 _target_has_data를 믿을 수 없다. 이 상태로 진행하면
        # 전 파티션이 미완료로 보여 이미 옮긴 것까지 다시 적재된다.
        # _selection_verified를 올리지 않아 '다음'이 잠긴 채로 남는다.
        self._target_has_data = {}
        self._render_partition_list()
        self._update_counts()
        self.discover_status.setText("대상 확인 실패 — 다시 시도하세요")
        self.add_log(f"완료 여부 확인 실패: {message}", "ERROR")
        self._update_nav_state()

    def _on_target_check_finished(self):
        """성공·실패·취소 공통 정리."""
        self._mark_scan_finished("target")
        self._sync_scan_buttons()

        worker = self.sender()
        gen = getattr(worker, "generation", self._scan_gen)
        if self._is_current_generation(gen) and not self._selection_verified:
            # 결과도 실패도 오지 않았다(취소). 상태 문구가 '확인 중...'에 멈추지 않게 한다.
            if "확인 중" in self.discover_status.text():
                self.discover_status.setText("대상 확인이 중단되었습니다")

        self._update_nav_state()

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
            # 재개 모드의 실행 대상은 미완료 체크포인트지 목록의 체크 상태가 아니다.
            # 여기서 목록 선택을 요구하면 재개 → 이전 → 다음에서 실행 페이지로 못 돌아간다.
            if self.resume_mode and self._frozen_selection:
                self.pages.setCurrentIndex(2)
                self._refresh_summary()
                self._update_step_text()
                self._update_nav_state()
                return

            selected = self.get_selected_partition_names()
            if not selected:
                QMessageBox.warning(self, "선택 오류", "실행할 파티션을 1개 이상 선택하세요.")
                return

            # 버튼 상태만 믿지 않는다. 큐에 남아 있던 클릭이 뒤늦게 배달될 수 있어
            # 실제로 대상을 굳히는 이 지점에서 한 번 더 막는다.
            if not self._can_freeze_selection():
                QMessageBox.warning(
                    self,
                    "대상 확인 필요",
                    "대상에 이미 있는 파티션을 확인하기 전에는 진행할 수 없습니다.\n\n"
                    "'완료여부 확인'을 먼저 실행하세요. 확인하지 않고 진행하면 "
                    "이미 완료된 파티션까지 다시 적재됩니다.",
                )
                return

            self._frozen_selection = selected
            self.pages.setCurrentIndex(2)
            self._refresh_summary()
        self._update_step_text()
        self._update_nav_state()

    def _update_step_text(self):
        idx = self.pages.currentIndex()
        titles = ["연결 확인", "범위/파티션 선택", "실행"]
        hints = [
            f"{self._source_label()}와 {self._target_label()}를 확인합니다.",
            "날짜와 항목을 고른 뒤 실행할 파티션만 체크합니다.",
            "파일 아카이브 마이그레이션을 실행하고 진행 상황을 확인합니다.",
        ]
        self.step_rail.set_current(idx)
        self.step_title.setText(titles[idx])
        self.step_hint.setText(hints[idx])

    # ============================
    # 조회(scan) 작업 골격
    # ============================

    def _on_scan_generation_changed(self) -> None:
        """조건이 바뀌면 지금 목록은 더 이상 그 조건의 결과가 아니다.

        '다음'을 잠그는 것만으로는 부족하다. 화면에 남은 목록을 사용자는
        새 조건의 결과로 읽는다. 늦게 도착할 결과를 버리는 것과, 이미
        그려진 것을 지우는 것은 별개다.
        """
        self._selection_verified = False

        if self.discovered_partitions:
            self.discovered_partitions = []
            self._target_has_data = {}
            self._render_partition_list()
            self._update_counts()
            self.discover_status.setText("조건이 바뀌었습니다. 파티션을 다시 찾으세요.")
            self._sync_scan_buttons()

        self._update_nav_state()

    def _set_scan_status(self, text: str) -> None:
        self.discover_status.setText(text)

    def _on_scans_abandoned(self) -> None:
        self.add_log("조회 작업이 멈추지 않아 분리하고 창을 닫습니다", "WARNING")

    def _sync_scan_buttons(self) -> None:
        """조회 버튼 활성화를 한 곳에서 정한다.

        각 핸들러가 따로 켜고 끄면 순서에 따라 어긋난다. 실제로
        탐색 완료 핸들러가 (그 뒤에 시작된) 대상 확인 도중에 버튼을
        되살리는 문제가 있었다.
        """
        busy = self._is_scan_inflight("discover") or self._is_scan_inflight("target")
        self.discover_btn.setEnabled(not busy)
        # '완료여부 확인'은 찾은 파티션이 있을 때만 의미가 있다.
        self.check_completed_btn.setEnabled(not busy and bool(self.discovered_partitions))

    def _is_running(self) -> bool:
        if self.run_state == "running":
            return True
        return bool(self.worker and getattr(self.worker, "is_running", False))

    def _update_nav_state(self):
        idx = self.pages.currentIndex()
        running = self._is_running()

        # 한 번 실행을 시작한 마법사는 그 실행에 묶인다.
        run_page_locked = idx == 2 and self.run_state != "idle"
        self.back_btn.setEnabled(idx > 0 and not running and not run_page_locked)
        self.back_btn.setToolTip(
            "이미 실행한 작업입니다. 다른 범위로 실행하려면 창을 닫고 다시 여세요."
            if run_page_locked
            else "이전 단계로 돌아갑니다."
        )
        if idx == 0:
            self.next_btn.setVisible(True)
            self.next_btn.setEnabled(self.source_connected and self.target_connected)
        elif idx == 1:
            self.next_btn.setVisible(True)
            # 재개 모드는 목록이 아니라 미완료 체크포인트가 실행 대상이다.
            has_target = bool(self.resume_mode and self._frozen_selection) or bool(
                self.get_selected_partition_names()
            )
            # 대상 확인이 끝나지 않은 목록으로는 넘어갈 수 없다. 확인 전에는
            # 모든 파티션이 체크된 것처럼 보이고, 그대로 실행하면 이미 완료된
            # 파티션까지 TRUNCATE 후 재적재된다.
            self.next_btn.setEnabled(has_target and self._can_freeze_selection())
            self.next_btn.setToolTip(
                "대상 확인이 끝나야 다음 단계로 넘어갈 수 있습니다. '완료여부 확인'을 눌러주세요."
                if has_target and not self._can_freeze_selection()
                else "현재 설정을 확인하고 다음 단계로 이동합니다."
            )
        else:
            self.next_btn.setVisible(False)
        self.close_btn.setEnabled(not running)

    def _can_freeze_selection(self) -> bool:
        """지금 목록을 실행 대상으로 굳혀도 되는가.

        재개 모드의 실행 대상은 미완료 체크포인트라 목록과 무관하므로 통과시킨다.
        """
        return self.resume_mode or self._selection_verified

    def _row_total(self, names) -> tuple[int, bool]:
        """선택분의 행 수 합계와, 그 합계가 추정치를 포함하는지 돌려준다.

        하나라도 추정치가 섞이면 합계도 추정치다. 정확한 값처럼 보이면
        사용자가 그 숫자로 용량이나 시간을 계산한다.
        """
        wanted = set(names)
        picked = [part for part in self.discovered_partitions if part.table_name in wanted]
        total = sum(part.row_count for part in picked)
        return total, any(part.row_count_estimated for part in picked)

    def _refresh_summary(self):
        parts = self._frozen_selection or self.get_selected_partition_names()
        mode_text = {
            "postgres_to_file": "PostgreSQL → File Archive",
            "file_to_postgres": "File Archive → PostgreSQL",
        }.get(self.profile.migration_mode, self.profile.migration_mode)
        lines = [
            f"프로필      {self.profile.name}",
            f"방향        {mode_text}",
            f"파티션      {len(parts):,}개 · {format_row_count(*self._row_total(parts))}",
            f"에러 처리   {'중단' if self.error_strategy == 'stop' else '건너뛰기'}",
        ]
        if self.resume_mode:
            lines.append("재개        미완료 파티션만 다시 실행")
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
            if self.profile.id is None:
                QMessageBox.warning(
                    self, "프로필 오류", "저장되지 않은 프로필로는 실행할 수 없습니다."
                )
                return
            start_date = cast(date, self.start_date_edit.date().toPython())
            end_date = cast(date, self.end_date_edit.date().toPython())
            planned_rows, _ = self._row_total(partitions)
            history = self.history_manager.create_history(
                self.profile.id,
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
                source_status=self.source_status_message,
                target_status=self.target_status_message,
                total_rows=planned_rows,
            )
            if history.id is None:
                QMessageBox.critical(self, "이력 생성 실패", "작업 이력을 만들지 못했습니다.")
                return
            self.history_id = history.id
            for partition in partitions:
                self.checkpoint_manager.create_checkpoint(self.history_id, partition)

        if self.history_id is None:
            QMessageBox.critical(self, "이력 없음", "실행할 작업 이력이 없습니다.")
            return

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

        self._set_run_state("running", f"파티션 {len(partitions):,}개")
        self.add_log(f"마이그레이션 시작 - 파티션 {len(partitions)}개", "INFO")
        self.worker.start()

    def cancel_migration(self):
        """진행 중인 작업만 멈춘다. 창을 닫는 일은 '닫기'가 한다."""
        if not self.worker or not self._is_running():
            return

        reply = QMessageBox.question(
            self,
            "작업 취소",
            "작업을 취소하시겠습니까? 완료된 파티션은 유지됩니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.worker.stop(reason="user_cancel")
            self.add_log("사용자가 작업을 취소했습니다", "WARNING")

    def on_progress(self, data: dict):
        if "total_progress" in data:
            self.total_progress.setValue(int(data["total_progress"]))
            done = data.get("completed_partitions", 0)
            total = data.get("total_partitions", 0)
            self.total_label.setText(f"{done} / {total}")
            if self.run_state == "running":
                self.run_detail_label.setText(f"파티션 {done} / {total} 완료")
        if "current_progress" in data:
            self.current_progress.setValue(int(data["current_progress"]))
            self.current_label.setText(
                f"{data.get('current_partition', '')} ({int(data.get('current_rows', 0) or 0):,} rows)"
            )
        if "speed" in data:
            self.speed_metric.set_value(f"{int(data['speed']):,} rows/s")

    def on_performance_update(self, stats: dict):
        self.speed_metric.set_value(
            f"{float(stats.get('instant_rows_per_sec', 0) or 0):,.0f} rows/s"
        )
        self.data_rate_metric.set_value(
            f"{float(stats.get('instant_mb_per_sec', 0.0) or 0.0):.1f} MB/s"
        )
        self.eta_metric.set_value(str(stats.get("eta_time", "계산 중")))
        self.elapsed_metric.set_value(str(stats.get("elapsed_time", "00:00:00")))

    def _get_processed_rows(self) -> int:
        """이 이력에서 지금까지 옮긴 누적 행 수.

        체크포인트 합계를 쓴다. 워커의 카운터는 실행 1회분만 세므로,
        재개한 뒤 그 값을 이력에 쓰면 진행량이 뒤로 간다.

        둘 중 큰 값을 취한다. 어느 쪽도 실제보다 클 수 없는 하한이다 —
        체크포인트 기록이 조용히 실패하면 합계가 작고, 재개 실행이라면
        워커 카운터가 작다. 진행량을 잃는 쪽으로 틀리지 않게 한다.
        """
        counts = []

        if self.history_id:
            try:
                counts.append(self.checkpoint_manager.get_processed_rows(self.history_id))
            except Exception as e:
                self.add_log(f"진행량 집계 실패, 이번 실행분만 기록합니다: {e}", "WARNING")

        if self.worker and hasattr(self.worker, "get_stats"):
            try:
                counts.append(int(self.worker.get_stats().get("total_rows") or 0))
            except Exception:
                pass

        return max(counts, default=0)

    def on_worker_finished(self):
        was_normal_completion = bool(self.worker and getattr(self.worker, "is_running", False))
        if self.worker:
            self.worker.is_running = False

        rows_processed = self._get_processed_rows()

        if self._worker_had_error:
            self._set_run_state("failed", "실행 로그에서 원인을 확인하세요")
            return

        if not was_normal_completion:
            if self.history_id:
                self.history_manager.update_history_status(
                    self.history_id,
                    "running",
                    processed_rows=rows_processed,
                )
            self.add_log(
                "작업이 중단되었습니다. '이어서 시작'을 누르면 남은 파티션부터 재개합니다.",
                "WARNING",
            )
            self._set_run_state("stopped", f"{rows_processed:,} rows 처리 후 중단")
            return

        self.total_progress.setValue(100)
        self.current_progress.setValue(100)
        total = len(self.worker.partitions) if self.worker else 0
        self.total_label.setText(f"{total} / {total}")

        failures = self.worker.partition_failures if self.worker else []
        if failures:
            failed_count = len(failures)
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
            self._set_run_state("partial", f"실패 {failed_count}건 · {rows_processed:,} rows 처리")
            QMessageBox.warning(
                self,
                "부분 완료",
                f"마이그레이션이 완료되었지만 {failed_count}개 파티션이 실패했습니다.\n"
                "'실패분 다시 실행'을 누르면 실패한 파티션만 다시 처리합니다.",
            )
        else:
            if self.history_id:
                self.history_manager.update_history_status(
                    self.history_id,
                    "completed",
                    processed_rows=rows_processed,
                )
            self.add_log("마이그레이션이 완료되었습니다", "SUCCESS")
            self.current_label.setText("완료")
            self._set_run_state("done", f"파티션 {total:,}개 · {rows_processed:,} rows")
            QMessageBox.information(
                self,
                "완료",
                f"마이그레이션이 완료되었습니다.\n\n파티션 {total:,}개 · {rows_processed:,} rows",
            )

    def on_error(self, error_msg: str):
        self._worker_had_error = True
        if self.worker:
            self.worker.is_running = False
        if self.history_id:
            self.history_manager.update_history_status(self.history_id, "failed")
        self.add_log(f"오류 발생: {error_msg}", "ERROR")
        self._set_run_state("failed", "실행 로그에서 원인을 확인하세요")
        QMessageBox.critical(self, "오류", error_msg)

    def add_log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        # 메시지에 <, & 같은 문자가 있어도 그대로 보이도록 이스케이프한다.
        safe = html.escape(message).replace("\n", "<br>")
        self.log_text.append(
            f'<span style="color:{TEXT_MUTED}">[{timestamp}]</span> '
            f'<span style="color:{log_color(level)}">{level:<7} {safe}</span>'
        )
        self.log_text.moveCursor(QTextCursor.MoveOperation.End)
        log_emitter.emit_log(level, message)

    def _block_close_while_running(self) -> bool:
        if not self._is_running():
            return False
        QMessageBox.warning(
            self,
            "진행 중",
            "작업 진행 중에는 닫을 수 없습니다.\n먼저 '작업 취소'로 작업을 멈추세요.",
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
        if not self._prepare_close():
            event.ignore()
            QTimer.singleShot(self.SCAN_SHUTDOWN_STEP_MS, self.close)
            return
        event.accept()
