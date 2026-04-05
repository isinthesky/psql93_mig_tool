"""PostgreSQL ↔ File archive migration workers."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extensions
from psycopg2 import sql
from PySide6.QtCore import Signal

from src.core.archive_manifest import ArchiveManifestStore, ArchivePartitionEntry
from src.core.base_migration_worker import BaseMigrationWorker
from src.core.performance_metrics import PerformanceMetrics
from src.core.table_creator import TableCreator, _build_column_definition, _validate_identifier
from src.core.table_types import (
    TABLE_TYPE_CONFIG,
    TableType,
    get_partition_primary_key_columns,
    get_table_type,
    infer_partition_range,
)
from src.models.profile import ConnectionProfile


class MigrationInterruptedError(RuntimeError):
    """사용자 중단/연결 해제 등으로 현재 파티션을 정상 종료할 수 없을 때 사용."""


class ManifestTableCreator(TableCreator):
    """Manifest metadata를 사용해 대상 테이블을 준비합니다."""

    def __init__(self, manifest_store: ArchiveManifestStore, target_conn):
        super().__init__(source_conn=None, target_conn=target_conn)
        self.manifest_store = manifest_store

    @staticmethod
    def _resolve_table_type(parent_table: str, *codes: str | None) -> TableType:
        resolved = get_table_type(parent_table)
        for code in codes:
            if code and code != resolved.value:
                raise ValueError(
                    f"manifest 테이블 타입 불일치: parent={parent_table}, expected={resolved.value}, actual={code}"
                )
        return resolved

    def _get_partition_info(self, partition_name: str, parent_table: str) -> dict[str, Any]:
        _validate_identifier(partition_name)
        _validate_identifier(parent_table)

        entry = self.manifest_store.get_partition_entry(partition_name)
        if not entry:
            raise Exception(f"manifest에 파티션 정보가 없습니다: {partition_name}")

        metadata = self.manifest_store.get_parent_table_metadata(parent_table)
        table_type = self._resolve_table_type(
            parent_table,
            entry.table_type,
            metadata.get("table_type"),
        )
        from_ts = entry.from_timestamp
        to_ts = entry.to_timestamp
        if from_ts is None or to_ts is None:
            inferred_from, inferred_to = infer_partition_range(table_type, partition_name)
            from_ts = from_ts if from_ts is not None else inferred_from
            to_ts = to_ts if to_ts is not None else inferred_to

        return {
            "table_data": table_type.value,
            "table_type": table_type,
            "from_date": from_ts,
            "to_date": to_ts,
        }

    def _sync_partition_info(self, partition_name: str):
        """소스 DB 대신 manifest에서 파티션 정보를 가져와 대상 DB에 동기화."""
        parent_table = "_".join(partition_name.split("_")[:-1])
        partition_info = self._get_partition_info(partition_name, parent_table)
        self._add_partition_info(partition_name, partition_info)

    def _create_parent_table(self, parent_table: str, table_type: TableType = None):
        metadata = self.manifest_store.get_parent_table_metadata(parent_table)
        resolved_type = self._resolve_table_type(parent_table, metadata.get("table_type"))
        if table_type is None:
            table_type = resolved_type
        elif table_type != resolved_type:
            raise ValueError(
                f"manifest 부모 테이블 타입 불일치: parent={parent_table}, expected={resolved_type.value}, actual={table_type.value}"
            )

        config = TABLE_TYPE_CONFIG[table_type]
        columns = metadata.get("columns", [])
        if not columns:
            raise Exception(f"manifest에 부모 테이블 컬럼 정보가 없습니다: {parent_table}")

        _validate_identifier(parent_table)
        column_defs = [_build_column_definition(col) for col in columns]

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
        self.skip_on_error = False
        self.partition_failures: list[dict[str, str]] = []

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
            cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(partition_name)))
            return int(cur.fetchone()[0])

    @staticmethod
    def _cleanup_file(path: Path) -> bool:
        try:
            if path.exists():
                path.unlink()
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _iso_now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S")

    def _raise_if_stopped(self, partition_name: str, phase: str) -> None:
        if not self.is_running:
            raise MigrationInterruptedError(f"{partition_name} 작업이 중단되었습니다 (phase={phase})")

    def _build_checkpoint_payload(
        self,
        *,
        partition_name: str,
        phase: str,
        reason: str,
        detail: str,
        resumable: bool,
        next_action: str,
        file_path: str | None = None,
        temp_file_path: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "partition": partition_name,
            "history_id": self.history_id,
            "phase": phase,
            "reason": reason,
            "detail": detail,
            "resumable": resumable,
            "resume_strategy": "partition-retry",
            "next_action": next_action,
            "updated_at": self._iso_now(),
        }
        if file_path:
            payload["file_path"] = file_path
        if temp_file_path:
            payload["temp_file_path"] = temp_file_path
        if extra:
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)

    def _update_checkpoint_detail(
        self,
        checkpoint: Any,
        status: str,
        *,
        phase: str,
        reason: str,
        detail: str,
        resumable: bool,
        next_action: str,
        rows_processed: int | None = None,
        bytes_transferred: int | None = None,
        copy_method: str | None = None,
        file_path: str | None = None,
        temp_file_path: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.checkpoint_manager.update_checkpoint_status(
            checkpoint.id,
            status,
            rows_processed=rows_processed,
            error_message=self._build_checkpoint_payload(
                partition_name=checkpoint.partition_name,
                phase=phase,
                reason=reason,
                detail=detail,
                resumable=resumable,
                next_action=next_action,
                file_path=file_path,
                temp_file_path=temp_file_path,
                extra=extra,
            ),
            copy_method=copy_method,
            bytes_transferred=bytes_transferred,
        )

    def _mark_partition_running(
        self,
        checkpoint: Any,
        *,
        phase: str,
        detail: str,
        rows_processed: int | None = None,
        bytes_transferred: int | None = None,
        copy_method: str | None = None,
        file_path: str | None = None,
        temp_file_path: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._update_checkpoint_detail(
            checkpoint,
            "running",
            phase=phase,
            reason="in_progress",
            detail=detail,
            resumable=True,
            next_action="중단되면 재개(resume) 시 이 파티션 전체를 다시 처리합니다.",
            rows_processed=rows_processed,
            bytes_transferred=bytes_transferred,
            copy_method=copy_method,
            file_path=file_path,
            temp_file_path=temp_file_path,
            extra=extra,
        )

    def _mark_partition_finished(
        self,
        checkpoint: Any,
        *,
        rows_processed: int,
        bytes_transferred: int,
        copy_method: str,
    ) -> None:
        self.checkpoint_manager.update_checkpoint_status(
            checkpoint.id,
            "completed",
            rows_processed=rows_processed,
            copy_method=copy_method,
            bytes_transferred=bytes_transferred,
            error_message="",
        )

    def _record_partition_failure(
        self,
        partition_name: str,
        exc: Exception,
        *,
        phase: str,
    ) -> None:
        self.partition_failures.append(
            {
                "partition": partition_name,
                "phase": phase,
                "error": str(exc),
            }
        )

    def _detect_event_type(self, exc: Exception, *, phase: str) -> str:
        if isinstance(exc, MigrationInterruptedError) or not self.is_running:
            return self.stop_reason or "interrupted"

        message = str(exc).lower()
        if isinstance(exc, FileNotFoundError):
            return "archive_file_missing"
        if phase == "verify_archive" and (
            "checksum" in message
            or "체크섬" in str(exc)
            or "크기 불일치" in str(exc)
            or "integrity" in message
        ):
            return "file_integrity_error"
        if isinstance(exc, psycopg2.Error):
            if any(
                token in message
                for token in [
                    "server closed the connection",
                    "terminating connection",
                    "connection not open",
                    "could not receive data from server",
                    "connection refused",
                    "ssl syscall error",
                    "broken pipe",
                    "closed the connection unexpectedly",
                ]
            ):
                return "network_disconnect"
            return "db_error"
        return "application_error"

    def _log_final_summary(self, success_message: str) -> None:
        if self.partition_failures:
            failed_list = ", ".join(
                f"{item['partition']}({item['phase']})" for item in self.partition_failures
            )
            self._log(
                f"{success_message} - 실패/건너뜀 {len(self.partition_failures)}건: {failed_list}",
                "WARNING",
            )
        else:
            self._log(success_message, "SUCCESS")


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

                try:
                    self._export_partition(partition_name, checkpoint, manifest)
                except Exception as exc:
                    if not self.is_running:
                        self._log(f"{partition_name} - 작업 중단: {exc}", "WARNING")
                        break
                    if self.skip_on_error:
                        self._record_partition_failure(partition_name, exc, phase="export")
                        self._log(
                            f"{partition_name} - 오류 발생, 건너뛰고 계속 진행: {exc}",
                            "WARNING",
                        )
                        continue
                    raise

            if self.is_running:
                self._emit_performance_metrics(force=True)
                self._log_final_summary("File Archive 내보내기 완료")
        finally:
            if manifest is not None:
                self.archive_store.save(manifest)
            if self.source_conn is not None:
                try:
                    self.source_conn.close()
                except Exception:
                    pass

    def _export_partition(self, partition_name: str, checkpoint: Any, manifest):
        copy_method = "FILE_ARCHIVE_EXPORT"
        current_phase = "prepare"
        file_path = self.archive_store.build_partition_file_path(partition_name)
        temp_file_path = self.archive_store.build_partition_temp_file_path(partition_name)
        relative_file_path = str(file_path.relative_to(self.archive_store.archive_dir))

        actual_temp = None
        self._log(f"{partition_name} 아카이브 내보내기 시작", "INFO")
        try:
            self._raise_if_stopped(partition_name, current_phase)
            self._mark_partition_running(
                checkpoint,
                phase=current_phase,
                detail="소스 메타데이터 조회 및 export 준비 중",
                rows_processed=0,
                bytes_transferred=0,
                copy_method=copy_method,
                file_path=relative_file_path,
                temp_file_path=str(temp_file_path),
            )

            self.source_conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ
            )

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

            current_phase = "export"
            self._raise_if_stopped(partition_name, current_phase)
            self._mark_partition_running(
                checkpoint,
                phase=current_phase,
                detail="PostgreSQL COPY TO STDOUT로 아카이브 파일 생성 중",
                rows_processed=0,
                bytes_transferred=0,
                copy_method=copy_method,
                file_path=relative_file_path,
            )

            # O_EXCL로 원자적 임시 파일 생성 (symlink 공격 방어)
            temp_file_path.parent.mkdir(parents=True, exist_ok=True)
            fd, actual_temp_path = tempfile.mkstemp(
                dir=str(temp_file_path.parent),
                prefix=f".{partition_name}_",
                suffix=".csv.tmp",
            )
            actual_temp = Path(actual_temp_path)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as fp:
                    with self.source_conn.cursor() as cur:
                        cur.copy_expert(copy_query, fp)
                    fp.flush()
                    os.fsync(fp.fileno())
            except Exception:
                self._cleanup_file(actual_temp)
                raise

            bytes_written = actual_temp.stat().st_size if actual_temp.exists() else 0
            current_phase = "verify"
            self._raise_if_stopped(partition_name, current_phase)
            self._mark_partition_running(
                checkpoint,
                phase=current_phase,
                detail="내보낸 파일의 크기/sha256 검증 중",
                rows_processed=total_rows,
                bytes_transferred=bytes_written,
                copy_method=copy_method,
                file_path=relative_file_path,
            )

            self.archive_store.compute_file_metadata(actual_temp)
            self.archive_store.replace_file_atomically(actual_temp, file_path)
            integrity = self.archive_store.compute_file_metadata(file_path)

            entry = ArchivePartitionEntry(
                partition_name=partition_name,
                table_type=table_type.value,
                parent_table=parent_table,
                row_count=total_rows,
                file_path=relative_file_path,
                columns=list(table_config.columns),
                from_timestamp=partition_meta.get("from_date"),
                to_timestamp=partition_meta.get("to_date"),
                bytes_written=int(integrity["bytes_written"]),
                exported_at=self._iso_now(),
                copy_method=copy_method,
                checksum_sha256=integrity["checksum_sha256"],
                verified_at=integrity["verified_at"],
            )

            # REPEATABLE READ 트랜잭션 해제 (읽기 전용이므로 rollback)
            try:
                self.source_conn.rollback()
            except Exception:
                pass

            current_phase = "manifest"
            self._raise_if_stopped(partition_name, current_phase)
            self._mark_partition_running(
                checkpoint,
                phase=current_phase,
                detail="manifest 원자적 저장 및 체크포인트 마무리 중",
                rows_processed=total_rows,
                bytes_transferred=int(integrity["bytes_written"]),
                copy_method=copy_method,
                file_path=relative_file_path,
                extra={"checksum_sha256": integrity["checksum_sha256"]},
            )
            self.archive_store.upsert_partition(manifest, entry)
            self.archive_store.save(manifest)

            self.performance_metrics.update(total_rows, int(integrity["bytes_written"]))
            self.performance_metrics.complete_partition()
            self._mark_partition_finished(
                checkpoint,
                rows_processed=total_rows,
                bytes_transferred=int(integrity["bytes_written"]),
                copy_method=copy_method,
            )
            self._emit_performance_metrics(force=True)
            self._log(f"{partition_name} 아카이브 내보내기 완료 ({total_rows:,} rows)", "SUCCESS")
        except Exception as exc:
            bytes_written = 0
            cleanup_target = actual_temp if actual_temp is not None else temp_file_path
            if cleanup_target.exists():
                try:
                    bytes_written = cleanup_target.stat().st_size
                except Exception:
                    bytes_written = 0
            partial_removed = self._cleanup_file(cleanup_target)
            extra = {
                "event_type": self._detect_event_type(exc, phase=current_phase),
                "exception_type": type(exc).__name__,
                "partial_file_removed": partial_removed,
                "final_file_present": file_path.exists(),
            }
            if isinstance(exc, MigrationInterruptedError) or not self.is_running:
                self._update_checkpoint_detail(
                    checkpoint,
                    "pending",
                    phase=current_phase,
                    reason="interrupted",
                    detail=f"{partition_name} export가 중단되었습니다: {exc}",
                    resumable=True,
                    next_action="재개(resume) 시 이 파티션 전체를 처음부터 다시 export 하면 됩니다.",
                    rows_processed=0,
                    bytes_transferred=bytes_written,
                    copy_method=copy_method,
                    file_path=relative_file_path,
                    extra=extra,
                )
            else:
                self._update_checkpoint_detail(
                    checkpoint,
                    "failed",
                    phase=current_phase,
                    reason=f"{current_phase}_failed",
                    detail=str(exc),
                    resumable=True,
                    next_action="원인을 확인한 뒤 재개(resume)하거나 재실행하면 이 파티션 전체를 다시 export 합니다.",
                    rows_processed=0,
                    bytes_transferred=bytes_written,
                    copy_method=copy_method,
                    file_path=relative_file_path,
                    extra=extra,
                )
            raise

    @staticmethod
    def _detect_table_type(partition_name: str) -> TableType:
        parent_table = "_".join(partition_name.split("_")[:-1])
        for table_type, config in TABLE_TYPE_CONFIG.items():
            if config.table_name == parent_table:
                return table_type
        raise ValueError(f"알 수 없는 파티션 타입: {partition_name}")

    def stop(self, reason: str = "user_stop"):
        super().stop(reason=reason)
        try:
            if self.source_conn is not None:
                self.source_conn.cancel()
        except Exception:
            pass
        try:
            if self.source_conn is not None:
                self.source_conn.rollback()
        except Exception:
            pass


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

                try:
                    self._import_partition(partition_name, checkpoint, creator, manifest)
                except Exception as exc:
                    if not self.is_running:
                        self._log(f"{partition_name} - 작업 중단: {exc}", "WARNING")
                        break
                    if self.skip_on_error:
                        self._record_partition_failure(partition_name, exc, phase="import")
                        self._log(
                            f"{partition_name} - 오류 발생, 건너뛰고 계속 진행: {exc}",
                            "WARNING",
                        )
                        continue
                    raise

            if self.is_running:
                self._emit_performance_metrics(force=True)
                self._log_final_summary("File Archive 가져오기 완료")
        finally:
            if self.target_conn is not None:
                try:
                    self.target_conn.close()
                except Exception:
                    pass

    def _import_partition(self, partition_name: str, checkpoint: Any, creator: ManifestTableCreator, manifest):
        copy_method = "FILE_ARCHIVE_IMPORT"
        current_phase = "verify_archive"
        entry = next(
            (
                ArchivePartitionEntry.from_dict(item)
                for item in manifest.partitions
                if item.get("partition_name") == partition_name
            ),
            None,
        )
        if entry is None:
            raise Exception(f"manifest에 파티션이 없습니다: {partition_name}")

        file_path = self.archive_store.resolve_safe_path(entry.file_path)
        self._log(f"{partition_name} 아카이브 가져오기 시작", "INFO")

        try:
            self._raise_if_stopped(partition_name, current_phase)
            self._mark_partition_running(
                checkpoint,
                phase=current_phase,
                detail="아카이브 파일 열기 및 sha256/크기 검증 중",
                rows_processed=0,
                bytes_transferred=0,
                copy_method=copy_method,
                file_path=entry.file_path,
            )

            # 파일을 한 번 열어 검증 후 같은 핸들로 COPY (TOCTOU 방어)
            archive_fp = file_path.open("rb")
            try:
                integrity = self.archive_store.verify_open_file(entry, archive_fp)
                archive_fp.seek(0)
            except Exception:
                archive_fp.close()
                raise

            expected_rows = int(entry.row_count or 0)
            self.performance_metrics.start_partition(partition_name, expected_rows)

            table_type = TableType(entry.table_type)
            table_config = TABLE_TYPE_CONFIG[table_type]
            expected_columns = list(table_config.columns)
            if entry.columns and list(entry.columns) != expected_columns:
                archive_fp.close()
                raise ValueError(
                    f"manifest 컬럼 순서 불일치: expected={expected_columns}, actual={entry.columns}"
                )

            current_phase = "prepare"
            self._raise_if_stopped(partition_name, current_phase)
            self._mark_partition_running(
                checkpoint,
                phase=current_phase,
                detail="대상 파티션 생성/초기화 준비 중",
                rows_processed=0,
                bytes_transferred=0,
                copy_method=copy_method,
                file_path=entry.file_path,
                extra={"archive_checksum_sha256": integrity["checksum_sha256"]},
            )
            self._prepare_target_table(partition_name, checkpoint, creator)

            cols_sql = sql.SQL(", ").join(sql.Identifier(col) for col in expected_columns)
            copy_query = sql.SQL("COPY {tbl} ({cols}) FROM STDIN WITH (FORMAT CSV, HEADER FALSE)").format(
                tbl=sql.Identifier(partition_name),
                cols=cols_sql,
            ).as_string(self.target_conn)

            current_phase = "copy"
            self._raise_if_stopped(partition_name, current_phase)
            self._mark_partition_running(
                checkpoint,
                phase=current_phase,
                detail="검증된 파일을 대상 PostgreSQL 파티션으로 COPY 중",
                rows_processed=0,
                bytes_transferred=int(integrity["bytes_written"]),
                copy_method=copy_method,
                file_path=entry.file_path,
            )

            try:
                with self.target_conn.cursor() as cur:
                    cur.copy_expert(copy_query, archive_fp)
            finally:
                archive_fp.close()

            current_phase = "verify"
            self._raise_if_stopped(partition_name, current_phase)
            self._mark_partition_running(
                checkpoint,
                phase=current_phase,
                detail="커밋 전 대상 row_count 검증 중",
                rows_processed=expected_rows,
                bytes_transferred=int(integrity["bytes_written"]),
                copy_method=copy_method,
                file_path=entry.file_path,
            )
            imported_rows = self._query_row_count(self.target_conn, partition_name)
            if imported_rows != expected_rows:
                self.target_conn.rollback()
                raise ValueError(
                    f"대상 row_count 검증 실패: expected={expected_rows}, actual={imported_rows}"
                )

            current_phase = "commit"
            self._mark_partition_running(
                checkpoint,
                phase=current_phase,
                detail="검증 통과, 대상 트랜잭션 커밋 중",
                rows_processed=expected_rows,
                bytes_transferred=int(integrity["bytes_written"]),
                copy_method=copy_method,
                file_path=entry.file_path,
            )
            self.target_conn.commit()

            self.performance_metrics.update(expected_rows, int(integrity["bytes_written"]))
            self.performance_metrics.complete_partition()
            self._mark_partition_finished(
                checkpoint,
                rows_processed=expected_rows,
                bytes_transferred=int(integrity["bytes_written"]),
                copy_method=copy_method,
            )
            self._emit_performance_metrics(force=True)
            self._log(f"{partition_name} 아카이브 가져오기 완료 ({expected_rows:,} rows)", "SUCCESS")
        except Exception as exc:
            try:
                if self.target_conn is not None:
                    self.target_conn.rollback()
            except Exception:
                pass

            bytes_transferred = 0
            try:
                if file_path.exists():
                    bytes_transferred = file_path.stat().st_size
            except Exception:
                bytes_transferred = 0

            target_may_contain_data = current_phase in {"commit", "verify"}
            next_action = (
                "재개(resume) 시 대상 파티션을 자동 TRUNCATE 후 전체 파일을 다시 import 합니다. "
                "commit/verify 단계에서 멈췄다면 대상에 일부 또는 전체 데이터가 남아있을 수 있습니다."
                if target_may_contain_data
                else "재개(resume) 시 이 파티션 전체를 다시 import 하면 됩니다."
            )

            if isinstance(exc, MigrationInterruptedError) or not self.is_running:
                self._update_checkpoint_detail(
                    checkpoint,
                    "pending",
                    phase=current_phase,
                    reason="interrupted",
                    detail=f"{partition_name} import가 중단되었습니다: {exc}",
                    resumable=True,
                    next_action=next_action,
                    rows_processed=0,
                    bytes_transferred=bytes_transferred,
                    copy_method=copy_method,
                    file_path=entry.file_path,
                    extra={
                        "event_type": self._detect_event_type(exc, phase=current_phase),
                        "exception_type": type(exc).__name__,
                        "target_may_contain_data": target_may_contain_data,
                    },
                )
            else:
                self._update_checkpoint_detail(
                    checkpoint,
                    "failed",
                    phase=current_phase,
                    reason=f"{current_phase}_failed",
                    detail=str(exc),
                    resumable=True,
                    next_action=next_action,
                    rows_processed=0,
                    bytes_transferred=bytes_transferred,
                    copy_method=copy_method,
                    file_path=entry.file_path,
                    extra={
                        "event_type": self._detect_event_type(exc, phase=current_phase),
                        "exception_type": type(exc).__name__,
                        "target_may_contain_data": target_may_contain_data,
                    },
                )
            raise

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

    def stop(self, reason: str = "user_stop"):
        super().stop(reason=reason)
        try:
            if self.target_conn is not None:
                self.target_conn.cancel()
        except Exception:
            pass
        try:
            if self.target_conn is not None:
                self.target_conn.rollback()
        except Exception:
            pass
