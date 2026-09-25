"""OS 신뢰 CA 번들 해석 테스트 (감사 H-05 리뷰 지적: `sslrootcert=system` 무력화)

psycopg/psycopg2 바이너리 휠의 libpq는 번들 OpenSSL 빌드 시점의 OPENSSLDIR
(macOS `/tmp/libpq.build`, Windows `C:\\Program Files\\Common Files\\SSL`)을 기본 신뢰
저장소로 쓴다. 그 경로는 비어 있거나 존재하지 않아 `sslrootcert=system`은 공인 CA 서버까지
'certificate verify failed'로 거부한다. 그래서 CA 파일을 지정하지 않은 프로필은 OS 신뢰 저장소를
실제 PEM 파일로 찾아 sslrootcert에 넘겨야 한다.
"""

from __future__ import annotations

import datetime as dt
import ssl
import sys
from pathlib import Path

import pytest

from src.database import system_ca

SERVER_AUTH = "1.3.6.1.5.5.7.3.1"
EMAIL_PROTECTION = "1.3.6.1.5.5.7.3.4"


def _ca_der(common_name: str) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


def _pem_bundle(path: Path, *names: str) -> Path:
    path.write_text("".join(ssl.DER_cert_to_PEM_cert(_ca_der(n)) for n in names))
    return path


def _ca_count(path: Path) -> int:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=str(path))
    return int(ctx.cert_store_stats()["x509_ca"])


def _no_cache_dir():
    raise AssertionError("Windows가 아니면 캐시 디렉터리(앱 데이터)를 만들면 안 됩니다")


# ── 파일 번들 탐색 (macOS·Linux) ─────────────────────────────


class TestFileBundles:
    def test_first_candidate_with_certificates_wins(self, tmp_path):
        empty = tmp_path / "empty.pem"
        empty.write_text("")
        garbage = tmp_path / "garbage.pem"
        garbage.write_text("not a certificate")
        good = _pem_bundle(tmp_path / "good.pem", "root-a")

        found = system_ca.find_ca_bundle(
            env_file=None,
            platform="darwin",
            candidates=[tmp_path / "missing.pem", empty, garbage, good],
            cache_dir=_no_cache_dir,
        )

        assert found == good

    def test_ssl_cert_file_env_takes_precedence(self, tmp_path):
        """관리자가 SSL_CERT_FILE로 지정한 번들(사내 CA 포함)을 존중한다."""
        env_bundle = _pem_bundle(tmp_path / "corp.pem", "corp-root")
        other = _pem_bundle(tmp_path / "os.pem", "os-root")

        found = system_ca.find_ca_bundle(
            env_file=str(env_bundle),
            platform="darwin",
            candidates=[other],
            cache_dir=_no_cache_dir,
        )

        assert found == env_bundle

    def test_invalid_ssl_cert_file_env_is_ignored(self, tmp_path):
        other = _pem_bundle(tmp_path / "os.pem", "os-root")

        found = system_ca.find_ca_bundle(
            env_file=str(tmp_path / "nope.pem"),
            platform="linux",
            candidates=[other],
            cache_dir=_no_cache_dir,
        )

        assert found == other

    def test_nothing_usable_returns_none(self, tmp_path):
        """쓸 수 있는 저장소가 없으면 None — 호출 측이 'CA 파일 지정' 오류를 낸다."""
        found = system_ca.find_ca_bundle(
            env_file=None,
            platform="linux",
            candidates=[tmp_path / "missing.pem"],
            cache_dir=_no_cache_dir,
        )
        assert found is None

    def test_default_candidates_cover_common_os_bundles(self):
        # Path 비교 — Windows에서 str(Path)는 구분자가 역슬래시가 된다.
        paths = set(system_ca.default_candidates())
        assert Path("/etc/ssl/cert.pem") in paths  # macOS, Alpine
        assert Path("/etc/ssl/certs/ca-certificates.crt") in paths  # Debian/Ubuntu
        assert Path("/etc/pki/tls/certs/ca-bundle.crt") in paths  # RHEL 계열


# ── Windows 인증서 저장소 내보내기 ───────────────────────────


class TestWindowsStore:
    def _fake_store(self, entries_by_store):
        calls = []

        def enum_certificates(store_name):
            calls.append(store_name)
            if store_name not in entries_by_store:
                raise PermissionError(store_name)
            return entries_by_store[store_name]

        return enum_certificates, calls

    def test_exports_server_auth_roots_to_pem(self, tmp_path):
        root_any = _ca_der("root-any-purpose")
        root_tls = _ca_der("root-server-auth")
        root_mail = _ca_der("root-email-only")
        enum, calls = self._fake_store(
            {
                "ROOT": [
                    (root_any, "x509_asn", True),
                    (root_tls, "x509_asn", {SERVER_AUTH}),
                    (root_mail, "x509_asn", {EMAIL_PROTECTION}),
                    (b"\x00", "pkcs_7_asn", True),
                ],
                "CA": [(root_any, "x509_asn", True)],  # 중복은 한 번만
            }
        )

        found = system_ca.find_ca_bundle(
            env_file=None,
            platform="win32",
            candidates=[],
            cache_dir=lambda: tmp_path / "tls",
            enum_certificates=enum,
        )

        assert found is not None
        assert found.parent == tmp_path / "tls"
        assert set(calls) == {"ROOT", "CA"}
        assert _ca_count(found) == 2  # 서버 인증 용도만(이메일 전용 제외)
        text = found.read_text()
        assert ssl.DER_cert_to_PEM_cert(root_mail) not in text

    def test_export_is_stable_across_calls(self, tmp_path):
        enum, _ = self._fake_store({"ROOT": [(_ca_der("r"), "x509_asn", True)], "CA": []})
        kwargs = {
            "env_file": None,
            "platform": "win32",
            "candidates": [],
            "cache_dir": lambda: tmp_path,
            "enum_certificates": enum,
        }

        first = system_ca.find_ca_bundle(**kwargs)
        second = system_ca.find_ca_bundle(**kwargs)

        assert first == second
        assert len(list(tmp_path.glob("*.pem"))) == 1

    def test_changed_store_replaces_old_export(self, tmp_path):
        for name in ("old-root", "new-root"):
            enum, _ = self._fake_store({"ROOT": [(_ca_der(name), "x509_asn", True)], "CA": []})
            latest = system_ca.find_ca_bundle(
                env_file=None,
                platform="win32",
                candidates=[],
                cache_dir=lambda: tmp_path,
                enum_certificates=enum,
            )
        assert list(tmp_path.glob("*.pem")) == [latest]

    def test_empty_windows_store_falls_back_to_candidates(self, tmp_path):
        enum, _ = self._fake_store({"ROOT": [], "CA": []})
        fallback = _pem_bundle(tmp_path / "fallback.pem", "r")

        found = system_ca.find_ca_bundle(
            env_file=None,
            platform="win32",
            candidates=[fallback],
            cache_dir=lambda: tmp_path / "tls",
            enum_certificates=enum,
        )

        assert found == fallback

    def test_windows_env_override_skips_store_export(self, tmp_path):
        env_bundle = _pem_bundle(tmp_path / "corp.pem", "corp-root")

        def enum(_store):
            raise AssertionError("SSL_CERT_FILE이 있으면 저장소를 내보내지 않는다")

        found = system_ca.find_ca_bundle(
            env_file=str(env_bundle),
            platform="win32",
            candidates=[],
            cache_dir=_no_cache_dir,
            enum_certificates=enum,
        )
        assert found == env_bundle


# ── 실제 플랫폼 스모크 ───────────────────────────────────────


class TestRealPlatformStore:
    def test_default_store_on_this_platform_is_not_empty(self, tmp_path):
        """배포 대상(macOS·Windows)에서 기본 저장소가 비어 있지 않아야 공인 CA 서버에 붙는다."""
        found = system_ca.find_ca_bundle(
            env_file=None,
            platform=sys.platform,
            candidates=system_ca.default_candidates(),
            cache_dir=lambda: tmp_path / "tls",
        )

        assert found is not None
        assert _ca_count(found) > 10

    def test_resolve_caches_per_process_and_follows_env(self, tmp_path, monkeypatch):
        bundle = _pem_bundle(tmp_path / "env.pem", "env-root")
        # Windows에서도 실제 앱 데이터 디렉터리에 쓰지 않는다
        monkeypatch.setattr(system_ca, "_default_cache_dir", lambda: tmp_path / "tls")
        system_ca.clear_cache()
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
        try:
            assert system_ca.resolve_system_ca_bundle() == bundle
            monkeypatch.delenv("SSL_CERT_FILE")
            assert system_ca.resolve_system_ca_bundle() != bundle
        finally:
            system_ca.clear_cache()


@pytest.fixture(autouse=True)
def _reset_cache():
    system_ca.clear_cache()
    yield
    system_ca.clear_cache()
