import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.archive_manifest import ArchivePartitionEntry
from src.core.file_archive_workers import (
    FileToPostgresArchiveWorker,
    ManifestTableCreator,
    MigrationInterruptedError,
    PostgresToFileArchiveWorker,
)
from src.core.table_types import TableType
from src.models.profile import ConnectionProfile


class DummyConn:
    def __init__(self):
        self.closed = False
        self.cancelled = False
        self.rolled_back = False
        self.committed = False

    def close(self):
        self.closed = True

    def cancel(self):
        self.cancelled = True

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True


class RecordingCursor:
    def __init__(self, executed):
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql_text, *args, **kwargs):
        self.executed.append(sql_text)


class RecordingConn(DummyConn):
    def __init__(self):
        super().__init__()
        self.executed = []

    def cursor(self):
        return RecordingCursor(self.executed)


def make_profile(tmp_path, mode: str) -> ConnectionProfile:
    if mode == "postgres_to_file":
        return ConnectionProfile(
            id=1,
            name="test",
            source_config={
                "kind": "postgres",
                "host": "localhost",
                "port": 5432,
                "database": "db",
                "username": "user",
                "password": "pass",
                "ssl": False,
            },
            target_config={
                "kind": "file",
                "archive_path": str(tmp_path / "archive"),
            },
        )
    return ConnectionProfile(
        id=1,
        name="test",
        source_config={
            "kind": "file",
            "archive_path": str(tmp_path / "archive"),
        },
        target_config={
            "kind": "postgres",
            "host": "localhost",
            "port": 5432,
            "database": "db",
            "username": "user",
            "password": "pass",
            "ssl": False,
        },
    )


def build_manifest_with_partition(worker: FileToPostgresArchiveWorker, *, checksum_sha256: str):
    store = worker.archive_store
    manifest = store.load_or_create(source={"kind": "file"}, target={"kind": "postgres"})
    store.upsert_parent_table(
        manifest,
        parent_table="point_history",
        table_type=TableType.POINT_HISTORY,
        columns=[
            {
                "name": "path_id",
                "data_type": "integer",
                "character_maximum_length": None,
                "is_nullable": "NO",
                "column_default": None,
            },
            {
                "name": "issued_date",
                "data_type": "timestamp without time zone",
                "character_maximum_length": None,
                "is_nullable": "NO",
                "column_default": None,
            },
            {
                "name": "changed_value",
                "data_type": "double precision",
                "character_maximum_length": None,
                "is_nullable": "YES",
                "column_default": None,
            },
            {
                "name": "connection_status",
                "data_type": "integer",
                "character_maximum_length": None,
                "is_nullable": "YES",
                "column_default": None,
            },
        ],
    )

    file_path = store.build_partition_file_path("point_history_240101")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("1,2024-01-01 00:00:00,12.3,0\n", encoding="utf-8")
    metadata = store.compute_file_metadata(file_path)
    entry = ArchivePartitionEntry(
        partition_name="point_history_240101",
        table_type="PH",
        parent_table="point_history",
        row_count=1,
        file_path="partitions/point_history_240101.csv",
        columns=["path_id", "issued_date", "changed_value", "connection_status"],
        from_timestamp=1704067200000,
        to_timestamp=1704153599000,
        bytes_written=metadata["bytes_written"],
        checksum_sha256=checksum_sha256,
        verified_at=metadata["verified_at"],
    )
    store.upsert_partition(manifest, entry)
    store.save(manifest)
    return manifest, entry, metadata


def test_export_worker_detect_table_type(tmp_path):
    worker = PostgresToFileArchiveWorker(
        make_profile(tmp_path, "postgres_to_file"), [], history_id=1
    )
    assert worker._detect_table_type("point_history_240101") == TableType.POINT_HISTORY


def test_archive_worker_connection_defaults_missing_port(tmp_path, monkeypatch):
    captured = {}

    def fake_connect(**params):
        captured.update(params)
        return DummyConn()

    monkeypatch.setattr("src.core.file_archive_workers.psycopg2.connect", fake_connect)

    worker = PostgresToFileArchiveWorker(
        make_profile(tmp_path, "postgres_to_file"), [], history_id=1
    )
    config = {
        "kind": "postgres",
        "host": "localhost",
        "database": "db",
        "username": "user",
        "password": "pass",
    }

    conn = worker._create_psycopg2_connection(config)

    assert conn.closed is False
    assert captured["port"] == 5432


def test_import_worker_prepare_target_table_resume_uses_auto(tmp_path):
    worker = FileToPostgresArchiveWorker(
        make_profile(tmp_path, "file_to_postgres"),
        [],
        history_id=1,
        resume=True,
    )
    creator = Mock()
    worker.is_running = True
    worker._prepare_target_table("point_history_240101", SimpleNamespace(id=1), creator)
    creator.ensure_partition_ready.assert_called_once()
    assert creator.ensure_partition_ready.call_args.kwargs["truncate_mode"] == "auto"


def test_export_worker_skip_on_error_continues_to_next_partition(tmp_path):
    worker = PostgresToFileArchiveWorker(
        make_profile(tmp_path, "postgres_to_file"),
        ["p1", "p2"],
        history_id=1,
    )
    worker.skip_on_error = True
    worker.is_running = True
    worker.checkpoint_manager.get_checkpoints = Mock(
        return_value=[
            SimpleNamespace(id=1, partition_name="p1", status="pending"),
            SimpleNamespace(id=2, partition_name="p2", status="pending"),
        ]
    )
    worker._create_psycopg2_connection = Mock(return_value=DummyConn())
    worker.archive_store.load_or_create = Mock(return_value=SimpleNamespace(partitions=[]))
    worker.archive_store.save = Mock()

    calls = []

    def fake_export(partition_name, checkpoint, manifest):
        calls.append(partition_name)
        if partition_name == "p1":
            raise RuntimeError("boom")

    worker._export_partition = fake_export

    worker._execute_migration()

    assert calls == ["p1", "p2"]
    assert worker.partition_failures[0]["partition"] == "p1"


def test_import_worker_skip_on_error_continues_to_next_partition(tmp_path):
    worker = FileToPostgresArchiveWorker(
        make_profile(tmp_path, "file_to_postgres"),
        ["p1", "p2"],
        history_id=1,
    )
    worker.skip_on_error = True
    worker.is_running = True
    worker.checkpoint_manager.get_checkpoints = Mock(
        return_value=[
            SimpleNamespace(id=1, partition_name="p1", status="pending"),
            SimpleNamespace(id=2, partition_name="p2", status="pending"),
        ]
    )
    worker._create_psycopg2_connection = Mock(return_value=DummyConn())
    worker.archive_store.load = Mock(return_value=SimpleNamespace(partitions=[]))

    calls = []

    def fake_import(partition_name, checkpoint, creator, manifest):
        calls.append(partition_name)
        if partition_name == "p1":
            raise RuntimeError("broken import")

    worker._import_partition = fake_import

    worker._execute_migration()

    assert calls == ["p1", "p2"]
    assert worker.partition_failures[0]["partition"] == "p1"


def test_import_partition_verifies_archive_before_prepare(tmp_path):
    worker = FileToPostgresArchiveWorker(
        make_profile(tmp_path, "file_to_postgres"),
        ["point_history_240101"],
        history_id=1,
    )
    manifest, _, _ = build_manifest_with_partition(worker, checksum_sha256="0" * 64)
    worker.is_running = True
    worker.target_conn = Mock()
    worker.checkpoint_manager.update_checkpoint_status = Mock()
    creator = Mock()
    checkpoint = SimpleNamespace(id=1, partition_name="point_history_240101")

    with pytest.raises(ValueError, match="체크섬"):
        worker._import_partition("point_history_240101", checkpoint, creator, manifest)

    creator.ensure_partition_ready.assert_not_called()
    args, kwargs = worker.checkpoint_manager.update_checkpoint_status.call_args_list[-1]
    assert args[1] == "failed"
    payload = json.loads(kwargs["error_message"])
    assert payload["phase"] == "verify_archive"
    assert payload["event_type"] == "file_integrity_error"
    assert payload["resumable"] is True
    assert "resume" in payload["next_action"]


def test_import_partition_stop_requested_records_pending_interruption(tmp_path):
    worker = FileToPostgresArchiveWorker(
        make_profile(tmp_path, "file_to_postgres"),
        ["point_history_240101"],
        history_id=1,
    )
    manifest, _, _ = build_manifest_with_partition(worker, checksum_sha256=None)
    worker.is_running = False
    worker.stop_reason = "user_cancel"
    worker.target_conn = Mock()
    worker.checkpoint_manager.update_checkpoint_status = Mock()
    creator = Mock()
    checkpoint = SimpleNamespace(id=1, partition_name="point_history_240101")

    with pytest.raises(MigrationInterruptedError, match="중단"):
        worker._import_partition("point_history_240101", checkpoint, creator, manifest)

    creator.ensure_partition_ready.assert_not_called()
    args, kwargs = worker.checkpoint_manager.update_checkpoint_status.call_args_list[-1]
    assert args[1] == "pending"
    payload = json.loads(kwargs["error_message"])
    assert payload["phase"] == "verify_archive"
    assert payload["event_type"] == "user_cancel"
    assert payload["reason"] == "interrupted"


def test_manifest_table_creator_rejects_unsafe_manifest_column_type(tmp_path):
    worker = FileToPostgresArchiveWorker(
        make_profile(tmp_path, "file_to_postgres"),
        [],
        history_id=1,
    )
    store = worker.archive_store
    manifest = store.load_or_create(source={"kind": "file"}, target={"kind": "postgres"})
    manifest.parent_tables["point_history"] = {
        "table_type": "PH",
        "table_name": "point_history",
        "date_column": "issued_date",
        "date_is_timestamp": True,
        "columns": [
            {
                "name": "path_id",
                "data_type": "integer; DROP TABLE point_history; --",
                "character_maximum_length": None,
                "is_nullable": "NO",
                "column_default": None,
            }
        ],
    }
    store.save(manifest)

    creator = ManifestTableCreator(store, RecordingConn())
    with pytest.raises(ValueError, match="안전하지 않은 데이터 타입"):
        creator._create_parent_table("point_history")


def test_manifest_table_creator_skips_unsafe_default_expression(tmp_path):
    worker = FileToPostgresArchiveWorker(
        make_profile(tmp_path, "file_to_postgres"),
        [],
        history_id=1,
    )
    store = worker.archive_store
    manifest = store.load_or_create(source={"kind": "file"}, target={"kind": "postgres"})
    manifest.parent_tables["point_history"] = {
        "table_type": "PH",
        "table_name": "point_history",
        "date_column": "issued_date",
        "date_is_timestamp": True,
        "columns": [
            {
                "name": "path_id",
                "data_type": "integer",
                "character_maximum_length": None,
                "is_nullable": "NO",
                "column_default": "pg_sleep(10)",
            }
        ],
    }
    store.save(manifest)

    conn = RecordingConn()
    creator = ManifestTableCreator(store, conn)
    creator._create_parent_table("point_history")

    assert conn.executed
    assert "pg_sleep(10)" not in conn.executed[0]
