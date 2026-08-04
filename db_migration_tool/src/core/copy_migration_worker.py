"""
PostgreSQL COPY 명령 기반 고성능 마이그레이션 워커
"""

import os
import threading
import time
from queue import Empty, Queue
from typing import Any

import psycopg2
from psycopg2 import sql
from PySide6.QtCore import Signal

from src.core.base_migration_worker import BaseMigrationWorker
from src.core.performance_metrics import PerformanceMetrics
from src.core.table_creator import TableCreator
from src.core.table_types import (
    TABLE_TYPE_CONFIG,
    get_partition_primary_key_columns,
    get_table_type,
)
from src.database.postgres_utils import PostgresOptimizer
from src.database.version_info import PgVersionInfo
from src.models.profile import ConnectionProfile
from src.utils.enhanced_logger import log_emitter
from src.utils.validators import VersionValidator


class CopyStreamBuffer:
    """COPY OUT 데이터를 스트리밍으로 흘려보내며 마지막 키를 추적하는 버퍼

    - write(): 소스 COPY OUT이 호출, 큐에 청크를 적재하며 마지막 행 path_id/issued_date 추적
    - read(): 대상 COPY IN이 호출, 큐에서 청크를 꺼내 전송 (EOF 시 빈 문자열 반환)
    큐 크기를 제한해 한번에 전체 파티션을 메모리에 적재하지 않는다.
    """

    # write()가 큐에 넣을 때 최대 대기 시간 (초). 이 시간이 지나면 교착 대신 예외 발생
    _WRITE_TIMEOUT = 30

    def __init__(self, max_queue_size: int = 8, extra_track_indices: list[int] | None = None):
        """
        Args:
            max_queue_size: 큐 최대 크기
            extra_track_indices: CSV에서 추가로 추적할 컬럼 인덱스 목록
                (예: save_type이 columns[2]이면 [2] 전달)
        """
        self.queue: Queue[str | bytes | None] = Queue(maxsize=max_queue_size)
        self.last_key: str | None = None
        self.last_date: str | None = None
        self.last_extra: dict[int, str] = {}  # {csv_index: last_value}
        self._extra_track_indices = extra_track_indices or []
        self.row_count: int = 0
        self.total_bytes: int = 0
        self._partial_line: str = ""
        self._closed = False
        self._cancel = threading.Event()
        self.error: Exception | None = None

    def write(self, data: str | bytes):
        """COPY OUT이 호출하는 write; 청크를 큐에 적재

        교착 방지: 큐가 가득 찬 상태에서 _WRITE_TIMEOUT 초 대기 후 예외 발생
        """
        if self._closed or self._cancel.is_set():
            return

        # psycopg2는 bytes를 줄 수 있으므로 문자열로 변환
        if isinstance(data, bytes):
            data_str = data.decode("utf-8")
        else:
            data_str = data

        self._track_last_row(data_str)
        self.total_bytes += len(data_str.encode("utf-8"))
        try:
            self.queue.put(data, timeout=self._WRITE_TIMEOUT)
        except Exception:
            if not self._cancel.is_set():
                self.set_error(TimeoutError("CopyStreamBuffer.write() 큐 대기 시간 초과"))

    def read(self, size: int = -1) -> str:
        """COPY IN이 호출하는 read; 큐에서 꺼내 전달"""
        if self.error:
            raise self.error

        if self._closed and self.queue.empty():
            return ""

        chunks: list[str] = []
        bytes_read = 0

        while size < 0 or bytes_read < size:
            if self._cancel.is_set():
                break
            try:
                chunk = self.queue.get(timeout=1)
            except Empty:
                if self._closed or self._cancel.is_set():
                    break
                if self.error:
                    raise self.error
                continue

            if chunk is None:
                self._closed = True
                break

            # psycopg2 COPY IN은 str을 기대하므로 bytes면 디코드
            if isinstance(chunk, bytes):
                chunk_str = chunk.decode("utf-8")
            else:
                chunk_str = chunk

            chunks.append(chunk_str)
            bytes_read += len(chunk_str)

            if size > 0 and bytes_read >= size:
                break

        return "".join(chunks)

    def close(self):
        """생산 종료 시 호출 (EOF 신호)"""
        if self._partial_line:
            self._finalize_partial_line()

        self._closed = True
        # 큐가 가득 차 있어도 안전하게 종료 신호 전달
        try:
            self.queue.put_nowait(None)
        except Exception:
            # 큐가 가득 차면 cancel 이벤트로 소비자 깨움
            self._cancel.set()

    def cancel(self):
        """양쪽 스레드를 강제 해제하기 위한 취소"""
        self._cancel.set()
        self._closed = True

    def set_error(self, exc: Exception):
        """프로듀서에서 발생한 오류를 기록"""
        self.error = exc
        self._cancel.set()
        self.close()

    def _track_last_row(self, data: str):
        """마지막 행 키와 행 수 추적 (청크 경계 고려)"""
        combined = self._partial_line + data
        lines = combined.split("\n")
        self._partial_line = lines.pop()  # 마지막 조각은 다음 청크와 합치기

        for line in lines:
            if not line:
                continue

            self.row_count += 1
            parts = line.split(",")
            try:
                self.last_key = parts[0]
                self.last_date = parts[1]
            except IndexError:
                continue
            # 추가 PK 컬럼 추적
            for idx in self._extra_track_indices:
                if idx < len(parts):
                    self.last_extra[idx] = parts[idx]

    def _finalize_partial_line(self):
        """마지막 미완성 행 정리 (COPY OUT이 개행 없이 끝난 경우)"""
        line = self._partial_line
        self._partial_line = ""

        if not line:
            return

        self.row_count += 1
        parts = line.split(",")
        try:
            self.last_key = parts[0]
            self.last_date = parts[1]
        except IndexError:
            pass
        for idx in self._extra_track_indices:
            if idx < len(parts):
                self.last_extra[idx] = parts[idx]


class CopyMigrationWorker(BaseMigrationWorker):
    """COPY 명령 기반 고성능 마이그레이션 워커"""

    # CopyMigrationWorker 전용 시그널
    performance = Signal(dict)  # 성능 지표
    connection_checking = Signal()  # 연결 확인 시작
    source_connection_status = Signal(bool, str)  # 연결 성공 여부, 메시지
    target_connection_status = Signal(bool, str)  # 연결 성공 여부, 메시지
    truncate_requested = Signal(str, int)  # 테이블명, 기존 행 수 (기존 데이터 경고/확인)

    def __init__(
        self,
        profile: ConnectionProfile,
        partitions: list[str],
        history_id: int,
        resume: bool = False,
        batch_size: int = 100000,
        copy_mode: str = "python",
    ):
        super().__init__(profile, partitions, history_id, resume)
        self.batch_size = batch_size
        self.copy_mode = copy_mode  # "python" | "server" | "auto"

        # auto 모드: server-side COPY 가능 여부를 1회 확인 후 캐시
        self._auto_server_copy_ok: bool | None = None

        # COPY 워커 전용 필드
        self.performance_metrics = PerformanceMetrics()

        # psycopg2 연결 (COPY 명령용). psycopg2는 타입 스텁이 없어 Any로 둔다.
        self.source_conn: Any = None
        self.target_conn: Any = None

        # 버전 정보 (연결 후 감지)
        self.source_version: PgVersionInfo | None = None
        self.target_version: PgVersionInfo | None = None

        # 성능 지표 업데이트 타이머
        self.last_metric_update = 0.0
        self.metric_update_interval = 1.0  # 1초마다 업데이트

        # 에러 처리 전략
        # True: 파티션 단위 에러 발생 시 로그만 남기고 다음 파티션으로 진행
        # False: 에러 즉시 중단
        self.skip_on_error: bool = False

        # 기존 데이터(TRUNCATE) 처리: UI가 응답을 넣어주는 필드
        # None=미응답 / True=TRUNCATE 승인 / False=거부
        self.truncate_permission: bool | None = None

        # True면 실제 마이그레이션 대신 소스/대상 연결 확인만 하고 끝낸다.
        # (연결 확인 마법사 단계가 워커를 재사용하려고 켜는 플래그)
        self.check_connections_only: bool = False

    def _execute_migration(self):
        """COPY 기반 마이그레이션 실행"""
        # 연결 확인만 수행하는 경우
        if self.check_connections_only:
            self._check_connections()
            return

        # 방어: server-side COPY는 재개 모드 미지원 → Python COPY로 자동 전환
        if self.copy_mode in ("server", "auto") and self.should_resume:
            self.log.emit(
                "Server-side COPY는 재개 모드 미지원. Python COPY로 전환합니다.",
                "WARNING",
            )
            log_emitter.emit_log("WARNING", "Server-side COPY → Python COPY 자동 전환 (resume)")
            self.copy_mode = "python"

        try:
            # 체크포인트를 딕셔너리로 캐싱 (성능 개선)
            # NOTE: 연결 생성 전에 캐싱을 수행해, 연결 설정 오류가 있더라도
            # 체크포인트 조회/캐싱 로직은 독립적으로 동작하도록 한다.
            checkpoints_list = self.checkpoint_manager.get_checkpoints(self.history_id)
            checkpoints_dict = {cp.partition_name: cp for cp in checkpoints_list}

            # psycopg2 연결 생성 (COPY 명령용)
            self.log.emit("PostgreSQL 연결 생성 중...", "INFO")
            log_emitter.emit_log("INFO", "COPY 기반 마이그레이션 시작")

            self.source_conn = self._create_psycopg2_connection(self.profile.source_config)
            self.target_conn = self._create_psycopg2_connection(self.profile.target_config)

            # 버전 감지 및 파라미터 적용
            self._detect_and_apply_version_optimizations()

            # COPY 권한 확인
            self._check_copy_permissions()

            # 성능 지표 초기화
            self.performance_metrics.total_partitions = len(self.partitions)

            # 각 파티션 처리
            for i, partition in enumerate(self.partitions):
                if not self.is_running:
                    break

                self.current_partition_index = i

                # O(1) 체크포인트 조회
                checkpoint = checkpoints_dict.get(partition)

                if checkpoint and checkpoint.status == "completed":
                    self.log.emit(f"{partition} - 이미 완료됨, 건너뛰기", "INFO")
                    log_emitter.emit_log("INFO", f"{partition} - 이미 완료됨, 건너뛰기")
                    self.performance_metrics.completed_partitions += 1
                    continue

                # 파티션 마이그레이션
                try:
                    if self.copy_mode == "server":
                        self._migrate_partition_server_copy(partition, checkpoint)
                    elif self.copy_mode == "auto":
                        # auto: server-side 가능 여부를 1회 확인 후 캐시
                        if self._auto_server_copy_ok is False:
                            self._migrate_partition_with_copy(partition, checkpoint)
                        else:
                            try:
                                self._migrate_partition_server_copy(partition, checkpoint)
                                self._auto_server_copy_ok = True
                            except Exception as e:
                                self._auto_server_copy_ok = False
                                self._log(
                                    f"{partition} - Server-side COPY 실패 → Python COPY로 전환: {e}",
                                    "WARNING",
                                )
                                if checkpoint and checkpoint.id is not None:
                                    try:
                                        self.checkpoint_manager.update_checkpoint_status(
                                            checkpoint.id,
                                            "pending",
                                            error_message="",
                                            rows_processed=0,
                                            bytes_transferred=0,
                                        )
                                    except Exception:
                                        pass
                                self._migrate_partition_with_copy(partition, checkpoint)
                    else:
                        self._migrate_partition_with_copy(partition, checkpoint)
                except Exception as e:
                    if self.skip_on_error and self.is_running:
                        self.log.emit(
                            f"{partition} - 오류 발생, 건너뛰고 계속 진행: {str(e)}",
                            "WARNING",
                        )
                        log_emitter.emit_log(
                            "WARNING",
                            f"{partition} - 오류 발생, 건너뛰고 계속 진행: {str(e)}",
                        )
                        continue
                    raise

            if self.is_running:  # 정상 완료
                final_stats = self.performance_metrics.get_stats()
                self.log.emit(
                    f"마이그레이션 완료! 총 {final_stats['total_rows']:,}개 행, "
                    f"평균 속도: {final_stats['avg_rows_per_sec']:,.0f} rows/sec",
                    "SUCCESS",
                )
                log_emitter.emit_log(
                    "SUCCESS", "COPY 기반 마이그레이션이 정상적으로 완료되었습니다"
                )

        except Exception as e:
            self.log.emit(f"마이그레이션 오류: {str(e)}", "ERROR")
            log_emitter.emit_log("ERROR", f"마이그레이션 오류: {str(e)}")
            raise
        finally:
            # 연결 종료 (성공/실패/취소 모두 확실히 닫기)
            for conn in (self.source_conn, self.target_conn):
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def _create_psycopg2_connection(self, config: dict[str, Any]) -> psycopg2.extensions.connection:
        """psycopg2 연결 생성 (COPY 명령용)

        Note: 기존 최적화 대신 버전별 최적화를 나중에 적용합니다.
        """
        try:
            # 연결 파라미터 준비
            conn_params = {
                "host": config.get("host", "localhost"),
                "port": config.get("port", 5432),
                "database": config.get("database", ""),
                "user": config.get("username", ""),
                "password": config.get("password", ""),
            }

            # SSL 설정
            if config.get("ssl"):
                conn_params["sslmode"] = "require"

            # 연결 생성
            conn = psycopg2.connect(**conn_params)
            conn.autocommit = False

            self.log.emit("PostgreSQL 연결 생성 완료", "INFO")
            return conn
        except Exception as e:
            raise Exception(f"데이터베이스 연결 실패: {str(e)}")

    def _detect_and_apply_version_optimizations(self):
        """버전 감지 및 버전별 세션 파라미터 적용"""
        # 소스 DB 버전 감지
        source_compat_mode = self.profile.source_config.get("compat_mode", "auto")
        self.source_version = PostgresOptimizer.resolve_effective_version(
            self.source_conn, source_compat_mode
        )

        # 대상 DB 버전 감지
        target_compat_mode = self.profile.target_config.get("compat_mode", "auto")
        self.target_version = PostgresOptimizer.resolve_effective_version(
            self.target_conn, target_compat_mode
        )

        # 버전 정보 로깅
        self.log.emit(
            f"소스 DB: {self.source_version} (호환 모드: {source_compat_mode})",
            "INFO",
        )
        self.log.emit(
            f"대상 DB: {self.target_version} (호환 모드: {target_compat_mode})",
            "INFO",
        )
        log_emitter.emit_log(
            "INFO",
            f"버전 감지 완료 - 소스: {self.source_version.family.value}, 대상: {self.target_version.family.value}",
        )

        # 버전 호환성 검증
        is_compatible, warnings = VersionValidator.validate_version_compatibility(
            self.source_version, self.target_version
        )
        for warning in warnings:
            self.log.emit(f"버전 호환성 경고: {warning}", "WARNING")
            log_emitter.emit_log("WARNING", warning)

        # 버전별 세션 파라미터 적용
        self.log.emit("소스 DB 세션 파라미터 적용 중...", "INFO")
        PostgresOptimizer.apply_version_params(self.source_conn, self.source_version)

        self.log.emit("대상 DB 세션 파라미터 적용 중...", "INFO")
        PostgresOptimizer.apply_version_params(self.target_conn, self.target_version)

        self.log.emit("버전별 최적화 적용 완료", "INFO")

    def _check_copy_permissions(self):
        """COPY 권한 확인"""
        # 소스 DB COPY TO 권한
        can_copy_from, error_msg = PostgresOptimizer.check_copy_permissions(
            self.source_conn, check_write=False, version_info=self.source_version
        )
        if not can_copy_from:
            raise PermissionError(f"소스 데이터베이스 COPY TO 권한 없음:\n{error_msg}")

        # 대상 DB COPY FROM 권한
        can_copy_to, error_msg = PostgresOptimizer.check_copy_permissions(
            self.target_conn, check_write=True, version_info=self.target_version
        )
        if not can_copy_to:
            raise PermissionError(f"대상 데이터베이스 COPY FROM 권한 없음:\n{error_msg}")

        self.log.emit("COPY 권한 확인 완료", "INFO")

    def _detect_table_type(self, partition_name: str):
        """파티션명에서 테이블 타입 추론 (부모 테이블 기준)"""
        parent_table = "_".join(partition_name.split("_")[:-1])
        try:
            return get_table_type(parent_table)
        except Exception:
            # 기본값: POINT_HISTORY
            return get_table_type("point_history")

    @staticmethod
    def _format_literal(value: Any, is_timestamp: bool) -> str:
        """값을 SQL 리터럴로 변환 (재개 조건용)"""
        if value is None:
            return "NULL"

        # 숫자(bigint 등)
        if not is_timestamp:
            try:
                int_val = int(value)
                return str(int_val)
            except (TypeError, ValueError):
                pass

        # timestamp(예: energy_display.issued_date)
        if is_timestamp:
            try:
                import datetime as _dt

                # date.isoformat()은 sep 인자를 받지 않는다. datetime을 먼저 걸러야
                # date에서 TypeError가 나 except로 새지 않는다 (datetime은 date의 하위 타입).
                if isinstance(value, _dt.datetime):
                    safe = value.isoformat(sep=" ")
                elif isinstance(value, _dt.date):
                    safe = value.isoformat()
                else:
                    safe = str(value)
            except Exception:
                safe = str(value)

            safe = safe.replace("T", " ")
            safe = safe.replace("'", "''")
            return f"'{safe}'::timestamp"

        safe = str(value).replace("'", "''")
        return f"'{safe}'"

    def _get_target_last_key(self, partition_name: str, key_column: str, date_column: str):
        """대상 테이블에서 마지막 키(재개 SSOT) 조회 (2-컬럼 PK용)

        Returns:
            (last_key, last_date) 또는 None
        """
        if not self.target_conn:
            return None

        with self.target_conn.cursor() as cur:
            q = sql.SQL("SELECT {k}, {d} FROM {t} ORDER BY {k} DESC, {d} DESC LIMIT 1").format(
                k=sql.Identifier(key_column),
                d=sql.Identifier(date_column),
                t=sql.Identifier(partition_name),
            )
            try:
                cur.execute(q)
                row = cur.fetchone()
            except Exception:
                return None

        if not row:
            return None
        return row[0], row[1]

    def _get_target_last_key_multi(self, partition_name: str, pk_columns: list[str]):
        """대상 테이블에서 마지막 PK(재개 SSOT) 조회 (N-컬럼 PK용)

        Returns:
            {col_name: value} dict 또는 None
        """
        if not self.target_conn or not pk_columns:
            return None

        with self.target_conn.cursor() as cur:
            cols = sql.SQL(", ").join(sql.Identifier(c) for c in pk_columns)
            order = sql.SQL(", ").join(
                sql.SQL("{} DESC").format(sql.Identifier(c)) for c in pk_columns
            )
            q = sql.SQL("SELECT {cols} FROM {t} ORDER BY {order} LIMIT 1").format(
                cols=cols, t=sql.Identifier(partition_name), order=order
            )
            try:
                cur.execute(q)
                row = cur.fetchone()
            except Exception:
                return None

        if not row:
            return None
        # row는 pk_columns로 만든 SELECT 결과라 길이가 항상 같다.
        return dict(zip(pk_columns, row, strict=True))

    def _migrate_partition_with_copy(self, partition_name: str, checkpoint: Any):
        """COPY 명령을 사용한 파티션 마이그레이션 (청크 단위 처리)
        정합성(중복 방지) 정책:
        - resume 모드에서는 checkpoint보다 **대상 테이블의 마지막 키**를 SSOT로 삼아 이어서 진행한다.
        """
        self._log(f"{partition_name} COPY 마이그레이션 시작 (배치 크기: {self.batch_size:,})")
        accumulated_rows = 0
        last_path_id = None
        last_issued_date = None
        last_issued_date_text = None
        # 테이블 타입/컬럼 구성 (resume 판단에도 필요)
        table_type = self._detect_table_type(partition_name)
        table_config = TABLE_TYPE_CONFIG[table_type]
        key_column = table_config.columns[0]
        date_column = table_config.date_column
        is_timestamp_date = table_config.date_is_timestamp
        # PK 컬럼이 3개 이상인 경우(예: RUNNING_TIME_HISTORY) 추가 키 추적
        pk_columns = get_partition_primary_key_columns(table_type)
        extra_pk_columns = [c for c in pk_columns if c not in (key_column, date_column)]
        # 추가 PK 컬럼별 마지막 값 추적 딕셔너리
        last_extra_pk: dict = {}  # {col_name: value}
        try:
            # 테이블 크기 추정
            table_info = PostgresOptimizer.estimate_table_size(
                self.source_conn, partition_name, self.source_version
            )
            # 테이블이 존재하지 않는 경우
            if not table_info.get("exists", True):
                self._log(f"{partition_name} - 소스 테이블이 존재하지 않음, 건너뛰기", "WARNING")
                self.performance_metrics.completed_partitions += 1
                self._update_checkpoint_completed(checkpoint, 0, copy_method="COPY")
                return
            total_rows = table_info["row_count"]
            total_mb = table_info["total_size_mb"]
            if total_rows == 0:
                self._log(f"{partition_name} - 데이터 없음", "WARNING")
                self._update_checkpoint_completed(checkpoint, 0, copy_method="COPY")
                self.performance_metrics.completed_partitions += 1
                return
            self._log(f"{partition_name} - {total_rows:,}개 행, {total_mb:.1f}MB")
            # 성능 지표 시작
            self.performance_metrics.start_partition(partition_name, total_rows)
            # --- checkpoint 기반(보조) 재개 지점 로드 ---
            if checkpoint:
                if checkpoint.last_path_id is not None:
                    last_path_id = checkpoint.last_path_id
                    if is_timestamp_date:
                        last_issued_date_text = getattr(checkpoint, "last_issued_date_text", None)
                        if (
                            last_issued_date_text is None
                            and checkpoint.last_issued_date is not None
                        ):
                            last_issued_date_text = str(checkpoint.last_issued_date)
                    else:
                        last_issued_date = checkpoint.last_issued_date
                    accumulated_rows = int(checkpoint.rows_processed or 0)
                    self._log(
                        f"재개 지점 (DB checkpoint): key={last_path_id}, issued_date={last_issued_date_text or last_issued_date}",
                    )
                elif checkpoint.rows_processed > 0 and checkpoint.error_message:
                    import json

                    try:
                        data = json.loads(checkpoint.error_message)
                        last_path_id = data.get("last_path_id")
                        if is_timestamp_date:
                            last_issued_date_text = data.get("last_issued_date")
                        else:
                            last_issued_date = data.get("last_issued_date")
                        accumulated_rows = int(checkpoint.rows_processed or 0)
                        self._log(
                            f"재개 지점 (JSON): key={last_path_id}, issued_date={last_issued_date_text or last_issued_date}",
                        )
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass
            # --- 대상 테이블 준비 ---
            resume_expected = bool(self.should_resume)
            _created, target_row_count = self._prepare_target_table(
                partition_name, checkpoint=checkpoint, resume_expected=resume_expected
            )
            if resume_expected and target_row_count and accumulated_rows < int(target_row_count):
                accumulated_rows = int(target_row_count)
            if not checkpoint:
                checkpoint = self.checkpoint_manager.create_checkpoint(
                    self.history_id, partition_name
                )
            if accumulated_rows:
                self.performance_metrics.current_partition_rows = accumulated_rows
            # --- resume SSOT: 대상 테이블 마지막 키 조회 ---
            if resume_expected:
                if extra_pk_columns:
                    # 멀티 PK (예: RUNNING_TIME_HISTORY의 save_type 포함)
                    anchor_dict = self._get_target_last_key_multi(partition_name, pk_columns)
                    if anchor_dict:
                        anchor_key = anchor_dict.get(key_column)
                        anchor_date = anchor_dict.get(date_column)
                        for ec in extra_pk_columns:
                            last_extra_pk[ec] = anchor_dict.get(ec)
                    else:
                        anchor_key = anchor_date = None
                    anchor = (anchor_key, anchor_date) if anchor_key is not None else None
                else:
                    anchor = self._get_target_last_key(partition_name, key_column, date_column)
                if anchor:
                    anchor_key, anchor_date = anchor
                    try:
                        last_path_id = int(anchor_key)
                    except Exception:
                        last_path_id = anchor_key
                    if is_timestamp_date:
                        last_issued_date_text = str(anchor_date)
                        last_issued_date = None
                    else:
                        try:
                            last_issued_date = int(anchor_date)
                        except Exception:
                            last_issued_date = anchor_date
                        last_issued_date_text = None
                    self._log(
                        f"재개 SSOT(대상): key={last_path_id}, issued_date={last_issued_date_text or last_issued_date}",
                        "INFO",
                    )
                    self.checkpoint_manager.update_checkpoint_status(
                        checkpoint.id,
                        "running",
                        rows_processed=accumulated_rows,
                        last_path_id=last_path_id,
                        last_issued_date=last_issued_date,
                        last_issued_date_text=last_issued_date_text,
                        copy_method="COPY",
                    )
                else:
                    last_path_id = None
                    last_issued_date = None
                    last_issued_date_text = None
                    self._log("재개 모드: 대상 테이블에 기존 데이터 없음 → 처음부터 진행", "INFO")
            # COPY FROM 쿼리는 루프 밖에서 한 번만 빌드 (식별자 안전 처리)
            cols_idents = sql.SQL(", ").join(sql.Identifier(c) for c in table_config.columns)
            tbl_ident = sql.Identifier(partition_name)
            copy_from_query = (
                sql.SQL(
                    "COPY {tbl} ({cols}) FROM STDIN WITH (FORMAT CSV, HEADER FALSE, NULL 'NULL')"
                )
                .format(tbl=tbl_ident, cols=cols_idents)
                .as_string(self.target_conn)
            )

            key_ident = sql.Identifier(key_column)
            date_ident = sql.Identifier(date_column)

            # ORDER BY에 PK 전체 컬럼 포함 (예: RUNNING_TIME_HISTORY → path_id, issued_date, save_type)
            order_idents = (
                sql.SQL(", ").join(sql.Identifier(c) for c in pk_columns)
                if pk_columns
                else sql.SQL("{k}, {d}").format(k=key_ident, d=date_ident)
            )

            # 청크 단위 처리 루프
            while self.is_running:
                self._check_pause()
                # COPY TO 쿼리 빌드 (식별자 안전 처리)
                if last_path_id is not None and (
                    last_issued_date_text is not None or last_issued_date is not None
                ):
                    key_literal = self._format_literal(last_path_id, is_timestamp=False)
                    date_val = last_issued_date_text if is_timestamp_date else last_issued_date
                    date_literal = self._format_literal(date_val, is_timestamp_date)

                    if extra_pk_columns and last_extra_pk:
                        # 멀티 PK WHERE: (k, d, extra...) > (kv, dv, ev...)
                        # ROW 비교를 사용하여 정확한 resume 지점 결정
                        all_pk_idents = sql.SQL(", ").join(sql.Identifier(c) for c in pk_columns)
                        all_pk_vals = []
                        for c in pk_columns:
                            if c == key_column:
                                all_pk_vals.append(sql.SQL(key_literal))
                            elif c == date_column:
                                all_pk_vals.append(sql.SQL(date_literal))
                            else:
                                val = last_extra_pk.get(c)
                                all_pk_vals.append(
                                    sql.SQL(self._format_literal(val, is_timestamp=False))
                                )
                        all_pk_val_csv = sql.SQL(", ").join(all_pk_vals)
                        where_fragment = sql.SQL("WHERE ({cols}) > ({vals})").format(
                            cols=all_pk_idents, vals=all_pk_val_csv
                        )
                    else:
                        where_fragment = sql.SQL(
                            "WHERE {k} > {kv} OR ({k} = {kv} AND {d} > {dv})"
                        ).format(
                            k=key_ident,
                            kv=sql.SQL(key_literal),
                            d=date_ident,
                            dv=sql.SQL(date_literal),
                        )
                else:
                    where_fragment = sql.SQL("")

                copy_to_query = (
                    sql.SQL(
                        "COPY (SELECT {cols} FROM {tbl} {where} ORDER BY {order} LIMIT {lim}) "
                        "TO STDOUT WITH (FORMAT CSV, HEADER FALSE, NULL 'NULL')"
                    )
                    .format(
                        cols=cols_idents,
                        tbl=tbl_ident,
                        where=where_fragment,
                        order=order_idents,
                        lim=sql.SQL(str(int(self.batch_size))),
                    )
                    .as_string(self.source_conn)
                )

                # extra PK 컬럼의 CSV 인덱스 계산
                extra_indices = []
                for ec in extra_pk_columns:
                    if ec in table_config.columns:
                        extra_indices.append(table_config.columns.index(ec))
                stream_buffer = CopyStreamBuffer(extra_track_indices=extra_indices)

                # 배치마다 재생성되는 값이라 기본 인자로 묶어 이 반복의 것을 확정한다.
                # (producer 스레드가 join 타임아웃을 넘겨 살아남으면 다음 배치의
                #  버퍼를 건드릴 수 있다)
                def copy_out(query=copy_to_query, buffer=stream_buffer):
                    try:
                        with self.source_conn.cursor() as source_cursor:
                            source_cursor.copy_expert(query, buffer)
                    except Exception as exc:
                        buffer.set_error(exc)
                    finally:
                        buffer.close()

                producer_thread = threading.Thread(target=copy_out, daemon=True)
                producer_thread.start()
                with self.target_conn.cursor() as target_cursor:
                    try:
                        target_cursor.copy_expert(copy_from_query, stream_buffer)
                    except Exception as exc:
                        stream_buffer.set_error(exc)
                        raise
                producer_thread.join(timeout=120)
                if producer_thread.is_alive():
                    stream_buffer.cancel()
                    producer_thread.join(timeout=10)
                    raise Exception(
                        f"{partition_name} COPY producer 스레드가 시간 내 종료되지 않음"
                    )
                if stream_buffer.error:
                    raise stream_buffer.error
                self.target_conn.commit()
                copied_rows = int(stream_buffer.row_count or 0)
                if copied_rows == 0:
                    break
                if stream_buffer.last_key is not None:
                    try:
                        last_path_id = int(stream_buffer.last_key)
                    except Exception:
                        last_path_id = stream_buffer.last_key
                if stream_buffer.last_date is not None:
                    if is_timestamp_date:
                        last_issued_date_text = str(stream_buffer.last_date)
                        last_issued_date = None
                    else:
                        try:
                            last_issued_date = int(stream_buffer.last_date)
                        except Exception:
                            last_issued_date = stream_buffer.last_date
                        last_issued_date_text = None
                # 추가 PK 컬럼 값 갱신 (매 배치 후 반드시 업데이트)
                if extra_pk_columns and stream_buffer.last_extra:
                    for ec in extra_pk_columns:
                        if ec in table_config.columns:
                            idx = table_config.columns.index(ec)
                            if idx in stream_buffer.last_extra:
                                last_extra_pk[ec] = stream_buffer.last_extra[idx]
                accumulated_rows += copied_rows
                self.performance_metrics.update(copied_rows, stream_buffer.total_bytes)
                self.checkpoint_manager.update_checkpoint_status(
                    checkpoint.id,
                    "running",
                    rows_processed=accumulated_rows,
                    last_path_id=last_path_id,
                    last_issued_date=last_issued_date,
                    last_issued_date_text=last_issued_date_text,
                    copy_method="COPY",
                    bytes_transferred=self.performance_metrics.total_bytes,
                )
                self._emit_performance_metrics()
            if not self.is_running:
                return
            self.performance_metrics.complete_partition()
            self._update_checkpoint_completed(
                checkpoint,
                accumulated_rows,
                last_path_id=last_path_id,
                last_issued_date=last_issued_date,
                last_issued_date_text=last_issued_date_text,
                copy_method="COPY",
            )
            self._log(f"{partition_name} COPY 완료: 총 {accumulated_rows:,}개 행", "SUCCESS")
        except Exception as e:
            # 트랜잭션 롤백 (skip_on_error 시 다음 파티션이 정상 동작하도록)
            try:
                if self.target_conn and not self.target_conn.closed:
                    self.target_conn.rollback()
            except Exception:
                pass
            if checkpoint is None:
                checkpoint = self.checkpoint_manager.create_checkpoint(
                    self.history_id, partition_name
                )
            self.checkpoint_manager.update_checkpoint_status(
                checkpoint.id,
                "failed",
                rows_processed=accumulated_rows,
                error_message=str(e),
                last_path_id=last_path_id,
                last_issued_date=last_issued_date,
                last_issued_date_text=last_issued_date_text,
                copy_method="COPY",
                bytes_transferred=self.performance_metrics.total_bytes,
            )
            raise Exception(f"{partition_name} COPY 실패: {str(e)}")

    def _migrate_partition_server_copy(self, partition_name: str, checkpoint: Any):
        """서버사이드 COPY를 사용한 파티션 마이그레이션 (os.pipe 직접 스트리밍)

        벤치마크(bench_server_copy_ph_3days.py)에서 검증된 패턴을 그대로 포팅.
        - TEXT 포맷 (tab-delimited, NULL=\\N) → 크로스 버전 안전
        - 파티션 통짜 스트리밍이라 배치 단위 체크포인트(재개) 불가
        - 중단 시 해당 파티션은 처음부터 다시 복사해야 함
        """
        self.log.emit(f"{partition_name} Server-side COPY 시작", "INFO")
        log_emitter.emit_log("INFO", f"{partition_name} Server-side COPY 시작")

        try:
            # 1. 테이블 크기 추정
            table_info = PostgresOptimizer.estimate_table_size(
                self.source_conn, partition_name, self.source_version
            )

            if not table_info.get("exists", True):
                self.log.emit(
                    f"{partition_name} - 소스 테이블이 존재하지 않음, 건너뛰기", "WARNING"
                )
                log_emitter.emit_log("WARNING", f"{partition_name} - 소스 테이블이 존재하지 않음")
                self.performance_metrics.completed_partitions += 1
                self._update_checkpoint_completed(checkpoint, 0)
                return

            total_rows = table_info["row_count"]
            total_mb = table_info["total_size_mb"]

            if total_rows == 0:
                self.log.emit(f"{partition_name} - 데이터 없음", "WARNING")
                self._update_checkpoint_completed(checkpoint, 0)
                self.performance_metrics.completed_partitions += 1
                return

            self.log.emit(f"{partition_name} - {total_rows:,}개 행, {total_mb:.1f}MB", "INFO")

            # 2. 성능 지표 시작
            self.performance_metrics.start_partition(partition_name, total_rows)

            # 3. 대상 테이블 준비 (서버 모드는 항상 full restart → resume_expected=False)
            self._prepare_target_table(partition_name, checkpoint=checkpoint, resume_expected=False)

            # 4. 체크포인트 생성/갱신 → "running"
            if not checkpoint:
                checkpoint = self.checkpoint_manager.create_checkpoint(
                    self.history_id, partition_name
                )
            self.checkpoint_manager.update_checkpoint_status(
                checkpoint.id,
                "running",
                copy_method="COPY_SRV",
            )

            # 5. 테이블 타입/컬럼 구성
            table_type = self._detect_table_type(partition_name)
            table_config = TABLE_TYPE_CONFIG[table_type]
            cols = table_config.columns

            cols_sql = sql.SQL(", ").join(map(sql.Identifier, cols))
            tbl = sql.Identifier(partition_name)

            copy_to = sql.SQL(
                "COPY (SELECT {cols} FROM {tbl}) "
                "TO STDOUT WITH (FORMAT text, DELIMITER E'\\t', NULL '\\N')"
            ).format(cols=cols_sql, tbl=tbl)

            copy_from = sql.SQL(
                "COPY {tbl} ({cols}) FROM STDIN WITH (FORMAT text, DELIMITER E'\\t', NULL '\\N')"
            ).format(tbl=tbl, cols=cols_sql)

            # 6. os.pipe → producer thread(source COPY TO) + consumer(target COPY FROM)
            rfd, wfd = os.pipe()
            try:
                rfile = os.fdopen(rfd, "rb", closefd=True)
            except Exception:
                os.close(rfd)
                os.close(wfd)
                raise
            try:
                wfile = os.fdopen(wfd, "wb", closefd=True)
            except Exception:
                rfile.close()
                os.close(wfd)
                raise

            errors: list[Exception] = []

            def _pump_source():
                try:
                    with self.source_conn.cursor() as cur:
                        cur.copy_expert(copy_to.as_string(self.source_conn), wfile)
                except Exception as e:
                    errors.append(e)
                finally:
                    try:
                        wfile.close()
                    except Exception:
                        pass

            producer = threading.Thread(target=_pump_source, daemon=True)
            producer.start()

            try:
                with self.target_conn.cursor() as target_cursor:
                    target_cursor.copy_expert(copy_from.as_string(self.target_conn), rfile)
                    copied_rows = target_cursor.rowcount
            finally:
                try:
                    rfile.close()
                except Exception:
                    pass
                producer.join(timeout=60)

            # 7. producer 완료 확인 및 errors 체크
            if producer.is_alive():
                raise Exception(f"{partition_name} producer 스레드가 시간 내 종료되지 않음")
            if errors:
                raise errors[0]

            # 8. target_conn.commit()
            self.target_conn.commit()

            # 9. 성능 지표 업데이트 (1회)
            estimated_bytes = int(total_mb * 1024 * 1024)
            self.performance_metrics.update(copied_rows, estimated_bytes)
            self._emit_performance_metrics()

            # 10. 파티션 완료
            self.performance_metrics.complete_partition()
            self._update_checkpoint_completed(checkpoint, copied_rows, copy_method="COPY_SRV")

            self.log.emit(
                f"{partition_name} Server-side COPY 완료: {copied_rows:,}개 행", "SUCCESS"
            )
            log_emitter.emit_log(
                "SUCCESS",
                f"{partition_name} Server-side COPY 완료: {copied_rows:,}개 행",
            )

        except Exception as e:
            # rollback
            try:
                self.target_conn.rollback()
            except Exception:
                pass

            if checkpoint is None:
                checkpoint = self.checkpoint_manager.create_checkpoint(
                    self.history_id, partition_name
                )
            if checkpoint:
                self.checkpoint_manager.update_checkpoint_status(
                    checkpoint.id,
                    "failed",
                    error_message=str(e),
                    copy_method="COPY_SRV",
                    bytes_transferred=self.performance_metrics.total_bytes,
                )
            raise Exception(f"{partition_name} Server-side COPY 실패: {str(e)}")

    def _prepare_target_table(
        self,
        partition_name: str,
        checkpoint: Any | None = None,
        resume_expected: bool = False,
    ) -> tuple[bool, int]:
        """대상 테이블 준비

        정책:
        - 테이블이 없으면 생성
        - 테이블이 있고 row_count=0이면 그대로 진행
        - 테이블이 있고 row_count>0이면:
          - 일반 모드: UI 확인 후 TRUNCATE 여부 결정 (ask)
          - 재개(resume) 모드: 기존 데이터가 "정상"이므로 TRUNCATE하지 않음 (keep)
        """

        def confirm_truncate(table: str, row_count: int) -> bool:
            # 워커 스레드에서 UI를 직접 띄울 수 없으므로 시그널로 위임
            self.log.emit(
                f"{table} 테이블에 {row_count:,}개의 기존 데이터가 있습니다",
                "WARNING",
            )
            log_emitter.emit_log(
                "WARNING",
                f"{table} 테이블에 {row_count:,}개의 기존 데이터가 있습니다",
            )

            self.truncate_permission = None
            self.truncate_requested.emit(table, row_count)

            # UI 응답 대기
            while self.is_running and self.truncate_permission is None:
                time.sleep(0.1)

            return bool(self.truncate_permission)

        creator = TableCreator(self.source_conn, self.target_conn)

        if resume_expected:
            # 재개 모드: 대상에 이미 부분 데이터가 있을 수 있으므로 항상 keep
            # (checkpoint가 0이어도 commit 후 checkpoint 갱신 전 crash 가능)
            truncate_mode = "keep"
            cb = None
        elif self.copy_mode == "server":
            # 서버 모드는 항상 full restart → 파티션별 프롬프트 없이 자동 truncate
            truncate_mode = "auto"
            cb = None
        else:
            truncate_mode = "ask"
            cb = confirm_truncate

        created, row_count = creator.ensure_partition_ready(
            partition_name,
            truncate_mode=truncate_mode,
            confirm_callback=cb,
        )

        # 결과에 따른 로그 출력
        if created:
            self.log.emit(f"{partition_name} 테이블 생성 완료", "SUCCESS")
            log_emitter.emit_log("SUCCESS", f"{partition_name} 테이블 생성 완료")
        elif row_count > 0:
            if truncate_mode == "keep":
                self.log.emit(
                    f"{partition_name} 재개 모드: 기존 데이터 유지 (rows={row_count:,})",
                    "INFO",
                )
            else:
                self.log.emit(f"{partition_name} 기존 데이터 삭제 완료", "INFO")
                log_emitter.emit_log("INFO", f"{partition_name} 기존 데이터 삭제 완료")

        return created, row_count

    def _update_checkpoint_completed(
        self,
        checkpoint: Any,
        rows: int,
        last_path_id: int | None = None,
        last_issued_date: int | None = None,
        last_issued_date_text: str | None = None,
        copy_method: str | None = None,
    ):
        """체크포인트 완료 업데이트"""
        if checkpoint:
            method = copy_method or ("COPY_SRV" if self.copy_mode == "server" else "COPY")
            self.checkpoint_manager.update_checkpoint_status(
                checkpoint.id,
                "completed",
                rows_processed=rows,
                copy_method=method,
                bytes_transferred=self.performance_metrics.total_bytes,
                last_path_id=last_path_id,
                last_issued_date=last_issued_date,
                last_issued_date_text=last_issued_date_text,
            )

    def _emit_performance_metrics(self):
        """성능 지표 시그널 전송"""
        current_time = time.time()
        if current_time - self.last_metric_update >= self.metric_update_interval:
            stats = self.performance_metrics.get_stats()
            self.performance.emit(stats)

            # 진행 상황도 함께 업데이트
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

    def get_stats(self) -> dict[str, Any]:
        """통계 정보 반환 (오버라이드 - 성능 지표 사용)"""
        return self.performance_metrics.get_stats()

    def _check_connections(self):
        """연결 상태만 확인"""
        self.connection_checking.emit()

        # 소스 DB 연결 확인
        source_connected, source_message = PostgresOptimizer.check_connection_quick(
            self.profile.source_config
        )
        self.source_connection_status.emit(source_connected, source_message)

        # 대상 DB 연결 확인
        target_connected, target_message = PostgresOptimizer.check_connection_quick(
            self.profile.target_config
        )
        self.target_connection_status.emit(target_connected, target_message)
