"""라이선스 테스트 공용 픽스처.

진짜 Ed25519 키 쌍을 만들어 실제로 서명하고 검증한다. 서명을 목으로 대체하면
"검증이 통과한다"는 사실이 아무것도 보장하지 못한다.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.licensing import payload as payload_mod
from src.licensing.payload import _b32encode, build_key
from src.utils.app_paths import AppPaths


@pytest.fixture
def signing_key(monkeypatch):
    """앱에 심긴 공개키를 테스트 전용 키 쌍으로 바꾼다.

    Returns:
        키를 발급하는 함수. `make(exp=..., cust=...)` 형태로 부른다.
    """
    private = Ed25519PrivateKey.generate()
    public_raw = private.public_key().public_bytes_raw()
    monkeypatch.setattr(payload_mod, "LICENSE_PUBLIC_KEY_B32", _b32encode(public_raw))

    def make(
        expires: date | None = None,
        customer: str = "테스트고객사",
        serial: str = "abc123",
        version: int = 1,
        issued: date | None = None,
    ) -> str:
        today = date.today()
        data = {
            "v": version,
            "cust": customer,
            "iss": (issued or today).isoformat(),
            "exp": (expires or today + timedelta(days=365)).isoformat(),
            "lic": serial,
        }
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return build_key(raw, private.sign(raw))

    make.private = private  # type: ignore[attr-defined]
    return make


@pytest.fixture
def app_dir(tmp_path):
    """license.key / .activation 이 쓰이는 곳을 임시 폴더로 돌린다."""
    AppPaths.set_custom_root(tmp_path)
    yield tmp_path
    AppPaths.set_custom_root(None)


@pytest.fixture
def fixed_machine(monkeypatch):
    """머신 ID를 고정한다. 실제 PC 값에 테스트가 흔들리지 않게."""
    from src.licensing import machine

    current = {"id": "machine-aaaa"}
    monkeypatch.setattr(machine, "get_machine_id", lambda: current["id"])
    return current
