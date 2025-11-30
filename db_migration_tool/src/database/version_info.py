"""
PostgreSQL 버전 정보 데이터 클래스

지원 대상: PostgreSQL 9.3, PostgreSQL 16
미확인 버전은 안전을 위해 9.3 프로파일로 취급
"""

import re
from dataclasses import dataclass
from enum import Enum


class PgVersionFamily(Enum):
    """PostgreSQL 버전 패밀리"""

    PG_9_3 = "9.3"
    PG_16 = "16"
    UNKNOWN = "unknown"


@dataclass
class PgVersionInfo:
    """PostgreSQL 버전 정보"""

    major: int
    minor: int
    full_version: str
    family: PgVersionFamily

    @property
    def is_legacy(self) -> bool:
        """레거시(9.3) 버전 여부"""
        return self.family == PgVersionFamily.PG_9_3

    @property
    def supports_jsonb(self) -> bool:
        """JSONB 타입 지원 여부 (16만 지원)"""
        return self.family == PgVersionFamily.PG_16

    @property
    def supports_parallel_query(self) -> bool:
        """병렬 쿼리 지원 여부 (16만 지원)"""
        return self.family == PgVersionFamily.PG_16

    @property
    def supports_pg_server_files_role(self) -> bool:
        """pg_read_server_files/pg_write_server_files 역할 지원 (16만 지원)"""
        return self.family == PgVersionFamily.PG_16

    def __str__(self) -> str:
        return f"PostgreSQL {self.major}.{self.minor} ({self.family.value})"


def parse_version_string(version_str: str) -> PgVersionInfo:
    """버전 문자열 파싱

    Args:
        version_str: SELECT version() 결과 문자열
            예: "PostgreSQL 9.3.25 on x86_64-pc-linux-gnu"
            예: "PostgreSQL 16.1 (Ubuntu 16.1-1.pgdg22.04+1)"

    Returns:
        PgVersionInfo: 파싱된 버전 정보
    """
    match = re.search(r"PostgreSQL (\d+)\.(\d+)", version_str)
    if match:
        major, minor = int(match.group(1)), int(match.group(2))

        # 9.3만 레거시로 인식
        if major == 9 and minor == 3:
            family = PgVersionFamily.PG_9_3
        # 16만 최신으로 인식
        elif major == 16:
            family = PgVersionFamily.PG_16
        else:
            # 그 외 버전은 UNKNOWN → 9.3 프로파일로 폴백
            family = PgVersionFamily.UNKNOWN

        return PgVersionInfo(major, minor, version_str, family)

    return PgVersionInfo(0, 0, version_str, PgVersionFamily.UNKNOWN)
