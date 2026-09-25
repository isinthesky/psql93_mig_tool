"""DBMT1 공용 테스트 벡터 — 검증 앱 쪽 확인.

벡터는 발급 서버(lgetech-license-server)의 `scripts/dbmt1_test_vectors.py`가 테스트 전용 고정
시드로 만든 것이다(운영 키와 무관). 서버 쪽 tests/test_dbmt1_vectors.py가 같은 파일을 쓴다.
서버가 발급한 키를 앱이 똑같이 읽는지, 둘 다 같은 키를 거부하는지를 고정한다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.licensing import payload as payload_mod
from src.licensing.payload import LicenseFormatError, verify_key

FIXTURE = Path(__file__).parent / "fixtures" / "dbmt1_vectors.json"
VECTORS = json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _vector_public_key(monkeypatch):
    monkeypatch.setattr(payload_mod, "LICENSE_PUBLIC_KEY_B32", VECTORS["public_key_b32"])


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["name"])
def test_server_issued_keys_verify(case):
    p = verify_key(case["key"])
    exp = case["expected"]
    assert (p.version, p.customer, p.serial) == (exp["v"], exp["cust"], exp["lic"])
    assert p.issued == date.fromisoformat(exp["iss"])
    assert p.expires == date.fromisoformat(exp["exp"])


@pytest.mark.parametrize("case", VECTORS["accepted_variants"], ids=lambda c: c["of"])
def test_human_copied_variants_are_accepted(case):
    original = next(v for v in VECTORS["valid"] if v["name"] == case["of"])
    assert verify_key(case["key"]) == verify_key(original["key"])


@pytest.mark.parametrize("case", VECTORS["rejected"], ids=lambda c: c["name"])
def test_rejected_vectors(case):
    with pytest.raises(LicenseFormatError):
        verify_key(case["key"])
