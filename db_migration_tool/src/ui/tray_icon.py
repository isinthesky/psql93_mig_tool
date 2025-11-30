"""
시스템 트레이 아이콘 관리자
macOS와 Windows에서 시스템 트레이 아이콘 기능 제공
"""

import sys

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class TrayIconManager(QObject):
    """시스템 트레이 아이콘 관리자

    기능:
    - 트레이 아이콘 표시/숨김
    - 컨텍스트 메뉴 제공 (열기, 최근 이력, 종료 등)
    - 마이그레이션 상태 알림
    - 동적 아이콘 변경 (대기 중/실행 중)
    """

    # 시그널 정의
    show_window_requested = Signal()  # 윈도우 표시 요청
    show_history_requested = Signal()  # 이력 보기 요청
    quit_requested = Signal()  # 종료 요청

    def __init__(self, app, main_window):
        """트레이 아이콘 관리자 초기화

        Args:
            app: QApplication 인스턴스
            main_window: MainWindow 인스턴스
        """
        super().__init__()
        self.app = app
        self.main_window = main_window
        self.tray_icon = None
        self.is_migration_running = False

        # 아이콘 경로 저장 (main.py의 get_resource_path 사용)
        from src.main import get_resource_path

        self.icon_normal = get_resource_path("resources/icons/app.ico")
        self.icon_running = get_resource_path("resources/icons/app_running.ico")

    def setup(self) -> bool:
        """트레이 아이콘 초기화

        Returns:
            성공 여부
        """
        # 시스템 트레이 가용성 확인
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("Warning: System tray is not available on this platform")
            return False

        # 트레이 아이콘 생성
        self.tray_icon = QSystemTrayIcon(self.app)
        self._set_normal_icon()
        self.tray_icon.setToolTip("DB Migration Tool - 대기 중")

        # 메뉴 생성
        self._create_menu()

        # 시그널 연결
        self.tray_icon.activated.connect(self._on_activated)
        self.tray_icon.messageClicked.connect(self._on_message_clicked)

        # 표시
        self.tray_icon.show()
        return True

    def _create_menu(self):
        """컨텍스트 메뉴 생성"""
        menu = QMenu()

        # === 윈도우 제어 ===
        show_action = QAction("열기", self.app)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)

        menu.addSeparator()

        # === 빠른 작업 ===
        quick_menu = menu.addMenu("빠른 작업")

        # 마이그레이션 작업 설정
        start_action = QAction("마이그레이션 작업 설정", self.app)
        start_action.triggered.connect(self._quick_start_migration)
        quick_menu.addAction(start_action)

        # 최근 이력 보기
        history_action = QAction("최근 이력 보기", self.app)
        history_action.triggered.connect(self._show_history)
        quick_menu.addAction(history_action)

        # 로그 뷰어
        log_action = QAction("로그 뷰어 열기", self.app)
        log_action.triggered.connect(self._show_logs)
        quick_menu.addAction(log_action)

        menu.addSeparator()

        # === 정보 ===
        about_action = QAction("정보", self.app)
        about_action.triggered.connect(self._show_about)
        menu.addAction(about_action)

        menu.addSeparator()

        # === 종료 ===
        quit_action = QAction("종료", self.app)
        quit_action.triggered.connect(self._quit_app)
        menu.addAction(quit_action)

        self.tray_icon.setContextMenu(menu)

    def _on_activated(self, reason):
        """트레이 아이콘 클릭 처리

        Args:
            reason: 클릭 이유 (QSystemTrayIcon.ActivationReason)
        """
        # macOS에서는 컨텍스트 메뉴 설정 시 더블클릭 이벤트가 발생하지 않음
        # Windows/Linux에서만 더블클릭으로 윈도우 표시
        if sys.platform != "darwin":
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
                self._show_window()
            elif reason == QSystemTrayIcon.ActivationReason.Trigger:
                # 싱글 클릭은 토글 동작 (선택적)
                if self.main_window.isVisible():
                    self.main_window.hide()
                else:
                    self._show_window()

    def _show_window(self):
        """메인 윈도우 표시"""
        self.main_window.show()
        self.main_window.raise_()
        self.main_window.activateWindow()
        self.show_window_requested.emit()

    def _quick_start_migration(self):
        """빠른 마이그레이션 시작"""
        self._show_window()
        # MainWindow에 마이그레이션 시작 메서드가 있다면 호출
        # self.main_window.start_quick_migration()

    def _show_history(self):
        """최근 이력 보기"""
        self._show_window()
        self.show_history_requested.emit()

    def _show_logs(self):
        """로그 뷰어 열기"""
        # MainWindow의 로그 뷰어 다이얼로그 열기
        if hasattr(self.main_window, "show_log_viewer"):
            self.main_window.show_log_viewer()

    def _show_about(self):
        """정보 다이얼로그 표시"""
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.about(
            self.main_window,
            "DB Migration Tool 정보",
            "<h3>DB Migration Tool</h3>"
            "<p>PostgreSQL 파티션 테이블 마이그레이션 도구</p>"
            "<p>버전: 1.0.0</p>"
            "<p>PySide6 기반 데스크톱 애플리케이션</p>",
        )

    def _quit_app(self):
        """애플리케이션 종료"""
        # 마이그레이션 실행 중이면 확인 메시지 표시
        if self.is_migration_running:
            from PySide6.QtWidgets import QMessageBox

            reply = QMessageBox.question(
                self.main_window,
                "종료 확인",
                "마이그레이션이 실행 중입니다.\n정말 종료하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )

            if reply == QMessageBox.StandardButton.No:
                return

        self.quit_requested.emit()
        self.cleanup()
        self.app.quit()

    def _on_message_clicked(self):
        """알림 메시지 클릭 처리"""
        self._show_window()

    # === 공개 메서드: 외부에서 호출 ===

    def show_message(
        self, title: str, message: str, icon=QSystemTrayIcon.MessageIcon.Information, duration: int = 3000
    ):
        """시스템 알림 표시

        Args:
            title: 알림 제목
            message: 알림 메시지
            icon: 아이콘 타입
            duration: 표시 시간 (밀리초)
        """
        if self.tray_icon and self.tray_icon.supportsMessages():
            self.tray_icon.showMessage(title, message, icon, duration)

    def set_migration_running(self, is_running: bool):
        """마이그레이션 실행 상태 설정

        Args:
            is_running: 실행 중 여부
        """
        self.is_migration_running = is_running

        if is_running:
            self._set_running_icon()
            self.tray_icon.setToolTip("DB Migration Tool - 마이그레이션 실행 중")
        else:
            self._set_normal_icon()
            self.tray_icon.setToolTip("DB Migration Tool - 대기 중")

    def notify_migration_started(self, profile_name: str):
        """마이그레이션 시작 알림

        Args:
            profile_name: 프로필 이름
        """
        self.set_migration_running(True)
        self.show_message(
            "마이그레이션 시작",
            f"프로필: {profile_name}\n마이그레이션을 시작합니다.",
            QSystemTrayIcon.MessageIcon.Information,
        )

    def notify_migration_completed(self, profile_name: str, rows_processed: int):
        """마이그레이션 완료 알림

        Args:
            profile_name: 프로필 이름
            rows_processed: 처리된 행 수
        """
        self.set_migration_running(False)
        self.show_message(
            "마이그레이션 완료",
            f"프로필: {profile_name}\n{rows_processed:,}개 행 처리 완료",
            QSystemTrayIcon.MessageIcon.Information,
            5000,  # 5초간 표시
        )

    def notify_migration_error(self, error_message: str):
        """마이그레이션 오류 알림

        Args:
            error_message: 오류 메시지
        """
        self.set_migration_running(False)
        self.show_message(
            "마이그레이션 오류", f"오류가 발생했습니다:\n{error_message}", QSystemTrayIcon.MessageIcon.Critical, 5000
        )

    def notify_first_minimize(self):
        """첫 최소화 시 안내 메시지"""
        self.show_message(
            "트레이로 최소화",
            "프로그램이 트레이로 최소화되었습니다.\n"
            "트레이 아이콘을 우클릭하여 메뉴를 열 수 있습니다.\n"
            "종료하려면 메뉴에서 '종료'를 선택하세요.",
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

    def _set_normal_icon(self):
        """일반 아이콘 설정"""
        import os

        if os.path.exists(self.icon_normal):
            self.tray_icon.setIcon(QIcon(self.icon_normal))
        else:
            # 기본 아이콘이 없으면 애플리케이션 아이콘 사용
            self.tray_icon.setIcon(self.app.windowIcon())

    def _set_running_icon(self):
        """실행 중 아이콘 설정"""
        import os

        if os.path.exists(self.icon_running):
            self.tray_icon.setIcon(QIcon(self.icon_running))
        else:
            # 실행 중 아이콘이 없으면 일반 아이콘 사용
            self._set_normal_icon()

    def cleanup(self):
        """트레이 아이콘 정리"""
        if self.tray_icon:
            self.tray_icon.hide()
            self.tray_icon.setContextMenu(None)
            self.tray_icon.deleteLater()
            self.tray_icon = None
