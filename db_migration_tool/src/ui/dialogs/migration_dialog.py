"""
마이그레이션 진행 다이얼로그 (모달)
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QProgressBar, QTextEdit, QGroupBox,
    QCalendarWidget, QListWidget, QListWidgetItem,
    QMessageBox, QSplitter, QWidget, QSpinBox, QCheckBox
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QDate
from PySide6.QtGui import QTextCursor
from datetime import datetime, date
from typing import List

from src.models.profile import ConnectionProfile
from src.models.history import HistoryManager, CheckpointManager
from src.core.migration_worker import MigrationWorker
from src.core.copy_migration_worker import CopyMigrationWorker
from src.core.partition_discovery import PartitionDiscovery
from src.core.table_types import TableType, TABLE_TYPE_CONFIG, get_all_table_types
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

        # 선택된 테이블 타입 (기본값: Point History만)
        self.selected_table_types: List[TableType] = [TableType.POINT_HISTORY]
        self.table_type_checkboxes = {}  # 체크박스 저장용

        self.setup_ui()
        self.check_incomplete_migration()
        
    def setup_ui(self):
        """UI 초기화"""
        self.setWindowTitle(f"마이그레이션 - {self.profile.name}")
        self.setModal(True)
        self.resize(900, 700)
        
        layout = QVBoxLayout(self)
        
        # 연결 상태 표시 영역
        connection_status_widget = self.create_connection_status_widget()
        layout.addWidget(connection_status_widget)
        
        # 상단: 날짜 선택 영역
        date_group = self.create_date_selection_group()
        layout.addWidget(date_group)
        
        # 중간: 진행 상황 영역
        progress_group = self.create_progress_group()
        layout.addWidget(progress_group)
        
        # 하단: 로그 영역
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
        
    def create_date_selection_group(self):
        """날짜 범위 및 테이블 타입 선택 그룹 생성"""
        group = QGroupBox("마이그레이션 설정")
        main_layout = QVBoxLayout()

        # 테이블 타입 선택 (상단)
        table_type_layout = QHBoxLayout()
        table_type_layout.addWidget(QLabel("테이블 타입:"))

        # 각 테이블 타입별 체크박스 생성
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

        # 날짜 선택 영역
        date_partition_layout = QHBoxLayout()

        # 시작 날짜
        start_layout = QVBoxLayout()
        start_layout.addWidget(QLabel("시작 날짜:"))
        self.start_calendar = QCalendarWidget()
        self.start_calendar.setMaximumHeight(200)
        self.start_calendar.selectionChanged.connect(self.on_date_changed)
        start_layout.addWidget(self.start_calendar)
        date_partition_layout.addLayout(start_layout)

        # 종료 날짜
        end_layout = QVBoxLayout()
        end_layout.addWidget(QLabel("종료 날짜:"))
        self.end_calendar = QCalendarWidget()
        self.end_calendar.setMaximumHeight(200)
        self.end_calendar.selectionChanged.connect(self.on_date_changed)
        end_layout.addWidget(self.end_calendar)
        date_partition_layout.addLayout(end_layout)

        # 파티션 목록
        partition_layout = QVBoxLayout()
        partition_layout.addWidget(QLabel("선택된 파티션:"))
        self.partition_list = QListWidget()
        self.partition_list.setMaximumHeight(200)
        partition_layout.addWidget(self.partition_list)
        self.partition_count_label = QLabel("총 0개 파티션")
        partition_layout.addWidget(self.partition_count_label)
        date_partition_layout.addLayout(partition_layout)

        main_layout.addLayout(date_partition_layout)

        group.setLayout(main_layout)
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
        
        # 처리 속도
        self.speed_label = QLabel("처리 속도: 0 rows/sec")
        info_layout.addWidget(self.speed_label)
        
        # 데이터 전송률
        self.data_rate_label = QLabel("전송 속도: 0 MB/sec")
        info_layout.addWidget(self.data_rate_label)
        
        # 예상 완료 시간
        self.eta_label = QLabel("예상 완료: 계산중...")
        info_layout.addWidget(self.eta_label)
        
        # 경과 시간
        self.elapsed_label = QLabel("경과 시간: 00:00:00")
        info_layout.addWidget(self.elapsed_label)
        
        # COPY 방식 표시
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
        self.source_status_icon.setStyleSheet("color: #FFFF00; font-size: 16px;")  # 노란색
        layout.addWidget(self.source_status_icon)
        
        self.source_status_text = QLabel("확인 중...")
        layout.addWidget(self.source_status_text)
        
        layout.addSpacing(30)
        
        # 대상 DB 상태
        target_label = QLabel("대상 DB:")
        target_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(target_label)
        
        self.target_status_icon = QLabel("●")
        self.target_status_icon.setStyleSheet("color: #FFFF00; font-size: 16px;")  # 노란색
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
        
        # 연결 상태 저장
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
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                self.resume_migration(incomplete.id)
                
    def on_table_type_changed(self, state):
        """테이블 타입 체크박스 변경 이벤트"""
        # 선택된 테이블 타입 업데이트
        self.selected_table_types = [
            table_type
            for table_type, checkbox in self.table_type_checkboxes.items()
            if checkbox.isChecked()
        ]

        # 최소 1개는 선택되어야 함
        if not self.selected_table_types:
            # 체크 해제한 것을 다시 체크
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

    def on_date_changed(self):
        """날짜 변경 이벤트"""
        start_date = self.start_calendar.selectedDate().toPython()
        end_date = self.end_calendar.selectedDate().toPython()

        if start_date > end_date:
            return

        # 파티션 목록 업데이트
        self.update_partition_list(start_date, end_date)
        
    def update_partition_list(self, start_date: date, end_date: date):
        """파티션 목록 업데이트"""
        self.partition_list.clear()

        try:
            # 파티션 탐색 (선택된 테이블 타입 전달)
            discovery = PartitionDiscovery(self.profile.source_config)
            partitions = discovery.discover_partitions(
                start_date,
                end_date,
                table_types=self.selected_table_types
            )

            if partitions:
                # 테이블 타입별로 그룹화하여 표시
                table_type_groups = {}
                for partition in partitions:
                    table_type = partition['table_type']
                    if table_type not in table_type_groups:
                        table_type_groups[table_type] = []
                    table_type_groups[table_type].append(partition)

                # 각 테이블 타입별로 항목 추가
                total_count = 0
                for table_type in sorted(table_type_groups.keys(), key=lambda t: t.value):
                    partitions_for_type = table_type_groups[table_type]
                    config = TABLE_TYPE_CONFIG[table_type]

                    for partition in partitions_for_type:
                        item_text = f"[{config.display_name}] {partition['table_name']} ({partition['row_count']:,} rows)"
                        item = QListWidgetItem(item_text)
                        item.setData(Qt.UserRole, partition['table_name'])
                        self.partition_list.addItem(item)
                        total_count += 1

                self.partition_count_label.setText(f"총 {total_count}개 파티션")
                self.add_log(f"파티션 {total_count}개 발견 (타입: {len(table_type_groups)}개)", "INFO")
            else:
                # 파티션이 없는 경우
                self.partition_count_label.setText("총 0개 파티션")
                self.add_log("선택한 조건에 해당하는 파티션이 없습니다", "WARNING")

        except Exception as e:
            self.add_log(f"파티션 탐색 오류: {str(e)}", "ERROR")
            self.partition_count_label.setText("총 0개 파티션 (오류)")
            manual_partitions = []
            
            while current <= end_date:
                partition_name = f"point_history_{current.strftime('%y%m%d')}"
                manual_partitions.append(partition_name)
                self.partition_list.addItem(partition_name)
                current = date(current.year, current.month, current.day + 1)
                
            self.partition_count_label.setText(f"총 {len(manual_partitions)}개 파티션 (수동)")
        
    def add_log(self, message: str, level: str = "INFO"):
        """로그 추가"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {level}: {message}"
        
        self.log_text.append(log_entry)
        self.log_text.moveCursor(QTextCursor.End)
        
        # 향상된 로거로도 기록
        log_emitter.emit_log(level, message)
        
    def start_migration(self):
        """마이그레이션 시작"""
        if self.is_running:
            return
            
        # 날짜 확인
        start_date = self.start_calendar.selectedDate().toPython()
        end_date = self.end_calendar.selectedDate().toPython()
        
        if start_date > end_date:
            QMessageBox.warning(self, "날짜 오류", "시작 날짜가 종료 날짜보다 늦습니다.")
            return
            
        # 파티션 목록 가져오기
        partitions = []
        for i in range(self.partition_list.count()):
            item = self.partition_list.item(i)
            # UserRole에 저장된 실제 테이블 이름 가져오기
            table_name = item.data(Qt.UserRole)
            if table_name:
                partitions.append(table_name)
            else:
                # fallback: 텍스트에서 테이블 이름 추출
                text = item.text()
                if ' (' in text:
                    table_name = text.split(' (')[0]
                else:
                    table_name = text
                partitions.append(table_name)
            
        if not partitions:
            QMessageBox.warning(self, "파티션 없음", "선택된 날짜 범위에 파티션이 없습니다.")
            return
            
        # 이력 생성 (연결 상태 포함)
        source_status = "연결 성공" if self.source_connected else self.source_status_text.text()
        target_status = "연결 성공" if self.target_connected else self.target_status_text.text()
        
        history = self.history_manager.create_history(
            self.profile.id,
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"),
            source_status=source_status,
            target_status=target_status
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
            
        # 날짜 설정
        start_date = datetime.strptime(history.start_date, "%Y-%m-%d").date()
        end_date = datetime.strptime(history.end_date, "%Y-%m-%d").date()
        
        self.start_calendar.setSelectedDate(QDate(start_date))
        self.end_calendar.setSelectedDate(QDate(end_date))
        
        # 미완료 체크포인트 가져오기
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
        self.apply_batch_btn.setEnabled(False)  # COPY 방식에서는 배치 크기 조정 불필요
        
        # COPY 기반 워커 생성
        self.worker = CopyMigrationWorker(
            self.profile,
            partitions,
            self.history_id,
            resume=resume
        )
        
        # 시그널 연결
        self.worker.progress.connect(self.on_progress)
        self.worker.log.connect(self.add_log)
        self.worker.finished.connect(self.on_finished)
        self.worker.error.connect(self.on_error)
        self.worker.performance.connect(self.on_performance_update)  # 새로운 성능 시그널
        
        # 워커 시작
        self.worker.start()
        
        # 타이머 시작
        self.update_timer.start(5000)  # 5초마다
        
        # 날짜 범위 변수 정의
        start_date = self.start_calendar.selectedDate().toPython()
        end_date = self.end_calendar.selectedDate().toPython()
        
        self.add_log(f"마이그레이션 시작 - {len(partitions)}개 파티션", "INFO")
        self.add_log(f"날짜 범위: {start_date} ~ {end_date}", "INFO")
        
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
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                if self.worker:
                    self.worker.stop()
                self.add_log("사용자가 마이그레이션을 취소했습니다", "WARNING")
        else:
            self.close()
            
    def on_progress(self, data: dict):
        """진행 상황 업데이트"""
        # 전체 진행률
        if 'total_progress' in data:
            self.total_progress.setValue(data['total_progress'])
            self.total_label.setText(
                f"{data['completed_partitions']} / {data['total_partitions']}"
            )
            
        # 현재 파티션 진행률
        if 'current_progress' in data:
            self.current_progress.setValue(data['current_progress'])
            self.current_label.setText(
                f"{data['current_partition']} ({data['current_rows']:,} rows)"
            )
            
        # 처리 속도
        if 'speed' in data:
            self.speed_label.setText(f"처리 속도: {data['speed']:,} rows/sec")
            
    def update_progress(self):
        """주기적 진행 상황 업데이트 (5초마다)"""
        if self.worker and self.is_running:
            # CopyMigrationWorker는 성능 시그널로 업데이트하므로 여기서는 스킵
            if hasattr(self.worker, 'performance_metrics'):
                # CopyMigrationWorker의 경우 performance 시그널로 처리됨
                return
            
            # 기존 MigrationWorker용 코드
            stats = self.worker.get_stats()
            
            # 예상 완료 시간
            if stats['eta_seconds'] > 0:
                hours = int(stats['eta_seconds'] // 3600)
                minutes = int((stats['eta_seconds'] % 3600) // 60)
                self.eta_label.setText(f"예상 완료: {hours}시간 {minutes}분")
            else:
                self.eta_label.setText("예상 완료: 계산중...")
                
            # 경과 시간
            elapsed = stats['elapsed_seconds']
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            seconds = int(elapsed % 60)
            self.elapsed_label.setText(f"경과 시간: {hours:02d}:{minutes:02d}:{seconds:02d}")
            
    def on_finished(self):
        """마이그레이션 완료"""
        self.is_running = False
        self.update_timer.stop()
        
        # 완료 후에는 다시 시작하지 못하도록 비활성화
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.apply_batch_btn.setEnabled(False)
        
        # 워커 정리
        if self.worker:
            self.worker.deleteLater()
            self.worker = None
        
        # 이력 상태 업데이트
        if self.history_id:
            self.history_manager.update_history_status(self.history_id, "completed")
            
        self.add_log("마이그레이션이 완료되었습니다", "SUCCESS")
        
        QMessageBox.information(
            self,
            "완료",
            "마이그레이션이 성공적으로 완료되었습니다.\n\n"
            "새로운 마이그레이션을 시작하려면 창을 닫고 다시 열어주세요."
        )
        
    def on_error(self, error_msg: str):
        """오류 발생"""
        self.is_running = False
        self.update_timer.stop()
        
        # 오류 발생 후에도 다시 시작하지 못하도록 비활성화
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.apply_batch_btn.setEnabled(False)
        
        # 워커 정리
        if self.worker:
            self.worker.deleteLater()
            self.worker = None
        
        # 이력 상태 업데이트
        if self.history_id:
            self.history_manager.update_history_status(self.history_id, "failed")
            
        self.add_log(f"오류 발생: {error_msg}", "ERROR")
        
        QMessageBox.critical(
            self,
            "오류",
            f"마이그레이션 중 오류가 발생했습니다:\n\n{error_msg}\n\n"
            "새로운 마이그레이션을 시작하려면 창을 닫고 다시 열어주세요."
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
            QMessageBox.No  # 기본값은 No
        )
        
        if self.worker:
            self.worker.truncate_permission = (reply == QMessageBox.Yes)
            
    def apply_batch_size(self):
        """배치 크기 적용"""
        if self.worker and self.is_running:
            new_size = self.batch_size_spin.value()
            self.worker.batch_size = new_size
            self.add_log(f"배치 크기를 {new_size:,} rows로 변경했습니다", "INFO")
        
    def on_performance_update(self, stats: dict):
        """성능 지표 업데이트"""
        # 처리 속도
        rows_per_sec = stats.get('instant_rows_per_sec', 0)
        if rows_per_sec >= 1000000:
            speed_text = f"{rows_per_sec/1000000:.1f}M rows/sec"
        elif rows_per_sec >= 1000:
            speed_text = f"{rows_per_sec/1000:.1f}K rows/sec"
        else:
            speed_text = f"{rows_per_sec:.0f} rows/sec"
        self.speed_label.setText(f"처리 속도: {speed_text}")
        
        # 데이터 전송률
        mb_per_sec = stats.get('instant_mb_per_sec', 0)
        self.data_rate_label.setText(f"전송 속도: {mb_per_sec:.1f} MB/sec")
        
        # 예상 완료 시간
        eta_time = stats.get('eta_time', '계산중...')
        self.eta_label.setText(f"예상 완료: {eta_time}")
        
        # 경과 시간
        elapsed_time = stats.get('elapsed_time', '00:00:00')
        self.elapsed_label.setText(f"경과 시간: {elapsed_time}")
        
    def closeEvent(self, event):
        """다이얼로그 닫기 이벤트"""
        if self.is_running:
            reply = QMessageBox.question(
                self,
                "확인",
                "마이그레이션이 진행 중입니다.\n"
                "창을 닫으면 작업이 중단됩니다.\n"
                "계속하시겠습니까?",
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.No:
                event.ignore()
                return
                
            if self.worker:
                self.worker.stop()
                self.worker.wait()  # 스레드 종료 대기
                
        event.accept()
    
    def check_connections(self):
        """연결 상태 확인"""
        # 임시 worker를 생성하여 연결 확인
        self.connection_checker = CopyMigrationWorker(
            profile=self.profile,
            partitions=[],  # 빈 리스트
            history_id=0,  # 임시 ID
            resume=False
        )
        
        # 연결 상태 시그널 연결
        self.connection_checker.connection_checking.connect(self.on_connection_checking)
        self.connection_checker.source_connection_status.connect(self.on_source_connection_status)
        self.connection_checker.target_connection_status.connect(self.on_target_connection_status)
        
        # 연결 확인만 수행하는 플래그 설정
        self.connection_checker.check_connections_only = True
        self.connection_checker.start()
    
    def on_connection_checking(self):
        """연결 확인 시작"""
        self.source_status_icon.setStyleSheet("color: #FFFF00; font-size: 16px;")  # 노란색
        self.source_status_text.setText("확인 중...")
        self.target_status_icon.setStyleSheet("color: #FFFF00; font-size: 16px;")  # 노란색
        self.target_status_text.setText("확인 중...")
    
    def on_source_connection_status(self, connected: bool, message: str):
        """소스 DB 연결 상태 업데이트"""
        self.source_connected = connected
        
        if connected:
            self.source_status_icon.setStyleSheet("color: #00FF00; font-size: 16px;")  # 초록색
            self.source_status_text.setText("연결됨")
        else:
            self.source_status_icon.setStyleSheet("color: #FF0000; font-size: 16px;")  # 빨간색
            self.source_status_text.setText(message)
        
        self.update_start_button_state()
    
    def on_target_connection_status(self, connected: bool, message: str):
        """대상 DB 연결 상태 업데이트"""
        self.target_connected = connected
        
        if connected:
            self.target_status_icon.setStyleSheet("color: #00FF00; font-size: 16px;")  # 초록색
            self.target_status_text.setText("연결됨")
        else:
            self.target_status_icon.setStyleSheet("color: #FF0000; font-size: 16px;")  # 빨간색
            self.target_status_text.setText(message)
        
        self.update_start_button_state()
    
    def update_start_button_state(self):
        """시작 버튼 상태 업데이트"""
        # 양쪽 DB가 모두 연결되었고, 현재 실행 중이 아닐 때만 활성화
        if self.source_connected and self.target_connected and not self.is_running:
            self.start_btn.setEnabled(True)
        else:
            self.start_btn.setEnabled(False)