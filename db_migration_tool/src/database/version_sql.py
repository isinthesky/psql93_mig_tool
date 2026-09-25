"""
PostgreSQL 버전별 SQL 템플릿 매트릭스

지원 대상: PostgreSQL 9.3, PostgreSQL 16

relation은 모두 `public`에서 카탈로그 oid로 고른다(감사 H-04). 이름 문자열을
`regclass`로 바꾸는 함수(`pg_table_size('name')` 등)는 search_path를 따르므로 쓰지 않는다.
예전에 있던 `copy_to` 템플릿은 호출처가 없고 `{table}`에 schema 없는 이름을 끼워 넣게
만드는 형태라 제거했다 — COPY 문은 워커가 `sql.Identifier`로 조립한다.
"""

from src.database.version_info import PgVersionFamily, PgVersionInfo

# 버전별 SQL 템플릿
SQL_TEMPLATES: dict[str, dict[str, str]] = {
    # PostgreSQL 9.3 호환 SQL
    "9.3": {
        # 테이블 크기 추정 (pg_table_size 사용). 파라미터: (table_name,)
        "estimate_size": """
            SELECT c.reltuples::bigint AS row_count,
                   pg_catalog.pg_table_size(c.oid) AS total_size
              FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relname = %s
               AND n.nspname = 'public'
               AND c.relkind IN ('r', 'p')
        """,
        # 권한 확인 (pg_read_server_files 역할 없음 - 슈퍼유저만 확인)
        "check_permission": """
            SELECT rolsuper FROM pg_roles WHERE rolname = current_user
        """,
    },
    # PostgreSQL 16 최적화 SQL
    "16": {
        # 테이블 크기 추정 (pg_total_relation_size 사용 - 인덱스 포함). 파라미터: (table_name,)
        "estimate_size": """
            SELECT c.reltuples::bigint AS row_count,
                   pg_catalog.pg_total_relation_size(c.oid) AS total_size
              FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relname = %s
               AND n.nspname = 'public'
               AND c.relkind IN ('r', 'p')
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
        query_name: 쿼리 이름 (estimate_size, check_permission)

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
