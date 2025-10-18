"""
메인 윈도우 UI
"""
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTableWidget, QTableWidgetItem,
    QSplitter, QGroupBox, QListWidget, QListWidgetItem,
    QToolBar, QStatusBar, QMessageBox, QHeaderView
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction

from src.ui.dialogs.connection_dialog import ConnectionDialog
from src.ui.dialogs.migration_dialog import MigrationDialog
from src.ui.dialogs.log_viewer_dialog import LogViewerDialog
from src.ui.viewmodels.main_viewmodel import MainViewModel


class MainWindow(QMainWindow):
    """메인 윈도우 클래스 (MVVM 패턴 적용)"""

    # 시그널 정의
    profile_selected = Signal(int)  # 프로필 ID
    migration_requested = Signal(int)  # 프로필 ID

    def __init__(self):
        super().__init__()

        # ViewModel 초기화
        self.vm = MainViewModel()

        # UI 상태
        self.log_viewer_dialog = None

        # UI 구성
        self.setup_ui()
        self.bind_viewmodel()

        # 초기 데이터 로드
        self.vm.initialize()
        self.refresh_ui_from_vm()
        
    def setup_ui(self):
        """UI 초기화"""
        self.setWindowTitle("DB 마이그레이션 도구")
        self.setGeometry(100, 100, 1200, 800)
        
        # 중앙 위젯
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 메인 레이아웃
        main_layout = QHBoxLayout(central_widget)
        
        # 좌측: 연결 프로필
        left_panel = self.create_profile_panel()
        
        # 우측: 작업 이력
        right_panel = self.create_history_panel()
        
        # 스플리터로 분할
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([400, 800])
        
        main_layout.addWidget(splitter)
        
        # 툴바 생성
        self.create_toolbar()
        
        # 상태바 생성
        self.create_statusbar()
        
    def create_toolbar(self):
        """툴바 생성"""
        toolbar = QToolBar("메인 툴바")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        
        # 새 연결 액션
        new_connection_action = QAction("새 연결", self)
        new_connection_action.setShortcut("Ctrl+N")
        new_connection_action.triggered.connect(self.new_connection)
        toolbar.addAction(new_connection_action)
        
        # 편집 액션
        edit_action = QAction("편집", self)
        edit_action.setShortcut("Ctrl+E")
        edit_action.triggered.connect(self.edit_connection)
        toolbar.addAction(edit_action)
        
        # 삭제 액션
        delete_action = QAction("삭제", self)
        delete_action.setShortcut("Delete")
        delete_action.triggered.connect(self.delete_connection)
        toolbar.addAction(delete_action)
        
        toolbar.addSeparator()
        
        # 마이그레이션 시작 액션
        migrate_action = QAction("마이그레이션 시작", self)
        migrate_action.setShortcut("F5")
        migrate_action.triggered.connect(self.start_migration)
        toolbar.addAction(migrate_action)
        
        toolbar.addSeparator()
        
        # 로그 뷰어 액션
        log_viewer_action = QAction("로그 뷰어", self)
        log_viewer_action.setShortcut("Ctrl+L")
        log_viewer_action.triggered.connect(self.show_log_viewer)
        toolbar.addAction(log_viewer_action)
        
    def create_statusbar(self):
        """상태바 생성"""
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("준비")
        
    def create_profile_panel(self):
        """연결 프로필 패널 생성"""
        group = QGroupBox("연결 프로필")
        layout = QVBoxLayout()
        
        # 프로필 목록
        self.profile_list = QListWidget()
        self.profile_list.itemSelectionChanged.connect(self.on_profile_selected)
        self.profile_list.itemDoubleClicked.connect(self.edit_connection)
        layout.addWidget(self.profile_list)
        
        # 버튼 레이아웃
        button_layout = QHBoxLayout()
        
        self.new_btn = QPushButton("새 연결")
        self.new_btn.clicked.connect(self.new_connection)
        button_layout.addWidget(self.new_btn)
        
        self.edit_btn = QPushButton("편집")
        self.edit_btn.clicked.connect(self.edit_connection)
        self.edit_btn.setEnabled(False)
        button_layout.addWidget(self.edit_btn)
        
        self.delete_btn = QPushButton("삭제")
        self.delete_btn.clicked.connect(self.delete_connection)
        self.delete_btn.setEnabled(False)
        button_layout.addWidget(self.delete_btn)
        
        layout.addLayout(button_layout)
        
        # 마이그레이션 시작 버튼
        self.migrate_btn = QPushButton("마이그레이션 시작")
        self.migrate_btn.clicked.connect(self.start_migration)
        self.migrate_btn.setEnabled(False)
        layout.addWidget(self.migrate_btn)
        
        group.setLayout(layout)
        return group
        
    def create_history_panel(self):
        """작업 이력 패널 생성"""
        group = QGroupBox("작업 이력")
        layout = QVBoxLayout()
        
        # 이력 테이블
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(6)
        self.history_table.setHorizontalHeaderLabels([
            "프로필", "시작 날짜", "종료 날짜", 
            "시작 시간", "완료 시간", "상태"
        ])
        
        # 열 너비 조정
        header = self.history_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        
        # 행 선택 모드
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        
        layout.addWidget(self.history_table)
        
        # 새로고침 버튼
        refresh_btn = QPushButton("새로고침")
        refresh_btn.clicked.connect(self.refresh_history)
        layout.addWidget(refresh_btn)
        
        group.setLayout(layout)
        return group
        
    def bind_viewmodel(self):
        """ViewModel 시그널 바인딩"""
        # ViewModel → UI 시그널
        self.vm.profiles_changed.connect(self.update_profile_list)
        self.vm.current_profile_changed.connect(self.update_profile_selection)
        self.vm.histories_changed.connect(self.update_history_table)
        self.vm.error_occurred.connect(self.show_error)
        self.vm.message_sent.connect(self.show_message)

        # UI → ViewModel 시그널
        self.profile_list.itemSelectionChanged.connect(self.on_profile_selected)

    def refresh_ui_from_vm(self):
        """ViewModel 상태를 기반으로 UI 초기화"""
        has_profile = self.vm.current_profile is not None
        self.edit_btn.setEnabled(has_profile)
        self.delete_btn.setEnabled(has_profile)
        self.migrate_btn.setEnabled(has_profile)

    def update_profile_list(self, profiles):
        """프로필 목록 UI 업데이트"""
        self.profile_list.clear()
        for profile in profiles:
            item = QListWidgetItem(profile.name)
            item.setData(Qt.UserRole, profile.id)
            self.profile_list.addItem(item)

    def update_profile_selection(self, profile):
        """프로필 선택 UI 업데이트"""
        has_profile = profile is not None
        self.edit_btn.setEnabled(has_profile)
        self.delete_btn.setEnabled(has_profile)
        self.migrate_btn.setEnabled(has_profile)

        if has_profile:
            self.status_bar.showMessage(f"프로필 선택됨: {profile.name}")
        else:
            self.status_bar.showMessage("준비")

    def update_history_table(self, histories):
        """작업 이력 테이블 UI 업데이트"""
        self.history_table.setRowCount(0)

        for history in histories:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)

            # 프로필 이름 가져오기
            profile = self.vm.profile_manager.get_profile(history.profile_id)
            profile_name = profile.name if profile else "알 수 없음"

            # 테이블 항목 설정
            self.history_table.setItem(row, 0, QTableWidgetItem(profile_name))
            self.history_table.setItem(row, 1, QTableWidgetItem(str(history.start_date)))
            self.history_table.setItem(row, 2, QTableWidgetItem(str(history.end_date)))
            self.history_table.setItem(row, 3, QTableWidgetItem(
                history.started_at.strftime("%Y-%m-%d %H:%M:%S") if history.started_at else ""
            ))
            self.history_table.setItem(row, 4, QTableWidgetItem(
                history.completed_at.strftime("%Y-%m-%d %H:%M:%S") if history.completed_at else ""
            ))

            # 상태 표시
            status_text = {
                "completed": "완료",
                "failed": "실패",
                "cancelled": "취소",
                "running": "진행중"
            }.get(history.status, history.status)
            self.history_table.setItem(row, 5, QTableWidgetItem(status_text))

    def show_error(self, message):
        """오류 메시지 표시"""
        QMessageBox.critical(self, "오류", message)

    def show_message(self, title, message):
        """일반 메시지 표시"""
        self.status_bar.showMessage(message)
            
    def on_profile_selected(self):
        """프로필 선택 이벤트 (ViewModel로 위임)"""
        selected_items = self.profile_list.selectedItems()
        if selected_items:
            profile_id = selected_items[0].data(Qt.UserRole)
            self.vm.select_profile(profile_id)
        else:
            self.vm.select_profile(None)  # 선택 해제 시 None 전달

    def new_connection(self):
        """새 연결 생성 (ViewModel로 위임)"""
        dialog = ConnectionDialog(self)
        if dialog.exec():
            profile_data = dialog.get_profile_data()
            self.vm.create_profile(profile_data)

    def edit_connection(self):
        """연결 편집 (ViewModel로 위임)"""
        if not self.vm.current_profile:
            return

        dialog = ConnectionDialog(self, self.vm.current_profile)
        if dialog.exec():
            profile_data = dialog.get_profile_data()
            self.vm.update_profile(self.vm.current_profile.id, profile_data)

    def delete_connection(self):
        """연결 삭제 (ViewModel로 위임)"""
        if not self.vm.current_profile:
            return

        reply = QMessageBox.question(
            self, "확인",
            "선택한 연결을 삭제하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            self.vm.delete_profile(self.vm.current_profile.id)

    def start_migration(self):
        """마이그레이션 시작"""
        if not self.vm.current_profile:
            return

        # 마이그레이션 다이얼로그 표시
        dialog = MigrationDialog(self, self.vm.current_profile)
        dialog.exec()

        # 완료 후 이력 새로고침
        self.vm.refresh_histories()

    def refresh_history(self):
        """작업 이력 새로고침 (ViewModel로 위임)"""
        self.vm.refresh_histories()
        self.status_bar.showMessage("작업 이력이 새로고침되었습니다.")
        
    def show_log_viewer(self):
        """로그 뷰어 표시"""
        if self.log_viewer_dialog is None:
            self.log_viewer_dialog = LogViewerDialog(self)
            
        if self.log_viewer_dialog.isHidden():
            self.log_viewer_dialog.show()
        else:
            self.log_viewer_dialog.raise_()
            self.log_viewer_dialog.activateWindow()