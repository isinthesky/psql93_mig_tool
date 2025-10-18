"""
트레이 아이콘 기능 테스트
"""

import pytest
from unittest.mock import MagicMock, patch
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from PySide6.QtGui import QIcon

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

    def test_notify_migration_started(self, tray_manager):
        """마이그레이션 시작 알림 테스트"""
        profile_name = "TestProfile"

        tray_manager.notify_migration_started(profile_name)

        # 실행 중 상태로 변경되었는지 확인
        assert tray_manager.is_migration_running is True

        # 알림이 표시되었는지 확인
        tray_manager.tray_icon.showMessage.assert_called_once()
        call_args = tray_manager.tray_icon.showMessage.call_args
        assert "마이그레이션 시작" in call_args[0][0]
        assert profile_name in call_args[0][1]

    def test_notify_migration_completed(self, tray_manager):
        """마이그레이션 완료 알림 테스트"""
        profile_name = "TestProfile"
        rows_processed = 100000

        # 먼저 실행 중 상태로 설정
        tray_manager.set_migration_running(True)

        # 완료 알림
        tray_manager.notify_migration_completed(profile_name, rows_processed)

        # 기본 상태로 복원되었는지 확인
        assert tray_manager.is_migration_running is False

        # 알림이 표시되었는지 확인
        call_args = tray_manager.tray_icon.showMessage.call_args
        assert "마이그레이션 완료" in call_args[0][0]
        assert profile_name in call_args[0][1]
        assert "100,000" in call_args[0][1]  # 포맷된 숫자

    def test_notify_migration_error(self, tray_manager):
        """마이그레이션 오류 알림 테스트"""
        error_message = "연결 실패"

        # 먼저 실행 중 상태로 설정
        tray_manager.set_migration_running(True)

        # 오류 알림
        tray_manager.notify_migration_error(error_message)

        # 기본 상태로 복원되었는지 확인
        assert tray_manager.is_migration_running is False

        # 알림이 표시되었는지 확인
        call_args = tray_manager.tray_icon.showMessage.call_args
        assert "마이그레이션 오류" in call_args[0][0]
        assert error_message in call_args[0][1]

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
            call[0][0]
            for call in tray_manager.tray_icon.setToolTip.call_args_list
            if call[0]
        ]
        assert any("DB Migration Tool" in tip for tip in tooltip_calls)

        # 실행 중 상태
        tray_manager.tray_icon.reset_mock()
        tray_manager.set_migration_running(True)
        tooltip_calls = [
            call[0][0]
            for call in tray_manager.tray_icon.setToolTip.call_args_list
            if call[0]
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


class TestTrayIconIntegration:
    """트레이 아이콘 통합 테스트"""

    def test_migration_lifecycle(self, tray_manager):
        """마이그레이션 전체 생명주기 테스트"""
        profile_name = "TestProfile"
        rows_processed = 50000

        # 1. 초기 상태 확인
        assert tray_manager.is_migration_running is False

        # 2. 마이그레이션 시작
        tray_manager.notify_migration_started(profile_name)
        assert tray_manager.is_migration_running is True

        # 3. 마이그레이션 완료
        tray_manager.notify_migration_completed(profile_name, rows_processed)
        assert tray_manager.is_migration_running is False

    def test_migration_error_lifecycle(self, tray_manager):
        """마이그레이션 오류 발생 시 생명주기 테스트"""
        profile_name = "TestProfile"
        error_msg = "DB 연결 실패"

        # 1. 마이그레이션 시작
        tray_manager.notify_migration_started(profile_name)
        assert tray_manager.is_migration_running is True

        # 2. 오류 발생
        tray_manager.notify_migration_error(error_msg)
        assert tray_manager.is_migration_running is False

    def test_multiple_migrations(self, tray_manager):
        """여러 마이그레이션 연속 실행 테스트"""
        # 첫 번째 마이그레이션
        tray_manager.notify_migration_started("Profile1")
        assert tray_manager.is_migration_running is True

        tray_manager.notify_migration_completed("Profile1", 10000)
        assert tray_manager.is_migration_running is False

        # 두 번째 마이그레이션
        tray_manager.notify_migration_started("Profile2")
        assert tray_manager.is_migration_running is True

        tray_manager.notify_migration_completed("Profile2", 20000)
        assert tray_manager.is_migration_running is False
