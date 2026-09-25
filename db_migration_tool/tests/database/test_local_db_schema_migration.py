"""구버전 로컬 SQLite 파일이 새 스키마로 올라가는가.

Windows 사용자 DB(%APPDATA%\\db_migration.db)에는 이미 수십 건의 이력이 있다.
H-08/H-09용 컬럼을 추가해도 기존 행이 깨지거나 사라지면 안 되고, 기존 이력은
지문이 없는 legacy로 남아야 한다(가짜 계획을 backfill하지 않는다).
"""

from __future__ import annotations

import sqlite3

import pytest

import src.database.local_db as local_db_module
from src.database.local_db import LocalDatabase

# 1.0 계열: checkpoints 재개 키·연결 상태 컬럼이 생기기 전
V1_DDL = """
CREATE TABLE profiles (
    id INTEGER NOT NULL, name VARCHAR(100) NOT NULL, source_config TEXT NOT NULL,
    target_config TEXT NOT NULL, created_at DATETIME, updated_at DATETIME,
    PRIMARY KEY (id), UNIQUE (name)
);
CREATE TABLE migration_history (
    id INTEGER NOT NULL, profile_id INTEGER NOT NULL, start_date VARCHAR(10),
    end_date VARCHAR(10), started_at DATETIME, completed_at DATETIME, status VARCHAR(20),
    total_rows INTEGER, processed_rows INTEGER, PRIMARY KEY (id)
);
CREATE TABLE checkpoints (
    id INTEGER NOT NULL, history_id INTEGER NOT NULL, partition_name VARCHAR(100) NOT NULL,
    status VARCHAR(20), rows_processed INTEGER, error_message TEXT, PRIMARY KEY (id)
);
"""

# 1.2.7 출시본 스키마 (H-08/H-09 컬럼 직전)
V127_DDL = """
CREATE TABLE profiles (
    id INTEGER NOT NULL, name VARCHAR(100) NOT NULL, source_config TEXT NOT NULL,
    target_config TEXT NOT NULL, created_at DATETIME, updated_at DATETIME,
    PRIMARY KEY (id), UNIQUE (name)
);
CREATE TABLE migration_history (
    id INTEGER NOT NULL, profile_id INTEGER NOT NULL, start_date VARCHAR(10),
    end_date VARCHAR(10), started_at DATETIME, completed_at DATETIME, status VARCHAR(20),
    total_rows INTEGER, processed_rows INTEGER, source_connection_status TEXT,
    target_connection_status TEXT, connection_check_time DATETIME, PRIMARY KEY (id)
);
CREATE TABLE checkpoints (
    id INTEGER NOT NULL, history_id INTEGER NOT NULL, partition_name VARCHAR(100) NOT NULL,
    status VARCHAR(20), rows_processed INTEGER, error_message TEXT, last_path_id INTEGER,
    last_issued_date INTEGER, last_issued_date_text TEXT, copy_method VARCHAR(10),
    bytes_transferred INTEGER, PRIMARY KEY (id)
);
"""

NEW_HISTORY_COLUMNS = {
    "plan_version",
    "migration_mode",
    "schema_name",
    "source_fingerprint",
    "target_fingerprint",
    "endpoint_label",
    "planned_partitions",
    "planned_count",
    "planned_hash",
    "plan_fingerprint",
    "legacy_adopted_at",
}

HISTORY_ROWS = 56  # Windows 사용자 DB 규모


def _build_old_db(path: str, ddl: str) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(ddl)
        statuses = ["completed", "failed", "running", "cancelled"]
        for i in range(1, HISTORY_ROWS + 1):
            con.execute(
                "INSERT INTO migration_history (id, profile_id, start_date, end_date, "
                "started_at, status, total_rows, processed_rows) VALUES (?,?,?,?,?,?,?,?)",
                (
                    i,
                    1 + i % 3,
                    "2026-01-01",
                    "2026-01-02",
                    f"2026-01-01 00:{i % 60:02d}:00",
                    statuses[i % 4],
                    1000 * i,
                    500 * i,
                ),
            )
            for j in range(2):
                con.execute(
                    "INSERT INTO checkpoints (history_id, partition_name, status, "
                    "rows_processed) VALUES (?,?,?,?)",
                    (i, f"point_history_2601{j + 1:02d}", "completed" if j == 0 else "pending", i),
                )
        con.commit()
    finally:
        con.close()


def _columns(path: str, table: str) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


@pytest.mark.parametrize("ddl", [V1_DDL, V127_DDL], ids=["v1.0", "v1.2.7"])
class TestUpgradeFromOldFile:
    def test_new_columns_are_added_and_rows_survive(self, tmp_path, monkeypatch, ddl):
        path = str(tmp_path / "db_migration.db")
        _build_old_db(path, ddl)

        db = LocalDatabase()
        db.db_path = path
        db.initialize()
        monkeypatch.setattr(local_db_module, "_db_instance", db, raising=False)
        try:
            assert NEW_HISTORY_COLUMNS <= _columns(path, "migration_history")
            assert {"last_path_id", "copy_method", "bytes_transferred"} <= _columns(
                path, "checkpoints"
            )

            from src.models.history import HistoryManager

            items = HistoryManager().get_all_history()
            assert len(items) == HISTORY_ROWS
            # 기존 값은 그대로, 새 값은 비어 있어야 한다(legacy).
            sample = next(h for h in items if h.id == 7)
            assert sample.total_rows == 7000 and sample.processed_rows == 3500
            assert all(h.plan_version is None for h in items)
            assert all(h.planned_hash is None for h in items)
        finally:
            db.close()

    def test_upgrade_is_idempotent(self, tmp_path, ddl):
        path = str(tmp_path / "db_migration.db")
        _build_old_db(path, ddl)

        for _ in range(3):
            db = LocalDatabase()
            db.db_path = path
            db.initialize()
            db.close()

        con = sqlite3.connect(path)
        try:
            assert con.execute("SELECT count(*) FROM migration_history").fetchone()[0] == 56
            assert con.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 112
        finally:
            con.close()


def test_fresh_database_has_the_new_columns(tmp_path):
    path = str(tmp_path / "fresh.db")
    db = LocalDatabase()
    db.db_path = path
    db.initialize()
    db.close()
    assert NEW_HISTORY_COLUMNS <= _columns(path, "migration_history")
