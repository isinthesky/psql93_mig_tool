"""
PostgreSQL 버전별 세션 파라미터 매트릭스

지원 대상: PostgreSQL 9.3, PostgreSQL 16
"""

from src.database.version_info import PgVersionFamily, PgVersionInfo

# 버전별 세션 파라미터
VERSION_PARAMS: dict[str, dict[str, str]] = {
    # PostgreSQL 9.3 호환 파라미터
    "9.3": {
        "work_mem": "128MB",
        "maintenance_work_mem": "512MB",
        "synchronous_commit": "off",
        # checkpoint_segments는 9.5부터 제거됨 - 9.3/9.4에서만 사용
        "checkpoint_segments": "32",
    },
    # PostgreSQL 16 최적화 파라미터
    "16": {
        "work_mem": "256MB",
        "maintenance_work_mem": "1GB",
        "synchronous_commit": "off",
        "max_wal_size": "4GB",
        "max_parallel_workers_per_gather": "2",
    },
}


def get_params_for_version(version_info: PgVersionInfo) -> dict[str, str]:
    """버전에 맞는 세션 파라미터 반환

    Args:
        version_info: PostgreSQL 버전 정보

    Returns:
        해당 버전에 적합한 세션 파라미터 딕셔너리
    """
    if version_info.family == PgVersionFamily.PG_9_3:
        return VERSION_PARAMS["9.3"].copy()

    if version_info.family == PgVersionFamily.PG_16:
        return VERSION_PARAMS["16"].copy()

    # UNKNOWN → 보수적으로 9.3 프로파일 사용
    return VERSION_PARAMS["9.3"].copy()
