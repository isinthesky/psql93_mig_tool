"""
마이그레이션 진행 다이얼로그 (모달)
"""

from datetime import date, datetime
from typing import Optional

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCalendarWidget,
    QCheckBox,
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.copy_migration_worker import CopyMigrationWorker
from src.core.migration_worker import MigrationWorker
from src.core.partition_discovery import PartitionDiscovery
from src.core.table_types import TableType, TABLE_TYPE_CONFIG, get_all_table_types
from src.models.history import CheckpointManager, HistoryManager
from src.models.profile import ConnectionProfile
from src.utils.enhanced_logger import log_emitter


class MigrationDialog(QDialog):
    """마이그레이션 진행 다이얼로그"""

    def __init__(self, parent=None, profile: ConnectionProfile = None):
        super().__init__(parent)
        self.profile = profile
        self.history_manager = HistoryManager()
        self.checkpoint_manager = CheckpointManager()
        self.worker = None
        self.history_id = None
        self.is_running = False
        self.all_partitions = []

        # 옵션 기본값
        self.selected_table_types: list[TableType] = [TableType.POINT_HISTORY]
        self.table_type_checkboxes: dict[TableType, QCheckBox] = {}
        self.migration_method = "copy"  # 'copy' or 'insert'
        self.error_strategy = "stop"  # 'stop' or 'skip'

        self.setup_ui()
        self.check_incomplete_migration()

    def setup_ui(self):
        """UI 초기화"""
        self.setWindowTitle(f"마이그레이션 작업 설정 - {self.profile.name}")
        self.setModal(True)
        self.resize(950, 800)

        layout = QVBoxLayout(self)

        # 연결 상태 표시 영역
        connection_status_widget = self.create_connection_status_widget()
        layout.addWidget(connection_status_widget)

        # 옵션 설정 영역 (새로 추가)
        options_group = self.create_options_group()
        layout.addWidget(options_group)

        # 날짜 선택 영역
        date_group = self.create_date_selection_group()
        layout.addWidget(date_group)

        # 진행 상황 영역
        progress_group = self.create_progress_group()
        layout.addWidget(progress_group)

        # 로그 영역
        log_group = self.create_log_group()
        layout.addWidget(log_group)

        # 버튼 영역
        button_layout = QHBoxLayout()

        self.start_btn = QPushButton("시작")
        self.start_btn.clicked.connect(self.start_migration)
        button_layout.addWidget(self.start_btn)

        self.pause_btn = QPushButton("일시정지")
        self.pause_btn.clicked.connect(self.pause_migration)
        self.pause_btn.setEnabled(False)
        button_layout.addWidget(self.pause_btn)

        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.clicked.connect(self.cancel_migration)
        button_layout.addWidget(self.cancel_btn)

        button_layout.addStretch()

        self.close_btn = QPushButton("닫기")
        self.close_btn.clicked.connect(self.close)
        button_layout.addWidget(self.close_btn)

        layout.addLayout(button_layout)

        # 타이머 설정 (5초마다 업데이트)
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_progress)

        # 다이얼로그가 열릴 때 연결 확인 시작
        QTimer.singleShot(100, self.check_connections)

    def create_options_group(self):
        """옵션 설정 그룹 생성"""
        group = QGroupBox("마이그레이션 옵션")
        main_layout = QVBoxLayout()

        # 첫 번째 행: 테이블 타입 선택
        table_type_layout = QHBoxLayout()
        table_type_layout.addWidget(QLabel("마이그레이션 항목:"))

        for table_type in get_all_table_types():
            config = TABLE_TYPE_CONFIG[table_type]
            checkbox = QCheckBox(f"{config.display_name} ({table_type.value})")
            checkbox.setToolTip(config.description)

            # Point History는 기본 선택
            if table_type == TableType.POINT_HISTORY:
                checkbox.setChecked(True)

            checkbox.stateChanged.connect(self.on_table_type_changed)
            self.table_type_checkboxes[table_type] = checkbox
            table_type_layout.addWidget(checkbox)

        table_type_layout.addStretch()
        main_layout.addLayout(table_type_layout)

        # 두 번째 행: 마이그레이션 방식 및 에러 처리 전략
        options_row_layout = QHBoxLayout()

        # 마이그레이션 방식 선택
        method_group = QGroupBox("마이그레이션 방식")
        method_layout = QHBoxLayout()

        self.copy_radio = QRadioButton("COPY (고성능)")
        self.copy_radio.setToolTip("PostgreSQL COPY 명령 사용 - 대용량 데이터에 적합")
        self.copy_radio.setChecked(True)
        self.copy_radio.toggled.connect(self.on_method_changed)

        self.insert_radio = QRadioButton("INSERT (호환성)")
        self.insert_radio.setToolTip("표준 INSERT 문 사용 - 호환성이 높음")
        self.insert_radio.toggled.connect(self.on_method_changed)

        method_button_group = QButtonGroup(self)
        method_button_group.addButton(self.copy_radio)
        method_button_group.addButton(self.insert_radio)

        method_layout.addWidget(self.copy_radio)
        method_layout.addWidget(self.insert_radio)
        method_group.setLayout(method_layout)
        options_row_layout.addWidget(method_group)

        # 에러 처리 전략 선택
        error_group = QGroupBox("에러 처리 전략")
        error_layout = QHBoxLayout()

        self.stop_on_error_radio = QRadioButton("중단")
        self.stop_on_error_radio.setToolTip("에러 발생 시 마이그레이션 즉시 중단")
        self.stop_on_error_radio.setChecked(True)
        self.stop_on_error_radio.toggled.connect(self.on_error_strategy_changed)

        self.skip_on_error_radio = QRadioButton("건너뛰기")
        self.skip_on_error_radio.setToolTip("에러 발생 시 해당 파티션 건너뛰고 계속 진행")
        self.skip_on_error_radio.toggled.connect(self.on_error_strategy_changed)

        error_button_group = QButtonGroup(self)
        error_button_group.addButton(self.stop_on_error_radio)
        error_button_group.addButton(self.skip_on_error_radio)

        error_layout.addWidget(self.stop_on_error_radio)
        error_layout.addWidget(self.skip_on_error_radio)
        error_group.setLayout(error_layout)
        options_row_layout.addWidget(error_group)

        options_row_layout.addStretch()
        main_layout.addLayout(options_row_layout)

        group.setLayout(main_layout)
        return group

    def create_date_selection_group(self):
        """날짜 선택 그룹 생성"""
        group = QGroupBox("날짜 범위 선택")
        layout = QHBoxLayout()

        # 시작 날짜
        start_layout = QVBoxLayout()
        start_layout.addWidget(QLabel("시작 날짜:"))
        self.start_calendar = QCalendarWidget()
        self.start_calendar.setMaximumHeight(200)
        self.start_calendar.selectionChanged.connect(self.on_date_changed)
        start_layout.addWidget(self.start_calendar)
        layout.addLayout(start_layout)

        # 종료 날짜
        end_layout = QVBoxLayout()
        end_layout.addWidget(QLabel("종료 날짜:"))
        self.end_calendar = QCalendarWidget()
        self.end_calendar.setMaximumHeight(200)
        self.end_calendar.selectionChanged.connect(self.on_date_changed)
        end_layout.addWidget(self.end_calendar)
        layout.addLayout(end_layout)

        # 파티션 목록
        partition_layout = QVBoxLayout()
        partition_layout.addWidget(QLabel("선택된 파티션:"))
        self.partition_list = QListWidget()
        self.partition_list.setMaximumHeight(200)
        partition_layout.addWidget(self.partition_list)
        self.partition_count_label = QLabel("총 0개 파티션")
        partition_layout.addWidget(self.partition_count_label)
        layout.addLayout(partition_layout)

        group.setLayout(layout)
        return group

    def create_progress_group(self):
        """진행 상황 그룹 생성"""
        group = QGroupBox("진행 상황")
        layout = QVBoxLayout()

        # 전체 진행률
        total_layout = QHBoxLayout()
        total_layout.addWidget(QLabel("전체 진행률:"))
        self.total_progress = QProgressBar()
        total_layout.addWidget(self.total_progress)
        self.total_label = QLabel("0 / 0")
        total_layout.addWidget(self.total_label)
        layout.addLayout(total_layout)

        # 현재 파티션 진행률
        current_layout = QHBoxLayout()
        current_layout.addWidget(QLabel("현재 파티션:"))
        self.current_progress = QProgressBar()
        current_layout.addWidget(self.current_progress)
        self.current_label = QLabel("대기중")
        current_layout.addWidget(self.current_label)
        layout.addLayout(current_layout)

        # 상태 정보
        info_layout = QHBoxLayout()

        self.speed_label = QLabel("처리 속도: 0 rows/sec")
        info_layout.addWidget(self.speed_label)

        self.data_rate_label = QLabel("전송 속도: 0 MB/sec")
        info_layout.addWidget(self.data_rate_label)

        self.eta_label = QLabel("예상 완료: 계산중...")
        info_layout.addWidget(self.eta_label)

        self.elapsed_label = QLabel("경과 시간: 00:00:00")
        info_layout.addWidget(self.elapsed_label)

        self.method_label = QLabel("방식: COPY (고성능)")
        self.method_label.setStyleSheet("QLabel { color: #0080FF; font-weight: bold; }")
        info_layout.addWidget(self.method_label)

        info_layout.addStretch()
        layout.addLayout(info_layout)

        # 배치 크기 설정
        batch_layout = QHBoxLayout()
        batch_layout.addWidget(QLabel("배치 크기:"))
        self.batch_size_spin = QSpinBox()
        self.batch_size_spin.setMinimum(1000)
        self.batch_size_spin.setMaximum(500000)
        self.batch_size_spin.setSingleStep(10000)
        self.batch_size_spin.setValue(100000)
        self.batch_size_spin.setSuffix(" rows")
        self.batch_size_spin.setToolTip("한 번에 처리할 행 수 (1,000 ~ 500,000)")
        batch_layout.addWidget(self.batch_size_spin)

        self.apply_batch_btn = QPushButton("적용")
        self.apply_batch_btn.clicked.connect(self.apply_batch_size)
        self.apply_batch_btn.setEnabled(False)
        batch_layout.addWidget(self.apply_batch_btn)

        batch_layout.addStretch()
        layout.addLayout(batch_layout)

        group.setLayout(layout)
        return group

    def create_connection_status_widget(self):
        """연결 상태 표시 위젯 생성"""
        widget = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(10, 5, 10, 5)

        # 소스 DB 상태
        source_label = QLabel("소스 DB:")
        source_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(source_label)

        self.source_status_icon = QLabel("●")
        self.source_status_icon.setStyleSheet("color: #FFFF00; font-size: 16px;")
        layout.addWidget(self.source_status_icon)

        self.source_status_text = QLabel("확인 중...")
        layout.addWidget(self.source_status_text)

        layout.addSpacing(30)

        # 대상 DB 상태
        target_label = QLabel("대상 DB:")
        target_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(target_label)

        self.target_status_icon = QLabel("●")
        self.target_status_icon.setStyleSheet("color: #FFFF00; font-size: 16px;")
        layout.addWidget(self.target_status_icon)

        self.target_status_text = QLabel("확인 중...")
        layout.addWidget(self.target_status_text)

        layout.addStretch()

        widget.setLayout(layout)
        widget.setStyleSheet("""
            QWidget {
                background-color: #2b2b2b;
                border: 1px solid #444;
                border-radius: 5px;
            }
        """)

        self.source_connected = False
        self.target_connected = False

        return widget

    def create_log_group(self):
        """로그 그룹 생성"""
        group = QGroupBox("실행 로그")
        layout = QVBoxLayout()

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)

        group.setLayout(layout)
        return group

    def on_table_type_changed(self, state):
        """테이블 타입 체크박스 변경 이벤트"""
        self.selected_table_types = [
            table_type
            for table_type, checkbox in self.table_type_checkboxes.items()
            if checkbox.isChecked()
        ]

        if not self.selected_table_types:
            sender = self.sender()
            if sender:
                sender.setChecked(True)
            QMessageBox.warning(
                self,
                "선택 오류",
                "최소 1개의 테이블 타입을 선택해야 합니다."
            )
            return

        # 파티션 목록 업데이트
        start_date = self.start_calendar.selectedDate().toPython()
        end_date = self.end_calendar.selectedDate().toPython()
        if start_date <= end_date:
            self.update_partition_list(start_date, end_date)

    def on_method_changed(self, checked):
        """마이그레이션 방식 변경 이벤트"""
        if self.copy_radio.isChecked():
            self.migration_method = "copy"
            self.method_label.setText("방식: COPY (고성능)")
            self.method_label.setStyleSheet("QLabel { color: #0080FF; font-weight: bold; }")
        else:
            self.migration_method = "insert"
            self.method_label.setText("방식: INSERT (호환성)")
            self.method_label.setStyleSheet("QLabel { color: #FF8000; font-weight: bold; }")

    def on_error_strategy_changed(self, checked):
        """에러 처리 전략 변경 이벤트"""
        if self.stop_on_error_radio.isChecked():
            self.error_strategy = "stop"
        else:
            self.error_strategy = "skip"

    def check_incomplete_migration(self):
        """미완료 마이그레이션 확인"""
        incomplete = self.history_manager.get_incomplete_history(self.profile.id)
        if incomplete:
            reply = QMessageBox.question(
                self,
                "미완료 작업",
                f"이전에 중단된 마이그레이션이 있습니다.\n"
                f"날짜: {incomplete.start_date} ~ {incomplete.end_date}\n"
                f"진행률: {incomplete.processed_rows} / {incomplete.total_rows}\n\n"
                f"이어서 진행하시겠습니까?",
                QMessageBox.Yes | QMessageBox.No,
            )

            if reply == QMessageBox.Yes:
                self.resume_migration(incomplete.id)

    def on_date_changed(self):
        """날짜 변경 이벤트"""
        start_date = self.start_calendar.selectedDate().toPython()
        end_date = self.end_calendar.selectedDate().toPython()

        if start_date > end_date:
            return

        self.update_partition_list(start_date, end_date)

    def update_partition_list(self, start_date: date, end_date: date):
        """파티션 목록 업데이트"""
        self.partition_list.clear()
        self.all_partitions = []

        try:
            discovery = PartitionDiscovery(self.profile.source_config)
            partitions = discovery.discover_partitions(
                start_date,
                end_date,
                table_types=self.selected_table_types
            )

            if partitions:
                self.all_partitions = partitions

                # 테이블 타입별로 그룹화하여 표시
                display_limit = 100
                display_count = 0

                for partition in partitions:
                    if display_count >= display_limit:
                        break

                    table_type = partition.get('table_type')
                    config = TABLE_TYPE_CONFIG.get(table_type) if table_type else None
                    type_prefix = f"[{config.display_name}] " if config else ""

                    item_text = f"{type_prefix}{partition['table_name']} ({partition['row_count']:,} rows)"
                    item = QListWidgetItem(item_text)
                    item.setData(Qt.UserRole, partition["table_name"])
                    self.partition_list.addItem(item)
                    display_count += 1

                if len(partitions) > display_limit:
                    remaining = len(partitions) - display_limit
                    summary_text = f"... 외 {remaining}개 파티션 (전체 {len(partitions)}개 포함됨)"
                    summary_item = QListWidgetItem(summary_text)
                    summary_item.setFlags(summary_item.flags() & ~Qt.ItemIsSelectable)
                    summary_item.setForeground(Qt.gray)
                    self.partition_list.addItem(summary_item)

                self.partition_count_label.setText(f"총 {len(partitions)}개 파티션")
                self.add_log(f"파티션 {len(partitions)}개 발견", "INFO")
            else:
                self.partition_count_label.setText("총 0개 파티션")
                self.add_log("선택한 조건에 해당하는 파티션이 없습니다", "WARNING")

        except Exception as e:
            self.add_log(f"파티션 탐색 오류: {str(e)}", "ERROR")
            self.partition_count_label.setText("총 0개 파티션 (오류)")

    def add_log(self, message: str, level: str = "INFO"):
        """로그 추가"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {level}: {message}"

        self.log_text.append(log_entry)
        self.log_text.moveCursor(QTextCursor.End)

        log_emitter.emit_log(level, message)

    def start_migration(self):
        """마이그레이션 시작"""
        if self.is_running:
            return

        start_date = self.start_calendar.selectedDate().toPython()
        end_date = self.end_calendar.selectedDate().toPython()

        if start_date > end_date:
            QMessageBox.warning(self, "날짜 오류", "시작 날짜가 종료 날짜보다 늦습니다.")
            return

        # 파티션 목록 가져오기
        partitions = []
        if self.all_partitions:
            for p in self.all_partitions:
                if isinstance(p, dict):
                    partitions.append(p["table_name"])
                else:
                    partitions.append(p)
        else:
            for i in range(self.partition_list.count()):
                item = self.partition_list.item(i)
                if item.text().startswith("... 외"):
                    continue
                table_name = item.data(Qt.UserRole)
                if table_name:
                    partitions.append(table_name)
                else:
                    text = item.text()
                    if " (" in text:
                        # [Type] name (rows) 형식에서 이름 추출
                        if "] " in text:
                            text = text.split("] ")[1]
                        table_name = text.split(" (")[0]
                    else:
                        table_name = text
                    partitions.append(table_name)

        if not partitions:
            QMessageBox.warning(self, "파티션 없음", "선택된 날짜 범위에 파티션이 없습니다.")
            return

        # 선택한 옵션 로그
        method_text = "COPY (고성능)" if self.migration_method == "copy" else "INSERT (호환성)"
        error_text = "중단" if self.error_strategy == "stop" else "건너뛰기"
        types_text = ", ".join([t.value for t in self.selected_table_types])
        self.add_log(f"옵션 - 방식: {method_text}, 에러 처리: {error_text}, 테이블 타입: {types_text}", "INFO")

        # 이력 생성
        source_status = "연결 성공" if self.source_connected else self.source_status_text.text()
        target_status = "연결 성공" if self.target_connected else self.target_status_text.text()

        history = self.history_manager.create_history(
            self.profile.id,
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"),
            source_status=source_status,
            target_status=target_status,
        )
        self.history_id = history.id

        # 체크포인트 생성
        for partition in partitions:
            self.checkpoint_manager.create_checkpoint(self.history_id, partition)

        # 워커 스레드 시작
        self.start_worker_thread(partitions)

    def resume_migration(self, history_id: int):
        """마이그레이션 재개"""
        self.history_id = history_id
        history = self.history_manager.get_history(history_id)

        if not history:
            return

        start_date = datetime.strptime(history.start_date, "%Y-%m-%d").date()
        end_date = datetime.strptime(history.end_date, "%Y-%m-%d").date()

        self.start_calendar.setSelectedDate(QDate(start_date))
        self.end_calendar.setSelectedDate(QDate(end_date))

        pending_checkpoints = self.checkpoint_manager.get_pending_checkpoints(history_id)
        partitions = [cp.partition_name for cp in pending_checkpoints]

        if partitions:
            self.add_log(f"재개: {len(partitions)}개 파티션 남음", "INFO")
            self.start_worker_thread(partitions, resume=True)

    def start_worker_thread(self, partitions: list, resume: bool = False):
        """워커 스레드 시작"""
        self.is_running = True
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)

        # 옵션 컨트롤 비활성화
        self.copy_radio.setEnabled(False)
        self.insert_radio.setEnabled(False)
        self.stop_on_error_radio.setEnabled(False)
        self.skip_on_error_radio.setEnabled(False)
        for checkbox in self.table_type_checkboxes.values():
            checkbox.setEnabled(False)

        # 마이그레이션 방식에 따라 워커 선택
        if self.migration_method == "copy":
            self.worker = CopyMigrationWorker(
                self.profile, partitions, self.history_id, resume=resume
            )
            self.apply_batch_btn.setEnabled(False)
        else:
            self.worker = MigrationWorker(
                self.profile, partitions, self.history_id, resume=resume
            )
            self.apply_batch_btn.setEnabled(True)

        # 에러 처리 전략 설정
        if hasattr(self.worker, 'skip_on_error'):
            self.worker.skip_on_error = (self.error_strategy == "skip")

        # 시그널 연결
        self.worker.progress.connect(self.on_progress)
        self.worker.log.connect(self.add_log)
        self.worker.finished.connect(self.on_finished)
        self.worker.error.connect(self.on_error)

        if hasattr(self.worker, 'performance'):
            self.worker.performance.connect(self.on_performance_update)

        # 워커 시작
        self.worker.start()
        self.update_timer.start(5000)

        start_date = self.start_calendar.selectedDate().toPython()
        end_date = self.end_calendar.selectedDate().toPython()

        self.add_log(f"마이그레이션 시작 - {len(partitions)}개 파티션", "INFO")
        self.add_log(f"날짜 범위: {start_date} ~ {end_date}", "INFO")

        # 트레이 아이콘 알림
        main_window = self.parent()
        if main_window and hasattr(main_window, "tray_icon") and main_window.tray_icon:
            main_window.tray_icon.set_migration_running(True)
            main_window.tray_icon.notify_migration_started(self.profile.name)

    def pause_migration(self):
        """마이그레이션 일시정지"""
        if self.worker and self.is_running:
            self.worker.pause()
            self.pause_btn.setText("재개")
            self.pause_btn.clicked.disconnect()
            self.pause_btn.clicked.connect(self.resume_paused)
            self.add_log("마이그레이션 일시정지", "INFO")

    def resume_paused(self):
        """일시정지된 마이그레이션 재개"""
        if self.worker:
            self.worker.resume()
            self.pause_btn.setText("일시정지")
            self.pause_btn.clicked.disconnect()
            self.pause_btn.clicked.connect(self.pause_migration)
            self.add_log("마이그레이션 재개", "INFO")

    def cancel_migration(self):
        """마이그레이션 취소"""
        if self.is_running:
            reply = QMessageBox.question(
                self,
                "확인",
                "마이그레이션을 취소하시겠습니까?\n"
                "완료된 파티션은 유지되며, 나중에 이어서 진행할 수 있습니다.",
                QMessageBox.Yes | QMessageBox.No,
            )

            if reply == QMessageBox.Yes:
                if self.worker:
                    self.worker.stop()
                self.add_log("사용자가 마이그레이션을 취소했습니다", "WARNING")

                main_window = self.parent()
                if main_window and hasattr(main_window, "tray_icon") and main_window.tray_icon:
                    main_window.tray_icon.set_migration_running(False)
        else:
            self.close()

    def on_progress(self, data: dict):
        """진행 상황 업데이트"""
        if "total_progress" in data:
            self.total_progress.setValue(data["total_progress"])
            self.total_label.setText(f"{data['completed_partitions']} / {data['total_partitions']}")

        if "current_progress" in data:
            self.current_progress.setValue(data["current_progress"])
            self.current_label.setText(
                f"{data['current_partition']} ({data['current_rows']:,} rows)"
            )

        if "speed" in data:
            self.speed_label.setText(f"처리 속도: {data['speed']:,} rows/sec")

    def update_progress(self):
        """주기적 진행 상황 업데이트"""
        if self.worker and self.is_running:
            if hasattr(self.worker, "performance_metrics"):
                return

            stats = self.worker.get_stats()

            if stats["eta_seconds"] > 0:
                hours = int(stats["eta_seconds"] // 3600)
                minutes = int((stats["eta_seconds"] % 3600) // 60)
                self.eta_label.setText(f"예상 완료: {hours}시간 {minutes}분")
            else:
                self.eta_label.setText("예상 완료: 계산중...")

            elapsed = stats["elapsed_seconds"]
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            seconds = int(elapsed % 60)
            self.elapsed_label.setText(f"경과 시간: {hours:02d}:{minutes:02d}:{seconds:02d}")

    def on_finished(self):
        """마이그레이션 완료"""
        self.is_running = False
        self.update_timer.stop()

        rows_processed = self._get_processed_rows()

        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.apply_batch_btn.setEnabled(False)

        if self.worker:
            self.worker.deleteLater()
            self.worker = None

        if self.history_id:
            self.history_manager.update_history_status(
                self.history_id, "completed", processed_rows=rows_processed
            )

        self.add_log("마이그레이션이 완료되었습니다", "SUCCESS")

        main_window = self.parent()
        if main_window and hasattr(main_window, "tray_icon") and main_window.tray_icon:
            if rows_processed == 0 and self.history_id:
                history = self.history_manager.get_history(self.history_id)
                rows_processed = history.processed_rows if history else 0
            main_window.tray_icon.set_migration_running(False)
            main_window.tray_icon.notify_migration_completed(self.profile.name, rows_processed)

        QMessageBox.information(
            self,
            "완료",
            "마이그레이션이 성공적으로 완료되었습니다.\n\n"
            "새로운 마이그레이션을 시작하려면 창을 닫고 다시 열어주세요.",
        )

    def on_error(self, error_msg: str):
        """오류 발생"""
        self.is_running = False
        self.update_timer.stop()

        rows_processed = self._get_processed_rows()

        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.apply_batch_btn.setEnabled(False)

        if self.worker:
            self.worker.deleteLater()
            self.worker = None

        if self.history_id:
            self.history_manager.update_history_status(
                self.history_id, "failed", processed_rows=rows_processed
            )

        self.add_log(f"오류 발생: {error_msg}", "ERROR")

        main_window = self.parent()
        if main_window and hasattr(main_window, "tray_icon") and main_window.tray_icon:
            main_window.tray_icon.set_migration_running(False)
            main_window.tray_icon.notify_migration_error(error_msg)

        QMessageBox.critical(
            self,
            "오류",
            f"마이그레이션 중 오류가 발생했습니다:\n\n{error_msg}\n\n"
            "새로운 마이그레이션을 시작하려면 창을 닫고 다시 열어주세요.",
        )

    def on_truncate_requested(self, table_name: str, row_count: int):
        """TRUNCATE 확인 요청"""
        reply = QMessageBox.question(
            self,
            "기존 데이터 확인",
            f"{table_name} 테이블에 {row_count:,}개의 기존 데이터가 있습니다.\n\n"
            "이 데이터를 삭제하고 계속하시겠습니까?\n\n"
            "Yes: 기존 데이터를 삭제하고 계속\n"
            "No: 마이그레이션 중단",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if self.worker:
            self.worker.truncate_permission = reply == QMessageBox.Yes

    def apply_batch_size(self):
        """배치 크기 적용"""
        if self.worker and self.is_running:
            new_size = self.batch_size_spin.value()
            self.worker.batch_size = new_size
            self.add_log(f"배치 크기를 {new_size:,} rows로 변경했습니다", "INFO")

    def on_performance_update(self, stats: dict):
        """성능 지표 업데이트"""
        rows_per_sec = stats.get("instant_rows_per_sec", 0)
        if rows_per_sec >= 1000000:
            speed_text = f"{rows_per_sec / 1000000:.1f}M rows/sec"
        elif rows_per_sec >= 1000:
            speed_text = f"{rows_per_sec / 1000:.1f}K rows/sec"
        else:
            speed_text = f"{rows_per_sec:.0f} rows/sec"
        self.speed_label.setText(f"처리 속도: {speed_text}")

        mb_per_sec = stats.get("instant_mb_per_sec", 0)
        self.data_rate_label.setText(f"전송 속도: {mb_per_sec:.1f} MB/sec")

        eta_time = stats.get("eta_time", "계산중...")
        self.eta_label.setText(f"예상 완료: {eta_time}")

        elapsed_time = stats.get("elapsed_time", "00:00:00")
        self.elapsed_label.setText(f"경과 시간: {elapsed_time}")

    def _get_processed_rows(self) -> int:
        """워커 또는 저장된 이력에서 처리된 행 수를 조회"""
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

    def closeEvent(self, event):
        """다이얼로그 닫기 이벤트"""
        if self.is_running:
            reply = QMessageBox.question(
                self,
                "확인",
                "마이그레이션이 진행 중입니다.\n창을 닫으면 작업이 중단됩니다.\n계속하시겠습니까?",
                QMessageBox.Yes | QMessageBox.No,
            )

            if reply == QMessageBox.No:
                event.ignore()
                return

            if self.worker:
                self.worker.stop()
                self.worker.wait()

        event.accept()

    def check_connections(self):
        """연결 상태 확인"""
        self.connection_checker = CopyMigrationWorker(
            profile=self.profile,
            partitions=[],
            history_id=0,
            resume=False,
        )

        self.connection_checker.connection_checking.connect(self.on_connection_checking)
        self.connection_checker.source_connection_status.connect(self.on_source_connection_status)
        self.connection_checker.target_connection_status.connect(self.on_target_connection_status)

        self.connection_checker.check_connections_only = True
        self.connection_checker.start()

    def on_connection_checking(self):
        """연결 확인 시작"""
        self.source_status_icon.setStyleSheet("color: #FFFF00; font-size: 16px;")
        self.source_status_text.setText("확인 중...")
        self.target_status_icon.setStyleSheet("color: #FFFF00; font-size: 16px;")
        self.target_status_text.setText("확인 중...")

    def on_source_connection_status(self, connected: bool, message: str):
        """소스 DB 연결 상태 업데이트"""
        self.source_connected = connected

        if connected:
            self.source_status_icon.setStyleSheet("color: #00FF00; font-size: 16px;")
            self.source_status_text.setText("연결됨")
        else:
            self.source_status_icon.setStyleSheet("color: #FF0000; font-size: 16px;")
            self.source_status_text.setText(message)

        self.update_start_button_state()

    def on_target_connection_status(self, connected: bool, message: str):
        """대상 DB 연결 상태 업데이트"""
        self.target_connected = connected

        if connected:
            self.target_status_icon.setStyleSheet("color: #00FF00; font-size: 16px;")
            self.target_status_text.setText("연결됨")
        else:
            self.target_status_icon.setStyleSheet("color: #FF0000; font-size: 16px;")
            self.target_status_text.setText(message)

        self.update_start_button_state()

    def update_start_button_state(self):
        """시작 버튼 상태 업데이트"""
        if self.source_connected and self.target_connected and not self.is_running:
            self.start_btn.setEnabled(True)
        else:
            self.start_btn.setEnabled(False)
