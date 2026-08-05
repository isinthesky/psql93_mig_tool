"""COPY 워커가 '통계상 0행'을 '빈 테이블'로 오해하지 않는지 검증한다.

`PostgresOptimizer.estimate_table_size`의 row_count는 `pg_class.reltuples`,
즉 **추정치**다. VACUUM/ANALYZE 전에는 0(PG14+는 -1)이라 방금 만들어진
파티션은 데이터가 있어도 0으로 보고된다.

그대로 믿고 건너뛰면 체크포인트가 완료로 마킹되어 재개해도 다시 시도하지
않는다. 이 도구는 최근 날짜 파티션을 옮기므로 갓 생성된 파티션이 정확히
이 조건에 해당한다 — 조용한 데이터 누락이 된다.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.core.copy_migration_worker import CopyMigrationWorker
from src.models.profile import ConnectionProfile


@pytest.fixture
def worker():
    profile = ConnectionProfile(
        id=1,
        name="p",
        source_config={"host": "h"},
        target_config={"host": "h"},
    )
    with (
        patch("src.core.base_migration_worker.HistoryManager"),
        patch("src.core.base_migration_worker.CheckpointManager"),
    ):
        w = CopyMigrationWorker(profile, ["tbl_x"], 1)
    w._log = lambda message, level="INFO": None
    return w


def _conn_returning(count):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (count,)
    return conn


class TestResolveTotalRows:
    def test_positive_estimate_is_trusted(self, worker):
        """추정치가 양수면 그대로 쓴다. 불필요한 COUNT(*)를 돌리지 않는다."""
        worker.source_conn = _conn_returning(999)

        assert worker._resolve_total_rows("tbl_x", {"row_count": 500}) == (500, False)
        worker.source_conn.cursor.assert_not_called()

    def test_zero_estimate_with_real_data_is_not_skipped(self, worker):
        """ANALYZE 전 파티션. 여기서 건너뛰면 데이터가 조용히 누락된다."""
        worker.source_conn = _conn_returning(412345)

        total, is_empty = worker._resolve_total_rows("tbl_x", {"row_count": 0})

        assert total == 412345
        assert not is_empty, "통계상 0행을 빈 테이블로 단정하면 안 됩니다"

    def test_negative_estimate_is_also_verified(self, worker):
        """PG14+ 는 미분석 테이블에 -1을 돌려준다."""
        worker.source_conn = _conn_returning(7)

        assert worker._resolve_total_rows("tbl_x", {"row_count": -1}) == (7, False)

    def test_truly_empty_table_is_reported_empty(self, worker):
        """진짜 빈 테이블은 건너뛰어야 한다(COUNT도 싸다)."""
        worker.source_conn = _conn_returning(0)

        assert worker._resolve_total_rows("tbl_x", {"row_count": 0}) == (0, True)

    def test_count_failure_does_not_claim_empty(self, worker):
        """셀 수 없으면 '비었다'고 단정하지 않는다. 건너뛰지 말고 진행시킨다."""
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("권한 없음")
        worker.source_conn = conn

        total, is_empty = worker._resolve_total_rows("tbl_x", {"row_count": 0})

        assert not is_empty, "확인 실패를 '빈 테이블'로 처리하면 데이터가 누락됩니다"
        assert total >= 0, "진행률 계산에 음수를 넘기면 안 됩니다"

    def test_count_failure_rolls_back_the_source_transaction(self, worker):
        """실패한 문장이 트랜잭션을 abort로 남긴다.

        되돌리지 않으면 이어지는 COPY가 전부
        'current transaction is aborted'로 실패한다.
        """
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("boom")
        worker.source_conn = conn

        worker._resolve_total_rows("tbl_x", {"row_count": 0})

        conn.rollback.assert_called_once()

    def test_missing_row_count_key_is_treated_as_unknown(self, worker):
        worker.source_conn = _conn_returning(3)

        assert worker._resolve_total_rows("tbl_x", {}) == (3, False)


class TestSkipDecisionUsesTheResolver:
    """건너뛰기 판단이 추정치를 직접 보지 않는지 소스로 고정한다."""

    def _source(self, func):
        import inspect
        import textwrap

        return textwrap.dedent(inspect.getsource(func))

    def test_python_copy_path_uses_is_empty(self):
        source = self._source(CopyMigrationWorker._migrate_partition_with_copy)
        assert "_resolve_total_rows" in source
        assert 'table_info["row_count"]' not in source, (
            "추정치를 직접 보고 건너뛰면 ANALYZE 전 파티션이 누락됩니다"
        )

    def test_server_side_copy_path_uses_is_empty(self):
        source = self._source(CopyMigrationWorker._migrate_partition_server_copy)
        assert "_resolve_total_rows" in source
        assert 'table_info["row_count"]' not in source
