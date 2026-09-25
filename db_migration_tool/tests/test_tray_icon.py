"""
트레이 아이콘 기능 테스트
"""

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication

from src.ui.tray_icon import TrayIconManager


@pytest.fixture
def qapp():
    """QApplication 인스턴스 생성"""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def mock_main_window():
    """메인 윈도우 모킹"""
    window = MagicMock()
    window.show = MagicMock()
    window.refresh_history = MagicMock()
    return window


@pytest.fixture
def tray_manager(qapp, mock_main_window):
    """트레이 아이콘 매니저 인스턴스"""
    with patch("src.ui.tray_icon.QSystemTrayIcon") as mock_tray:
        mock_tray_instance = MagicMock()
        mock_tray_instance.isSystemTrayAvailable.return_value = True
        mock_tray_instance.supportsMessages.return_value = True
        mock_tray.return_value = mock_tray_instance

        manager = TrayIconManager(qapp, mock_main_window)
        manager.tray_icon = mock_tray_instance
        yield manager


class TestTrayIconManager:
    """트레이 아이콘 매니저 테스트"""

    def test_setup_success(self, qapp, mock_main_window):
        """트레이 아이콘 설정 성공 테스트"""
        with patch("src.ui.tray_icon.QSystemTrayIcon") as mock_tray:
            mock_tray_instance = MagicMock()
            mock_tray_instance.isSystemTrayAvailable.return_value = True
            mock_tray.return_value = mock_tray_instance

            manager = TrayIconManager(qapp, mock_main_window)
            result = manager.setup()

            assert result is True
            mock_tray_instance.show.assert_called_once()

    def test_setup_fail_no_system_tray(self, qapp, mock_main_window):
        """시스템 트레이 미지원 시 설정 실패 테스트"""
        with patch("src.ui.tray_icon.QSystemTrayIcon") as mock_tray:
            # isSystemTrayAvailable은 정적 메서드이므로 클래스에 패치
            mock_tray.isSystemTrayAvailable.return_value = False

            manager = TrayIconManager(qapp, mock_main_window)
            result = manager.setup()

            assert result is False
            # 트레이 아이콘이 생성되지 않았으므로 show 호출되지 않음
            mock_tray.return_value.show.assert_not_called()

    def test_migration_running_icon_change(self, tray_manager):
        """마이그레이션 실행 시 아이콘 변경 테스트"""
        # 실행 중으로 변경
        tray_manager.set_migration_running(True)
        assert tray_manager.is_migration_running is True
        tray_manager.tray_icon.setIcon.assert_called()

        # 기본 상태로 복원
        tray_manager.set_migration_running(False)
        assert tray_manager.is_migration_running is False
        tray_manager.tray_icon.setIcon.assert_called()

    def test_show_window_action(self, tray_manager, mock_main_window):
        """메인 윈도우 표시 액션 테스트"""
        # 시그널 연결
        tray_manager.show_window_requested.connect(mock_main_window.show)

        # 시그널 발행
        tray_manager.show_window_requested.emit()

        # 메인 윈도우가 표시되었는지 확인
        mock_main_window.show.assert_called_once()

    def test_show_history_action(self, tray_manager, mock_main_window):
        """이력 새로고침 액션 테스트"""
        # 시그널 연결
        tray_manager.show_history_requested.connect(mock_main_window.refresh_history)

        # 시그널 발행
        tray_manager.show_history_requested.emit()

        # 이력이 새로고침되었는지 확인
        mock_main_window.refresh_history.assert_called_once()

    def test_quit_action(self, tray_manager, qapp):
        """종료 액션 테스트"""
        # quit 모킹
        qapp.quit = MagicMock()

        # 시그널 연결
        tray_manager.quit_requested.connect(qapp.quit)

        # 시그널 발행
        tray_manager.quit_requested.emit()

        # 종료가 호출되었는지 확인
        qapp.quit.assert_called_once()

    def test_tooltip_update_on_running_state(self, tray_manager):
        """실행 상태에 따른 툴팁 업데이트 테스트"""
        # 기본 상태
        tray_manager.set_migration_running(False)
        tooltip_calls = [
            call[0][0] for call in tray_manager.tray_icon.setToolTip.call_args_list if call[0]
        ]
        assert any("DB Migration Tool" in tip for tip in tooltip_calls)

        # 실행 중 상태
        tray_manager.tray_icon.reset_mock()
        tray_manager.set_migration_running(True)
        tooltip_calls = [
            call[0][0] for call in tray_manager.tray_icon.setToolTip.call_args_list if call[0]
        ]
        assert any("마이그레이션 실행 중" in tip for tip in tooltip_calls)

    def test_first_minimize_notification(self, tray_manager):
        """첫 최소화 시 알림 테스트"""
        tray_manager.notify_first_minimize()

        # 첫 최소화 알림이 표시되었는지 확인
        call_args = tray_manager.tray_icon.showMessage.call_args
        assert "트레이로 최소화" in call_args[0][0]

    def test_icon_resources_exist(self):
        """아이콘 리소스 파일 존재 확인"""
        import os

        icon_base_path = "resources/icons"

        # 기본 아이콘
        assert os.path.exists(f"{icon_base_path}/app.ico") or os.path.exists(
            f"{icon_base_path}/app.png"
        ), "기본 아이콘 파일이 존재하지 않습니다"

        # 실행 중 아이콘
        assert os.path.exists(f"{icon_base_path}/app_running.ico") or os.path.exists(
            f"{icon_base_path}/app_running.png"
        ), "실행 중 아이콘 파일이 존재하지 않습니다"


class TestTrayQuitGracefulShutdown:
    """감사 M-13: 트레이 '종료'는 앱을 바로 끝내지 않고 종료 조정자에게 맡긴다."""

    def test_quit_requests_shutdown_instead_of_quitting_directly(self, tray_manager, qapp):
        qapp.quit = MagicMock()
        qapp.exit = MagicMock()
        requested = MagicMock()
        tray_manager.quit_requested.connect(requested)
        tray_icon = tray_manager.tray_icon

        tray_manager._quit_app()

        requested.assert_called_once()
        qapp.quit.assert_not_called()
        qapp.exit.assert_not_called()
        # 트레이는 워커가 멈추고 flush가 끝날 때까지 남아 '종료 중'을 보여 준다.
        assert tray_manager.tray_icon is tray_icon
        tray_icon.hide.assert_not_called()

    def test_quit_while_running_declined_does_nothing(self, tray_manager):
        from PySide6.QtWidgets import QMessageBox

        requested = MagicMock()
        tray_manager.quit_requested.connect(requested)
        tray_manager.is_migration_running = True
        with patch(
            "PySide6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.No
        ):
            tray_manager._quit_app()

        requested.assert_not_called()

    def test_quit_while_running_confirm_explains_graceful_stop(self, tray_manager):
        from PySide6.QtWidgets import QMessageBox

        requested = MagicMock()
        tray_manager.quit_requested.connect(requested)
        tray_manager.is_migration_running = True
        with patch(
            "PySide6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes
        ) as question:
            tray_manager._quit_app()

        requested.assert_called_once()
        text = question.call_args[0][2]
        assert "이어서" in text and "30" in text, text

    def test_show_shutting_down_updates_tooltip(self, tray_manager):
        tray_manager.show_shutting_down()
        tips = [c[0][0] for c in tray_manager.tray_icon.setToolTip.call_args_list if c[0]]
        assert any("종료 중" in tip for tip in tips)

    def test_run_state_change_during_shutdown_keeps_shutting_down_tooltip(self, tray_manager):
        """워커가 멈추며 보내는 '실행 끝' 알림이 '종료 중' 표시를 '대기 중'으로 되돌리지 않는다."""
        tray_manager.show_shutting_down()
        tray_manager.tray_icon.reset_mock()

        tray_manager.set_migration_running(False)

        tips = [c[0][0] for c in tray_manager.tray_icon.setToolTip.call_args_list if c[0]]
        assert not any("대기 중" in tip for tip in tips)
