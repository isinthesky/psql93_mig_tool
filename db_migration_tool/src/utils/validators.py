"""
입력 검증 유틸리티
"""

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.database.version_info import PgVersionInfo

# 허용되는 호환 모드 값
VALID_COMPAT_MODES = {"auto", "9.3", "16"}


class ConnectionValidator:
    """연결 정보 검증"""

    @staticmethod
    def validate_connection_config(config: dict[str, Any]) -> tuple[bool, str]:
        """연결 설정 검증"""
        # 필수 필드 확인
        required_fields = ["host", "port", "database", "username"]
        for field in required_fields:
            if not config.get(field):
                return False, f"{field}는 필수 입력 항목입니다."

        # 호스트 검증
        host = config["host"]
        if not host or len(host) > 255:
            return False, "올바른 호스트 주소를 입력하세요."

        # 포트 검증
        port = config["port"]
        if not isinstance(port, int) or port < 1 or port > 65535:
            return False, "포트는 1-65535 사이의 숫자여야 합니다."

        # 데이터베이스명 검증
        database = config["database"]
        if not re.match(r"^[a-zA-Z0-9_.]+$", database):
            return False, "데이터베이스명은 영문자, 숫자, 언더스코어, 점만 사용 가능합니다."

        # 사용자명 검증
        username = config["username"]
        if not re.match(r"^[a-zA-Z0-9_]+$", username):
            return False, "사용자명은 영문자, 숫자, 언더스코어만 사용 가능합니다."

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

        # 날짜 차이 확인 (최대 1년)
        delta = end_date - start_date
        if delta.days > 365:
            return False, "날짜 범위는 최대 1년까지 선택 가능합니다."

        return True, ""


class VersionValidator:
    """PostgreSQL 버전 호환성 검증"""

    @staticmethod
    def validate_compat_mode(compat_mode: str) -> tuple[bool, str]:
        """호환 모드 값 검증

        Args:
            compat_mode: 호환 모드 값 ("auto", "9.3", "16")

        Returns:
            (유효 여부, 오류 메시지)
        """
        if compat_mode not in VALID_COMPAT_MODES:
            return False, f"잘못된 호환 모드입니다: {compat_mode}. 허용 값: {VALID_COMPAT_MODES}"
        return True, ""

    @staticmethod
    def validate_version_compatibility(
        source_version: "PgVersionInfo",
        target_version: "PgVersionInfo",
    ) -> tuple[bool, list[str]]:
        """소스-대상 버전 호환성 검증

        Args:
            source_version: 소스 DB 버전 정보
            target_version: 대상 DB 버전 정보

        Returns:
            (경고 없음 여부, 경고 메시지 리스트)
        """
        from src.database.version_info import PgVersionFamily

        warnings: list[str] = []

        # 대상이 소스보다 낮은 버전인 경우 경고
        if target_version.major < source_version.major:
            warnings.append(
                f"대상 DB({target_version})가 소스({source_version})보다 "
                "낮은 버전입니다. 일부 기능이 호환되지 않을 수 있습니다."
            )

        # JSONB 호환성 (16 → 9.3 마이그레이션 시)
        if source_version.supports_jsonb and not target_version.supports_jsonb:
            warnings.append(
                "소스 DB가 JSONB를 지원하지만 대상 DB(9.3)는 지원하지 않습니다. "
                "JSONB 컬럼이 있다면 마이그레이션이 실패할 수 있습니다."
            )

        # UNKNOWN 버전 경고
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
