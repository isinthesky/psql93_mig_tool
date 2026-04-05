"""
입력 검증 유틸리티
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.models.profile import ENDPOINT_KIND_FILE, ENDPOINT_KIND_POSTGRES

if TYPE_CHECKING:
    from src.database.version_info import PgVersionInfo

VALID_COMPAT_MODES = {"auto", "9.3", "16"}
SUPPORTED_MIGRATION_PAIRS = {
    (ENDPOINT_KIND_POSTGRES, ENDPOINT_KIND_POSTGRES),
    (ENDPOINT_KIND_POSTGRES, ENDPOINT_KIND_FILE),
    (ENDPOINT_KIND_FILE, ENDPOINT_KIND_POSTGRES),
}


class ConnectionValidator:
    """연결 정보 검증"""

    @staticmethod
    def validate_connection_config(config: dict[str, Any]) -> tuple[bool, str]:
        """엔드포인트 설정 검증 (kind 자동 분기)."""
        kind = (config or {}).get("kind", ENDPOINT_KIND_POSTGRES)
        if kind == ENDPOINT_KIND_FILE:
            return ConnectionValidator.validate_file_archive_config(config)
        return ConnectionValidator.validate_postgres_config(config)

    @staticmethod
    def validate_postgres_config(config: dict[str, Any]) -> tuple[bool, str]:
        """PostgreSQL 연결 설정 검증"""
        required_fields = ["host", "port", "database", "username"]
        for field in required_fields:
            if not config.get(field):
                return False, f"{field}는 필수 입력 항목입니다."

        host = config["host"]
        if not host or len(host) > 255:
            return False, "올바른 호스트 주소를 입력하세요."

        port = config["port"]
        if not isinstance(port, int) or port < 1 or port > 65535:
            return False, "포트는 1-65535 사이의 숫자여야 합니다."

        database = config["database"]
        if not re.match(r"^[a-zA-Z0-9_.]+$", database):
            return False, "데이터베이스명은 영문자, 숫자, 언더스코어, 점만 사용 가능합니다."

        username = config["username"]
        if not re.match(r"^[a-zA-Z0-9_]+$", username):
            return False, "사용자명은 영문자, 숫자, 언더스코어만 사용 가능합니다."

        return True, ""

    @staticmethod
    def validate_file_archive_config(
        config: dict[str, Any],
        *,
        must_exist: bool | None = None,
    ) -> tuple[bool, str]:
        """파일 아카이브 설정 검증"""
        archive_path = str((config or {}).get("archive_path", "")).strip()
        if not archive_path:
            return False, "아카이브 경로를 입력하세요."

        path = Path(archive_path).expanduser()
        if must_exist is True:
            if not path.exists() or not path.is_dir():
                return False, "아카이브 폴더가 존재하지 않습니다."
            manifest_path = path / "manifest.json"
            if not manifest_path.exists():
                return False, "manifest.json 파일이 없는 아카이브입니다."
        elif must_exist is False:
            if path.exists() and path.is_file():
                return False, "아카이브 대상 경로는 폴더여야 합니다."
            parent = path if path.exists() else path.parent
            if not parent.exists():
                return False, "아카이브 상위 폴더가 존재하지 않습니다."

        return True, ""

    @staticmethod
    def validate_endpoint_pair(
        source_config: dict[str, Any],
        target_config: dict[str, Any],
    ) -> tuple[bool, str]:
        source_kind = (source_config or {}).get("kind", ENDPOINT_KIND_POSTGRES)
        target_kind = (target_config or {}).get("kind", ENDPOINT_KIND_POSTGRES)

        if (source_kind, target_kind) not in SUPPORTED_MIGRATION_PAIRS:
            return False, "현재 버전은 PostgreSQL↔PostgreSQL / PostgreSQL→File / File→PostgreSQL만 지원합니다."

        if source_kind == ENDPOINT_KIND_FILE:
            valid, msg = ConnectionValidator.validate_file_archive_config(
                source_config, must_exist=True
            )
            if not valid:
                return False, f"소스 아카이브 오류: {msg}"

        if target_kind == ENDPOINT_KIND_FILE:
            valid, msg = ConnectionValidator.validate_file_archive_config(
                target_config, must_exist=False
            )
            if not valid:
                return False, f"대상 아카이브 오류: {msg}"

        return True, ""

    @staticmethod
    def validate_profile_name(name: str) -> tuple[bool, str]:
        """프로필 이름 검증"""
        if not name or not name.strip():
            return False, "프로필 이름을 입력하세요."

        if len(name) > 100:
            return False, "프로필 이름은 100자 이하여야 합니다."

        if not re.match(r"^[a-zA-Z0-9가-힣\s\-_]+$", name):
            return False, "프로필 이름에 특수문자를 사용할 수 없습니다."

        return True, ""


class DateValidator:
    """날짜 검증"""

    @staticmethod
    def validate_date_range(start_date, end_date) -> tuple[bool, str]:
        """날짜 범위 검증"""
        if not start_date or not end_date:
            return False, "시작 날짜와 종료 날짜를 선택하세요."

        if start_date > end_date:
            return False, "시작 날짜가 종료 날짜보다 늦습니다."

        delta = end_date - start_date
        if delta.days > 365:
            return False, "날짜 범위는 최대 1년까지 선택 가능합니다."

        return True, ""


class VersionValidator:
    """PostgreSQL 버전 호환성 검증"""

    @staticmethod
    def validate_compat_mode(compat_mode: str) -> tuple[bool, str]:
        if compat_mode not in VALID_COMPAT_MODES:
            return False, f"잘못된 호환 모드입니다: {compat_mode}. 허용 값: {VALID_COMPAT_MODES}"
        return True, ""

    @staticmethod
    def validate_version_compatibility(
        source_version: "PgVersionInfo",
        target_version: "PgVersionInfo",
    ) -> tuple[bool, list[str]]:
        from src.database.version_info import PgVersionFamily

        warnings: list[str] = []

        if target_version.major < source_version.major:
            warnings.append(
                f"대상 DB({target_version})가 소스({source_version})보다 "
                "낮은 버전입니다. 일부 기능이 호환되지 않을 수 있습니다."
            )

        if source_version.supports_jsonb and not target_version.supports_jsonb:
            warnings.append(
                "소스 DB가 JSONB를 지원하지만 대상 DB(9.3)는 지원하지 않습니다. "
                "JSONB 컬럼이 있다면 마이그레이션이 실패할 수 있습니다."
            )

        if source_version.family == PgVersionFamily.UNKNOWN:
            warnings.append(
                f"소스 DB 버전({source_version.full_version})이 지원 대상(9.3, 16)에 포함되지 않습니다. "
                "9.3 호환 모드로 처리됩니다."
            )

        if target_version.family == PgVersionFamily.UNKNOWN:
            warnings.append(
                f"대상 DB 버전({target_version.full_version})이 지원 대상(9.3, 16)에 포함되지 않습니다. "
                "9.3 호환 모드로 처리됩니다."
            )

        return len(warnings) == 0, warnings
