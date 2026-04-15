"""main.py 실행 스모크 테스트"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from cryptography.fernet import Fernet

import src.main as main_module
from src.utils.app_paths import AppPaths
from src.utils.master_password import MasterPasswordService


class FakeSharedMemory:
    """QSharedMemory 대체용 테스트 더블"""

    def __init__(self, _key):
        self.created = True

    def create(self, _size):
        return self.created


class FakeApp:
    """QApplication 대체용 테스트 더블"""

    def __init__(self):
        self.setQuitOnLastWindowClosed = MagicMock()
        self.exec = MagicMock(return_value=0)
        self.quit = MagicMock()


def test_ensure_authenticated_first_setup_flow(monkeypatch):
    """최초 실행 시 마스터 비밀번호 설정 후 인증되는지 확인"""
    with tempfile.TemporaryDirectory() as tmpdir:
        AppPaths.set_custom_root(Path(tmpdir))
        MasterPasswordService.lock()

        class FakeSetupDialog:
            def __init__(self, mode="unlock", parent=None):
                self.mode = mode

            def exec(self):
                return 1

            def get_password(self):
                return "2468"

        profile_manager = MagicMock()
        profile_manager.reencrypt_all_profiles.return_value = 2
        saved_connection_manager = MagicMock()
        saved_connection_manager.reencrypt_all_saved_connections.return_value = 3

        monkeypatch.setattr(main_module, "MasterPasswordDialog", FakeSetupDialog)
        monkeypatch.setattr(
            main_module,
            "ProfileManager",
            MagicMock(return_value=profile_manager),
        )
        monkeypatch.setattr(
            main_module,
            "SavedConnectionManager",
            MagicMock(return_value=saved_connection_manager),
        )
        monkeypatch.setattr(main_module.QMessageBox, "information", MagicMock())
        monkeypatch.setattr(main_module.QMessageBox, "critical", MagicMock())

        auth_service = main_module.ensure_authenticated()

        assert auth_service is not None
        assert auth_service.is_authenticated() is True
        profile_manager.reencrypt_all_profiles.assert_called_once()
        saved_connection_manager.reencrypt_all_saved_connections.assert_called_once()
        main_module.QMessageBox.information.assert_called_once()

        MasterPasswordService.lock()
        AppPaths.set_custom_root(None)


def test_main_starts_window_after_successful_auth(monkeypatch):
    """인증 성공 시 메인 윈도우가 표시되고 앱이 실행되는지 확인"""
    fake_app = FakeApp()
    fake_auth_service = MagicMock()
    fake_auth_service.get_active_cipher_suite.return_value = Fernet(Fernet.generate_key())

    fake_window = MagicMock()
    fake_window.show = MagicMock()

    fake_tray_manager = MagicMock()
    fake_tray_manager.setup.return_value = False

    monkeypatch.setattr(main_module, "initialize_application", lambda: fake_app)
    monkeypatch.setattr(main_module, "initialize_database", lambda: None)
    monkeypatch.setattr(main_module, "ensure_authenticated", lambda: fake_auth_service)
    monkeypatch.setattr(main_module, "QSharedMemory", FakeSharedMemory)
    monkeypatch.setattr(main_module, "MainWindow", MagicMock(return_value=fake_window))

    import src.ui.tray_icon as tray_icon_module

    monkeypatch.setattr(
        tray_icon_module,
        "TrayIconManager",
        MagicMock(return_value=fake_tray_manager),
    )

    result = main_module.main()

    assert result == 0
    fake_app.setQuitOnLastWindowClosed.assert_called_once_with(False)
    main_module.MainWindow.assert_called_once()
    fake_window.show.assert_called_once()
    fake_app.exec.assert_called_once()
