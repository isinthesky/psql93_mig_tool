"""H-08/H-09 — legacy 이력 채택이 H-09 누락을 '정상 계획'으로 고정하면 안 된다.

구버전은 이력을 먼저 commit하고 checkpoint를 하나씩 commit했다. 중간에 실패한 이력은
checkpoint가 일부만 남는다. 그 남은 집합을 그대로 계획으로 채택하면, 사용자가 '예'를 한 번
누르는 것만으로 일부 파티션만 처리하고 completed로 닫힌다(subset 완료).

채택 전에 기록된 날짜 범위와 파티션 명명 규칙으로 기대 집합을 다시 계산해 남은 checkpoint와
비교한다. 체크포인트가 있는 유형 안에서 빠진 파티션은 **보충해야만** 채택할 수 있다.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text

from src.database.local_db import Checkpoint, MigrationHistory
from src.models.history import (
    CheckpointManager,
    HistoryManager,
    LegacyPlanGapError,
    LegacyTypeDecisionError,
    ResumeVerdict,
)
from src.models.profile import ConnectionProfile

# 뒤 유형(PS·RT·TH)이 원래 작업에 없었다는 명시적 결정(PH 단일 유형 작업).
PH_ONLY = ["point_history"]


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

        # 뒤 유형(PS·RT·TH)은 원래 작업에 없었다고 명시한다(PH 단일 유형 작업).
        adopted = hm.adopt_legacy_history(
            hid, _profile(), supplement=["point_history_260103"], original_types=PH_ONLY
        )

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

        adopted = hm.adopt_legacy_history(hid, _profile(), original_types=PH_ONLY)
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

        adopted = HistoryManager().adopt_legacy_history(
            hid, _profile(), supplement=extra, original_types=["point_history", "point_sec_history"]
        )

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

        adopted = hm.adopt_legacy_history(hid, _profile(), original_types=PH_ONLY)
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
                original_types=PH_ONLY,
            )

        # 보충 checkpoint 일부와 계획 기록이 함께 rollback되어야 한다.
        item = hm.get_history(hid)
        assert item is not None and item.plan_version is None
        assert _checkpoint_names(history_db, hid) == ["point_history_260101"]


class TestMultiTypeInterruption:
    """리뷰 major(H-08): 여러 유형을 고른 legacy 이력이 비마지막 유형에서 끊긴 경우.

    구버전 루프는 선택한 파티션을 유형 코드 순서(ED→PH→PS→RT→TH)로 하나씩 commit했다.
    비마지막 유형에서 끊기면 **그 뒤 유형은 checkpoint가 통째로 없다**. 'checkpoint가 있는
    유형'만 기대 집합으로 보면 뒤 유형이 빠진 계획이 고정되어 subset만 처리하고 닫힌다.
    """

    RANGE = ("2026-01-01", "2026-01-02")
    PH = ["point_history_260101", "point_history_260102"]
    PS = ["point_sec_history_260101", "point_sec_history_260102"]

    def _cut_after_point_history(self) -> int:
        # 원래 선택 PH·PS·TH. PH를 다 만들고 PS 첫 INSERT 전에 끊겼다(유형 경계).
        return _legacy(*self.RANGE, self.PH)

    def test_trailing_types_need_a_decision_and_leading_absent_types_are_not_warned(
        self, history_db
    ):
        hid = self._cut_after_point_history()

        check = HistoryManager().prepare_resume(hid, _profile())

        cov = check.legacy
        assert cov is not None
        assert cov.represented_types == ["point_history"]
        # 끊겼다면 빠졌을 수 있는 뒤 유형: 원래 선택 여부를 로컬 데이터로는 알 수 없다.
        assert sorted(cov.undecided_types) == [
            "point_sec_history",
            "running_time_history",
            "trend_history",
        ]
        # 앞 유형(ED)은 생성 순서상 원래 선택하지 않았다 — 경고에 넣지 않는다.
        assert cov.excluded_types == ["energy_display"]
        assert "energy_display" not in check.message
        assert "point_sec_history" in check.message

    def test_adoption_without_deciding_trailing_types_is_refused(self, history_db):
        hid = self._cut_after_point_history()
        hm = HistoryManager()

        with pytest.raises(LegacyTypeDecisionError) as exc:
            hm.adopt_legacy_history(hid, _profile())

        assert sorted(exc.value.types) == [
            "point_sec_history",
            "running_time_history",
            "trend_history",
        ]
        item = hm.get_history(hid)
        assert item is not None and item.plan_version is None
        assert _checkpoint_names(history_db, hid) == self.PH

    def test_declared_trailing_types_are_supplemented(self, history_db):
        hid = self._cut_after_point_history()
        hm = HistoryManager()
        original = ["point_history", "point_sec_history", "trend_history"]

        check = hm.prepare_resume(hid, _profile(), original_types=original)

        cov = check.legacy
        assert cov is not None
        assert cov.gaps == [*self.PS, "trend_history_2601"]
        assert cov.undecided_types == {}
        # 선택하지 않은 유형(RT: 사용자가 제외, ED: 생성 순서상 제외)은 경고에 없다.
        assert "running_time_history" not in check.message
        assert "energy_display" not in check.message
        assert "누락 3개" in check.message

        # 뒤 유형의 누락도 보충하지 않으면 채택하지 않는다.
        with pytest.raises(LegacyPlanGapError):
            hm.adopt_legacy_history(hid, _profile(), original_types=original)

        adopted = hm.adopt_legacy_history(
            hid, _profile(), original_types=original, supplement=cov.gaps
        )
        assert adopted.verdict is ResumeVerdict.OK
        assert adopted.pending == sorted([*self.PH, *self.PS, "trend_history_2601"])
        item = hm.get_history(hid)
        assert item is not None and item.planned_count == 5

    def test_mid_type_cut_supplements_rest_of_type_and_trailing_types(self, history_db):
        # 원래 선택 ED·PH·PS. ED 1개, PH 1개까지 만들고 끊겼다.
        hid = _legacy(*self.RANGE, ["energy_display_2601", "point_history_260101"])
        hm = HistoryManager()
        original = ["energy_display", "point_history", "point_sec_history"]

        cov = hm.prepare_resume(hid, _profile(), original_types=original).legacy

        assert cov is not None
        assert cov.gaps == ["point_history_260102", *self.PS]
        adopted = hm.adopt_legacy_history(
            hid, _profile(), original_types=original, supplement=cov.gaps
        )
        assert adopted.pending == sorted(["energy_display_2601", *self.PH, *self.PS])

    def test_declining_every_trailing_type_keeps_the_single_type_plan(self, history_db):
        hid = self._cut_after_point_history()

        adopted = HistoryManager().adopt_legacy_history(
            hid, _profile(), original_types=["point_history"]
        )

        assert adopted.verdict is ResumeVerdict.OK
        assert adopted.pending == self.PH

    def test_type_with_checkpoints_cannot_be_declined(self, history_db):
        """checkpoint가 있는 유형은 원래 선택된 것이 확실하다 — 선언에서 빠져도 계획에 남는다."""
        hid = self._cut_after_point_history()

        adopted = HistoryManager().adopt_legacy_history(
            hid, _profile(), original_types=["point_sec_history"], supplement=self.PS
        )

        assert adopted.pending == sorted([*self.PH, *self.PS])

    def test_unknown_type_in_declaration_is_rejected(self, history_db):
        hid = self._cut_after_point_history()
        hm = HistoryManager()

        with pytest.raises(ValueError, match="유형"):
            hm.adopt_legacy_history(hid, _profile(), original_types=["point_history", "nope"])

        item = hm.get_history(hid)
        assert item is not None and item.plan_version is None


class TestSupplementRecord:
    """리뷰 라운드 1(H-09): 채택 때 **보충한 이름**을 따로 기록한다.

    아카이브 워커는 이 이름만 '원본에 없으면 0건 완료'로 닫는다. 원래 checkpoint는 구버전이
    실제로 고른 파티션이라, 없어졌다면 잘못된 원본·아카이브를 가리키므로 실패해야 한다.
    """

    def test_adoption_records_only_the_supplemented_names(self, history_db):
        hid = _legacy("2026-01-01", "2026-01-03", ["point_history_260101", "point_history_260102"])
        hm = HistoryManager()

        # 이미 checkpoint가 있는 이름을 함께 넘겨도 보충분으로 기록하지 않는다.
        hm.adopt_legacy_history(
            hid,
            _profile(),
            supplement=["point_history_260101", "point_history_260103"],
            original_types=PH_ONLY,
        )

        item = hm.get_history(hid)
        assert item is not None
        assert item.legacy_supplemented == ["point_history_260103"]

    def test_adoption_without_gaps_records_nothing(self, history_db):
        names = ["point_history_260101", "point_history_260102"]
        hid = _legacy("2026-01-01", "2026-01-02", names)
        hm = HistoryManager()

        hm.adopt_legacy_history(hid, _profile(), original_types=PH_ONLY)

        item = hm.get_history(hid)
        assert item is not None and item.legacy_adopted_at is not None
        assert item.legacy_supplemented == []

    def test_planned_history_has_no_supplemented_names(self, history_db):
        item = HistoryManager().create_planned_history(
            _profile(), ["point_history_260101"], "2026-01-01", "2026-01-01"
        )

        assert item.legacy_supplemented == []

    def test_unreadable_record_is_treated_as_nothing_supplemented(self, history_db):
        """기록이 깨졌으면 아무 이름도 '없어도 되는' 것으로 보지 않는다(엄격)."""
        hid = _legacy("2026-01-01", "2026-01-03", ["point_history_260101", "point_history_260102"])
        hm = HistoryManager()
        hm.adopt_legacy_history(
            hid, _profile(), supplement=["point_history_260103"], original_types=PH_ONLY
        )
        with history_db.session_scope() as s:
            s.execute(
                text("UPDATE migration_history SET legacy_supplemented = :v WHERE id = :id"),
                {"v": '{"not": "a list"}', "id": hid},
            )

        item = hm.get_history(hid)
        assert item is not None and item.legacy_supplemented == []


class TestTypeIntroductionDate:
    """리뷰 라운드 1(H-08): 이력이 시작된 날에 없던 유형은 원래 작업에 들어갈 수 없다.

    다중 유형(ED·RT·TH)은 94f5cae(2025-11-19), PS는 b7dbb5b(2026-03-30)에 들어왔다. 그 전에
    시작한 이력에는 그 유형을 묻지 않는다 — 가장 흔한 PH 단일 유형 이력이 불필요하게 뒤 유형
    보충(대상 데이터 삭제 가능)으로 바뀌는 일을 줄인다.
    """

    RANGE = ("2026-01-01", "2026-01-02")
    PH = ["point_history_260101", "point_history_260102"]

    def _started(self, db, hid: int, when: datetime | None) -> None:
        with db.session_scope() as s:
            s.query(MigrationHistory).filter(MigrationHistory.id == hid).update(
                {"started_at": when}
            )

    def test_point_sec_history_is_not_asked_before_it_existed(self, history_db):
        hid = _legacy(*self.RANGE, self.PH)
        self._started(history_db, hid, datetime(2026, 3, 29, 23, 59))

        check = HistoryManager().prepare_resume(hid, _profile())

        cov = check.legacy
        assert cov is not None
        assert sorted(cov.undecided_types) == ["running_time_history", "trend_history"]
        assert cov.unavailable_types == ["point_sec_history"]
        assert "point_sec_history" not in check.message

    def test_single_type_era_history_needs_no_type_decision(self, history_db):
        hid = _legacy(*self.RANGE, self.PH)
        self._started(history_db, hid, datetime(2025, 11, 18, 12, 0))
        hm = HistoryManager()

        cov = hm.prepare_resume(hid, _profile()).legacy
        assert cov is not None and cov.undecided_types == {}

        adopted = hm.adopt_legacy_history(hid, _profile())
        assert adopted.verdict is ResumeVerdict.OK
        assert adopted.pending == self.PH

    def test_type_is_still_asked_on_the_day_it_was_introduced(self, history_db):
        hid = _legacy(*self.RANGE, self.PH)
        self._started(history_db, hid, datetime(2026, 3, 30, 9, 0))

        cov = HistoryManager().prepare_resume(hid, _profile()).legacy

        assert cov is not None
        assert "point_sec_history" in cov.undecided_types

    def test_unknown_start_time_asks_every_trailing_type(self, history_db):
        hid = _legacy(*self.RANGE, self.PH)
        self._started(history_db, hid, None)

        cov = HistoryManager().prepare_resume(hid, _profile()).legacy

        assert cov is not None
        assert sorted(cov.undecided_types) == [
            "point_sec_history",
            "running_time_history",
            "trend_history",
        ]
        assert cov.unavailable_types == []
