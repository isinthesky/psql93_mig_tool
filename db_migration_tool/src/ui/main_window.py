"""
메인 윈도우 UI
"""
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTableWidget, QTableWidgetItem,
    QSplitter, QGroupBox, QListWidget, QListWidgetItem,
    QToolBar, QStatusBar, QMessageBox, QHeaderView
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QAction, QIcon

from src.ui.dialogs.connection_dialog import ConnectionDialog
from src.ui.dialogs.migration_dialog import MigrationDialog
from src.ui.dialogs.log_viewer_dialog import LogViewerDialog
from src.models.profile import ProfileManager
from src.models.history import HistoryManager


class MainWindow(QMainWindow):
    """메인 윈도우 클래스"""
    
    # 시그널 정의
    profile_selected = Signal(int)  # 프로필 ID
    migration_requested = Signal(int)  # 프로필 ID
    
    def __init__(self):
        super().__init__()
        self.profile_manager = ProfileManager()
        self.history_manager = HistoryManager()
        self.current_profile_id = None
        self.log_viewer_dialog = None
        
        self.setup_ui()
        self.load_data()
        
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
        
    def load_data(self):
        """데이터 로드"""
        self.load_profiles()
        self.load_history()
        
    def load_profiles(self):
        """프로필 목록 로드"""
        self.profile_list.clear()
        profiles = self.profile_manager.get_all_profiles()
        
        for profile in profiles:
            item = QListWidgetItem(profile.name)
            item.setData(Qt.UserRole, profile.id)
            self.profile_list.addItem(item)
            
    def load_history(self):
        """작업 이력 로드"""
        self.history_table.setRowCount(0)
        histories = self.history_manager.get_all_history()
        
        for history in histories:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            
            # 프로필 이름 가져오기
            profile = self.profile_manager.get_profile(history.profile_id)
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
            
    def on_profile_selected(self):
        """프로필 선택 이벤트"""
        selected_items = self.profile_list.selectedItems()
        if selected_items:
            item = selected_items[0]
            self.current_profile_id = item.data(Qt.UserRole)
            self.edit_btn.setEnabled(True)
            self.delete_btn.setEnabled(True)
            self.migrate_btn.setEnabled(True)
            self.status_bar.showMessage(f"프로필 선택됨: {item.text()}")
        else:
            self.current_profile_id = None
            self.edit_btn.setEnabled(False)
            self.delete_btn.setEnabled(False)
            self.migrate_btn.setEnabled(False)
            
    def new_connection(self):
        """새 연결 생성"""
        dialog = ConnectionDialog(self)
        if dialog.exec():
            profile_data = dialog.get_profile_data()
            try:
                self.profile_manager.create_profile(profile_data)
                self.load_profiles()
                self.status_bar.showMessage("새 연결이 생성되었습니다.")
            except Exception as e:
                QMessageBox.critical(self, "오류", f"연결 생성 실패: {str(e)}")
                
    def edit_connection(self):
        """연결 편집"""
        if not self.current_profile_id:
            return
            
        profile = self.profile_manager.get_profile(self.current_profile_id)
        if not profile:
            return
            
        dialog = ConnectionDialog(self, profile)
        if dialog.exec():
            profile_data = dialog.get_profile_data()
            try:
                self.profile_manager.update_profile(self.current_profile_id, profile_data)
                self.load_profiles()
                self.status_bar.showMessage("연결이 수정되었습니다.")
            except Exception as e:
                QMessageBox.critical(self, "오류", f"연결 수정 실패: {str(e)}")
                
    def delete_connection(self):
        """연결 삭제"""
        if not self.current_profile_id:
            return
            
        reply = QMessageBox.question(
            self, "확인", 
            "선택한 연결을 삭제하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            try:
                self.profile_manager.delete_profile(self.current_profile_id)
                self.load_profiles()
                self.status_bar.showMessage("연결이 삭제되었습니다.")
            except Exception as e:
                QMessageBox.critical(self, "오류", f"연결 삭제 실패: {str(e)}")
                
    def start_migration(self):
        """마이그레이션 시작"""
        if not self.current_profile_id:
            return
            
        profile = self.profile_manager.get_profile(self.current_profile_id)
        if not profile:
            return
            
        # 마이그레이션 다이얼로그 표시
        dialog = MigrationDialog(self, profile)
        dialog.exec()
        
        # 완료 후 이력 새로고침
        self.load_history()
        
    def refresh_history(self):
        """작업 이력 새로고침"""
        self.load_history()
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