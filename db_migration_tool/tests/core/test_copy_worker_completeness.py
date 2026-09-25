"""파티션이 '완료'로 기록되려면 원본과 대상 행 수가 정확히 같아야 한다.

워커의 성능 카운터·rows_processed는 전송 경로가 센 값이라, 전송 중 누락이 있으면 같이
틀린다(C-01). 완료 판정은 원본과 대상을 각각 COUNT(*)한 독립 값으로만 한다.

또한 skip_on_error로 건너뛴 파티션이 있으면 실행 전체가 '정상 완료'로 끝나면 안 된다
(감사 H-01: 실패 작업이 completed로 표시되어 재개 대상에서 사라짐).
"""

from __future__ import annotations

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
        w = CopyMigrationWorker(profile, ["tbl_a", "tbl_b"], 1)
    w._log = lambda message, level="INFO": None
    return w


def _conn_counting(count: int) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (count,)
    return conn


class TestPartitionRowCountVerification:
    def test_equal_counts_pass(self, worker):
        worker.source_conn = _conn_counting(4_715_570)
        worker.target_conn = _conn_counting(4_715_570)

        assert worker._verify_partition_row_count("point_history_260402") == 4_715_570

    @pytest.mark.parametrize("target", [4_715_569, 4_715_571, 0])
    def test_any_difference_fails(self, worker, target):
        worker.source_conn = _conn_counting(4_715_570)
        worker.target_conn = _conn_counting(target)

        with pytest.raises(RuntimeError) as exc:
            worker._verify_partition_row_count("point_history_260402")

        msg = str(exc.value)
        assert "4,715,570" in msg and f"{target:,}" in msg


class TestSkippedPartitionsFailTheRun:
    def _prepare(self, worker):
        worker.copy_mode = "python"
        worker.skip_on_error = True
        worker.is_running = True
        worker.checkpoint_manager.get_checkpoints.return_value = []
        worker._create_psycopg2_connection = MagicMock(return_value=MagicMock())
        worker._detect_and_apply_version_optimizations = MagicMock()
        worker._check_copy_permissions = MagicMock()

    def test_run_with_a_skipped_partition_raises(self, worker):
        self._prepare(worker)
        worker._migrate_partition_with_copy = MagicMock(side_effect=[RuntimeError("boom"), None])

        with pytest.raises(Exception) as exc:
            worker._execute_migration()

        assert "tbl_a" in str(exc.value)
        # 건너뛰기 전략이므로 다음 파티션은 계속 시도했어야 한다.
        assert worker._migrate_partition_with_copy.call_count == 2

    def test_run_without_failures_completes(self, worker):
        self._prepare(worker)
        worker._migrate_partition_with_copy = MagicMock(return_value=None)

        worker._execute_migration()  # 예외 없음
