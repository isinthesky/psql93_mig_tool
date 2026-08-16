"""제한 모드 — 재개는 열리고 신규는 막히는가.

설계 §2.1의 핵심이다. 이게 뒤집히면 만료된 고객사에서 중단된 마이그레이션을
복구할 수 없고, 소스와 대상이 어긋난 채 남는다.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.licensing import LicenseState, LicenseStatus
from src.ui.dialogs.file_archive_migration_dialog import FileArchiveMigrationDialog
from src.ui.dialogs.migration_wizard_dialog import MigrationWizardDialog
from src.ui.main_window import MainWindow


def _state(status: LicenseStatus) -> LicenseState:
    return LicenseState(status, message="테스트 상태", machine_id="m-1")


@pytest.fixture
def window():
    """MainWindow 를 만들지 않고 메서드만 떼어 검사한다.

    실제 창을 띄우면 DB·프로필 로딩까지 끌려와 이 테스트가 보려는
    '분기 하나'가 묻힌다.
    """
    win = MagicMock()
    win.license_state = None
    win.vm.current_profile = MagicMock(id=1, source_kind="postgres", target_kind="postgres")
    return win


class TestResumableDetection:
    def test_incomplete_history_means_resumable(self, window):
        window.vm.history_manager.get_incomplete_history.return_value = MagicMock(id=7)
        assert MainWindow._has_resumable_work(window, window.vm.current_profile) is True

    def test_no_incomplete_history_means_not_resumable(self, window):
        window.vm.history_manager.get_incomplete_history.return_value = None
        assert MainWindow._has_resumable_work(window, window.vm.current_profile) is False

    def test_lookup_failure_is_treated_as_resumable(self, window):
        """조회가 깨졌다고 재개를 막으면 복구 경로가 사라진다. 안전한 쪽은 열어 주는 쪽."""
        window.vm.history_manager.get_incomplete_history.side_effect = RuntimeError("db down")
        assert MainWindow._has_resumable_work(window, window.vm.current_profile) is True


class TestStartMigrationGate:
    """`start_migration` 이 제한 모드에서 어떻게 갈리는지."""

    @pytest.mark.parametrize(
        "status",
        [
            LicenseStatus.EXPIRED,
            LicenseStatus.MISSING,
            LicenseStatus.INVALID,
            LicenseStatus.WRONG_MACHINE,
        ],
    )
    def test_restricted_blocks_new_work(self, window, status):
        window.license_state = _state(status)
        window._has_resumable_work.return_value = False

        MainWindow.start_migration(window)

        window._explain_restricted_mode.assert_called_once()
        window.status_bar.showMessage.assert_not_called()

    @pytest.mark.parametrize(
        "status",
        [
            LicenseStatus.EXPIRED,
            LicenseStatus.MISSING,
            LicenseStatus.INVALID,
            LicenseStatus.WRONG_MACHINE,
        ],
    )
    def test_restricted_still_allows_resume(self, window, status):
        """만료돼도 이어서 끝낼 작업이 있으면 마법사가 열려야 한다."""
        window.license_state = _state(status)
        window._has_resumable_work.return_value = True

        with patch("src.ui.main_window.MigrationWizardDialog") as wizard:
            MainWindow.start_migration(window)

        window._explain_restricted_mode.assert_not_called()
        wizard.assert_called_once()

    @pytest.mark.parametrize("status", [LicenseStatus.VALID, LicenseStatus.EXPIRING])
    def test_valid_license_opens_normally(self, window, status):
        window.license_state = _state(status)

        with patch("src.ui.main_window.MigrationWizardDialog") as wizard:
            MainWindow.start_migration(window)

        window._has_resumable_work.assert_not_called()
        wizard.assert_called_once()

    def test_unchecked_license_does_not_block(self, window):
        """확인 전(None)에는 아무것도 막지 않는다 — 확인 실패로 도구가 잠기면 안 된다."""
        window.license_state = None

        with patch("src.ui.main_window.MigrationWizardDialog") as wizard:
            MainWindow.start_migration(window)

        wizard.assert_called_once()

    def test_restricted_state_is_passed_into_wizard(self, window):
        """마법사에 restricted가 전달돼야 한다. 안 넘기면 재개하러 들어간
        사용자가 '새 작업으로 진행'으로 새 실행을 시작할 수 있다."""
        window.license_state = _state(LicenseStatus.EXPIRED)
        window._has_resumable_work.return_value = True

        with patch("src.ui.main_window.MigrationWizardDialog") as wizard:
            MainWindow.start_migration(window)

        assert wizard.call_args.kwargs["restricted"] is True

    def test_valid_state_passes_unrestricted_wizard(self, window):
        window.license_state = _state(LicenseStatus.VALID)

        with patch("src.ui.main_window.MigrationWizardDialog") as wizard:
            MainWindow.start_migration(window)

        assert wizard.call_args.kwargs["restricted"] is False


class TestProfileCreationGate:
    """새 프로필 생성은 새 작업을 벌이는 일이므로 제한 모드에서 막는다.

    편집은 막지 않는다 — DB 암호가 바뀐 채 중단된 작업을 재개하려면
    연결 정보를 고칠 수 있어야 한다(§2.1의 재개 경로 보장).
    """

    def test_restricted_blocks_new_profile(self, window):
        window.license_state = _state(LicenseStatus.EXPIRED)

        with patch("src.ui.main_window.ConnectionDialog") as dialog:
            MainWindow.new_connection(window)

        window._explain_restricted_mode.assert_called_once()
        dialog.assert_not_called()

    def test_valid_license_allows_new_profile(self, window):
        window.license_state = _state(LicenseStatus.VALID)

        with patch("src.ui.main_window.ConnectionDialog") as dialog:
            dialog.return_value.exec.return_value = False
            MainWindow.new_connection(window)

        dialog.assert_called_once()

    def test_restricted_still_allows_editing_profile(self, window):
        """편집 차단은 재개 경로를 끊는다. 제한 모드에서도 열려 있어야 한다."""
        window.license_state = _state(LicenseStatus.EXPIRED)
        window.vm.current_profile = MagicMock(id=1)

        with patch("src.ui.main_window.ConnectionDialog") as dialog:
            dialog.return_value.exec.return_value = False
            MainWindow.edit_connection(window)

        dialog.assert_called_once()


def _wizard_mock(dialog_cls, restricted: bool, resume_mode: bool) -> MagicMock:
    """실제 Qt 창 없이 `_set_run_state` 분기만 본다. RUN_STATES 조회만 실제 값이 필요하다."""
    dlg = MagicMock()
    dlg.RUN_STATES = dialog_cls.RUN_STATES
    dlg.restricted = restricted
    dlg.resume_mode = resume_mode
    dlg.history_id = None
    return dlg


class TestWizardStartButtonGate:
    """마법사 안쪽 게이트 — 제한 모드 + 새 작업이면 시작 버튼이 죽어야 한다."""

    @pytest.mark.parametrize("dialog_cls", [MigrationWizardDialog, FileArchiveMigrationDialog])
    def test_restricted_new_run_disables_start(self, dialog_cls):
        dlg = _wizard_mock(dialog_cls, restricted=True, resume_mode=False)

        dialog_cls._set_run_state(dlg, "idle")

        dlg.start_btn.setEnabled.assert_called_once_with(False)
        tooltip = dlg.start_btn.setToolTip.call_args.args[0]
        assert "제한 모드" in tooltip

    @pytest.mark.parametrize("dialog_cls", [MigrationWizardDialog, FileArchiveMigrationDialog])
    def test_restricted_resume_keeps_start_enabled(self, dialog_cls):
        """재개는 라이선스 상태와 무관하게 끝까지 갈 수 있어야 한다 — §2.1."""
        dlg = _wizard_mock(dialog_cls, restricted=True, resume_mode=True)

        dialog_cls._set_run_state(dlg, "idle")

        dlg.start_btn.setEnabled.assert_called_once_with(True)

    @pytest.mark.parametrize("dialog_cls", [MigrationWizardDialog, FileArchiveMigrationDialog])
    def test_unrestricted_new_run_keeps_start_enabled(self, dialog_cls):
        dlg = _wizard_mock(dialog_cls, restricted=False, resume_mode=False)

        dialog_cls._set_run_state(dlg, "idle")

        dlg.start_btn.setEnabled.assert_called_once_with(True)

    @pytest.mark.parametrize("dialog_cls", [MigrationWizardDialog, FileArchiveMigrationDialog])
    def test_restricted_does_not_block_running_transition(self, dialog_cls):
        """실행 중 전이는 시작 버튼과 무관하게 원래 규칙대로 꺼진다."""
        dlg = _wizard_mock(dialog_cls, restricted=True, resume_mode=True)

        dialog_cls._set_run_state(dlg, "running")

        dlg.start_btn.setEnabled.assert_called_once_with(False)


class TestInitialButtonState:
    """생성 직후의 시작 버튼 상태.

    새 작업 경로는 '새 작업으로 진행' 버튼(=`_on_new_run_clicked`)을 거치지 않고도
    '다음'만으로 실행 페이지에 도달한다. 초기 상태가 게이트를 반영하지 않으면
    그 경로로 제한 모드를 우회한다.
    """

    def _wizard(self, qtbot, restricted: bool) -> MigrationWizardDialog:
        with (
            patch("src.ui.dialogs.migration_wizard_dialog.HistoryManager") as hm,
            patch("src.ui.dialogs.migration_wizard_dialog.CheckpointManager"),
        ):
            hm.return_value.get_incomplete_history.return_value = None
            dlg = MigrationWizardDialog(
                profile=MagicMock(id=1, name="테스트"), restricted=restricted
            )
        qtbot.addWidget(dlg)
        return dlg

    def _archive(self, qtbot, restricted: bool) -> FileArchiveMigrationDialog:
        with (
            patch("src.ui.dialogs.file_archive_migration_dialog.HistoryManager") as hm,
            patch("src.ui.dialogs.file_archive_migration_dialog.CheckpointManager"),
        ):
            hm.return_value.get_incomplete_history.return_value = None
            dlg = FileArchiveMigrationDialog(
                None, MagicMock(id=1, name="테스트"), restricted=restricted
            )
        qtbot.addWidget(dlg)
        return dlg

    def test_restricted_wizard_starts_disabled(self, qtbot):
        assert self._wizard(qtbot, restricted=True).start_btn.isEnabled() is False

    def test_unrestricted_wizard_starts_enabled(self, qtbot):
        assert self._wizard(qtbot, restricted=False).start_btn.isEnabled() is True

    def test_restricted_archive_starts_disabled(self, qtbot):
        assert self._archive(qtbot, restricted=True).start_btn.isEnabled() is False

    def test_unrestricted_archive_starts_enabled(self, qtbot):
        assert self._archive(qtbot, restricted=False).start_btn.isEnabled() is True


class TestStatusSemantics:
    @pytest.mark.parametrize(
        ("status", "restricted"),
        [
            (LicenseStatus.VALID, False),
            (LicenseStatus.EXPIRING, False),
            (LicenseStatus.EXPIRED, True),
            (LicenseStatus.MISSING, True),
            (LicenseStatus.INVALID, True),
            (LicenseStatus.WRONG_MACHINE, True),
        ],
    )
    def test_is_restricted_mapping(self, status, restricted):
        assert status.is_restricted is restricted
