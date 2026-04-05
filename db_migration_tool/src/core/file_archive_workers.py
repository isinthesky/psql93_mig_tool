"""PostgreSQL ↔ File archive migration workers."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2 import sql
from PySide6.QtCore import Signal

from src.core.archive_manifest import ArchiveManifestStore, ArchivePartitionEntry
from src.core.base_migration_worker import BaseMigrationWorker
from src.core.performance_metrics import PerformanceMetrics
from src.core.table_creator import TableCreator, _validate_identifier
from src.core.table_types import TABLE_TYPE_CONFIG, TableType, get_partition_primary_key_columns
from src.database.postgres_utils import PostgresOptimizer
from src.models.profile import ConnectionProfile


class ManifestTableCreator(TableCreator):
    """Manifest metadata를 사용해 대상 테이블을 준비합니다."""

    def __init__(self, manifest_store: ArchiveManifestStore, target_conn):
        super().__init__(source_conn=None, target_conn=target_conn)
        self.manifest_store = manifest_store

    def _get_partition_info(self, partition_name: str, parent_table: str) -> dict[str, Any]:
        entry = self.manifest_store.get_partition_entry(partition_name)
        if not entry:
            raise Exception(f"manifest에 파티션 정보가 없습니다: {partition_name}")
        return {
            "table_data": entry.table_type,
            "table_type": TableType(entry.table_type),
            "from_date": entry.from_timestamp,
            "to_date": entry.to_timestamp,
        }

    def _create_parent_table(self, parent_table: str, table_type: TableType = None):
        metadata = self.manifest_store.get_parent_table_metadata(parent_table)
        if table_type is None:
            table_type = TableType(metadata["table_type"])

        config = TABLE_TYPE_CONFIG[table_type]
        columns = metadata.get("columns", [])
        if not columns:
            raise Exception(f"manifest에 부모 테이블 컬럼 정보가 없습니다: {parent_table}")

        _validate_identifier(parent_table)
        column_defs = []
        for col in columns:
            col_name = col["name"]
            data_type = col["data_type"]
            max_length = col.get("character_maximum_length")
            is_nullable = col.get("is_nullable")
            default = col.get("column_default")

            _validate_identifier(col_name)
            col_def = f"    {col_name} {data_type}"
            if max_length:
                col_def += f"({int(max_length)})"
            if is_nullable == "NO":
                col_def += " NOT NULL"
            if default:
                col_def += f" DEFAULT {default}"
            column_defs.append(col_def)

        create_sql = f"CREATE TABLE IF NOT EXISTS {parent_table} (\n"
        create_sql += ",\n".join(column_defs) + "\n)"

        with self.target_conn.cursor() as target_cur:
            target_cur.execute(create_sql)
            if config.uses_trigger:
                self._create_trigger_based_partitioning(parent_table, table_type, target_cur)
            elif config.uses_rules:
                self._create_parent_indexes(parent_table, table_type, target_cur)
            self.target_conn.commit()


class ArchiveMigrationWorkerBase(BaseMigrationWorker):
    performance = Signal(dict)
    truncate_requested = Signal(str, int)

    def __init__(
        self,
        profile: ConnectionProfile,
        partitions: list[str],
        history_id: int,
        resume: bool = False,
    ):
        super().__init__(profile, partitions, history_id, resume)
        self.performance_metrics = PerformanceMetrics()
        self.last_metric_update = 0.0
        self.metric_update_interval = 0.5
        self.truncate_permission = None

    def get_stats(self) -> dict[str, Any]:
        return self.performance_metrics.get_stats()

    def _emit_performance_metrics(self, *, force: bool = False):
        current_time = time.time()
        if not force and current_time - self.last_metric_update < self.metric_update_interval:
            return

        stats = self.performance_metrics.get_stats()
        self.performance.emit(stats)
        self.progress.emit(
            {
                "total_progress": int(stats["total_progress"]),
                "current_progress": int(stats["partition_progress"]),
                "total_partitions": stats["total_partitions"],
                "completed_partitions": stats["completed_partitions"],
                "current_partition": stats["current_partition"],
                "current_rows": stats["current_partition_rows"],
                "speed": stats["instant_rows_per_sec"],
            }
        )
        self.last_metric_update = current_time

    @staticmethod
    def _create_psycopg2_connection(config: dict[str, Any]):
        params = {
            "host": config["host"],
            "port": config["port"],
            "database": config["database"],
            "user": config["username"],
            "password": config["password"],
        }
        if config.get("ssl"):
            params["sslmode"] = "require"
        conn = psycopg2.connect(**params)
        conn.autocommit = False
        return conn

    @staticmethod
    def _query_parent_columns(conn, parent_table: str) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type, character_maximum_length, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_name = %s
                ORDER BY ordinal_position
                """,
                (parent_table,),
            )
            rows = cur.fetchall()

        return [
            {
                "name": row[0],
                "data_type": row[1],
                "character_maximum_length": row[2],
                "is_nullable": row[3],
                "column_default": row[4],
            }
            for row in rows
        ]

    @staticmethod
    def _query_partition_meta(conn, partition_name: str, table_type: TableType) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_data, from_date, to_date
                FROM partition_table_info
                WHERE table_name = %s
                """,
                (partition_name,),
            )
            row = cur.fetchone()
            if row:
                return {
                    "table_data": row[0],
                    "from_date": row[1],
                    "to_date": row[2],
                }
        return {
            "table_data": table_type.value,
            "from_date": None,
            "to_date": None,
        }

    @staticmethod
    def _query_row_count(conn, partition_name: str) -> int:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT COUNT(*) FROM {}") .format(sql.Identifier(partition_name))
            )
            return int(cur.fetchone()[0])


class PostgresToFileArchiveWorker(ArchiveMigrationWorkerBase):
    """PostgreSQL partition export → file archive."""

    def __init__(
        self,
        profile: ConnectionProfile,
        partitions: list[str],
        history_id: int,
        resume: bool = False,
    ):
        super().__init__(profile, partitions, history_id, resume)
        self.source_conn = None
        self.archive_store = ArchiveManifestStore(self.profile.target_config["archive_path"])

    def _execute_migration(self):
        checkpoints = {
            cp.partition_name: cp for cp in self.checkpoint_manager.get_checkpoints(self.history_id)
        }
        self.performance_metrics.total_partitions = len(self.partitions)
        manifest = None

        try:
            self.source_conn = self._create_psycopg2_connection(self.profile.source_config)
            manifest = self.archive_store.load_or_create(
                source=self.profile.source_config,
                target=self.profile.target_config,
            )

            for idx, partition_name in enumerate(self.partitions):
                if not self.is_running:
                    break
                self.current_partition_index = idx

                checkpoint = checkpoints.get(partition_name)
                if checkpoint and checkpoint.status == "completed":
                    self.performance_metrics.completed_partitions += 1
                    continue
                if checkpoint is None:
                    checkpoint = self.checkpoint_manager.create_checkpoint(self.history_id, partition_name)
                    checkpoints[partition_name] = checkpoint

                self._export_partition(partition_name, checkpoint, manifest)

            if self.is_running:
                self._emit_performance_metrics(force=True)
                self._log("File Archive 내보내기 완료", "SUCCESS")
        finally:
            if manifest is not None:
                self.archive_store.save(manifest)
            if self.source_conn is not None:
                try:
                    self.source_conn.close()
                except Exception:
                    pass

    def _export_partition(self, partition_name: str, checkpoint: Any, manifest):
        self._log(f"{partition_name} 아카이브 내보내기 시작", "INFO")
        table_type = self._detect_table_type(partition_name)
        parent_table = TABLE_TYPE_CONFIG[table_type].table_name
        total_rows = self._query_row_count(self.source_conn, partition_name)
        self.performance_metrics.start_partition(partition_name, total_rows)

        parent_columns = self._query_parent_columns(self.source_conn, parent_table)
        partition_meta = self._query_partition_meta(self.source_conn, partition_name, table_type)
        self.archive_store.upsert_parent_table(
            manifest,
            parent_table=parent_table,
            table_type=table_type,
            columns=parent_columns,
        )

        file_path = self.archive_store.build_partition_file_path(partition_name)
        relative_file_path = str(file_path.relative_to(self.archive_store.archive_dir))

        table_config = TABLE_TYPE_CONFIG[table_type]
        cols_sql = sql.SQL(", ").join(sql.Identifier(col) for col in table_config.columns)
        order_columns = get_partition_primary_key_columns(table_type) or table_config.columns
        order_sql = sql.SQL(", ").join(sql.Identifier(col) for col in order_columns)
        copy_query = sql.SQL(
            "COPY (SELECT {cols} FROM {tbl} ORDER BY {order}) TO STDOUT WITH (FORMAT CSV, HEADER FALSE)"
        ).format(
            cols=cols_sql,
            tbl=sql.Identifier(partition_name),
            order=order_sql,
        ).as_string(self.source_conn)

        self.checkpoint_manager.update_checkpoint_status(
            checkpoint.id,
            "running",
            rows_processed=0,
            copy_method="FILE_ARCHIVE_EXPORT",
            bytes_transferred=0,
        )

        with file_path.open("w", encoding="utf-8", newline="") as fp:
            with self.source_conn.cursor() as cur:
                cur.copy_expert(copy_query, fp)

        bytes_written = file_path.stat().st_size if file_path.exists() else 0
        self.performance_metrics.update(total_rows, bytes_written)
        self.performance_metrics.complete_partition()

        entry = ArchivePartitionEntry(
            partition_name=partition_name,
            table_type=table_type.value,
            parent_table=parent_table,
            row_count=total_rows,
            file_path=relative_file_path,
            columns=list(table_config.columns),
            from_timestamp=partition_meta.get("from_date"),
            to_timestamp=partition_meta.get("to_date"),
            bytes_written=bytes_written,
            exported_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            copy_method="FILE_ARCHIVE_EXPORT",
        )
        self.archive_store.upsert_partition(manifest, entry)
        self.archive_store.save(manifest)
        self.checkpoint_manager.update_checkpoint_status(
            checkpoint.id,
            "completed",
            rows_processed=total_rows,
            copy_method="FILE_ARCHIVE_EXPORT",
            bytes_transferred=bytes_written,
            error_message="",
        )
        self._emit_performance_metrics(force=True)
        self._log(f"{partition_name} 아카이브 내보내기 완료 ({total_rows:,} rows)", "SUCCESS")

    @staticmethod
    def _detect_table_type(partition_name: str) -> TableType:
        parent_table = "_".join(partition_name.split("_")[:-1])
        for table_type, config in TABLE_TYPE_CONFIG.items():
            if config.table_name == parent_table:
                return table_type
        raise ValueError(f"알 수 없는 파티션 타입: {partition_name}")


class FileToPostgresArchiveWorker(ArchiveMigrationWorkerBase):
    """file archive → PostgreSQL import worker."""

    def __init__(
        self,
        profile: ConnectionProfile,
        partitions: list[str],
        history_id: int,
        resume: bool = False,
    ):
        super().__init__(profile, partitions, history_id, resume)
        self.archive_store = ArchiveManifestStore(self.profile.source_config["archive_path"])
        self.target_conn = None

    def _execute_migration(self):
        checkpoints = {
            cp.partition_name: cp for cp in self.checkpoint_manager.get_checkpoints(self.history_id)
        }
        manifest = self.archive_store.load()
        self.performance_metrics.total_partitions = len(self.partitions)

        try:
            self.target_conn = self._create_psycopg2_connection(self.profile.target_config)
            creator = ManifestTableCreator(self.archive_store, self.target_conn)

            for idx, partition_name in enumerate(self.partitions):
                if not self.is_running:
                    break
                self.current_partition_index = idx

                checkpoint = checkpoints.get(partition_name)
                if checkpoint and checkpoint.status == "completed":
                    self.performance_metrics.completed_partitions += 1
                    continue
                if checkpoint is None:
                    checkpoint = self.checkpoint_manager.create_checkpoint(self.history_id, partition_name)
                    checkpoints[partition_name] = checkpoint

                self._import_partition(partition_name, checkpoint, creator, manifest)

            if self.is_running:
                self._emit_performance_metrics(force=True)
                self._log("File Archive 가져오기 완료", "SUCCESS")
        finally:
            if self.target_conn is not None:
                try:
                    self.target_conn.close()
                except Exception:
                    pass

    def _import_partition(self, partition_name: str, checkpoint: Any, creator: ManifestTableCreator, manifest):
        entry = next(
            (ArchivePartitionEntry.from_dict(item) for item in manifest.partitions if item.get("partition_name") == partition_name),
            None,
        )
        if entry is None:
            raise Exception(f"manifest에 파티션이 없습니다: {partition_name}")

        file_path = self.archive_store.archive_dir / entry.file_path
        if not file_path.exists():
            raise FileNotFoundError(f"아카이브 데이터 파일이 없습니다: {file_path}")

        self.performance_metrics.start_partition(partition_name, int(entry.row_count or 0))
        self._prepare_target_table(partition_name, checkpoint, creator)

        table_type = TableType(entry.table_type)
        table_config = TABLE_TYPE_CONFIG[table_type]
        cols_sql = sql.SQL(", ").join(sql.Identifier(col) for col in table_config.columns)
        copy_query = sql.SQL(
            "COPY {tbl} ({cols}) FROM STDIN WITH (FORMAT CSV, HEADER FALSE)"
        ).format(
            tbl=sql.Identifier(partition_name),
            cols=cols_sql,
        ).as_string(self.target_conn)

        self.checkpoint_manager.update_checkpoint_status(
            checkpoint.id,
            "running",
            rows_processed=0,
            copy_method="FILE_ARCHIVE_IMPORT",
            bytes_transferred=0,
        )

        with file_path.open("r", encoding="utf-8", newline="") as fp:
            with self.target_conn.cursor() as cur:
                cur.copy_expert(copy_query, fp)
        self.target_conn.commit()

        bytes_transferred = file_path.stat().st_size
        self.performance_metrics.update(int(entry.row_count or 0), bytes_transferred)
        self.performance_metrics.complete_partition()
        self.checkpoint_manager.update_checkpoint_status(
            checkpoint.id,
            "completed",
            rows_processed=int(entry.row_count or 0),
            copy_method="FILE_ARCHIVE_IMPORT",
            bytes_transferred=bytes_transferred,
            error_message="",
        )
        self._emit_performance_metrics(force=True)
        self._log(f"{partition_name} 아카이브 가져오기 완료 ({entry.row_count:,} rows)", "SUCCESS")

    def _prepare_target_table(self, partition_name: str, checkpoint: Any, creator: ManifestTableCreator):
        def confirm_truncate(table: str, row_count: int) -> bool:
            self.truncate_permission = None
            self.truncate_requested.emit(table, row_count)
            while self.is_running and self.truncate_permission is None:
                time.sleep(0.1)
            return bool(self.truncate_permission)

        truncate_mode = "auto" if self.should_resume else "ask"
        creator.ensure_partition_ready(
            partition_name,
            truncate_mode=truncate_mode,
            confirm_callback=None if truncate_mode == "auto" else confirm_truncate,
        )

    def stop(self):
        super().stop()
        try:
            if self.target_conn is not None:
                self.target_conn.rollback()
        except Exception:
            pass
