"""파일 아카이브 워커의 M-02/M-04 경로.

- import: manifest 신뢰 판정(인증/legacy 확인)을 대상 DB 연결 전에 한다.
- import: checksum 없는 항목은 명시적 확인 없이는 적재하지 않는다.
- import: 대상 DDL은 검증된 메모리 manifest로 만든다(디스크 재읽기 TOCTOU 차단).
- export: 파일 교체와 manifest 항목 기록을 CAS 잠금 안에서 함께 한다.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from psycopg2 import sql

from src.core.archive_manifest import (
    ArchiveManifestStore,
    ArchivePartitionEntry,
    ManifestAuthError,
    ManifestConflictError,
)
from src.core.file_archive_workers import (
    FileToPostgresArchiveWorker,
    ManifestTableCreator,
    PostgresToFileArchiveWorker,
)
from src.core.table_types import TableType
from src.models.profile import ConnectionProfile

PASS = "archive-passphrase"
NAME = "point_history_240101"
COLUMNS = ["path_id", "issued_date", "changed_value", "connection_status"]
CSV = "1,2024-01-01 00:00:00,12.3,0\n"
PARENT_COLUMNS = [
    {
        "name": "path_id",
        "data_type": "integer",
        "character_maximum_length": None,
        "is_nullable": "NO",
        "column_default": None,
    }
]


def _import_profile(tmp_path) -> ConnectionProfile:
    return ConnectionProfile(
        id=1,
        name="t",
        source_config={"kind": "file", "archive_path": str(tmp_path / "archive")},
        target_config={
            "kind": "postgres",
            "host": "localhost",
            "port": 5432,
            "database": "db",
            "username": "u",
            "password": "p",
        },
    )


def _export_profile(tmp_path) -> ConnectionProfile:
    return ConnectionProfile(
        id=1,
        name="t",
        source_config={
            "kind": "postgres",
            "host": "localhost",
            "port": 5432,
            "database": "db",
            "username": "u",
            "password": "p",
        },
        target_config={"kind": "file", "archive_path": str(tmp_path / "archive")},
    )


def _write_signed_archive(tmp_path, passphrase: str | None = PASS) -> ArchiveManifestStore:
    store = ArchiveManifestStore(tmp_path / "archive", passphrase=passphrase, kdf_iterations=1000)
    manifest = store.load_or_create(source={"kind": "postgres"}, target={"kind": "file"})
    store.upsert_parent_table(
        manifest,
        parent_table="point_history",
        table_type=TableType.POINT_HISTORY,
        columns=PARENT_COLUMNS,
    )
    tmp = store.partitions_dir / ".tmp"
    tmp.write_text(CSV, encoding="utf-8")
    meta = store.compute_file_metadata(tmp)
    store.commit_partition(
        manifest,
        ArchivePartitionEntry(
            partition_name=NAME,
            table_type="PH",
            parent_table="point_history",
            row_count=1,
            file_path=f"partitions/{NAME}.csv",
            columns=list(COLUMNS),
            bytes_written=meta["bytes_written"],
            checksum_sha256=meta["checksum_sha256"],
        ),
        temp_path=tmp,
        final_path=store.build_partition_file_path(NAME),
    )
    return store


def _write_legacy_archive(tmp_path, *, checksum: bool) -> None:
    archive = tmp_path / "archive"
    (archive / "partitions").mkdir(parents=True)
    raw = CSV.encode("utf-8")
    (archive / "partitions" / f"{NAME}.csv").write_bytes(raw)
    data = {
        "format": "psql93-migration-archive",
        "version": 2,
        "source": {"kind": "postgres"},
        "target": {"kind": "file"},
        "parent_tables": {
            "point_history": {"table_type": "PH", "columns": PARENT_COLUMNS},
        },
        "partitions": [
            {
                "partition_name": NAME,
                "table_type": "PH",
                "parent_table": "point_history",
                "row_count": 1,
                "file_path": f"partitions/{NAME}.csv",
                "columns": COLUMNS,
                "bytes_written": len(raw),
                "checksum_sha256": hashlib.sha256(raw).hexdigest() if checksum else None,
            }
        ],
    }
    (archive / "manifest.json").write_text(json.dumps(data), encoding="utf-8")


def _collect_logs(worker) -> list[tuple[str, str]]:
    logs: list[tuple[str, str]] = []
    worker.log.connect(lambda message, level: logs.append((level, message)))
    return logs


def _import_worker(tmp_path, **security) -> FileToPostgresArchiveWorker:
    worker = FileToPostgresArchiveWorker(_import_profile(tmp_path), [NAME], history_id=1)
    worker.archive_store = ArchiveManifestStore(tmp_path / "archive", kdf_iterations=1000)
    worker.archive_store.set_warning_handler(lambda m: worker._log(m, "WARNING"))
    if security:
        worker.configure_archive_security(**security)
    worker.is_running = True
    worker.checkpoint_manager.get_checkpoints = Mock(
        return_value=[SimpleNamespace(id=1, partition_name=NAME, status="pending")]
    )
    worker.checkpoint_manager.update_checkpoint_status = Mock()
    return worker


# ── import: 신뢰 판정 ────────────────────────────────────────────────────


def test_import_refuses_legacy_archive_without_confirmation_before_connecting(tmp_path):
    _write_legacy_archive(tmp_path, checksum=True)
    worker = _import_worker(tmp_path)
    worker._create_psycopg2_connection = Mock()
    worker._import_partition = Mock()

    with pytest.raises(ManifestAuthError, match="확인"):
        worker._execute_migration()

    worker._create_psycopg2_connection.assert_not_called()
    worker._import_partition.assert_not_called()


def test_import_legacy_archive_with_confirmation_warns_and_proceeds(tmp_path):
    _write_legacy_archive(tmp_path, checksum=True)
    worker = _import_worker(tmp_path, allow_legacy_unverified=True)
    logs = _collect_logs(worker)
    worker._create_psycopg2_connection = Mock(return_value=MagicMock())
    worker._import_partition = Mock()

    worker._execute_migration()

    worker._import_partition.assert_called_once()
    assert any(level == "WARNING" and "인증" in msg for level, msg in logs)


def test_import_signed_archive_requires_passphrase(tmp_path):
    _write_signed_archive(tmp_path)
    worker = _import_worker(tmp_path, allow_legacy_unverified=True)
    worker._create_psycopg2_connection = Mock()
    with pytest.raises(ManifestAuthError, match="passphrase"):
        worker._execute_migration()
    worker._create_psycopg2_connection.assert_not_called()


def test_import_signed_archive_with_wrong_passphrase_fails(tmp_path):
    _write_signed_archive(tmp_path)
    worker = _import_worker(tmp_path, passphrase="nope")
    worker._create_psycopg2_connection = Mock()
    with pytest.raises(ManifestAuthError):
        worker._execute_migration()
    worker._create_psycopg2_connection.assert_not_called()


def test_import_signed_archive_with_passphrase_uses_verified_manifest_for_ddl(tmp_path):
    _write_signed_archive(tmp_path)
    worker = _import_worker(tmp_path, passphrase=PASS)
    logs = _collect_logs(worker)
    worker._create_psycopg2_connection = Mock(return_value=MagicMock())
    seen = {}

    def fake_import(partition_name, checkpoint, creator, manifest):
        seen["creator_manifest"] = creator.manifest
        seen["manifest"] = manifest

    worker._import_partition = fake_import
    worker._execute_migration()

    assert seen["creator_manifest"] is seen["manifest"]
    assert seen["manifest"].is_authenticated
    assert not any(level == "WARNING" for level, _ in logs)


def test_manifest_table_creator_ignores_disk_manifest_swapped_after_verification(tmp_path):
    store = _write_signed_archive(tmp_path)
    verified = ArchiveManifestStore(
        tmp_path / "archive", passphrase=PASS, kdf_iterations=1000
    ).load()

    # 검증 뒤 누군가 디스크 manifest의 DDL 메타데이터를 바꿔치기한다.
    data = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    data["parent_tables"]["point_history"]["columns"][0]["data_type"] = "integer; DROP TABLE x"
    store.manifest_path.write_text(json.dumps(data), encoding="utf-8")

    executed: list[str] = []
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.execute.side_effect = executed.append
    creator = ManifestTableCreator(store, conn, manifest=verified)
    creator._create_parent_table("point_history")

    assert executed and "DROP TABLE" not in executed[0]
    assert creator._get_partition_info(NAME, "point_history")["table_type"] == (
        TableType.POINT_HISTORY
    )


# ── import: checksum 없는 항목 ──────────────────────────────────────────


def _run_import_partition(worker, tmp_path, monkeypatch):
    manifest = worker.archive_store.load()
    worker.archive_store.require_trusted(manifest)
    worker.target_conn = MagicMock()
    monkeypatch.setattr(worker, "_query_row_count", lambda conn, name: 1)
    monkeypatch.setattr(sql.Composed, "as_string", lambda self, ctx: "COPY x FROM STDIN")
    creator = Mock()
    checkpoint = SimpleNamespace(id=1, partition_name=NAME)
    worker._import_partition(NAME, checkpoint, creator, manifest)
    return creator


def test_import_partition_without_checksum_is_rejected_by_default(tmp_path, monkeypatch):
    _write_legacy_archive(tmp_path, checksum=False)
    worker = _import_worker(tmp_path)
    worker.archive_store.allow_legacy_unverified = True  # manifest 판정만 통과시킨다
    with pytest.raises(ValueError, match="checksum"):
        _run_import_partition(worker, tmp_path, monkeypatch)
    args, kwargs = worker.checkpoint_manager.update_checkpoint_status.call_args_list[-1]
    assert args[1] == "failed"
    payload = json.loads(kwargs["error_message"])
    assert payload["phase"] == "verify_archive"
    assert payload["event_type"] == "file_integrity_error"


def test_import_partition_without_checksum_with_confirmation_warns(tmp_path, monkeypatch):
    _write_legacy_archive(tmp_path, checksum=False)
    worker = _import_worker(tmp_path, allow_legacy_unverified=True)
    logs = _collect_logs(worker)
    creator = _run_import_partition(worker, tmp_path, monkeypatch)

    creator.ensure_partition_ready.assert_called_once()
    worker.target_conn.commit.assert_called_once()
    assert any(level == "WARNING" and "checksum" in msg for level, msg in logs)


# ── export ─────────────────────────────────────────────────────────────


class _FakeSourceConn:
    def __init__(self, on_copy=None):
        self.on_copy = on_copy

    def set_isolation_level(self, level):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def cancel(self):
        pass

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def copy_expert(self, query, fp):
                fp.write(CSV)
                if conn.on_copy is not None:
                    conn.on_copy()

        return _Cur()


def _export_worker(tmp_path, monkeypatch, **security) -> PostgresToFileArchiveWorker:
    worker = PostgresToFileArchiveWorker(_export_profile(tmp_path), [NAME], history_id=1)
    worker.archive_store = ArchiveManifestStore(tmp_path / "archive", kdf_iterations=1000)
    worker.archive_store.set_warning_handler(lambda m: worker._log(m, "WARNING"))
    if security:
        worker.configure_archive_security(**security)
    worker.is_running = True
    worker.checkpoint_manager.get_checkpoints = Mock(
        return_value=[SimpleNamespace(id=1, partition_name=NAME, status="pending")]
    )
    worker.checkpoint_manager.update_checkpoint_status = Mock()
    monkeypatch.setattr(worker, "_query_row_count", lambda conn, name: 1)
    monkeypatch.setattr(worker, "_query_parent_columns", lambda conn, parent: PARENT_COLUMNS)
    monkeypatch.setattr(
        worker,
        "_query_partition_meta",
        lambda conn, name, tt: {"table_data": "PH", "from_date": 1, "to_date": 2},
    )
    monkeypatch.setattr(sql.Composed, "as_string", lambda self, ctx: "COPY x TO STDOUT")
    return worker


def test_export_with_passphrase_writes_signed_entry_with_checksum(tmp_path, monkeypatch):
    worker = _export_worker(tmp_path, monkeypatch, passphrase=PASS)
    worker._create_psycopg2_connection = Mock(return_value=_FakeSourceConn())

    worker._execute_migration()

    final = tmp_path / "archive" / "partitions" / f"{NAME}.csv"
    assert final.read_text(encoding="utf-8") == CSV
    assert not list((tmp_path / "archive" / "partitions").glob("*.tmp"))
    reader = ArchiveManifestStore(tmp_path / "archive", passphrase=PASS, kdf_iterations=1000)
    manifest = reader.load()
    assert manifest.is_authenticated
    item = manifest.partitions[0]
    assert item["checksum_sha256"] == hashlib.sha256(CSV.encode()).hexdigest()
    assert item["entry_version"] == 1
    args, _ = worker.checkpoint_manager.update_checkpoint_status.call_args_list[-1]
    assert args[1] == "completed"


def test_export_without_passphrase_warns_that_manifest_is_unauthenticated(tmp_path, monkeypatch):
    worker = _export_worker(tmp_path, monkeypatch)
    logs = _collect_logs(worker)
    worker._create_psycopg2_connection = Mock(return_value=_FakeSourceConn())

    worker._execute_migration()

    assert any(level == "WARNING" and "passphrase" in msg for level, msg in logs)
    item = json.loads((tmp_path / "archive" / "manifest.json").read_text(encoding="utf-8"))[
        "partitions"
    ][0]
    assert item["checksum_sha256"] == hashlib.sha256(CSV.encode()).hexdigest()


def test_export_without_passphrase_cannot_append_to_signed_archive(tmp_path, monkeypatch):
    _write_signed_archive(tmp_path)
    worker = _export_worker(tmp_path, monkeypatch)
    worker._create_psycopg2_connection = Mock(return_value=_FakeSourceConn())
    worker._export_partition = Mock()

    with pytest.raises(ManifestAuthError, match="passphrase"):
        worker._execute_migration()
    worker._export_partition.assert_not_called()


def test_export_racing_writer_on_same_partition_fails_without_mixing_file_and_manifest(
    tmp_path, monkeypatch
):
    worker = _export_worker(tmp_path, monkeypatch)
    rival_csv = "9,2024-01-01 00:00:00,9.9,1\n"

    def rival_commits_same_partition():
        rival = ArchiveManifestStore(tmp_path / "archive")
        manifest = rival.load()
        tmp = rival.partitions_dir / ".rival.tmp"
        tmp.write_text(rival_csv, encoding="utf-8")
        meta = rival.compute_file_metadata(tmp)
        rival.commit_partition(
            manifest,
            ArchivePartitionEntry(
                partition_name=NAME,
                table_type="PH",
                parent_table="point_history",
                row_count=1,
                file_path=f"partitions/{NAME}.csv",
                columns=list(COLUMNS),
                bytes_written=meta["bytes_written"],
                checksum_sha256=meta["checksum_sha256"],
            ),
            temp_path=tmp,
            final_path=rival.build_partition_file_path(NAME),
        )

    worker._create_psycopg2_connection = Mock(
        return_value=_FakeSourceConn(on_copy=rival_commits_same_partition)
    )

    with pytest.raises(ManifestConflictError):
        worker._execute_migration()

    final = tmp_path / "archive" / "partitions" / f"{NAME}.csv"
    assert final.read_text(encoding="utf-8") == rival_csv
    item = json.loads((tmp_path / "archive" / "manifest.json").read_text(encoding="utf-8"))[
        "partitions"
    ][0]
    assert item["checksum_sha256"] == hashlib.sha256(rival_csv.encode()).hexdigest()
    assert not list((tmp_path / "archive" / "partitions").glob(f".{NAME}_*.tmp"))
    args, _ = worker.checkpoint_manager.update_checkpoint_status.call_args_list[-1]
    assert args[1] == "failed"


# ── import: 대상 쓰기 전 전 파일 사전 검증 ────────────────────────────────


def test_import_preflight_rejects_corrupted_file_before_touching_target(tmp_path):
    store = _write_signed_archive(tmp_path)
    store.build_partition_file_path(NAME).write_text("tampered\n", encoding="utf-8")
    worker = _import_worker(tmp_path, passphrase=PASS)
    worker._create_psycopg2_connection = Mock()
    worker._import_partition = Mock()

    with pytest.raises(ValueError, match=NAME):
        worker._execute_migration()

    worker._create_psycopg2_connection.assert_not_called()
    worker._import_partition.assert_not_called()


def test_import_preflight_rejects_partition_missing_from_manifest(tmp_path):
    _write_signed_archive(tmp_path)
    worker = _import_worker(tmp_path, passphrase=PASS)
    worker.partitions = [NAME, "point_history_240102"]
    worker._create_psycopg2_connection = Mock()
    with pytest.raises(ValueError, match="point_history_240102"):
        worker._execute_migration()
    worker._create_psycopg2_connection.assert_not_called()


def test_import_preflight_with_skip_on_error_warns_and_lets_partition_fail_individually(
    tmp_path,
):
    store = _write_signed_archive(tmp_path)
    store.build_partition_file_path(NAME).write_text("tampered\n", encoding="utf-8")
    worker = _import_worker(tmp_path, passphrase=PASS)
    worker.skip_on_error = True
    logs = _collect_logs(worker)
    worker._create_psycopg2_connection = Mock(return_value=MagicMock())
    worker._import_partition = Mock()

    worker._execute_migration()

    worker._import_partition.assert_called_once()
    assert any(level == "WARNING" and "사전 검증" in msg for level, msg in logs)


def test_import_preflight_stops_quietly_when_cancelled(tmp_path):
    _write_signed_archive(tmp_path)
    worker = _import_worker(tmp_path, passphrase=PASS)
    worker._create_psycopg2_connection = Mock()
    real = ArchiveManifestStore.compute_file_metadata

    def stop_then_compute(path, *, should_stop=None):
        worker.stop("user_cancel")
        return real(worker.archive_store, path, should_stop=should_stop)

    worker.archive_store.compute_file_metadata = stop_then_compute
    worker._execute_migration()
    worker._create_psycopg2_connection.assert_not_called()
