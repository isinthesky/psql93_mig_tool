"""테스트용 키 보호기 — DPAPI를 흉내 낸다.

실제 DPAPI는 "이 사용자·이 장치"에서만 풀린다. 여기서는 그 범위를 `machine_secret`으로
모사한다. 같은 비밀로 만든 보호기끼리만 서로의 blob을 풀 수 있다.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from src.utils.secret_store import KeyProtectError, KeyUnprotectError


class FakeDpapi:
    scheme = "dpapi"
    os_protected = True

    def __init__(self, machine_secret: bytes = b"machine-A/user-1", *, fail_protect: bool = False):
        derived = hashlib.sha256(b"fake-dpapi:" + machine_secret).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))
        self.fail_protect = fail_protect
        self.protect_calls = 0

    def protect(self, data: bytes) -> bytes:
        self.protect_calls += 1
        if self.fail_protect:
            raise KeyProtectError("주입된 보호 실패")
        return self._fernet.encrypt(data)

    def unprotect(self, blob: bytes) -> bytes:
        try:
            return self._fernet.decrypt(blob)
        except InvalidToken as exc:
            raise KeyUnprotectError("다른 사용자·장치 범위의 blob") from exc
