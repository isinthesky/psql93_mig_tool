"""잠긴 프로필과 키 재설정 — UI 복구 경로(리뷰 지적 4).

복호화할 수 없는 프로필이 있어도 목록·이력은 보이고, 잠긴 프로필로는 작업을 시작할 수 없으며,
키를 쓸 수 없을 때는 명시적인 재설정 경로가 있어야 한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.models.profile import ConnectionProfile
from src.ui.main_window import MainWindow
from src.ui.viewmodels.main_viewmodel import MainViewModel


def _locked(name: str = "foreign") -> ConnectionProfile:
    return ConnectionProfile(id=3, name=name, locked=True, lock_reason="다른 키로 저장됨")


@pytest.fixture
def manager():
    m = Mock()
    m.get_all_profiles = Mock(return_value=[])
    m.key_available = True
    m.key_error = None
    return m


@pytest.fixture
def vm(manager):
    history = Mock()
    history.get_all_history = Mock(return_value=[])
    return MainViewModel(profile_manager=manager, history_manager=history)


class TestViewModelKeyProblem:
    def test_unavailable_key_is_reported_with_profiles_still_listed(self, vm, manager, qtbot):
        manager.key_available = False
        manager.key_error = "다른 장치의 키"
        manager.get_all_profiles.return_value = [_locked()]

        with qtbot.waitSignal(vm.key_problem, timeout=1000) as blocker:
            vm.load_profiles()

        assert "다른 장치의 키" in blocker.args[0]
        assert [p.name for p in vm.profiles] == ["foreign"]

    def test_usable_key_emits_no_key_problem(self, vm, manager, qtbot):
        with qtbot.assertNotEmitted(vm.key_problem):
            vm.load_profiles()

    def test_reset_encryption_key_resets_and_reloads(self, vm, manager):
        assert vm.reset_encryption_key() is True
        manager.reset_encryption_key.assert_called_once()
        manager.get_all_profiles.assert_called_once()

    def test_reset_failure_is_reported(self, vm, manager, qtbot):
        manager.reset_encryption_key.side_effect = RuntimeError("재설정 실패")
        with qtbot.waitSignal(vm.error_occurred, timeout=1000):
            assert vm.reset_encryption_key() is False


class TestMainWindowLockedProfile:
    @pytest.fixture
    def window(self):
        win = MagicMock()
        win.license_state = None
        return win

    def test_locked_profile_cannot_start_migration(self, window):
        window.vm.current_profile = _locked()
        with (
            patch("src.ui.main_window.MigrationWizardDialog") as wizard,
            patch("src.ui.main_window.FileArchiveMigrationDialog") as archive,
        ):
            MainWindow.start_migration(window)

        window.show_error.assert_called_once()
        assert "잠긴" in window.show_error.call_args[0][0]
        wizard.assert_not_called()
        archive.assert_not_called()

    def test_selecting_locked_profile_keeps_edit_and_delete_but_not_migrate(self, window):
        MainWindow.update_profile_selection(window, _locked())

        window.edit_btn.setEnabled.assert_called_with(True)
        window.delete_btn.setEnabled.assert_called_with(True)
        window.migrate_btn.setEnabled.assert_called_with(False)

    def test_locked_profile_is_marked_in_list(self, qtbot):
        from PySide6.QtWidgets import QListWidget

        window = MagicMock()
        window.profile_list = QListWidget()
        qtbot.addWidget(window.profile_list)
        window._endpoint_summary.return_value = "PostgreSQL → PostgreSQL"

        MainWindow.update_profile_list(window, [_locked("foreign")])

        item = window.profile_list.item(0)
        assert item.text().startswith("[잠김]")
        assert "다른 키로 저장됨" in item.toolTip()

    def test_key_problem_offers_reset_and_resets_on_confirm(self, window):
        window._confirm_key_reset.return_value = True
        MainWindow.on_key_problem(window, "다른 장치의 키")
        window._confirm_key_reset.assert_called_once_with("다른 장치의 키")
        window.vm.reset_encryption_key.assert_called_once()

    def test_key_problem_declined_changes_nothing(self, window):
        window._confirm_key_reset.return_value = False
        MainWindow.on_key_problem(window, "다른 장치의 키")
        window.vm.reset_encryption_key.assert_not_called()

    def test_key_problem_is_offered_once_per_session(self, window):
        window._confirm_key_reset.return_value = False
        MainWindow.on_key_problem(window, "첫 번째")
        MainWindow.on_key_problem(window, "두 번째")
        window._confirm_key_reset.assert_called_once_with("첫 번째")


class TestHistoryDialogNameLookup:
    def test_profile_name_is_resolved_without_decryption(self, qtbot):
        with (
            patch("src.ui.dialogs.history_dialog.HistoryManager"),
            patch("src.ui.dialogs.history_dialog.ProfileManager") as pm_cls,
        ):
            from src.ui.dialogs.history_dialog import HistoryDialog

            pm = pm_cls.return_value
            pm.get_profile_name.return_value = "foreign"
            pm.get_profile.side_effect = AssertionError("이름 조회에 복호화가 필요 없다")
            dialog = HistoryDialog()
            qtbot.addWidget(dialog)

            assert dialog._resolve_profile_name(3) == "foreign"
            pm.get_profile_name.return_value = None
            dialog._profile_name_cache.clear()
            assert dialog._resolve_profile_name(4) == "알 수 없음"
