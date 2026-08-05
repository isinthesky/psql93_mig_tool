"""아카이브 다이얼로그의 실행 페이지와 목록 상한.

세 가지를 지킨다:
- 눌러도 안 멈추는 '일시정지' 버튼을 두지 않는다(워커가 `_check_pause()`를
  부르지 않아 `pause()`가 아무 일도 안 했다)
- 목록에 담기지 않은 파티션은 실행 대상에서도 빠진다. 조용히 자르지 않는다
- 로그 창은 오래된 줄을 버린다. 전체 기록이 어디 있는지 알린다
"""

import ast
import inspect
import textwrap
from unittest.mock import MagicMock, patch

import pytest

import src.ui.dialogs.file_archive_migration_dialog as archive_mod
import src.ui.dialogs.migration_wizard_dialog as wizard_mod
from src.core.table_types import TableType
from src.ui.dialogs.scan_host import PARTITION_DISPLAY_LIMIT


@pytest.fixture
def dialog(qapp):
    profile = MagicMock()
    profile.id = 1
    profile.name = "프로필"
    profile.migration_mode = "postgres_to_file"
    profile.source_kind = "postgres"
    profile.target_kind = "file"
    profile.source_config = {"host": "h"}
    profile.target_config = {"archive_path": "c:/a"}

    with (
        patch.object(archive_mod, "HistoryManager") as history_manager,
        patch.object(archive_mod, "CheckpointManager"),
        patch.object(
            archive_mod.FileArchiveMigrationDialog, "check_connections", lambda self: None
        ),
    ):
        history_manager.return_value.get_incomplete_history.return_value = None
        dlg = archive_mod.FileArchiveMigrationDialog(None, profile)
    yield dlg
    dlg.deleteLater()


class TestNoDeadPauseButton:
    """아카이브 워커는 일시정지를 지원하지 않는다.

    `ArchiveMigrationWorkerBase`가 `_check_pause()`를 한 번도 부르지 않으므로
    `pause()`는 플래그만 세우고 작업은 계속 돈다.
    """

    def test_the_worker_really_cannot_pause(self):
        source = inspect.getsource(archive_mod.ArchiveMigrationWorkerBase)
        tree = ast.parse(textwrap.dedent(source))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "_check_pause" not in called, (
            "워커가 일시정지를 지원하게 됐다면 버튼을 되살려도 된다"
        )

    def test_the_dialog_has_no_pause_button(self, dialog):
        assert not hasattr(dialog, "pause_btn")

    def test_the_dialog_has_no_pause_handler(self, dialog):
        assert not hasattr(dialog, "pause_migration")

    def test_paused_is_not_a_reachable_state(self, dialog):
        assert "paused" not in dialog.RUN_STATES

    def test_cancel_still_works(self, dialog):
        """일시정지를 없앤 대가로 취소까지 잃으면 안 된다."""
        dialog._set_run_state("running")
        assert dialog.cancel_btn.isEnabled()

    def test_the_wizard_keeps_its_pause_button(self, dialog):
        """COPY 워커는 `_check_pause()`를 부른다. 그쪽은 진짜로 멈춘다."""
        source = inspect.getsource(wizard_mod.CopyMigrationWorker._migrate_partition_with_copy)
        assert "_check_pause" in source
        assert hasattr(dialog, "cancel_btn")  # 아카이브 쪽은 취소만 남는다

    def test_every_copy_mode_can_actually_pause(self):
        """server-side COPY는 파티션 하나가 단일 명령이라 중간에 못 멈춘다.

        파티션 경계에서 확인하지 않으면 server/auto 모드에서 '일시정지'를
        눌러도 작업이 끝까지 그냥 돈다 — 화면만 '일시정지'라고 말한다.
        """
        source = inspect.getsource(wizard_mod.CopyMigrationWorker._execute_migration)
        tree = ast.parse(textwrap.dedent(source))
        loops = [n for n in ast.walk(tree) if isinstance(n, ast.For)]
        partition_loop = next(
            loop
            for loop in loops
            if isinstance(loop.iter, ast.Call)
            and getattr(loop.iter.func, "id", "") == "enumerate"
            and getattr(loop.iter.args[0], "attr", "") == "partitions"
        )
        calls = {
            n.func.attr
            for n in ast.walk(partition_loop)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "_check_pause" in calls


class TestPauseTellsTheTruth:
    """일시정지가 언제 실제로 걸리는지 말한다."""

    def _wizard(self, qapp, copy_mode):
        profile = MagicMock()
        profile.id = 1
        profile.name = "프로필"
        profile.source_kind = "postgres"
        profile.target_kind = "postgres"
        profile.source_config = {"host": "h"}
        profile.target_config = {"host": "h"}

        with (
            patch.object(wizard_mod, "HistoryManager") as history_manager,
            patch.object(wizard_mod, "CheckpointManager"),
            patch.object(wizard_mod.MigrationWizardDialog, "check_connections", lambda s: None),
        ):
            history_manager.return_value.get_incomplete_history.return_value = None
            dlg = wizard_mod.MigrationWizardDialog(None, profile)
        dlg.worker = MagicMock()
        dlg.worker.copy_mode = copy_mode
        dlg._set_run_state("running")
        return dlg

    def test_python_copy_pauses_immediately(self, qapp):
        dlg = self._wizard(qapp, "python")
        try:
            dlg.pause_migration()
            assert dlg.run_detail_label.text() == "일시정지"
        finally:
            dlg.deleteLater()

    def test_server_copy_says_it_waits_for_the_partition(self, qapp):
        """이미 멈춘 줄 알고 창을 닫거나 DB를 만지면 곤란하다."""
        dlg = self._wizard(qapp, "server")
        try:
            dlg.pause_migration()
            assert "파티션이 끝나면" in dlg.run_detail_label.text()
        finally:
            dlg.deleteLater()

    def test_auto_mode_is_treated_as_server(self, qapp):
        """auto는 server-side를 먼저 시도한다. 낙관적으로 쓰지 않는다."""
        dlg = self._wizard(qapp, "auto")
        try:
            dlg.pause_migration()
            assert "파티션이 끝나면" in dlg.run_detail_label.text()
        finally:
            dlg.deleteLater()


class TestPartitionDisplayLimit:
    """마법사에만 있던 상한을 아카이브에도 적용한다."""

    def _many(self, count):
        return [
            {
                "table_name": f"t{i:05d}",
                "row_count": 1,
                "table_type": TableType.POINT_HISTORY,
            }
            for i in range(count)
        ]

    def test_everything_fits_under_the_limit(self, dialog):
        dialog._on_discovery_result(dialog._scan_gen, self._many(3))

        assert dialog.partition_list.count() == 3

    def test_overflow_is_announced_not_hidden(self, dialog):
        over = PARTITION_DISPLAY_LIMIT + 7
        dialog._on_discovery_result(dialog._scan_gen, self._many(over))

        # 상한만큼의 항목 + 경고 1줄
        assert dialog.partition_list.count() == PARTITION_DISPLAY_LIMIT + 1
        warning = dialog.partition_list.item(PARTITION_DISPLAY_LIMIT)
        assert "7개는 목록에 표시되지 않아" in warning.text()

    def test_the_overflow_notice_cannot_be_checked(self, dialog):
        """경고를 실행 대상으로 셀 수 없어야 한다."""
        dialog._on_discovery_result(dialog._scan_gen, self._many(PARTITION_DISPLAY_LIMIT + 1))

        warning = dialog.partition_list.item(PARTITION_DISPLAY_LIMIT)
        assert not warning.data(archive_mod.Qt.ItemDataRole.UserRole)
        assert len(dialog.get_selected_partition_names()) == PARTITION_DISPLAY_LIMIT

    def test_the_count_label_does_not_cry_wolf_at_the_limit(self, dialog):
        """상한에 걸린 것과 필터로 가려진 것은 다른 사건이다."""
        dialog._on_discovery_result(dialog._scan_gen, self._many(PARTITION_DISPLAY_LIMIT + 3))

        assert dialog.partition_count_label.text() == f"총 {PARTITION_DISPLAY_LIMIT + 3}개"


class TestLogRetentionIsVisible:
    """로그 창은 오래된 줄을 조용히 버린다.

    수천 개 파티션을 옮기는 동안 초반 경고가 밀려나면 사용자는 그런 경고가
    없었다고 믿는다. 전체 기록은 파일에 남으므로 그 사실을 알린다.
    """

    def _hint_labels(self, dlg):
        from PySide6.QtWidgets import QLabel

        return [
            w.text()
            for w in dlg.findChildren(QLabel)
            if "로그 파일" in w.text() or "줄만 남습니다" in w.text()
        ]

    def test_the_archive_dialog_says_where_the_full_log_is(self, dialog):
        hints = self._hint_labels(dialog)

        assert hints, "로그가 잘린다는 사실이 화면 어디에도 없습니다"
        assert "logs" in hints[0].lower()

    def test_the_wizard_says_it_too(self, qapp):
        profile = MagicMock()
        profile.id = 1
        profile.name = "프로필"
        profile.source_kind = "postgres"
        profile.target_kind = "postgres"
        profile.source_config = {"host": "h"}
        profile.target_config = {"host": "h"}

        with (
            patch.object(wizard_mod, "HistoryManager") as history_manager,
            patch.object(wizard_mod, "CheckpointManager"),
            patch.object(wizard_mod.MigrationWizardDialog, "check_connections", lambda s: None),
        ):
            history_manager.return_value.get_incomplete_history.return_value = None
            dlg = wizard_mod.MigrationWizardDialog(None, profile)
        try:
            assert self._hint_labels(dlg)
        finally:
            dlg.deleteLater()
