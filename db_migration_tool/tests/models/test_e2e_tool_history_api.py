"""tools/e2e_verify_copy.py가 바뀐 이력 API와 계속 맞물리는가(실DB 없이).

도구는 실DB가 있어야 끝까지 돌지만, 이력을 만들고 재개를 검증하는 부분은 로컬 SQLite만
쓴다. API가 바뀌어 도구가 조용히 깨지는 일을 여기서 막는다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from src.models.history import HistoryManager, ResumeVerdict
from src.models.profile import ConnectionProfile

TOOL = Path(__file__).resolve().parents[2] / "tools" / "e2e_verify_copy.py"

SRC = {"host": "s", "port": 5446, "database": "bms93", "username": "migtool", "password": "a"}
DST = {"host": "d", "port": 5445, "database": "temp", "username": "migtool", "password": "b"}


def _load_tool():
    spec = importlib.util.spec_from_file_location("e2e_verify_copy", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_history_flow_matches_the_app(history_db):
    tool = _load_tool()
    hm = HistoryManager()
    profile = ConnectionProfile(id=0, name="e2e", source_config=SRC, target_config=DST)

    hid = hm.create_planned_history(profile, ["point_history_260420"], "e2e", "e2e").id
    assert hid is not None

    assert tool._identity_gate_ok(hm, hid, SRC, DST) is True
    check = hm.prepare_resume(hid, profile)
    assert check.verdict is ResumeVerdict.OK
    assert check.pending == ["point_history_260420"]
