"""H-09 — history와 전체 checkpoint는 함께 생기거나 함께 사라져야 한다.

예전에는 history를 먼저 commit하고 checkpoint를 파티션마다 따로 commit했다.
N번째 checkpoint에서 실패(디스크·잠금·프로세스 종료)하면 앞의 N-1개만 남고,
재개는 '남아 있는 checkpoint 중 미완료'만 돌린 뒤 이력을 완료로 닫았다 —
계획의 나머지 파티션은 조용히 빠졌다.

여기서는 실제 SQLite 트리거로 N번째 INSERT를 실패시켜 원자성을 확인하고,
불변 계획(planned set/count/hash)으로 재개 전 완전성을 검증하는지 본다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.database.local_db import Checkpoint, MigrationHistory
from src.models.history import (
    CheckpointManager,
    HistoryManager,
    MigrationPlan,
    ResumeVerdict,
    planned_set_hash,
)
from src.models.profile import ConnectionProfile

PLAN = [f"point_history_2601{d:02d}" for d in range(1, 8)]


def _profile() -> ConnectionProfile:
    return ConnectionProfile(
        id=1,
        name="p",
        source_config={
            "host": "src.example",
            "port": 5446,
            "database": "bms93",
            "username": "migtool",
            "password": "s3cret",
        },
        target_config={
            "host": "dst.example",
            "port": 5445,
            "database": "bms30",
            "username": "migtool",
            "password": "s3cret",
        },
    )


def _fail_on_partition(db, partition_name: str) -> None:
    """이 파티션의 checkpoint INSERT를 DB 수준에서 실패시킨다."""
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER inject_fail BEFORE INSERT ON checkpoints "
                f"WHEN NEW.partition_name = '{partition_name}' "
                "BEGIN SELECT RAISE(ABORT, 'injected checkpoint failure'); END"
            )
        )


def _counts(db) -> tuple[int, int]:
    with db.session_scope() as s:
        return s.query(MigrationHistory).count(), s.query(Checkpoint).count()


class TestAtomicCreation:
    @pytest.mark.parametrize("fail_index", [0, 3, len(PLAN) - 1])
    def test_nth_checkpoint_failure_rolls_back_everything(self, history_db, fail_index):
        _fail_on_partition(history_db, PLAN[fail_index])

        with pytest.raises(Exception, match="injected checkpoint failure"):
            HistoryManager().create_planned_history(
                _profile(), PLAN, "2026-01-01", "2026-01-07", total_rows=100
            )

        assert _counts(history_db) == (0, 0)

    def test_success_creates_history_and_every_planned_checkpoint(self, history_db):
        item = HistoryManager().create_planned_history(
            _profile(), PLAN, "2026-01-01", "2026-01-07", total_rows=100
        )

        names = [c.partition_name for c in CheckpointManager().get_checkpoints(item.id)]
        assert sorted(names) == sorted(PLAN)
        assert item.planned_count == len(PLAN)
        assert item.planned_hash == planned_set_hash(PLAN)
        assert item.plan_version == 1
        assert _counts(history_db) == (1, len(PLAN))

    def test_duplicate_partitions_are_rejected_before_anything_is_written(self, history_db):
        with pytest.raises(ValueError):
            HistoryManager().create_planned_history(
                _profile(), [PLAN[0], PLAN[0]], "2026-01-01", "2026-01-01"
            )
        assert _counts(history_db) == (0, 0)

    def test_empty_plan_is_rejected(self, history_db):
        with pytest.raises(ValueError):
            HistoryManager().create_planned_history(_profile(), [], "2026-01-01", "2026-01-01")
        assert _counts(history_db) == (0, 0)


class TestResumeCompleteness:
    def _history(self) -> int:
        item = HistoryManager().create_planned_history(_profile(), PLAN, "2026-01-01", "2026-01-07")
        assert item.id is not None
        return item.id

    def test_pending_comes_from_the_plan_not_from_surviving_rows(self, history_db):
        hid = self._history()
        cm = CheckpointManager()
        done = {PLAN[0], PLAN[2]}
        for cp in cm.get_checkpoints(hid):
            if cp.partition_name in done:
                cm.update_checkpoint_status(cp.id, "completed")

        check = HistoryManager().prepare_resume(hid, _profile())

        assert check.verdict is ResumeVerdict.OK
        assert check.pending == sorted(set(PLAN) - done)
        assert check.supplemented == []

    def test_missing_checkpoints_are_restored_from_the_plan(self, history_db):
        """계획에는 있는데 checkpoint가 사라졌다 → 계획대로 보충하고 재개 대상에 넣는다."""
        hid = self._history()
        with history_db.session_scope() as s:
            s.query(Checkpoint).filter(
                Checkpoint.history_id == hid, Checkpoint.partition_name.in_(PLAN[4:])
            ).delete(synchronize_session=False)

        check = HistoryManager().prepare_resume(hid, _profile())

        assert check.verdict is ResumeVerdict.OK
        assert check.supplemented == sorted(PLAN[4:])
        assert check.pending == sorted(PLAN)
        names = sorted(c.partition_name for c in CheckpointManager().get_checkpoints(hid))
        assert names == sorted(PLAN)

    def test_tampered_plan_blocks_resume(self, history_db):
        hid = self._history()
        with history_db.session_scope() as s:
            s.query(MigrationHistory).filter_by(id=hid).update(
                {"planned_partitions": '["point_history_260101"]'}
            )

        check = HistoryManager().prepare_resume(hid, _profile())

        assert check.verdict is ResumeVerdict.PLAN_INVALID
        assert not check.allowed
        assert check.pending == []

    def test_checkpoint_outside_the_plan_blocks_resume(self, history_db):
        hid = self._history()
        CheckpointManager().create_checkpoint(hid, "point_history_990101")

        check = HistoryManager().prepare_resume(hid, _profile())

        assert check.verdict is ResumeVerdict.PLAN_INVALID
        assert "point_history_990101" in " ".join(check.problems)

    def test_everything_completed_leaves_nothing_pending(self, history_db):
        hid = self._history()
        cm = CheckpointManager()
        for cp in cm.get_checkpoints(hid):
            cm.update_checkpoint_status(cp.id, "completed")

        check = HistoryManager().prepare_resume(hid, _profile())

        assert check.verdict is ResumeVerdict.OK
        assert check.pending == []


class TestPlanValue:
    def test_hash_is_order_independent_and_count_is_exact(self):
        a = MigrationPlan.from_profile(_profile(), PLAN)
        b = MigrationPlan.from_profile(_profile(), list(reversed(PLAN)))
        assert a.planned_hash == b.planned_hash
        assert a.fingerprint == b.fingerprint
        assert a.planned_count == len(PLAN)

    def test_fingerprint_changes_with_schema_or_scope(self):
        base = MigrationPlan.from_profile(_profile(), PLAN)
        assert MigrationPlan.from_profile(_profile(), PLAN[:-1]).fingerprint != base.fingerprint
        assert (
            MigrationPlan.from_profile(_profile(), PLAN, schema="other").fingerprint
            != base.fingerprint
        )
