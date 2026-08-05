"""`estimate_table_size`가 '확인 실패'를 '테이블 없음'으로 보고하지 않는지 검증한다.

COPY 워커는 `exists=False`를 '소스 테이블이 없다'로 읽고 체크포인트를 **완료로
마킹한 뒤 건너뛴다**. 추정 쿼리가 어떤 이유로든 실패했을 때 그 값을 돌려주면,
멀쩡한 파티션이 조용히 누락되고 재개해도 다시 시도하지 않는다.
"""

from unittest.mock import MagicMock

from src.database.postgres_utils import PostgresOptimizer
from src.database.version_info import PgVersionFamily, PgVersionInfo
from src.database.version_sql import SQL_TEMPLATES

# 버전 감지 쿼리가 목의 호출 순서를 흐트러뜨리지 않도록 고정한다.
PG93 = PgVersionInfo(major=9, minor=3, full_version="9.3.25", family=PgVersionFamily.PG_9_3)


def _conn(*, table_exists=True, estimate_error=None, estimate_row=(100, 4096)):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value

    calls = {"n": 0}

    def execute(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] >= 2 and estimate_error is not None:
            raise estimate_error

    def fetchone():
        if calls["n"] == 1:
            return (table_exists,)
        return estimate_row

    cur.execute.side_effect = execute
    cur.fetchone.side_effect = fetchone
    return conn


class TestLookupFailureIsNotAbsence:
    def test_estimate_error_does_not_report_missing_table(self):
        conn = _conn(estimate_error=RuntimeError("more than one row returned by a subquery"))

        info = PostgresOptimizer.estimate_table_size(conn, "tbl_x", PG93)

        assert info["exists"] is True, (
            "확인 실패를 '테이블 없음'으로 보고하면 워커가 파티션을 건너뛰고 완료로 마킹합니다"
        )
        assert info["lookup_failed"] is True
        assert "error" in info

    def test_estimate_error_rolls_back(self):
        conn = _conn(estimate_error=RuntimeError("boom"))

        PostgresOptimizer.estimate_table_size(conn, "tbl_x", PG93)

        conn.rollback.assert_called_once()

    def test_genuinely_missing_table_still_reports_absent(self):
        """진짜 없는 테이블은 그대로 없다고 해야 한다(정당한 건너뛰기)."""
        conn = _conn(table_exists=False)

        info = PostgresOptimizer.estimate_table_size(conn, "tbl_x", PG93)

        assert info["exists"] is False
        assert not info.get("lookup_failed")

    def test_success_path_is_unchanged(self):
        conn = _conn(estimate_row=(42, 1024 * 1024))

        info = PostgresOptimizer.estimate_table_size(conn, "tbl_x", PG93)

        assert info["exists"] is True
        assert info["row_count"] == 42
        assert not info.get("lookup_failed")


class TestEstimateQueryIsUnambiguous:
    """`pg_class`는 모든 스키마의 테이블·인덱스·시퀀스를 담는다.

    이름만으로 고르면 동명 행이 둘 이상일 때 스칼라 서브쿼리가 여러 행을
    돌려 쿼리가 통째로 실패하고, 그게 위의 '테이블 없음' 오판으로 이어진다.
    """

    def test_every_version_filters_schema_and_relkind(self):
        for version, queries in SQL_TEMPLATES.items():
            estimate = queries["estimate_size"]
            assert "pg_namespace" in estimate, f"{version}: 스키마 조인이 없습니다"
            assert "nspname" in estimate, f"{version}: 스키마 필터가 없습니다"
            assert "relkind" in estimate, f"{version}: relation kind 필터가 없습니다"
