"""H-08/H-09 — legacy 이력 채택이 H-09 누락을 '정상 계획'으로 고정하면 안 된다.

구버전은 이력을 먼저 commit하고 checkpoint를 하나씩 commit했다. 중간에 실패한 이력은
checkpoint가 일부만 남는다. 그 남은 집합을 그대로 계획으로 채택하면, 사용자가 '예'를 한 번
누르는 것만으로 일부 파티션만 처리하고 completed로 닫힌다(subset 완료).

채택 전에 기록된 날짜 범위와 파티션 명명 규칙으로 기대 집합을 다시 계산해 남은 checkpoint와
비교한다. 체크포인트가 있는 유형 안에서 빠진 파티션은 **보충해야만** 채택할 수 있다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.database.local_db import Checkpoint
from src.models.history import (
    CheckpointManager,
    HistoryManager,
    LegacyPlanGapError,
    ResumeVerdict,
)
from src.models.profile import ConnectionProfile


def _profile() -> ConnectionProfile:
    return ConnectionProfile(
        id=1,
        name="p",
        source_config={
            "host": "src.example",
            "port": 5446,
            "database": "bms93",
            "username": "migtool",
            "password": "pw",
        },
        target_config={
            "host": "dst.example",
            "port": 5445,
            "database": "bms30",
            "username": "migtool",
            "password": "pw",
        },
    )


def _legacy(start: str, end: str, names: list[str], completed: tuple[str, ...] = ()) -> int:
    """구버전 경로(create_history + checkpoint 개별 생성)로 legacy 이력을 만든다."""
    hm, cm = HistoryManager(), CheckpointManager()
    item = hm.create_history(1, start, end)
    assert item.id is not None
    for name in names:
        cp = cm.create_checkpoint(item.id, name)
        if name in completed:
            assert cp.id is not None
            cm.update_checkpoint_status(cp.id, "completed")
    return item.id


def _checkpoint_names(db, history_id: int) -> list[str]:
    with db.session_scope() as s:
        return sorted(
            n
            for (n,) in s.query(Checkpoint.partition_name).filter(
                Checkpoint.history_id == history_id
            )
        )


class TestReviewerReproduction:
    """3일 범위에 checkpoint가 2개만 남은 legacy 이력(리뷰 재현 사례)."""

    def _truncated(self) -> int:
        return _legacy("2026-01-01", "2026-01-03", ["point_history_260101", "point_history_260102"])

    def test_prepare_resume_reports_gap_in_range(self, history_db):
        hid = self._truncated()

        check = HistoryManager().prepare_resume(hid, _profile())

        assert check.verdict is ResumeVerdict.LEGACY
        assert check.legacy is not None
        assert check.legacy.range_ok
        assert check.legacy.gaps == ["point_history_260103"]
        # 확인 창에 보여 줄 수치: 범위 일수, 기대 개수, 남은 개수, 누락 개수
        assert check.legacy.range_days == 3
        assert check.legacy.expected_count == 3
        assert "누락 1개" in check.message
        assert "point_history_260103" in check.message

    def test_adopting_without_supplementing_the_gap_is_refused(self, history_db):
        hid = self._truncated()
        hm = HistoryManager()

        with pytest.raises(LegacyPlanGapError) as exc:
            hm.adopt_legacy_history(hid, _profile())

        assert exc.value.gaps == ["point_history_260103"]
        # 아무것도 쓰지 않는다: 여전히 legacy, checkpoint 그대로
        item = hm.get_history(hid)
        assert item is not None and item.plan_version is None
        assert _checkpoint_names(history_db, hid) == [
            "point_history_260101",
            "point_history_260102",
        ]

    def test_adopting_with_gap_supplemented_plans_the_full_range(self, history_db):
        hid = self._truncated()
        hm = HistoryManager()

        adopted = hm.adopt_legacy_history(hid, _profile(), supplement=["point_history_260103"])

        assert adopted.verdict is ResumeVerdict.OK
        assert adopted.pending == [
            "point_history_260101",
            "point_history_260102",
            "point_history_260103",
        ]
        item = hm.get_history(hid)
        assert item is not None
        assert item.plan_version == 1
        assert item.planned_count == 3
        assert _checkpoint_names(history_db, hid) == [
            "point_history_260101",
            "point_history_260102",
            "point_history_260103",
        ]


class TestCoverage:
    def test_complete_legacy_has_no_gap_and_adopts_without_supplement(self, history_db):
        names = ["point_history_260101", "point_history_260102", "point_history_260103"]
        hid = _legacy("2026-01-01", "2026-01-03", names, completed=(names[0],))
        hm = HistoryManager()

        check = hm.prepare_resume(hid, _profile())
        assert check.legacy is not None
        assert check.legacy.gaps == []

        adopted = hm.adopt_legacy_history(hid, _profile())
        assert adopted.verdict is ResumeVerdict.OK
        assert adopted.pending == names[1:]

    def test_types_without_any_checkpoint_are_reported_not_forced(self, history_db):
        names = ["point_history_260101", "point_history_260102"]
        hid = _legacy("2026-01-01", "2026-01-02", names)

        check = HistoryManager().prepare_resume(hid, _profile())

        assert check.legacy is not None
        absent = check.legacy.absent_types
        # 체크포인트가 전혀 없는 유형은 원래 선택하지 않았을 수 있으므로 알려만 준다.
        assert absent["point_sec_history"] == [
            "point_sec_history_260101",
            "point_sec_history_260102",
        ]
        assert absent["trend_history"] == ["trend_history_2601"]
        assert check.legacy.gaps == []
        assert "point_sec_history" in check.message

    def test_absent_type_can_be_explicitly_supplemented(self, history_db):
        names = ["point_history_260101", "point_history_260102"]
        hid = _legacy("2026-01-01", "2026-01-02", names)
        extra = ["point_sec_history_260101", "point_sec_history_260102"]

        adopted = HistoryManager().adopt_legacy_history(hid, _profile(), supplement=extra)

        assert adopted.verdict is ResumeVerdict.OK
        assert adopted.pending == sorted(names + extra)

    def test_monthly_gap_detected(self, history_db):
        hid = _legacy(
            "2025-12-15",
            "2026-02-02",
            ["trend_history_2512", "trend_history_2602"],
        )

        check = HistoryManager().prepare_resume(hid, _profile())

        assert check.legacy is not None
        assert check.legacy.gaps == ["trend_history_2601"]

    def test_names_outside_range_are_kept_and_not_counted_as_gaps(self, history_db):
        names = ["point_history_251231", "point_history_260101"]
        hid = _legacy("2026-01-01", "2026-01-01", names)
        hm = HistoryManager()

        check = hm.prepare_resume(hid, _profile())
        assert check.legacy is not None
        assert check.legacy.gaps == []
        assert check.legacy.outside == ["point_history_251231"]

        adopted = hm.adopt_legacy_history(hid, _profile())
        assert adopted.pending == names

    def test_supplement_outside_expected_candidates_is_rejected(self, history_db):
        hid = _legacy("2026-01-01", "2026-01-02", ["point_history_260101"])
        hm = HistoryManager()

        with pytest.raises(ValueError, match="범위"):
            hm.adopt_legacy_history(
                hid, _profile(), supplement=["point_history_260102", "point_history_260105"]
            )

        item = hm.get_history(hid)
        assert item is not None and item.plan_version is None
        assert _checkpoint_names(history_db, hid) == ["point_history_260101"]

    def test_unparsable_range_cannot_be_adopted(self, history_db):
        hid = _legacy("", "", ["point_history_260101"])
        hm = HistoryManager()

        check = hm.prepare_resume(hid, _profile())
        assert check.legacy is not None
        assert not check.legacy.range_ok

        with pytest.raises(ValueError, match="범위"):
            hm.adopt_legacy_history(hid, _profile())
        item = hm.get_history(hid)
        assert item is not None and item.plan_version is None


class TestAtomicAdoption:
    def test_supplement_failure_rolls_back_binding(self, history_db):
        hid = _legacy("2026-01-01", "2026-01-03", ["point_history_260101"])
        with history_db.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TRIGGER inject_fail BEFORE INSERT ON checkpoints "
                    "WHEN NEW.partition_name = 'point_history_260103' "
                    "BEGIN SELECT RAISE(ABORT, 'injected checkpoint failure'); END"
                )
            )
        hm = HistoryManager()

        with pytest.raises(Exception, match="injected checkpoint failure"):
            hm.adopt_legacy_history(
                hid,
                _profile(),
                supplement=["point_history_260102", "point_history_260103"],
            )

        # 보충 checkpoint 일부와 계획 기록이 함께 rollback되어야 한다.
        item = hm.get_history(hid)
        assert item is not None and item.plan_version is None
        assert _checkpoint_names(history_db, hid) == ["point_history_260101"]
