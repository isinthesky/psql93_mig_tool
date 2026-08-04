"""
메인 윈도우 UI
"""

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from src.ui.dialogs.connection_dialog import ConnectionDialog
from src.ui.dialogs.file_archive_migration_dialog import FileArchiveMigrationDialog
from src.ui.dialogs.history_dialog import HistoryDialog
from src.ui.dialogs.log_viewer_dialog import LogViewerDialog
from src.ui.dialogs.migration_wizard_dialog import MigrationWizardDialog
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
        self.history_dialog = None

        # 트레이 아이콘 관련
        self.tray_icon = None  # TrayIconManager 인스턴스 (main.py에서 설정)
        self.minimize_to_tray = True  # 트레이 최소화 활성화 (설정으로 관리 가능)
        self.first_minimize = True  # 첫 최소화 여부

        # UI 구성
        self.setup_ui()
        self.bind_viewmodel()

        # 초기 데이터 로드
        self.vm.initialize()
        self.refresh_ui_from_vm()

    def setup_ui(self):
        """UI 초기화"""
        self.setWindowTitle("DB 마이그레이션 도구")
        self.setGeometry(100, 100, 480, 600)

        # 중앙 위젯
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # 메인 레이아웃
        main_layout = QHBoxLayout(central_widget)
        main_layout.addWidget(self.create_profile_panel())

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
        new_connection_action.setStatusTip("새 연결 프로필을 만듭니다.")
        new_connection_action.setToolTip("새 연결 프로필 만들기 (Ctrl+N)")
        new_connection_action.triggered.connect(self.new_connection)
        toolbar.addAction(new_connection_action)

        # 편집 액션
        edit_action = QAction("편집", self)
        edit_action.setShortcut("Ctrl+E")
        edit_action.setStatusTip("선택한 연결 프로필을 편집합니다.")
        edit_action.setToolTip("선택한 연결 프로필 편집 (Ctrl+E)")
        edit_action.triggered.connect(self.edit_connection)
        toolbar.addAction(edit_action)

        # 삭제 액션
        delete_action = QAction("삭제", self)
        delete_action.setShortcut("Delete")
        delete_action.setStatusTip("선택한 연결 프로필을 삭제합니다. 작업 이력은 삭제하지 않습니다.")
        delete_action.setToolTip("선택한 연결 프로필 삭제 (작업 이력 삭제 아님)")
        delete_action.triggered.connect(self.delete_connection)
        toolbar.addAction(delete_action)

        toolbar.addSeparator()

        # 마이그레이션 작업 설정 액션
        self.migrate_action = QAction("마이그레이션 작업 설정", self)
        self.migrate_action.setShortcut("F5")
        self.migrate_action.setStatusTip("선택한 프로필로 마이그레이션 범위와 실행 옵션을 설정합니다.")
        self.migrate_action.setToolTip("프로필을 선택한 뒤 마이그레이션 작업을 설정합니다. (F5)")
        self.migrate_action.triggered.connect(self.start_migration)
        toolbar.addAction(self.migrate_action)

        toolbar.addSeparator()

        # 작업 이력 액션
        history_action = QAction("작업 이력", self)
        history_action.setShortcut("Ctrl+H")
        history_action.setStatusTip("마이그레이션 실행 이력을 확인합니다.")
        history_action.setToolTip("작업 이력 보기 (Ctrl+H)")
        history_action.triggered.connect(self.show_history_dialog)
        toolbar.addAction(history_action)

        # 로그 뷰어 액션
        log_viewer_action = QAction("로그 뷰어", self)
        log_viewer_action.setShortcut("Ctrl+L")
        log_viewer_action.setStatusTip("애플리케이션 로그를 확인합니다.")
        log_viewer_action.setToolTip("로그 뷰어 열기 (Ctrl+L)")
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

        # 색/크기는 전역 테마(src/ui/theme.py)에서 objectName·variant로 받는다.
        self.new_btn = QPushButton("새 연결")
        self.new_btn.setObjectName("newProfileButton")
        self.new_btn.setToolTip("새 연결 프로필을 만듭니다.")
        self.new_btn.clicked.connect(self.new_connection)
        button_layout.addWidget(self.new_btn)

        self.edit_btn = QPushButton("편집")
        self.edit_btn.setToolTip("연결 프로필을 선택하면 편집할 수 있습니다.")
        self.edit_btn.clicked.connect(self.edit_connection)
        self.edit_btn.setEnabled(False)
        button_layout.addWidget(self.edit_btn)

        self.delete_btn = QPushButton("삭제")
        self.delete_btn.setObjectName("dangerAction")
        self.delete_btn.setToolTip("연결 프로필을 선택하면 삭제할 수 있습니다. 작업 이력은 삭제하지 않습니다.")
        self.delete_btn.clicked.connect(self.delete_connection)
        self.delete_btn.setEnabled(False)
        button_layout.addWidget(self.delete_btn)

        layout.addLayout(button_layout)

        # 마이그레이션 작업 설정 버튼
        self.migrate_btn = QPushButton("마이그레이션 작업 설정")
        self.migrate_btn.setObjectName("migrationButton")
        self.migrate_btn.setToolTip("연결 프로필을 선택하면 마이그레이션 작업을 설정할 수 있습니다.")
        self.migrate_btn.clicked.connect(self.start_migration)
        self.migrate_btn.setEnabled(False)
        layout.addWidget(self.migrate_btn)

        for btn in (self.new_btn, self.edit_btn, self.delete_btn, self.migrate_btn):
            btn.setProperty("variant", "large")

        group.setLayout(layout)
        return group

    def bind_viewmodel(self):
        """ViewModel 시그널 바인딩"""
        # ViewModel → UI 시그널
        self.vm.profiles_changed.connect(self.update_profile_list)
        self.vm.current_profile_changed.connect(self.update_profile_selection)
        self.vm.error_occurred.connect(self.show_error)
        self.vm.message_sent.connect(self.show_message)

        # UI → ViewModel 시그널
        self.profile_list.itemSelectionChanged.connect(self.on_profile_selected)

    def _migration_mode_text(self, profile) -> str:
        if not profile:
            return "마이그레이션 작업 설정"
        mode = profile.migration_mode
        return {
            "postgres_to_postgres": "DB → DB 마이그레이션",
            "postgres_to_file": "DB → 파일 아카이브 내보내기",
            "file_to_postgres": "파일 아카이브 → DB 가져오기",
        }.get(mode, "마이그레이션 작업 설정")

    def _endpoint_summary(self, profile) -> str:
        if not profile:
            return ""
        source = "PostgreSQL" if profile.source_kind == "postgres" else "File Archive"
        target = "PostgreSQL" if profile.target_kind == "postgres" else "File Archive"
        return f"{source} → {target}"

    def refresh_ui_from_vm(self):
        """ViewModel 상태를 기반으로 UI 초기화"""
        has_profile = self.vm.current_profile is not None
        self.edit_btn.setEnabled(has_profile)
        self.delete_btn.setEnabled(has_profile)
        self.migrate_btn.setEnabled(has_profile)
        label = self._migration_mode_text(self.vm.current_profile)
        self.migrate_btn.setText(label)
        default_migrate_tip = "연결 프로필을 선택하면 마이그레이션 작업을 설정할 수 있습니다."
        self.migrate_btn.setToolTip(default_migrate_tip)
        self.edit_btn.setToolTip("연결 프로필을 선택하면 편집할 수 있습니다.")
        self.delete_btn.setToolTip("연결 프로필을 선택하면 삭제할 수 있습니다. 작업 이력은 삭제하지 않습니다.")
        if hasattr(self, "migrate_action"):
            self.migrate_action.setText(label)
            self.migrate_action.setToolTip(default_migrate_tip)

    def update_profile_list(self, profiles):
        """프로필 목록 UI 업데이트"""
        self.profile_list.clear()

        if not profiles:
            # 빈 목록은 막다른 화면이 아니라 다음 할 일을 알려주는 자리다.
            empty = QListWidgetItem("연결 프로필이 없습니다. ‘새 연결’로 시작하세요.")
            empty.setFlags(Qt.NoItemFlags)
            self.profile_list.addItem(empty)
            return

        for profile in profiles:
            summary = self._endpoint_summary(profile)
            item = QListWidgetItem(f"{profile.name}  [{summary}]")
            item.setToolTip(
                f"프로필: {profile.name}\n"
                f"마이그레이션 경로: {summary}\n"
                f"실행 동작: {self._migration_mode_text(profile)}"
            )
            item.setData(Qt.UserRole, profile.id)
            self.profile_list.addItem(item)

    def update_profile_selection(self, profile):
        """프로필 선택 UI 업데이트"""
        has_profile = profile is not None
        self.edit_btn.setEnabled(has_profile)
        self.delete_btn.setEnabled(has_profile)
        self.migrate_btn.setEnabled(has_profile)

        label = self._migration_mode_text(profile)
        self.migrate_btn.setText(label)
        default_migrate_tip = "연결 프로필을 선택하면 마이그레이션 작업을 설정할 수 있습니다."
        if has_profile:
            migrate_tip = f"선택한 프로필 '{profile.name}'로 {label}을 시작합니다."
            self.edit_btn.setToolTip(f"선택한 프로필 '{profile.name}'의 연결 정보를 편집합니다.")
            self.delete_btn.setToolTip(
                f"선택한 프로필 '{profile.name}'을 삭제합니다. 작업 이력은 삭제하지 않습니다."
            )
        else:
            migrate_tip = default_migrate_tip
            self.edit_btn.setToolTip("연결 프로필을 선택하면 편집할 수 있습니다.")
            self.delete_btn.setToolTip("연결 프로필을 선택하면 삭제할 수 있습니다. 작업 이력은 삭제하지 않습니다.")
        self.migrate_btn.setToolTip(migrate_tip)
        if hasattr(self, "migrate_action"):
            self.migrate_action.setText(label)
            self.migrate_action.setToolTip(migrate_tip)

        if has_profile:
            self.status_bar.showMessage(
                f"프로필 선택됨: {profile.name} · {self._endpoint_summary(profile)}"
            )
        else:
            self.status_bar.showMessage("준비")

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

        # 무엇을 지우는지, 무엇이 남는지를 확인 창에서 그대로 말한다.
        reply = QMessageBox.question(
            self,
            "연결 프로필 삭제",
            f"'{self.vm.current_profile.name}' 연결 프로필을 삭제할까요?\n\n"
            "작업 이력과 저장된 연결은 삭제되지 않습니다.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if reply == QMessageBox.Yes:
            self.vm.delete_profile(self.vm.current_profile.id)

    def start_migration(self):
        """마이그레이션 시작"""
        if not self.vm.current_profile:
            return

        profile = self.vm.current_profile
        self.status_bar.showMessage(
            f"{self._migration_mode_text(profile)} 실행 준비 · {self._endpoint_summary(profile)}"
        )
        if profile.source_kind == "postgres" and profile.target_kind == "postgres":
            dialog = MigrationWizardDialog(self, profile)
        else:
            dialog = FileArchiveMigrationDialog(self, profile)
        dialog.exec()

        if self.history_dialog and not self.history_dialog.isHidden():
            self.history_dialog.refresh()

    def show_history_dialog(self):
        """작업 이력 다이얼로그 표시 (modeless 싱글톤)"""
        if self.history_dialog is None:
            self.history_dialog = HistoryDialog(self)

        if self.history_dialog.isHidden():
            self.history_dialog.show()
        else:
            self.history_dialog.refresh()
            self.history_dialog.raise_()
            self.history_dialog.activateWindow()

    def show_log_viewer(self):
        """로그 뷰어 표시"""
        if self.log_viewer_dialog is None:
            self.log_viewer_dialog = LogViewerDialog(self)

        if self.log_viewer_dialog.isHidden():
            self.log_viewer_dialog.show()
        else:
            self.log_viewer_dialog.raise_()
            self.log_viewer_dialog.activateWindow()

    # === 트레이 아이콘 관련 메서드 ===

    def closeEvent(self, event: QCloseEvent):
        """윈도우 닫기 이벤트 처리

        트레이 아이콘이 활성화되어 있으면 트레이로 최소화
        """
        if self.minimize_to_tray and self.tray_icon:
            # 트레이로 최소화
            event.ignore()
            self.hide()

            # 첫 최소화 시 안내 메시지
            if self.first_minimize:
                self.tray_icon.notify_first_minimize()
                self.first_minimize = False
        else:
            # 실제 종료
            event.accept()

    def changeEvent(self, event: QEvent):
        """윈도우 상태 변경 이벤트

        최소화 버튼 클릭 시 트레이로 숨김
        """
        if event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized() and self.minimize_to_tray and self.tray_icon:
                # 최소화 시 트레이로 숨김 (약간의 지연 후)
                QTimer.singleShot(0, self.hide)
                event.ignore()
                return

        super().changeEvent(event)
