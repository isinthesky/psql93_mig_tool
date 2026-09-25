"""H-09 리뷰 major — 아카이브 워커의 '원본에 없는 파티션' 처리.

legacy 이력을 채택할 때 보충하는 파티션은 명명 규칙으로 만든 '있을 수 있는 이름'이지 실제로
있는 파티션이 아니다. copy 워커는 원본 테이블이 없으면 0건 완료로 처리하는데, 아카이브 워커는
그렇지 않아 채택한 이력이 영영 끝나지 않았다(FileToPostgres: manifest에 없으면 예외,
PostgresToFile: 없는 테이블의 COUNT 실패).

규칙
- 원본 부재를 0건 완료로 보는 것은 **legacy에서 채택한 이력**(`tolerate_absent_source`)만이다.
  새로 만든 계획은 원본 목록에서 고른 파티션이므로, 없어졌다면 여전히 실패한다.
- '실제 부재'만 0건 완료다. manifest에 항목이 있는데 파일이 없거나, 항목은 없는데 파일은
  있거나, 존재 확인 쿼리 자체가 실패하면 여전히 실패한다(무결성 판정 유지).
"""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg2
import pytest

from src.core.archive_manifest import ArchivePartitionEntry
from src.core.file_archive_workers import (
    FileToPostgresArchiveWorker,
    PostgresToFileArchiveWorker,
)
from src.models.profile import ConnectionProfile

PRESENT = "point_history_240101"
ABSENT = "point_history_240102"

PG = {
    "kind": "postgres",
    "host": "localhost",
    "port": 5432,
    "database": "db",
    "username": "user",
    "password": "pass",
    "ssl": False,
}


class DummyConn:
    def __init__(self, exists: dict[str, bool] | None = None, fail: Exception | None = None):
        self.exists = exists or {}
        self.fail = fail
        self.queries: list[tuple[str, tuple]] = []
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self)

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        pass

    def close(self):
        pass

    def cancel(self):
        pass


class _Cursor:
    def __init__(self, conn: DummyConn):
        self.conn = conn
        self._row: tuple | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.conn.queries.append((sql, tuple(params)))
        if self.conn.fail is not None:
            raise self.conn.fail
        self._row = (self.conn.exists.get(params[-1], False),)

    def fetchone(self):
        return self._row


def _checkpoints(names: list[str]) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(id=i, partition_name=n, status="pending")
        for i, n in enumerate(names, start=1)
    ]


def _completed_zero(worker, name: str) -> bool:
    cp_id = {n: i for i, n in enumerate(worker.partitions, start=1)}[name]
    for call in worker.checkpoint_manager.update_checkpoint_status.call_args_list:
        if call.args[:2] == (cp_id, "completed") and call.kwargs.get("rows_processed") == 0:
            return True
    return False


# ---------------------------------------------------------------- File → PostgreSQL


def _import_worker(tmp_path, names: list[str], *, tolerate: bool):
    profile = ConnectionProfile(
        id=1,
        name="t",
        source_config={"kind": "file", "archive_path": str(tmp_path / "archive")},
        target_config=dict(PG),
    )
    worker = FileToPostgresArchiveWorker(profile, names, history_id=1, resume=True)
    worker.tolerate_absent_source = tolerate
    worker.is_running = True
    worker.checkpoint_manager.get_checkpoints = Mock(return_value=_checkpoints(names))
    worker.checkpoint_manager.update_checkpoint_status = Mock()
    worker._create_psycopg2_connection = Mock(return_value=DummyConn())
    worker.archive_store.require_trusted = Mock(return_value=[])
    worker._import_partition = Mock()
    return worker


def _entry(name: str) -> dict:
    return asdict(
        ArchivePartitionEntry(
            partition_name=name,
            table_type="PH",
            parent_table="point_history",
            row_count=1,
            file_path=f"partitions/{name}.csv",
            columns=["path_id", "issued_date", "changed_value", "connection_status"],
            checksum_sha256="0" * 64,
        )
    )


def _manifest(*names: str) -> SimpleNamespace:
    return SimpleNamespace(partitions=[_entry(n) for n in names], is_authenticated=False)


class TestImportAbsentPartition:
    def test_adopted_legacy_absent_partition_completes_with_zero_rows(self, tmp_path):
        worker = _import_worker(tmp_path, [PRESENT, ABSENT], tolerate=True)
        worker.archive_store.load = Mock(return_value=_manifest(PRESENT))
        worker.archive_store.verify_partition_file = Mock()

        worker._execute_migration()

        # 있는 파티션만 가져오고, 없는 파티션은 0건 완료로 닫는다(작업이 끝날 수 있다).
        assert [c.args[0] for c in worker._import_partition.call_args_list] == [PRESENT]
        assert _completed_zero(worker, ABSENT)
        assert worker.partition_failures == []
        assert worker.performance_metrics.completed_partitions == 1

    def test_absent_partition_without_legacy_adoption_still_fails(self, tmp_path):
        worker = _import_worker(tmp_path, [ABSENT], tolerate=False)
        worker.archive_store.load = Mock(return_value=_manifest())

        with pytest.raises(ValueError, match="manifest에 없음"):
            worker._execute_migration()

        worker._import_partition.assert_not_called()
        assert not _completed_zero(worker, ABSENT)

    def test_manifest_gap_with_file_on_disk_is_not_absence(self, tmp_path):
        """manifest에서 항목만 빠지고 파일은 남아 있다 — 부재가 아니라 manifest 누락이다."""
        worker = _import_worker(tmp_path, [ABSENT], tolerate=True)
        worker.archive_store.load = Mock(return_value=_manifest())
        path = worker.archive_store.build_partition_file_path(ABSENT)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1,2024-01-02 00:00:00,1.0,0\n", encoding="utf-8")

        with pytest.raises(ValueError, match="manifest에 없음"):
            worker._execute_migration()

        assert not _completed_zero(worker, ABSENT)

    def test_listed_partition_with_missing_file_still_fails(self, tmp_path):
        """manifest에 있는데 파일이 없다 — 원본 부재가 아니라 아카이브 손상이다."""
        worker = _import_worker(tmp_path, [PRESENT], tolerate=True)
        worker.archive_store.load = Mock(return_value=_manifest(PRESENT))

        with pytest.raises(ValueError, match="사전 검증 실패"):
            worker._execute_migration()

        worker._import_partition.assert_not_called()
        assert not _completed_zero(worker, PRESENT)


# ---------------------------------------------------------------- PostgreSQL → File


def _export_worker(tmp_path, names: list[str], conn: DummyConn, *, tolerate: bool):
    profile = ConnectionProfile(
        id=1,
        name="t",
        source_config=dict(PG),
        target_config={"kind": "file", "archive_path": str(tmp_path / "archive")},
    )
    worker = PostgresToFileArchiveWorker(profile, names, history_id=1, resume=True)
    worker.tolerate_absent_source = tolerate
    worker.is_running = True
    worker.checkpoint_manager.get_checkpoints = Mock(return_value=_checkpoints(names))
    worker.checkpoint_manager.update_checkpoint_status = Mock()
    worker._create_psycopg2_connection = Mock(return_value=conn)
    worker.archive_store.load_or_create = Mock(return_value=SimpleNamespace(partitions=[]))
    worker.archive_store.save = Mock()
    worker.archive_store.commit_partition = Mock()
    worker._export_partition = Mock()
    return worker


class TestExportAbsentPartition:
    def test_adopted_legacy_absent_source_table_completes_with_zero_rows(self, tmp_path):
        conn = DummyConn(exists={PRESENT: True, ABSENT: False})
        worker = _export_worker(tmp_path, [PRESENT, ABSENT], conn, tolerate=True)

        worker._execute_migration()

        assert [c.args[0] for c in worker._export_partition.call_args_list] == [PRESENT]
        assert _completed_zero(worker, ABSENT)
        worker.archive_store.commit_partition.assert_not_called()
        assert worker.partition_failures == []
        # 존재 확인은 schema를 한정하고 이름을 파라미터로 넘긴다. 권한과 무관한 pg_catalog를 본다.
        sql, params = conn.queries[-1]
        assert "pg_catalog" in sql
        assert params == ("public", ABSENT)
        # 확인 트랜잭션을 닫아 다음 파티션의 격리 수준 설정과 겹치지 않게 한다.
        assert conn.rollbacks >= 2

    def test_existence_check_failure_is_not_absence(self, tmp_path):
        conn = DummyConn(fail=psycopg2.OperationalError("server closed the connection"))
        worker = _export_worker(tmp_path, [ABSENT], conn, tolerate=True)

        with pytest.raises(psycopg2.OperationalError):
            worker._execute_migration()

        worker._export_partition.assert_not_called()
        assert not _completed_zero(worker, ABSENT)

    def test_without_legacy_adoption_absence_is_not_checked(self, tmp_path):
        """새 계획은 원본 목록에서 고른 파티션이다 — 없어졌으면 기존처럼 export에서 실패한다."""
        conn = DummyConn(exists={ABSENT: False})
        worker = _export_worker(tmp_path, [ABSENT], conn, tolerate=False)

        worker._execute_migration()

        assert conn.queries == []
        assert [c.args[0] for c in worker._export_partition.call_args_list] == [ABSENT]
        assert not _completed_zero(worker, ABSENT)
