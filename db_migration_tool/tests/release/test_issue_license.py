"""발급 도구의 개인키 보호(감사 M-06).

개인키는 반드시 암호화(PKCS#8 + BestAvailableEncryption)로 저장되고, 패스프레이즈는
대화형 입력이나 환경변수로만 받는다. 평문 키는 경고와 함께 '변환'에만 쓸 수 있다.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
import sys
from datetime import date
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.licensing import payload as payload_mod
from src.licensing.payload import _b32encode, verify_key

TOOL_PATH = Path(__file__).resolve().parents[2] / "tools" / "issue_license.py"
PASSPHRASE = "correct horse battery staple"
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX 권한 비트 검사")


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("issue_license_under_test", TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env_passphrase(monkeypatch, tool):
    monkeypatch.setenv(tool.PASSPHRASE_ENV, PASSPHRASE)
    return PASSPHRASE


def _raw_private(private: Ed25519PrivateKey) -> bytes:
    return private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_raw(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


def _write_legacy_raw_key(path: Path) -> Ed25519PrivateKey:
    private = Ed25519PrivateKey.generate()
    path.write_bytes(_raw_private(private))
    path.chmod(0o600)
    return private


class TestGenerateEncrypted:
    def test_private_key_is_encrypted_pkcs8(self, tool, tmp_path):
        out = tmp_path / "issuer.key"
        tool.generate_keypair(out, PASSPHRASE.encode())

        data = out.read_bytes()
        assert data.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
        # 패스프레이즈 없이는 열리지 않는다.
        with pytest.raises(TypeError):
            serialization.load_pem_private_key(data, password=None)
        private = serialization.load_pem_private_key(data, password=PASSPHRASE.encode())
        assert isinstance(private, Ed25519PrivateKey)
        # 평문 32바이트가 파일 어디에도 없다.
        assert _raw_private(private) not in data

    @POSIX_ONLY
    def test_file_mode_is_0600(self, tool, tmp_path):
        out = tmp_path / "issuer.key"
        tool.generate_keypair(out, PASSPHRASE.encode())
        assert stat.S_IMODE(out.stat().st_mode) == 0o600

    def test_returns_public_key_and_fingerprint(self, tool, tmp_path):
        out = tmp_path / "issuer.key"
        public_b32, fingerprint = tool.generate_keypair(out, PASSPHRASE.encode())

        private = tool.load_private_key(out, lambda: PASSPHRASE.encode())
        assert public_b32 == _b32encode(_public_raw(private))
        assert fingerprint == "SHA256:" + hashlib.sha256(_public_raw(private)).hexdigest()

    def test_refuses_existing_file(self, tool, tmp_path):
        out = tmp_path / "issuer.key"
        out.write_bytes(b"keep me")
        with pytest.raises(SystemExit):
            tool.generate_keypair(out, PASSPHRASE.encode())
        assert out.read_bytes() == b"keep me"

    @pytest.mark.parametrize("weak", [b"", b"short"])
    def test_refuses_weak_passphrase(self, tool, tmp_path, weak):
        out = tmp_path / "issuer.key"
        with pytest.raises(SystemExit):
            tool.generate_keypair(out, weak)
        assert not out.exists()


class TestLoadEncrypted:
    def test_wrong_passphrase_is_rejected(self, tool, tmp_path):
        out = tmp_path / "issuer.key"
        tool.generate_keypair(out, PASSPHRASE.encode())
        with pytest.raises(SystemExit) as info:
            tool.load_private_key(out, lambda: b"wrong passphrase!!")
        assert "패스프레이즈" in str(info.value)

    @POSIX_ONLY
    def test_group_or_world_readable_key_is_rejected(self, tool, tmp_path):
        out = tmp_path / "issuer.key"
        tool.generate_keypair(out, PASSPHRASE.encode())
        out.chmod(0o644)
        with pytest.raises(SystemExit) as info:
            tool.load_private_key(out, lambda: PASSPHRASE.encode())
        assert "0600" in str(info.value)


class TestLegacyPlaintext:
    """기존 평문 키(raw 32바이트)는 경고와 함께 변환만 허용한다."""

    def test_legacy_key_cannot_be_used_to_issue(self, tool, tmp_path):
        legacy = tmp_path / "legacy.key"
        _write_legacy_raw_key(legacy)

        asked = []
        with pytest.raises(SystemExit) as info:
            tool.load_private_key(legacy, lambda: asked.append(1) or PASSPHRASE.encode())
        assert "--convert-legacy" in str(info.value)
        assert asked == []  # 패스프레이즈를 묻기 전에 거부한다

    def test_unencrypted_pem_is_also_legacy(self, tool, tmp_path):
        legacy = tmp_path / "legacy.pem"
        private = Ed25519PrivateKey.generate()
        legacy.write_bytes(
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        with pytest.raises(SystemExit) as info:
            tool.load_private_key(legacy, lambda: PASSPHRASE.encode())
        assert "--convert-legacy" in str(info.value)

    def test_convert_preserves_key_and_warns(self, tool, tmp_path, capsys):
        legacy = tmp_path / "legacy.key"
        private = _write_legacy_raw_key(legacy)
        out = tmp_path / "converted.key"

        fingerprint = tool.convert_legacy_key(legacy, out, PASSPHRASE.encode())

        converted = tool.load_private_key(out, lambda: PASSPHRASE.encode())
        assert _raw_private(converted) == _raw_private(private)
        assert fingerprint == "SHA256:" + hashlib.sha256(_public_raw(private)).hexdigest()
        assert out.read_bytes().startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
        # 원본은 도구가 지우지 않는다(되돌릴 수 없는 작업이라 사람이 한다). 경고만 한다.
        assert legacy.exists()
        assert "평문" in capsys.readouterr().err

    def test_convert_refuses_same_path(self, tool, tmp_path):
        legacy = tmp_path / "legacy.key"
        _write_legacy_raw_key(legacy)
        with pytest.raises(SystemExit):
            tool.convert_legacy_key(legacy, legacy, PASSPHRASE.encode())

    def test_convert_rejects_already_encrypted(self, tool, tmp_path):
        enc = tmp_path / "enc.key"
        tool.generate_keypair(enc, PASSPHRASE.encode())
        with pytest.raises(SystemExit):
            tool.convert_legacy_key(enc, tmp_path / "again.key", PASSPHRASE.encode())


class TestPassphraseSource:
    def test_no_passphrase_cli_argument(self, tool):
        parser = tool.build_parser()
        for flag in ("--passphrase", "--password", "--pass"):
            with pytest.raises(SystemExit):
                parser.parse_args(["--key-file", "k", flag, "x"])

    def test_env_passphrase(self, tool, env_passphrase):
        assert tool.read_passphrase(confirm=False) == env_passphrase.encode()

    def test_empty_env_is_rejected(self, tool, monkeypatch):
        monkeypatch.setenv(tool.PASSPHRASE_ENV, "")
        with pytest.raises(SystemExit):
            tool.read_passphrase(confirm=False)

    def test_non_interactive_without_env_is_rejected(self, tool, monkeypatch):
        monkeypatch.delenv(tool.PASSPHRASE_ENV, raising=False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        with pytest.raises(SystemExit):
            tool.read_passphrase(confirm=False)

    def test_interactive_confirmation_mismatch(self, tool, monkeypatch):
        monkeypatch.delenv(tool.PASSPHRASE_ENV, raising=False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        answers = iter(["first passphrase!!", "second passphrase!"])
        monkeypatch.setattr(tool.getpass, "getpass", lambda prompt="": next(answers))
        with pytest.raises(SystemExit):
            tool.read_passphrase(confirm=True)


class TestRestrictPermissionsWindows:
    def test_icacls_is_used_on_windows(self, tool, tmp_path, monkeypatch):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)

            class Done:
                returncode = 0

            return Done()

        monkeypatch.setattr(tool.subprocess, "run", fake_run)
        target = tmp_path / "k"
        target.write_bytes(b"x")
        tool.restrict_permissions(target, platform="nt")
        assert calls and calls[0][0] == "icacls"
        assert "/inheritance:r" in calls[0]


class TestMainEndToEnd:
    def test_genkey_then_issue(self, tool, tmp_path, env_passphrase, monkeypatch, capsys):
        key_file = tmp_path / "issuer.key"
        assert tool.main(["--genkey", "--out-private", str(key_file)]) == 0
        out = capsys.readouterr().out
        assert "SHA256:" in out
        assert PASSPHRASE not in out

        private = tool.load_private_key(key_file, lambda: PASSPHRASE.encode())
        monkeypatch.setattr(payload_mod, "LICENSE_PUBLIC_KEY_B32", _b32encode(_public_raw(private)))

        exp = date(date.today().year + 1, 12, 31).isoformat()
        assert tool.main(["--key-file", str(key_file), "--cust", "테스트", "--exp", exp]) == 0
        printed = capsys.readouterr().out
        key_line = next(line for line in printed.splitlines() if line.startswith("DBMT1-"))
        assert verify_key(key_line).customer == "테스트"
        assert "SHA256:" in printed  # 어느 키로 서명했는지 남긴다

    def test_convert_legacy_cli(self, tool, tmp_path, env_passphrase, capsys):
        legacy = tmp_path / "legacy.key"
        _write_legacy_raw_key(legacy)
        out = tmp_path / "new.key"
        rc = tool.main(["--convert-legacy", "--key-file", str(legacy), "--out-private", str(out)])
        assert rc == 0
        assert out.read_bytes().startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
        assert "SHA256:" in capsys.readouterr().out


def _server_signer_load(path: Path, expected_public_b32: str) -> Ed25519PrivateKey:
    """라이선스 서버 signer의 키 로드 계약(lgetech-license-server app/signer.py load_private_key).

    서버는 raw 32바이트 파일만 읽고, 그 공개키가 고정 공개키(Base32, 패딩 없음)와 같아야 한다.
    다른 저장소라 import 하지 않고 계약만 재현한다.
    """
    raw = path.read_bytes()
    if len(raw) != 32:
        raise RuntimeError("Ed25519 개인키는 정확히 32바이트여야 합니다.")
    private = Ed25519PrivateKey.from_private_bytes(raw)
    if _b32encode(_public_raw(private)) != expected_public_b32:
        raise RuntimeError("개인키가 고정 공개키와 일치하지 않습니다.")
    return private


class TestExportSignerRaw:
    """키 교체 때 서버 signer용 raw 32바이트 사본을 만드는 유일한 경로(리뷰 지적 — M-06 후속).

    서버 signer는 raw 32바이트만 읽으므로, 도구가 이 형식을 내보내지 못하면 운영자가 즉석에서
    평문 사본을 만들게 된다. 내보내기는 암호화 원본에서만, 새 파일(O_EXCL·0600)로만 한다.
    """

    def test_export_matches_server_signer_contract(self, tool, tmp_path):
        key_file = tmp_path / "issuer.key"
        public_b32, fingerprint = tool.generate_keypair(key_file, PASSPHRASE.encode())
        out = tmp_path / "license_private.key"

        exported_fp = tool.export_signer_raw(key_file, out, lambda: PASSPHRASE.encode())

        assert exported_fp == fingerprint
        assert len(out.read_bytes()) == 32
        loaded = _server_signer_load(out, public_b32)
        original = tool.load_private_key(key_file, lambda: PASSPHRASE.encode())
        assert _raw_private(loaded) == _raw_private(original)

    @POSIX_ONLY
    def test_export_file_mode_is_0600(self, tool, tmp_path):
        key_file = tmp_path / "issuer.key"
        tool.generate_keypair(key_file, PASSPHRASE.encode())
        out = tmp_path / "license_private.key"
        tool.export_signer_raw(key_file, out, lambda: PASSPHRASE.encode())
        assert stat.S_IMODE(out.stat().st_mode) == 0o600

    def test_export_refuses_existing_output(self, tool, tmp_path):
        key_file = tmp_path / "issuer.key"
        tool.generate_keypair(key_file, PASSPHRASE.encode())
        out = tmp_path / "license_private.key"
        out.write_bytes(b"keep me")
        with pytest.raises(SystemExit):
            tool.export_signer_raw(key_file, out, lambda: PASSPHRASE.encode())
        assert out.read_bytes() == b"keep me"

    def test_export_refuses_same_path_as_source(self, tool, tmp_path):
        key_file = tmp_path / "issuer.key"
        tool.generate_keypair(key_file, PASSPHRASE.encode())
        before = key_file.read_bytes()
        with pytest.raises(SystemExit):
            tool.export_signer_raw(key_file, key_file, lambda: PASSPHRASE.encode())
        assert key_file.read_bytes() == before

    def test_export_requires_encrypted_source(self, tool, tmp_path):
        legacy = tmp_path / "legacy.key"
        _write_legacy_raw_key(legacy)
        out = tmp_path / "license_private.key"
        with pytest.raises(SystemExit):
            tool.export_signer_raw(legacy, out, lambda: PASSPHRASE.encode())
        assert not out.exists()

    def test_wrong_passphrase_writes_nothing(self, tool, tmp_path):
        key_file = tmp_path / "issuer.key"
        tool.generate_keypair(key_file, PASSPHRASE.encode())
        out = tmp_path / "license_private.key"
        with pytest.raises(SystemExit):
            tool.export_signer_raw(key_file, out, lambda: b"wrong passphrase!!")
        assert not out.exists()

    def test_exported_raw_cannot_be_used_to_issue(self, tool, tmp_path):
        """내보낸 raw 사본이 도구의 새 발급 경로가 되지 않는다(평문 키로 취급해 거부)."""
        key_file = tmp_path / "issuer.key"
        tool.generate_keypair(key_file, PASSPHRASE.encode())
        out = tmp_path / "license_private.key"
        tool.export_signer_raw(key_file, out, lambda: PASSPHRASE.encode())
        with pytest.raises(SystemExit) as info:
            tool.load_private_key(out, lambda: PASSPHRASE.encode())
        assert "--convert-legacy" in str(info.value)

    def test_export_cli_prints_key_id_and_warns(self, tool, tmp_path, env_passphrase, capsys):
        key_file = tmp_path / "issuer.key"
        public_b32, fingerprint = tool.generate_keypair(key_file, PASSPHRASE.encode())
        out = tmp_path / "license_private.key"

        rc = tool.main(["--export-signer-raw", str(out), "--key-file", str(key_file)])

        assert rc == 0
        captured = capsys.readouterr()
        assert fingerprint in captured.out
        assert public_b32 in captured.out  # 서버 EXPECTED_PUBLIC_KEY_B32 와 대조할 값
        assert "평문" in captured.err  # 평문 사본이라는 경고와 폐기 안내
        assert PASSPHRASE not in captured.out + captured.err
        _server_signer_load(out, public_b32)

    def test_export_cli_requires_key_file(self, tool, tmp_path, env_passphrase):
        with pytest.raises(SystemExit):
            tool.main(["--export-signer-raw", str(tmp_path / "out.key")])


class TestRotationDocs:
    """교체 절차가 서버 signer 키 형식(raw 32바이트)과 만드는 방법을 명시한다."""

    GUIDE = Path(__file__).resolve().parents[2] / "LICENSE_GUIDE.md"

    @pytest.mark.parametrize("source", ["docstring", "guide"])
    def test_rotation_names_signer_export(self, tool, source):
        text = tool.__doc__ if source == "docstring" else self.GUIDE.read_text(encoding="utf-8")
        assert "--export-signer-raw" in text
        assert "license_private.key" in text
        assert "32바이트" in text
