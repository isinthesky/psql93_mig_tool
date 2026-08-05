"""이력의 진행량 기록 — '어디까지 갔나'에 답할 수 있어야 한다.

두 가지가 깨져 있었다:

1. `total_rows` 컬럼에 **쓰는 코드가 없었다.** `create_history()`도
   `update_history_status()`도 이 값을 건드리지 않아 항상 0이었고, 재개
   안내는 늘 `진행: 1,234 / 0 (rows)`로 떴다.

2. `processed_rows`가 **재개하면 뒤로 갔다.** 워커의 성능 카운터는 실행
   1회분만 센다. 70만 행 처리 후 중단하고 재개해 30만을 더 옮기면, 이력에는
   100만이 아니라 30만이 남았다.
"""

import os

import pytest

import src.database.local_db as local_db_module
from src.models.history import CheckpointManager, HistoryManager


@pytest.fixture
def db(tmp_path, monkeypatch):
    """파일 기반 SQLite. 실제 SQL SUM 경로를 그대로 탄다."""
    path = str(tmp_path / "history.db")
    instance = local_db_module.LocalDatabase()
    instance.db_path = path
    instance.initialize()
    monkeypatch.setattr(local_db_module, "_db_instance", instance, raising=False)
    yield instance
    instance.engine.dispose() if instance.engine else None
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture
def managers(db):
    return HistoryManager(), CheckpointManager()


class TestTotalRowsIsRecorded:
    def test_the_planned_scope_is_stored(self, managers):
        history_manager, _ = managers

        created = history_manager.create_history(
            1, "2026-01-01", "2026-01-31", total_rows=1_000_000
        )

        assert history_manager.get_history(created.id).total_rows == 1_000_000

    def test_it_survives_a_status_update(self, managers):
        """상태만 바꿀 때 범위가 0으로 지워지면 안 된다."""
        history_manager, _ = managers
        created = history_manager.create_history(1, "2026-01-01", "2026-01-31", total_rows=500)

        history_manager.update_history_status(created.id, "completed", processed_rows=500)

        assert history_manager.get_history(created.id).total_rows == 500

    def test_it_can_be_corrected_later(self, managers):
        """실행 시점의 값은 추정치다. 나중에 바로잡을 수 있어야 한다."""
        history_manager, _ = managers
        created = history_manager.create_history(1, "2026-01-01", "2026-01-31", total_rows=500)

        history_manager.update_history_status(created.id, "completed", total_rows=623)

        assert history_manager.get_history(created.id).total_rows == 623

    def test_omitting_it_keeps_the_old_default(self, managers):
        """인자를 안 주는 기존 호출부가 깨지면 안 된다."""
        history_manager, _ = managers

        created = history_manager.create_history(1, "2026-01-01", "2026-01-31")

        assert created.total_rows == 0


class TestProcessedRowsAccumulateAcrossRuns:
    def _run_partitions(self, checkpoint_manager, history_id, rows_by_name):
        for name, rows in rows_by_name.items():
            checkpoint = checkpoint_manager.create_checkpoint(history_id, name)
            checkpoint_manager.update_checkpoint_status(
                checkpoint.id, "completed", rows_processed=rows
            )

    def test_a_resumed_run_adds_instead_of_replacing(self, managers):
        """이게 깨지면 재개할수록 진행량이 작아진다."""
        history_manager, checkpoint_manager = managers
        created = history_manager.create_history(1, "2026-01-01", "2026-01-31", total_rows=1_000)

        self._run_partitions(checkpoint_manager, created.id, {"p1": 400, "p2": 300})
        after_first = checkpoint_manager.get_processed_rows(created.id)

        # 재개: 새 워커의 카운터는 0부터 다시 시작한다
        self._run_partitions(checkpoint_manager, created.id, {"p3": 300})

        assert after_first == 700
        assert checkpoint_manager.get_processed_rows(created.id) == 1_000

    def test_pending_checkpoints_contribute_nothing(self, managers):
        history_manager, checkpoint_manager = managers
        created = history_manager.create_history(1, "2026-01-01", "2026-01-31")
        checkpoint_manager.create_checkpoint(created.id, "p1")

        assert checkpoint_manager.get_processed_rows(created.id) == 0

    def test_partial_progress_counts(self, managers):
        """중단된 파티션도 옮긴 만큼은 옮긴 것이다."""
        history_manager, checkpoint_manager = managers
        created = history_manager.create_history(1, "2026-01-01", "2026-01-31")
        checkpoint = checkpoint_manager.create_checkpoint(created.id, "p1")
        checkpoint_manager.update_checkpoint_status(checkpoint.id, "failed", rows_processed=120)

        assert checkpoint_manager.get_processed_rows(created.id) == 120

    def test_another_history_is_not_mixed_in(self, managers):
        history_manager, checkpoint_manager = managers
        mine = history_manager.create_history(1, "2026-01-01", "2026-01-31")
        theirs = history_manager.create_history(2, "2026-01-01", "2026-01-31")
        self._run_partitions(checkpoint_manager, mine.id, {"p1": 100})
        self._run_partitions(checkpoint_manager, theirs.id, {"p1": 999})

        assert checkpoint_manager.get_processed_rows(mine.id) == 100

    def test_an_empty_history_is_zero_not_none(self, managers):
        """SUM은 행이 없으면 NULL을 준다. 그대로 두면 포맷에서 터진다."""
        history_manager, checkpoint_manager = managers
        created = history_manager.create_history(1, "2026-01-01", "2026-01-31")

        total = checkpoint_manager.get_processed_rows(created.id)

        assert total == 0
        assert isinstance(total, int)


class TestResumeBannerReadsRight:
    """재개 안내 문구는 이 값들이 화면에 닿는 유일한 곳이다."""

    def _banner(self, dialog_class, total_rows, done_rows, managers, stored_rows=None):
        from unittest.mock import MagicMock

        history_manager, checkpoint_manager = managers
        history = history_manager.create_history(
            1, "2026-01-01", "2026-01-31", total_rows=total_rows
        )
        if done_rows:
            checkpoint = checkpoint_manager.create_checkpoint(history.id, "p1")
            checkpoint_manager.update_checkpoint_status(
                checkpoint.id, "completed", rows_processed=done_rows
            )
        if stored_rows is not None:
            history_manager.update_history_status(history.id, "running", processed_rows=stored_rows)

        dialog = MagicMock()
        dialog.checkpoint_manager = checkpoint_manager
        return dialog_class._describe_progress(dialog, history_manager.get_history(history.id))

    def test_a_known_scope_shows_a_percentage(self, managers):
        from src.ui.dialogs.migration_wizard_dialog import MigrationWizardDialog

        text = self._banner(MigrationWizardDialog, 1_000, 700, managers)

        assert text == "700 / 약 1,000 rows (70%)"

    def test_an_unknown_scope_shows_only_what_was_done(self, managers):
        """`total_rows`를 기록하지 않던 시절의 이력이 '/ 0'으로 뜨면 안 된다."""
        from src.ui.dialogs.migration_wizard_dialog import MigrationWizardDialog

        text = self._banner(MigrationWizardDialog, 0, 700, managers)

        assert text == "700 rows 처리됨"
        assert "/ 0" not in text

    def test_going_over_the_estimate_does_not_exceed_100(self, managers):
        """분모는 추정치다. 실제가 더 많으면 120%가 뜬다."""
        from src.ui.dialogs.migration_wizard_dialog import MigrationWizardDialog

        text = self._banner(MigrationWizardDialog, 100, 250, managers)

        assert "(100%)" in text

    def test_the_archive_dialog_says_the_same_thing(self, managers):
        from src.ui.dialogs.file_archive_migration_dialog import FileArchiveMigrationDialog

        text = self._banner(FileArchiveMigrationDialog, 1_000, 700, managers)

        assert text == "700 / 약 1,000 rows (70%)"

    def test_a_missing_checkpoint_write_does_not_erase_progress(self, managers):
        """체크포인트 기록이 조용히 실패해도 저장된 진행량을 잃지 않는다.

        두 값 모두 실제보다 클 수 없는 하한이므로 큰 쪽을 취한다.
        """
        from src.ui.dialogs.migration_wizard_dialog import MigrationWizardDialog

        text = self._banner(MigrationWizardDialog, 1_000, 0, managers, stored_rows=650)

        assert text == "650 / 약 1,000 rows (65%)"

    def test_checkpoints_win_when_they_know_more(self, managers):
        """재개한 이력은 저장값이 마지막 실행분뿐이라 실제보다 작다."""
        from src.ui.dialogs.migration_wizard_dialog import MigrationWizardDialog

        text = self._banner(MigrationWizardDialog, 1_000, 700, managers, stored_rows=300)

        assert text == "700 / 약 1,000 rows (70%)"
