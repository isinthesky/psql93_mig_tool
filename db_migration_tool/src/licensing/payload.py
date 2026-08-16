"""라이선스 키 문자열의 인코딩·디코딩과 Ed25519 서명 검증.

키 형식:

    DBMT1-<payload_b32>-<signature_b32>

**페이로드 바이트를 그대로 실어 보낸다.** 검증할 때 JSON을 다시 만들지 않고,
Base32로 복원한 원본 바이트 위에서 서명을 확인한 뒤에야 파싱한다. 그래서 키 순서·
공백 같은 직렬화 차이가 서명을 깨뜨릴 여지가 없다(canonical JSON이 필요 없다).

하이픈은 눈으로 옮겨 적기 쉬우라고 넣는 장식이며 검증 전에 전부 제거한다.
그래도 경계가 모호해지지 않는 이유는 **Ed25519 서명이 항상 64바이트 = Base32 103자**로
고정이기 때문이다. 뒤에서 103자를 떼면 서명, 남은 앞부분이 페이로드다.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .keys import LICENSE_PUBLIC_KEY_B32

PREFIX = "DBMT1"

# Ed25519 서명은 64바이트로 고정. Base32(패딩 제거)로 103자가 된다.
SIGNATURE_BYTES = 64
SIGNATURE_B32_LEN = 103

# 지원하는 페이로드 버전. 모르는 버전은 거부한다 —
# 서명이 유효해도 의미가 다른 페이로드를 옛 앱이 멋대로 해석하면 안 된다.
SUPPORTED_VERSIONS = frozenset({1})

_GROUP = 5  # 표시할 때 하이픈으로 끊는 간격


class LicenseFormatError(Exception):
    """키 문자열이 형식에 맞지 않거나 서명이 유효하지 않다."""


@dataclass(frozen=True)
class LicensePayload:
    """서명이 검증된 라이선스 내용."""

    version: int
    customer: str
    issued: date
    expires: date
    serial: str

    def days_left(self, today: date) -> int:
        """만료까지 남은 일수. 만료 당일은 0이고 아직 유효하다."""
        return (self.expires - today).days

    def is_expired(self, today: date) -> bool:
        """만료 여부. `exp` **당일은 유효하다** — 하루 일찍 멈추면 고객사 사고다."""
        return today > self.expires


def _b32encode(raw: bytes) -> str:
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _b32decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 8)
    try:
        return base64.b32decode(text + padding, casefold=False)
    except Exception as exc:  # binascii.Error 등
        raise LicenseFormatError(f"키를 해독할 수 없습니다: {exc}") from exc


def normalize(key: str) -> str:
    """사람이 옮겨 적은 키를 검증 가능한 형태로 정리한다.

    붙여넣기 사고(줄바꿈, 하이픈, 소문자, 공백)를 여기서 흡수한다.
    """
    return "".join(ch for ch in (key or "").upper() if ch.isalnum())


def format_for_display(key: str) -> str:
    """키를 5자씩 끊어 보여준다. 정규화하면 원래 값으로 돌아온다."""
    body = normalize(key)
    if not body:
        return ""
    return "-".join(body[i : i + _GROUP] for i in range(0, len(body), _GROUP))


def build_key(payload_bytes: bytes, signature: bytes) -> str:
    """발급 도구가 쓰는 조립 함수. 앱은 쓰지 않는다."""
    if len(signature) != SIGNATURE_BYTES:
        raise ValueError(f"Ed25519 서명은 {SIGNATURE_BYTES}바이트여야 합니다.")
    return f"{PREFIX}-{_b32encode(payload_bytes)}-{_b32encode(signature)}"


def split_key(key: str) -> tuple[bytes, bytes]:
    """키 문자열에서 (페이로드 바이트, 서명 바이트)를 꺼낸다. 서명 검증은 하지 않는다."""
    body = normalize(key)
    if not body.startswith(PREFIX):
        raise LicenseFormatError("라이선스 키 형식이 아닙니다.")

    body = body[len(PREFIX) :]
    if len(body) <= SIGNATURE_B32_LEN:
        raise LicenseFormatError("라이선스 키가 잘렸습니다.")

    payload_b32 = body[:-SIGNATURE_B32_LEN]
    signature_b32 = body[-SIGNATURE_B32_LEN:]

    signature = _b32decode(signature_b32)
    if len(signature) != SIGNATURE_BYTES:
        raise LicenseFormatError("서명 길이가 올바르지 않습니다.")

    return _b32decode(payload_b32), signature


def _load_public_key() -> Ed25519PublicKey:
    raw = _b32decode(normalize(LICENSE_PUBLIC_KEY_B32))
    return Ed25519PublicKey.from_public_bytes(raw)


def _parse_payload(raw: bytes) -> LicensePayload:
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise LicenseFormatError(f"라이선스 내용을 읽을 수 없습니다: {exc}") from exc

    if not isinstance(data, dict):
        raise LicenseFormatError("라이선스 내용의 형식이 올바르지 않습니다.")

    version = data.get("v")
    if version not in SUPPORTED_VERSIONS:
        raise LicenseFormatError(
            f"지원하지 않는 라이선스 버전입니다: {version}. 프로그램을 새 버전으로 올리세요."
        )

    try:
        return LicensePayload(
            version=int(version),
            customer=str(data["cust"]),
            issued=date.fromisoformat(str(data["iss"])),
            expires=date.fromisoformat(str(data["exp"])),
            serial=str(data["lic"]),
        )
    except KeyError as exc:
        raise LicenseFormatError(f"라이선스에 필수 항목이 없습니다: {exc}") from exc
    except ValueError as exc:
        raise LicenseFormatError(f"라이선스의 날짜 형식이 올바르지 않습니다: {exc}") from exc


def verify_key(key: str) -> LicensePayload:
    """키 문자열을 검증하고 내용을 돌려준다.

    Raises:
        LicenseFormatError: 형식 오류, 서명 불일치, 지원하지 않는 버전.
    """
    if not LICENSE_PUBLIC_KEY_B32.strip():
        # 공개키를 심지 않고 빌드한 경우. 조용히 통과시키면 검증이 없는 것과 같다.
        raise LicenseFormatError("이 빌드에는 라이선스 공개키가 없습니다.")

    payload_bytes, signature = split_key(key)

    try:
        _load_public_key().verify(signature, payload_bytes)
    except InvalidSignature as exc:
        raise LicenseFormatError("라이선스 서명이 올바르지 않습니다.") from exc

    return _parse_payload(payload_bytes)
