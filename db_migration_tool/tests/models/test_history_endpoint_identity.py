"""H-08 — 이력은 만들어질 때의 endpoint에 묶여 있어야 한다.

예전 이력은 바뀔 수 있는 `profile_id`만 저장했다. 중단 뒤 같은 프로필의
source/target(또는 방향)을 바꾸고 '이어서 진행'을 누르면 남은 파티션이 새
endpoint로 복사되어 한 이력이 두 대상에 나뉘어 적재됐다.

이제 비밀을 뺀 endpoint identity 지문, migration mode, schema, plan 지문을
이력에 불변으로 저장하고, 재개 전에 현재 프로필과 비교한다.
비밀번호 교체는 identity가 아니므로 허용한다.
"""

from __future__ import annotations

import copy
from datetime import datetime

import pytest

from src.database.local_db import MigrationHistory
from src.models.history import (
    CheckpointManager,
    HistoryManager,
    ResumeVerdict,
    endpoint_fingerprint,
)
from src.models.profile import ConnectionProfile

PLAN = ["point_history_260101", "point_history_260102", "point_history_260103"]

SRC = {
    "host": "facreport.iptime.org",
    "port": 5446,
    "database": "bms93",
    "username": "migtool",
    "password": "old-pw",
    "ssl": False,
}
DST = {
    "host": "facreport.iptime.org",
    "port": 5445,
    "database": "bms30",
    "username": "migtool",
    "password": "old-pw",
    "ssl": False,
}


def _profile(src=None, dst=None) -> ConnectionProfile:
    return ConnectionProfile(
        id=1,
        name="p",
        source_config=copy.deepcopy(src or SRC),
        target_config=copy.deepcopy(dst or DST),
    )


def _history(profile=None) -> int:
    item = HistoryManager().create_planned_history(
        profile or _profile(), PLAN, "2026-01-01", "2026-01-03"
    )
    assert item.id is not None
    return item.id


class TestFingerprint:
    def test_password_is_not_part_of_the_identity(self):
        changed = dict(SRC, password="new-pw")
        assert endpoint_fingerprint(changed) == endpoint_fingerprint(SRC)

    def test_ssl_and_compat_mode_are_not_part_of_the_identity(self):
        changed = dict(SRC, ssl=True, compat_mode="9.3")
        assert endpoint_fingerprint(changed) == endpoint_fingerprint(SRC)

    def test_host_case_and_whitespace_do_not_matter(self):
        changed = dict(SRC, host="  FacReport.IPTIME.org ")
        assert endpoint_fingerprint(changed) == endpoint_fingerprint(SRC)

    @pytest.mark.parametrize(
        "field,value",
        [("host", "other.example"), ("port", 5447), ("database", "bms94"), ("username", "x")],
    )
    def test_each_identity_field_changes_the_fingerprint(self, field, value):
        assert endpoint_fingerprint(dict(SRC, **{field: value})) != endpoint_fingerprint(SRC)

    def test_fingerprint_does_not_leak_secrets(self):
        fp = endpoint_fingerprint(SRC)
        assert "old-pw" not in fp and "migtool" not in fp and "bms93" not in fp

    def test_stored_history_contains_no_password(self, history_db):
        hid = _history()
        with history_db.session_scope() as s:
            row = s.query(MigrationHistory).filter_by(id=hid).one()
            stored = " ".join(str(v) for v in row.__dict__.values())
        assert "old-pw" not in stored


class TestResumeIdentityGate:
    def test_same_profile_resumes(self, history_db):
        hid = _history()
        check = HistoryManager().prepare_resume(hid, _profile())
        assert check.verdict is ResumeVerdict.OK
        assert check.pending == PLAN

    def test_password_rotation_still_resumes(self, history_db):
        hid = _history()
        rotated = _profile(dict(SRC, password="new-pw"), dict(DST, password="new-pw2"))

        check = HistoryManager().prepare_resume(hid, rotated)

        assert check.verdict is ResumeVerdict.OK
        assert check.allowed

    @pytest.mark.parametrize(
        "src,dst",
        [
            (SRC, dict(DST, database="temp")),  # 대상 DB 변경
            (SRC, dict(DST, host="10.0.0.9")),  # 대상 호스트 변경
            (dict(SRC, port=5432), DST),  # 원본 포트 변경
            (SRC, dict(DST, username="postgres")),  # 대상 사용자 변경
            (DST, SRC),  # 방향 뒤집기
        ],
    )
    def test_identity_change_is_refused(self, history_db, src, dst):
        hid = _history()

        check = HistoryManager().prepare_resume(hid, _profile(src, dst))

        assert check.verdict is ResumeVerdict.IDENTITY_CHANGED
        assert not check.allowed
        assert check.pending == []
        # 무엇이 바뀌었는지 말해야 사용자가 프로필을 되돌릴 수 있다.
        assert check.problems

    def test_mode_change_is_refused(self, history_db):
        hid = _history()
        to_file = _profile(SRC, {"kind": "file", "archive_path": "D:/archive"})

        check = HistoryManager().prepare_resume(hid, to_file)

        assert check.verdict is ResumeVerdict.IDENTITY_CHANGED

    def test_schema_change_is_refused(self, history_db):
        hid = _history()
        check = HistoryManager().prepare_resume(hid, _profile(), schema="other")
        assert check.verdict is ResumeVerdict.IDENTITY_CHANGED

    def test_refusal_does_not_touch_checkpoints(self, history_db):
        hid = _history()
        before = [(c.partition_name, c.status) for c in CheckpointManager().get_checkpoints(hid)]

        HistoryManager().prepare_resume(hid, _profile(SRC, dict(DST, database="temp")))

        after = [(c.partition_name, c.status) for c in CheckpointManager().get_checkpoints(hid)]
        assert after == before

    def test_unknown_history(self, history_db):
        check = HistoryManager().prepare_resume(424242, _profile())
        assert check.verdict is ResumeVerdict.NOT_FOUND
        assert not check.allowed


class TestLegacyHistory:
    """지문이 없는 구버전 이력 — 자동 재개하지 않고 명시적 확인을 요구한다."""

    def _legacy(self) -> int:
        hm, cm = HistoryManager(), CheckpointManager()
        item = hm.create_history(1, "2026-01-01", "2026-01-03")
        assert item.id is not None
        for name in PLAN:
            cm.create_checkpoint(item.id, name)
        done = cm.get_checkpoints(item.id)[0]
        cm.update_checkpoint_status(done.id, "completed")
        return item.id

    def test_legacy_history_needs_explicit_confirmation(self, history_db):
        hid = self._legacy()

        check = HistoryManager().prepare_resume(hid, _profile())

        assert check.verdict is ResumeVerdict.LEGACY
        assert not check.allowed
        # 확인 창에 보여 줄 수 있도록 무엇을 재개할지는 알려 준다.
        assert check.pending == PLAN[1:]

    def test_adoption_binds_current_identity_and_existing_plan(self, history_db):
        hid = self._legacy()
        hm = HistoryManager()

        adopted = hm.adopt_legacy_history(hid, _profile())

        assert adopted.verdict is ResumeVerdict.OK
        assert adopted.pending == PLAN[1:]
        item = hm.get_history(hid)
        assert item is not None
        assert item.plan_version == 1
        assert item.planned_count == len(PLAN)
        assert isinstance(item.legacy_adopted_at, datetime)

        # 채택 이후에는 엄격 모드: endpoint를 바꾸면 거부된다.
        moved = hm.prepare_resume(hid, _profile(SRC, dict(DST, database="temp")))
        assert moved.verdict is ResumeVerdict.IDENTITY_CHANGED

    def test_adoption_refuses_already_bound_history(self, history_db):
        hid = _history()
        with pytest.raises(ValueError):
            HistoryManager().adopt_legacy_history(hid, _profile(SRC, dict(DST, database="temp")))
