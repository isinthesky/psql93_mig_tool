"""키 문자열 인코딩과 서명 검증."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from src.licensing.payload import (
    LicenseFormatError,
    build_key,
    format_for_display,
    normalize,
    split_key,
    verify_key,
)


class TestRoundTrip:
    def test_issued_key_verifies(self, signing_key):
        key = signing_key(customer="OO전자", serial="deadbeef")
        payload = verify_key(key)
        assert payload.customer == "OO전자"
        assert payload.serial == "deadbeef"

    def test_korean_customer_name_survives(self, signing_key):
        """고객사명이 한글이면 UTF-8 왕복이 깨지기 쉽다."""
        key = signing_key(customer="주식회사 한글이름 테스트")
        assert verify_key(key).customer == "주식회사 한글이름 테스트"


class TestNormalization:
    """사용자가 메일로 받은 키를 붙여넣는 경로. 여기서 막히면 전화가 온다."""

    @pytest.mark.parametrize(
        "mangle",
        [
            lambda k: k.lower(),
            lambda k: format_for_display(k),
            lambda k: format_for_display(k).lower(),
            lambda k: f"  {k}  ",
            lambda k: k.replace("-", " "),
            lambda k: k + "\n",
        ],
        ids=["소문자", "5자끊기", "5자끊기+소문자", "앞뒤공백", "하이픈→공백", "줄바꿈"],
    )
    def test_messy_input_still_verifies(self, signing_key, mangle):
        key = signing_key(serial="norm01")
        assert verify_key(mangle(key)).serial == "norm01"

    def test_display_round_trips(self, signing_key):
        key = signing_key()
        assert normalize(format_for_display(key)) == normalize(key)


class TestRejection:
    def test_signature_tampering_rejected(self, signing_key):
        """서명 바이트를 직접 뒤집는다.

        문자열 마지막 글자를 바꾸는 방식은 쓰지 않는다 — 서명 64바이트(512비트)를
        Base32 103자(515비트)로 담으므로 끝의 3비트는 디코딩 때 버려진다.
        마지막 글자만 바꾸면 같은 서명으로 복원되어 '위조가 통과했다'는 오해를 부른다.
        """
        raw, signature = split_key(signing_key())
        broken = bytes([signature[0] ^ 0xFF]) + signature[1:]

        with pytest.raises(LicenseFormatError):
            verify_key(build_key(raw, broken))

    def test_payload_character_tampering_rejected(self, signing_key):
        """페이로드 문자를 하나 바꾼다. 여기는 잉여 비트 문제가 없다."""
        key = normalize(signing_key(customer="원래고객사"))
        target = len(key) // 2  # 페이로드 한가운데
        flipped = key[:target] + ("A" if key[target] != "A" else "B") + key[target + 1 :]

        with pytest.raises(LicenseFormatError):
            verify_key(flipped)

    def test_extending_expiry_rejected(self, signing_key):
        """서명을 그대로 두고 만료일만 늘리는 가장 흔한 시도."""
        key = signing_key(expires=date.today() - timedelta(days=1))
        raw, signature = split_key(key)

        data = json.loads(raw)
        data["exp"] = "2099-12-31"
        forged = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()

        with pytest.raises(LicenseFormatError):
            verify_key(build_key(forged, signature))

    def test_key_signed_by_other_party_rejected(self, signing_key):
        """다른 개인키로 만든 키. 위조 시도의 본체."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        attacker = Ed25519PrivateKey.generate()
        raw = json.dumps(
            {
                "v": 1,
                "cust": "무단",
                "iss": "2026-01-01",
                "exp": "2099-12-31",
                "lic": "forged",
            },
            separators=(",", ":"),
        ).encode()

        with pytest.raises(LicenseFormatError):
            verify_key(build_key(raw, attacker.sign(raw)))

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "NOTAKEY", "DBMT1-", "DBMT1-AAAA", "완전히다른문자열"],
    )
    def test_malformed_rejected(self, signing_key, bad):
        with pytest.raises(LicenseFormatError):
            verify_key(bad)

    def test_unsupported_version_rejected(self, signing_key):
        """서명은 유효하지만 앱이 모르는 버전. 조용히 해석하면 안 된다."""
        with pytest.raises(LicenseFormatError, match="버전"):
            verify_key(signing_key(version=99))

    def test_missing_public_key_rejects_everything(self, signing_key, monkeypatch):
        """공개키를 안 심고 빌드하면 전부 거부되어야 한다. 통과시키면 검증이 없는 것과 같다."""
        from src.licensing import payload as payload_mod

        key = signing_key()
        monkeypatch.setattr(payload_mod, "LICENSE_PUBLIC_KEY_B32", "")
        with pytest.raises(LicenseFormatError):
            verify_key(key)


class TestExpiryBoundary:
    """off-by-one 이면 고객사에서 하루 일찍 멈춘다."""

    def test_expiry_day_is_still_valid(self, signing_key):
        exp = date(2027, 3, 31)
        payload = verify_key(signing_key(expires=exp))
        assert not payload.is_expired(exp)
        assert payload.days_left(exp) == 0

    def test_day_after_expiry_is_expired(self, signing_key):
        exp = date(2027, 3, 31)
        payload = verify_key(signing_key(expires=exp))
        assert payload.is_expired(exp + timedelta(days=1))
