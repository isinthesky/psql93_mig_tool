"""OS 신뢰 CA 번들 해석 (libpq `sslrootcert=system` 대체)

psycopg/psycopg2 바이너리 휠의 libpq는 번들 OpenSSL을 쓰고, 그 OpenSSL은 빌드 시점의
OPENSSLDIR(macOS `/tmp/libpq.build`, Windows `C:\\Program Files\\Common Files\\SSL`)을 기본
신뢰 저장소로 삼는다. 배포 PC에서 그 경로는 비어 있거나 존재하지 않으므로
`sslrootcert=system`은 공인 CA 서버까지 'certificate verify failed'로 거부한다.

그래서 CA 파일을 지정하지 않은 SSL 프로필은 OS 신뢰 저장소를 **실제 PEM 파일 경로**로 찾아
sslrootcert에 직접 넘긴다. 파일 경로는 모든 libpq 버전이 지원한다.

탐색 순서
1. 환경변수 SSL_CERT_FILE — 관리자가 지정한 번들(사내 CA 포함)을 존중한다.
2. Windows: 인증서 저장소 ROOT·CA에서 서버 인증 용도 인증서를 PEM으로 내보내
   앱 데이터 디렉터리(`tls/`)에 캐시한다(Python `ssl.load_default_certs`와 같은 규칙).
3. 잘 알려진 OS 번들 파일(Python ssl 기본 cafile, macOS·Debian·RHEL 경로).

주의: macOS 키체인에만 추가한 사내 CA는 `/etc/ssl/cert.pem`에 없다. 그런 서버는
연결 설정에서 CA 파일을 지정해야 한다.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import ssl
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SERVER_AUTH_OID = "1.3.6.1.5.5.7.3.1"
WINDOWS_STORES = ("ROOT", "CA")
WINDOWS_BUNDLE_PREFIX = "windows-ca-"

_WELL_KNOWN_BUNDLES = (
    "/etc/ssl/cert.pem",  # macOS, Alpine, BSD
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/Fedora
    "/etc/ssl/ca-bundle.pem",  # openSUSE
)

EnumCertificates = Callable[[str], Iterable[tuple[bytes, str, Any]]]


def default_candidates() -> list[Path]:
    """OS 번들 파일 후보(중복 제거, 순서 유지)."""
    paths = ssl.get_default_verify_paths()
    ordered = [paths.cafile, paths.openssl_cafile, *_WELL_KNOWN_BUNDLES]
    seen: set[str] = set()
    result: list[Path] = []
    for raw in ordered:
        if raw and raw not in seen:
            seen.add(raw)
            result.append(Path(raw))
    return result


def _has_ca_certificates(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=str(path))
        return int(ctx.cert_store_stats().get("x509_ca", 0)) > 0
    except (OSError, ssl.SSLError, ValueError):
        return False


def _default_cache_dir() -> Path:
    from src.utils.app_paths import AppPaths

    return AppPaths.get_app_data_dir() / "tls"


def _export_windows_store(
    enum_certificates: EnumCertificates, cache_dir: Callable[[], Path]
) -> Path | None:
    ders: list[bytes] = []
    seen: set[bytes] = set()
    for store in WINDOWS_STORES:
        try:
            entries = list(enum_certificates(store))
        except OSError as exc:
            logger.warning("Windows 인증서 저장소 %s를 읽지 못했습니다: %s", store, exc)
            continue
        for cert, encoding, trust in entries:
            if encoding != "x509_asn":
                continue
            if trust is not True and SERVER_AUTH_OID not in trust:
                continue
            if cert in seen:
                continue
            seen.add(cert)
            ders.append(cert)
    if not ders:
        return None

    pem = "".join(ssl.DER_cert_to_PEM_cert(der) for der in ders).encode("ascii")
    directory = cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # 내용 해시를 파일명에 넣는다: 저장소가 같으면 다시 쓰지 않고(다른 연결이 읽는 중일 수 있다),
    # 바뀌면 새 파일을 만든다.
    target = directory / f"{WINDOWS_BUNDLE_PREFIX}{hashlib.sha256(pem).hexdigest()[:16]}.pem"
    if not target.is_file() or target.stat().st_size != len(pem):
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_bytes(pem)
        os.replace(tmp, target)
    for stale in directory.glob(f"{WINDOWS_BUNDLE_PREFIX}*.pem"):
        if stale != target:
            try:
                stale.unlink()
            except OSError:
                pass  # 다른 프로세스가 읽는 중이면 다음 기회에 지운다
    return target if _has_ca_certificates(target) else None


def find_ca_bundle(
    *,
    env_file: str | None,
    platform: str,
    candidates: Sequence[Path],
    cache_dir: Callable[[], Path],
    enum_certificates: EnumCertificates | None = None,
) -> Path | None:
    """CA 인증서가 실제로 들어 있는 신뢰 번들 파일. 없으면 None."""
    if env_file:
        env_path = Path(env_file).expanduser()
        if _has_ca_certificates(env_path):
            return env_path
        logger.warning("SSL_CERT_FILE에 쓸 수 있는 CA 인증서가 없어 무시합니다: %s", env_path)

    if platform == "win32":
        enum = enum_certificates or getattr(ssl, "enum_certificates", None)
        if enum is not None:
            exported = _export_windows_store(enum, cache_dir)
            if exported is not None:
                return exported

    for candidate in candidates:
        if _has_ca_certificates(candidate):
            return candidate
    return None


@functools.lru_cache(maxsize=4)
def _resolve_cached(env_file: str) -> Path | None:
    found = find_ca_bundle(
        env_file=env_file or None,
        platform=sys.platform,
        candidates=default_candidates(),
        cache_dir=lambda: _default_cache_dir(),
    )
    if found is None:
        logger.warning("OS 신뢰 CA 저장소를 찾지 못했습니다.")
    else:
        logger.info("TLS 서버 검증에 OS 신뢰 CA 번들을 사용합니다: %s", found)
    return found


def resolve_system_ca_bundle() -> Path | None:
    """현재 플랫폼의 OS 신뢰 CA 번들 파일(프로세스 단위 캐시, SSL_CERT_FILE 변경은 반영)."""
    found = _resolve_cached(os.environ.get("SSL_CERT_FILE", ""))
    if found is not None and not found.is_file():
        # 캐시 이후 파일이 지워졌다(예: 다른 프로세스의 오래된 내보내기 정리) — 다시 찾는다.
        _resolve_cached.cache_clear()
        found = _resolve_cached(os.environ.get("SSL_CERT_FILE", ""))
    return found


def clear_cache() -> None:
    _resolve_cached.cache_clear()
