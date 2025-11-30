"""
PostgreSQL 버전 감지 및 매핑 테스트
"""

import pytest

from src.database.version_info import (
    PgVersionFamily,
    PgVersionInfo,
    parse_version_string,
)
from src.database.version_params import VERSION_PARAMS, get_params_for_version
from src.database.version_sql import SQL_TEMPLATES, get_sql_for_version
from src.utils.validators import VersionValidator


class TestVersionParsing:
    """버전 문자열 파싱 테스트"""

    def test_parse_version_9_3(self):
        """9.3 버전 문자열 파싱"""
        version_str = "PostgreSQL 9.3.25 on x86_64-pc-linux-gnu, compiled by gcc 4.8.5"
        info = parse_version_string(version_str)

        assert info.major == 9
        assert info.minor == 3
        assert info.family == PgVersionFamily.PG_9_3
        assert info.is_legacy is True
        assert info.supports_jsonb is False

    def test_parse_version_16(self):
        """16 버전 문자열 파싱"""
        version_str = "PostgreSQL 16.1 (Ubuntu 16.1-1.pgdg22.04+1) on x86_64-pc-linux-gnu"
        info = parse_version_string(version_str)

        assert info.major == 16
        assert info.minor == 1
        assert info.family == PgVersionFamily.PG_16
        assert info.is_legacy is False
        assert info.supports_jsonb is True

    def test_parse_version_unknown_14(self):
        """14 버전은 UNKNOWN으로 분류"""
        version_str = "PostgreSQL 14.5 (Debian 14.5-1.pgdg110+1)"
        info = parse_version_string(version_str)

        assert info.major == 14
        assert info.minor == 5
        assert info.family == PgVersionFamily.UNKNOWN

    def test_parse_version_unknown_9_6(self):
        """9.6 버전은 UNKNOWN으로 분류"""
        version_str = "PostgreSQL 9.6.24 on x86_64-pc-linux-gnu"
        info = parse_version_string(version_str)

        assert info.major == 9
        assert info.minor == 6
        assert info.family == PgVersionFamily.UNKNOWN

    def test_parse_version_invalid_string(self):
        """잘못된 버전 문자열"""
        version_str = "MySQL 8.0.30"
        info = parse_version_string(version_str)

        assert info.major == 0
        assert info.minor == 0
        assert info.family == PgVersionFamily.UNKNOWN


class TestVersionParams:
    """버전별 파라미터 매핑 테스트"""

    def test_params_for_9_3(self):
        """9.3 파라미터 매핑"""
        info = PgVersionInfo(9, 3, "test", PgVersionFamily.PG_9_3)
        params = get_params_for_version(info)

        assert "checkpoint_segments" in params
        assert "max_parallel_workers_per_gather" not in params
        assert params["work_mem"] == "128MB"

    def test_params_for_16(self):
        """16 파라미터 매핑"""
        info = PgVersionInfo(16, 0, "test", PgVersionFamily.PG_16)
        params = get_params_for_version(info)

        assert "checkpoint_segments" not in params
        assert "max_parallel_workers_per_gather" in params
        assert params["work_mem"] == "256MB"

    def test_params_for_unknown_fallback_to_9_3(self):
        """UNKNOWN 버전은 9.3 프로파일로 폴백"""
        info = PgVersionInfo(14, 0, "test", PgVersionFamily.UNKNOWN)
        params = get_params_for_version(info)

        assert params == VERSION_PARAMS["9.3"]


class TestVersionSQL:
    """버전별 SQL 템플릿 테스트"""

    def test_sql_for_9_3(self):
        """9.3 SQL 템플릿"""
        info = PgVersionInfo(9, 3, "test", PgVersionFamily.PG_9_3)

        check_perm_sql = get_sql_for_version(info, "check_permission")
        assert "pg_has_role" not in check_perm_sql
        assert "rolsuper" in check_perm_sql

    def test_sql_for_16(self):
        """16 SQL 템플릿"""
        info = PgVersionInfo(16, 0, "test", PgVersionFamily.PG_16)

        check_perm_sql = get_sql_for_version(info, "check_permission")
        assert "pg_has_role" in check_perm_sql

    def test_sql_for_unknown_uses_9_3(self):
        """UNKNOWN 버전은 9.3 템플릿 사용"""
        info = PgVersionInfo(14, 0, "test", PgVersionFamily.UNKNOWN)

        check_perm_sql = get_sql_for_version(info, "check_permission")
        assert check_perm_sql == SQL_TEMPLATES["9.3"]["check_permission"]


class TestVersionCompatibility:
    """버전 호환성 검증 테스트"""

    def test_same_version_no_warnings(self):
        """동일 버전 간 마이그레이션은 경고 없음"""
        source = PgVersionInfo(16, 0, "test", PgVersionFamily.PG_16)
        target = PgVersionInfo(16, 0, "test", PgVersionFamily.PG_16)

        is_ok, warnings = VersionValidator.validate_version_compatibility(source, target)
        assert is_ok is True
        assert len(warnings) == 0

    def test_16_to_9_3_jsonb_warning(self):
        """16 → 9.3 마이그레이션 시 JSONB 경고"""
        source = PgVersionInfo(16, 0, "test", PgVersionFamily.PG_16)
        target = PgVersionInfo(9, 3, "test", PgVersionFamily.PG_9_3)

        is_ok, warnings = VersionValidator.validate_version_compatibility(source, target)
        assert is_ok is False
        assert any("JSONB" in w for w in warnings)
        assert any("낮은 버전" in w for w in warnings)

    def test_unknown_version_warning(self):
        """UNKNOWN 버전 경고"""
        source = PgVersionInfo(14, 0, "test", PgVersionFamily.UNKNOWN)
        target = PgVersionInfo(16, 0, "test", PgVersionFamily.PG_16)

        is_ok, warnings = VersionValidator.validate_version_compatibility(source, target)
        assert is_ok is False
        assert any("지원 대상" in w for w in warnings)

    def test_validate_compat_mode_valid(self):
        """유효한 호환 모드 값"""
        assert VersionValidator.validate_compat_mode("auto")[0] is True
        assert VersionValidator.validate_compat_mode("9.3")[0] is True
        assert VersionValidator.validate_compat_mode("16")[0] is True

    def test_validate_compat_mode_invalid(self):
        """잘못된 호환 모드 값"""
        valid, msg = VersionValidator.validate_compat_mode("10")
        assert valid is False
        assert "잘못된 호환 모드" in msg


class TestPgVersionInfo:
    """PgVersionInfo 데이터 클래스 테스트"""

    def test_str_representation(self):
        """문자열 표현"""
        info = PgVersionInfo(9, 3, "test", PgVersionFamily.PG_9_3)
        assert str(info) == "PostgreSQL 9.3 (9.3)"

    def test_is_legacy_property(self):
        """레거시 버전 속성"""
        info_9_3 = PgVersionInfo(9, 3, "test", PgVersionFamily.PG_9_3)
        info_16 = PgVersionInfo(16, 0, "test", PgVersionFamily.PG_16)

        assert info_9_3.is_legacy is True
        assert info_16.is_legacy is False

    def test_supports_parallel_query(self):
        """병렬 쿼리 지원 속성"""
        info_9_3 = PgVersionInfo(9, 3, "test", PgVersionFamily.PG_9_3)
        info_16 = PgVersionInfo(16, 0, "test", PgVersionFamily.PG_16)

        assert info_9_3.supports_parallel_query is False
        assert info_16.supports_parallel_query is True
