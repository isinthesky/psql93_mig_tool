from types import SimpleNamespace
from unittest.mock import Mock

from src.core.file_archive_workers import FileToPostgresArchiveWorker, PostgresToFileArchiveWorker
from src.core.table_types import TableType
from src.models.profile import ConnectionProfile


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


def test_export_worker_detect_table_type(tmp_path):
    worker = PostgresToFileArchiveWorker(make_profile(tmp_path, "postgres_to_file"), [], history_id=1)
    assert worker._detect_table_type("point_history_240101") == TableType.POINT_HISTORY


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
