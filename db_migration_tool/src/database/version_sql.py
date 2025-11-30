"""
PostgreSQL 버전별 SQL 템플릿 매트릭스

지원 대상: PostgreSQL 9.3, PostgreSQL 16
"""

from src.database.version_info import PgVersionFamily, PgVersionInfo

# 버전별 SQL 템플릿
SQL_TEMPLATES: dict[str, dict[str, str]] = {
    # PostgreSQL 9.3 호환 SQL
    "9.3": {
        # COPY TO 쿼리
        "copy_to": """
            COPY (
                SELECT path_id, issued_date, changed_value,
                       COALESCE(connection_status::text, 'true') as connection_status
                FROM {table}
                {where_clause}
                ORDER BY path_id, issued_date
                LIMIT {limit}
            ) TO STDOUT WITH (FORMAT CSV, HEADER FALSE, NULL 'NULL')
        """,
        # 테이블 크기 추정 (pg_table_size 사용)
        "estimate_size": """
            SELECT
                (SELECT reltuples::bigint FROM pg_class WHERE relname = %s) as row_count,
                pg_table_size(%s) as total_size
        """,
        # 권한 확인 (pg_read_server_files 역할 없음 - 슈퍼유저만 확인)
        "check_permission": """
            SELECT rolsuper FROM pg_roles WHERE rolname = current_user
        """,
    },
    # PostgreSQL 16 최적화 SQL
    "16": {
        # COPY TO 쿼리 (동일하지만 확장 가능)
        "copy_to": """
            COPY (
                SELECT path_id, issued_date, changed_value,
                       COALESCE(connection_status::text, 'true') as connection_status
                FROM {table}
                {where_clause}
                ORDER BY path_id, issued_date
                LIMIT {limit}
            ) TO STDOUT WITH (FORMAT CSV, HEADER FALSE, NULL 'NULL')
        """,
        # 테이블 크기 추정 (pg_total_relation_size 사용 - 인덱스 포함)
        "estimate_size": """
            SELECT
                (SELECT reltuples::bigint FROM pg_class WHERE relname = %s) as row_count,
                pg_total_relation_size(%s) as total_size
        """,
        # 권한 확인 (pg_read_server_files 역할 지원)
        "check_permission": """
            SELECT rolsuper OR pg_has_role(current_user, 'pg_read_server_files', 'MEMBER')
            FROM pg_roles WHERE rolname = current_user
        """,
    },
}


def get_sql_for_version(version_info: PgVersionInfo, query_name: str) -> str:
    """버전에 맞는 SQL 템플릿 반환

    Args:
        version_info: PostgreSQL 버전 정보
        query_name: 쿼리 이름 (copy_to, estimate_size, check_permission)

    Returns:
        해당 버전에 적합한 SQL 템플릿

    Raises:
        KeyError: 존재하지 않는 query_name
    """
    # 9.3 또는 UNKNOWN은 9.3 템플릿 사용
    if version_info.family in (PgVersionFamily.PG_9_3, PgVersionFamily.UNKNOWN):
        key = "9.3"
    else:
        key = "16"

    return SQL_TEMPLATES[key][query_name]
