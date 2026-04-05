from pathlib import Path

from src.utils.validators import ConnectionValidator


def test_validate_postgres_config_ok():
    valid, msg = ConnectionValidator.validate_connection_config(
        {
            "kind": "postgres",
            "host": "localhost",
            "port": 5432,
            "database": "testdb",
            "username": "tester",
        }
    )
    assert valid is True
    assert msg == ""


def test_validate_file_archive_source_requires_manifest(tmp_path: Path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    valid, msg = ConnectionValidator.validate_file_archive_config(
        {"kind": "file", "archive_path": str(archive_dir)},
        must_exist=True,
    )
    assert valid is False
    assert "manifest.json" in msg


def test_validate_file_archive_target_ok_with_existing_parent(tmp_path: Path):
    archive_dir = tmp_path / "export-archive"
    valid, msg = ConnectionValidator.validate_file_archive_config(
        {"kind": "file", "archive_path": str(archive_dir)},
        must_exist=False,
    )
    assert valid is True
    assert msg == ""


def test_validate_endpoint_pair_rejects_file_to_file():
    valid, msg = ConnectionValidator.validate_endpoint_pair(
        {"kind": "file", "archive_path": "/tmp/source"},
        {"kind": "file", "archive_path": "/tmp/target"},
    )
    assert valid is False
    assert "PostgreSQL↔PostgreSQL" in msg
