"""PostgreSQL 연결 파라미터 단일 빌더 (psycopg / psycopg2 공용)

모든 PostgreSQL 연결은 여기서 만든 libpq 파라미터로만 연다. 연결 지점마다 dict를
따로 조립하면 한 곳만 `sslmode=require`로 남거나 timeout이 빠지는 일이 생긴다
(감사 H-05, H-02/H-04 연결 단계). `tests/database/test_connection_params.py`가
`psycopg.connect`/`psycopg2.connect` 직접 호출이 이 모듈 밖에 없는지 검사한다.

프로필(엔드포인트 config) 키
- host, port, database, username, password: 기존 그대로.
- ssl (bool): 기존 'SSL 연결 사용' 체크박스. 꺼져 있으면 sslmode를 주지 않는다
  (기존 동작 유지 — libpq 기본값).
- sslmode: "verify-full"(기본) | "verify-ca" | "require"(검증 없음, 위험 승인 필요).
- sslrootcert: CA(루트 인증서) 파일 경로. 비우면(또는 'system') verify-full에서
  OS 신뢰 저장소를 담은 실제 PEM 파일을 쓴다(`system_ca`). libpq의 `sslrootcert=system`
  키워드는 넘기지 않는다 — 바이너리 휠 libpq는 번들 OpenSSL의 빈 빌드 경로를 신뢰 저장소로
  삼아 공인 CA 서버까지 거부한다.
- ssl_allow_insecure (bool): `require`(서버 인증서·hostname 미검증)를 쓰겠다는 명시적 위험 승인.
- connect_timeout (int, 초): 없으면 호출 지점 기본값, 그것도 없으면 10초.

모든 연결에 `options=-c search_path=public`을 넣는다. 선행 스키마(`$user` 등)의
동명 객체를 잡지 않게 하려는 것이며, 시작 패킷의 options는 PostgreSQL 9.3에서도 동작한다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.database import system_ca

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
SEARCH_PATH_OPTIONS = "-c search_path=public"

# 프로필 config 키 (UI·저장과 공유)
PROFILE_KEY_SSL = "ssl"
PROFILE_KEY_SSLMODE = "sslmode"
PROFILE_KEY_SSLROOTCERT = "sslrootcert"
PROFILE_KEY_ALLOW_INSECURE = "ssl_allow_insecure"
PROFILE_KEY_CONNECT_TIMEOUT = "connect_timeout"

SSLMODE_VERIFY_FULL = "verify-full"
SSLMODE_VERIFY_CA = "verify-ca"
SSLMODE_REQUIRE = "require"
SUPPORTED_SSLMODES = (SSLMODE_VERIFY_FULL, SSLMODE_VERIFY_CA, SSLMODE_REQUIRE)
DEFAULT_SSLMODE = SSLMODE_VERIFY_FULL

# CA 칸에 적으면 '비움'과 같다(OS 신뢰 저장소). libpq 키워드로는 넘기지 않는다.
SSLROOTCERT_SYSTEM = "system"

AUDIT_LOGGER_NAME = "dbmig.security.audit"
_audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)


class ConnectionConfigError(ValueError):
    """연결 설정이 안전하게 연결할 수 없는 상태일 때 (네트워크 접속 전에 발생)."""


def _resolve_connect_timeout(config: dict[str, Any], default: int | None) -> int:
    raw = config.get(PROFILE_KEY_CONNECT_TIMEOUT)
    if raw is None:
        return default if default is not None else DEFAULT_CONNECT_TIMEOUT_SECONDS
    # bool은 int의 하위 타입이라 먼저 막는다. 0은 libpq에서 '무한 대기'다.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ConnectionConfigError(f"연결 타임아웃은 1 이상의 정수(초)여야 합니다: {raw!r}")
    return raw


def _endpoint_label(config: dict[str, Any]) -> str:
    return (
        f"{config.get('username', '')}@{config.get('host', 'localhost')}:"
        f"{config.get('port', 5432)}/{config.get('database', '')}"
    )


def _emit_app_log(level: str, message: str) -> None:
    """앱 로그(파일·로그 DB·로그 뷰어)에도 남긴다. 실패해도 연결을 막지 않는다."""
    try:
        from src.utils.enhanced_logger import log_emitter

        log_emitter.emit_log(level, message)
    except Exception:  # noqa: BLE001 — 감사 보조 채널, stdlib 감사 로그는 이미 남았다
        pass


def _default_audit(message: str) -> None:
    _audit_logger.warning(message)
    _emit_app_log("WARNING", message)


def _resolve_tls(config: dict[str, Any]) -> tuple[dict[str, str], str | None]:
    """(libpq TLS 파라미터, 감사 메시지 또는 None)."""
    if not config.get(PROFILE_KEY_SSL):
        return {}, None

    raw_mode = config.get(PROFILE_KEY_SSLMODE)
    mode = str(raw_mode).strip().lower() if raw_mode else DEFAULT_SSLMODE
    if mode not in SUPPORTED_SSLMODES:
        raise ConnectionConfigError(
            f"지원하지 않는 SSL 모드입니다: {raw_mode!r} "
            f"(사용 가능: {', '.join(SUPPORTED_SSLMODES)})"
        )

    rootcert = str(config.get(PROFILE_KEY_SSLROOTCERT) or "").strip()
    params: dict[str, str] = {"sslmode": mode}

    if rootcert and rootcert != SSLROOTCERT_SYSTEM:
        path = Path(rootcert).expanduser()
        if not path.is_file():
            raise ConnectionConfigError(f"CA 인증서 파일을 찾을 수 없습니다: {path}")
        params["sslrootcert"] = str(path)

    if mode == SSLMODE_REQUIRE:
        if not config.get(PROFILE_KEY_ALLOW_INSECURE):
            raise ConnectionConfigError(
                "SSL 모드 'require'는 서버 인증서와 호스트 이름을 검증하지 않아 "
                "중간자 공격에 노출됩니다. 위험 승인 설정 없이 사용할 수 없습니다. "
                "verify-full과 CA 인증서를 사용하세요."
            )
        audit = (
            "[보안 감사] TLS 서버 신원 검증 없이 연결합니다(sslmode=require, 위험 승인됨): "
            f"{_endpoint_label(config)}"
        )
        return params, audit

    if "sslrootcert" in params:
        return params, None

    # CA 파일이 없다: verify-full이면 OS 신뢰 저장소 번들 파일을 쓴다.
    if mode != SSLMODE_VERIFY_FULL:
        raise ConnectionConfigError(
            f"SSL 모드 '{mode}'는 CA 인증서 파일이 필요합니다. "
            "CA 파일 경로를 지정하거나 verify-full을 사용하세요."
        )
    bundle = system_ca.resolve_system_ca_bundle()
    if bundle is None:
        raise ConnectionConfigError(
            "OS 신뢰 CA 저장소를 찾을 수 없어 서버 인증서를 검증할 수 없습니다. "
            "연결 설정에서 서버 인증서를 서명한 CA 인증서 파일 경로를 지정하세요."
        )
    params["sslrootcert"] = str(bundle)
    return params, None


def build_libpq_params(
    config: dict[str, Any],
    *,
    connect_timeout: int | None = None,
    audit: Any = None,
) -> dict[str, Any]:
    """프로필 config → psycopg/psycopg2 `connect(**params)`용 libpq 키워드.

    Args:
        config: 엔드포인트 config (모듈 docstring의 키).
        connect_timeout: 이 호출 지점의 기본 타임아웃(초). 프로필 값이 있으면 프로필이 우선.
        audit: 검증 없는 TLS 사용 시 호출할 감사 콜백(str 한 개). 기본은 감사 로거 + 앱 로그.

    Raises:
        ConnectionConfigError: 안전하게 연결할 수 없는 설정(네트워크 접속 전).
    """
    params: dict[str, Any] = {
        "host": config.get("host") or "localhost",
        "port": config.get("port") or 5432,
        "dbname": config.get("database") or "",
        "user": config.get("username") or "",
        "password": config.get("password") or "",
        "connect_timeout": _resolve_connect_timeout(config, connect_timeout),
        "options": SEARCH_PATH_OPTIONS,
    }

    tls, audit_message = _resolve_tls(config)
    params.update(tls)

    if audit_message:
        (audit or _default_audit)(audit_message)

    return params


def validate_tls_settings(config: dict[str, Any]) -> str | None:
    """연결 없이 설정만 검사한다(저장 전 UI 검증용). 문제가 없으면 None."""
    try:
        _resolve_tls(config)
        _resolve_connect_timeout(config, None)
    except ConnectionConfigError as exc:
        return str(exc)
    return None


def connect_psycopg(config: dict[str, Any], *, connect_timeout: int | None = None) -> Any:
    """psycopg(3) 연결. 설정 오류는 접속 전에 ConnectionConfigError로 난다."""
    import psycopg

    params = build_libpq_params(config, connect_timeout=connect_timeout)
    return psycopg.connect(**params)


def connect_psycopg2(config: dict[str, Any], *, connect_timeout: int | None = None) -> Any:
    """psycopg2 연결(COPY 경로). 설정 오류는 접속 전에 ConnectionConfigError로 난다."""
    import psycopg2

    params = build_libpq_params(config, connect_timeout=connect_timeout)
    return psycopg2.connect(**params)
