"""
PostgreSQL COPY 명령 기반 고성능 마이그레이션 워커
"""

import csv
import os
import re
import threading
import time
from queue import Empty, Full, Queue
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
from src.database.connection_params import connect_psycopg2
from src.database.postgres_utils import PostgresOptimizer
from src.database.version_info import PgVersionInfo
from src.models.profile import ConnectionProfile
from src.utils.enhanced_logger import log_emitter
from src.utils.validators import VersionValidator


class _CsvRecordTracker:
    """PostgreSQL COPY ... (FORMAT CSV) 출력의 **레코드 경계**를 청크 단위로 추적한다.

    CSV에서 따옴표 안의 줄바꿈·쉼표는 데이터다. 물리 줄(split("\\n"))을 행으로 세면 행 수가
    부풀고, 배치 마지막 행의 값에 줄바꿈이 있으면 값 조각을 재개 키로 기록하게 된다(감사 C-02).

    - 따옴표 상태를 청크 사이에 이어서 추적한다. `""` 이스케이프는 토글이 두 번이라 자연히 상쇄된다.
    - 따옴표가 전혀 없는 청크(대부분의 숫자 이력 데이터)는 기존처럼 split으로 빠르게 처리한다.
    - 필드 분해는 마지막 레코드 하나만 `csv` 모듈로 한다.
    """

    _SPECIAL = re.compile(r'["\n]')

    def __init__(self) -> None:
        self._pending = ""  # 아직 끝나지 않은 레코드
        self._in_quotes = False
        self._last_record: str | None = None
        self.row_count = 0

    def feed(self, data: str) -> None:
        if not data:
            return
        if not self._in_quotes and '"' not in data and '"' not in self._pending:
            lines = (self._pending + data).split("\n")
            self._pending = lines.pop()
            for line in lines:
                if line:
                    self.row_count += 1
                    self._last_record = line
            return

        pending, in_q, start = self._pending, self._in_quotes, 0
        for m in self._SPECIAL.finditer(data):
            if m.group() == '"':
                in_q = not in_q
            elif not in_q:
                end = m.start()
                record = pending + data[start:end]
                pending, start = "", end + 1
                if record:
                    self.row_count += 1
                    self._last_record = record
        self._pending = pending + data[start:]
        self._in_quotes = in_q

    def finish(self) -> None:
        """EOF. 개행 없이 끝난 마지막 레코드를 확정한다."""
        if self._in_quotes:
            raise RuntimeError("COPY CSV 스트림이 닫히지 않은 따옴표로 끝났습니다")
        if self._pending:
            self.row_count += 1
            self._last_record = self._pending
            self._pending = ""

    def last_fields(self) -> list[str] | None:
        if self._last_record is None:
            return None
        record = self._last_record[:-1] if self._last_record.endswith("\r") else self._last_record
        return next(csv.reader([record]))


class CopyStreamBuffer:
    """원본 COPY OUT → 대상 COPY IN 스트리밍 버퍼.

    - write(): 원본 COPY OUT(생산자 스레드)이 호출. 청크를 제한 크기 큐에 넣는다.
    - read(): 대상 COPY IN(소비자)이 호출. EOF면 "" 를 돌려준다.
    - 행 수·마지막 키는 **소비자가 실제로 대상에 넘긴 데이터** 기준으로 센다.

    정합성 규약(감사 C-01):
    - 정상 종료(close)와 취소(cancel/set_error)는 다른 상태다. 정상 종료는 큐에 빈자리가 날
      때까지 기다려 종료 표시를 넣고, 소비자는 그 표시까지 큐를 끝까지 비운다.
    - 취소·오류에서 read()는 짧은 EOF가 아니라 **예외**를 던진다. 그래야 대상 COPY가
      실패하고 커밋되지 않는다.
    - 커밋 전 assert_fully_consumed()로 생산량 == 소비량을 확인한다.
    """

    # write()/close()가 큐 빈자리를 기다리는 최대 시간(초). 넘으면 교착 대신 오류로 끝낸다.
    _WRITE_TIMEOUT = 30

    def __init__(self, max_queue_size: int = 8, extra_track_indices: list[int] | None = None):
        """
        Args:
            max_queue_size: 큐 최대 크기
            extra_track_indices: CSV에서 추가로 추적할 컬럼 인덱스 목록
                (예: save_type이 columns[2]이면 [2] 전달)
        """
        self.queue: Queue[str | None] = Queue(maxsize=max_queue_size)
        self._extra_track_indices = extra_track_indices or []
        self._tracker = _CsvRecordTracker()
        self.total_bytes: int = 0
        self.produced_chars: int = 0
        self.consumed_chars: int = 0
        self._closed = False  # 생산자가 끝났다(더 쓰지 않는다)
        self._eof = False  # 소비자가 종료 표시까지 다 읽었다
        self._cancel = threading.Event()
        self.error: Exception | None = None

    # --- 소비자 기준 추적 결과 -------------------------------------------------
    @property
    def row_count(self) -> int:
        return self._tracker.row_count

    def _last_field(self, idx: int) -> str | None:
        fields = self._tracker.last_fields()
        if fields is None or idx >= len(fields):
            return None
        return fields[idx]

    @property
    def last_key(self) -> str | None:
        return self._last_field(0)

    @property
    def last_date(self) -> str | None:
        return self._last_field(1)

    @property
    def last_extra(self) -> dict[int, str]:
        fields = self._tracker.last_fields()
        if fields is None:
            return {}
        return {i: fields[i] for i in self._extra_track_indices if i < len(fields)}

    # --- 생산자 ---------------------------------------------------------------
    def write(self, data: str | bytes):
        """COPY OUT이 호출하는 write; 청크를 큐에 적재"""
        if self._closed or self._cancel.is_set():
            return

        # psycopg2는 bytes를 줄 수 있으므로 문자열로 변환
        data_str = data.decode("utf-8") if isinstance(data, bytes) else data

        self.produced_chars += len(data_str)
        self.total_bytes += len(data_str.encode("utf-8"))
        try:
            self.queue.put(data_str, timeout=self._WRITE_TIMEOUT)
        except Full:
            if not self._cancel.is_set():
                self.set_error(TimeoutError("CopyStreamBuffer.write() 큐 대기 시간 초과"))

    def close(self):
        """생산 종료(정상 EOF). 소비자가 큐를 비울 때까지 기다렸다가 종료 표시를 넣는다."""
        if self._closed:
            return
        self._closed = True
        deadline = time.monotonic() + self._WRITE_TIMEOUT
        while not self._cancel.is_set():
            try:
                self.queue.put(None, timeout=0.2)
                return
            except Full:
                if time.monotonic() > deadline:
                    self.set_error(
                        TimeoutError("CopyStreamBuffer.close() 종료 신호 전달 시간 초과")
                    )
                    return

    # --- 소비자 ---------------------------------------------------------------
    def read(self, size: int = -1) -> str:
        """COPY IN이 호출하는 read; 큐에서 꺼내 전달. 취소·오류는 예외로 알린다."""
        self._raise_if_aborted()
        if self._eof:
            return ""

        chunks: list[str] = []
        read_chars = 0
        while size < 0 or read_chars < size:
            try:
                chunk = self.queue.get(timeout=1)
            except Empty:
                self._raise_if_aborted()
                continue

            if chunk is None:
                self._eof = True
                self._tracker.finish()
                break

            chunks.append(chunk)
            read_chars += len(chunk)
            self.consumed_chars += len(chunk)
            self._tracker.feed(chunk)

        return "".join(chunks)

    def _raise_if_aborted(self) -> None:
        if self.error:
            raise self.error
        if self._cancel.is_set():
            raise RuntimeError("COPY 스트림이 취소되었습니다")

    def assert_fully_consumed(self) -> None:
        """커밋 직전 호출. 원본이 보낸 모든 데이터가 대상 COPY로 넘어갔는지 확인한다."""
        self._raise_if_aborted()
        if not self._eof or self.consumed_chars != self.produced_chars:
            raise RuntimeError(
                "COPY 스트림 불완전 — 커밋하지 않습니다: "
                f"생산 {self.produced_chars:,}자 / 소비 {self.consumed_chars:,}자 (EOF={self._eof})"
            )

    # --- 중단 -----------------------------------------------------------------
    def cancel(self):
        """양쪽 스레드를 강제 해제하기 위한 취소"""
        self._cancel.set()
        self._closed = True

    def set_error(self, exc: Exception):
        """프로듀서/컨슈머에서 발생한 오류를 기록하고 양쪽을 멈춘다"""
        if self.error is None:
            self.error = exc
        self._cancel.set()
        self._closed = True


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
            # skip_on_error로 건너뛴 파티션. 하나라도 있으면 실행을 '완료'로 끝내지 않는다.
            skipped: list[str] = []

            # 각 파티션 처리
            for i, partition in enumerate(self.partitions):
                # 일시정지는 파티션 경계에서도 확인한다.
                #
                # Python COPY는 배치 루프 안에서 스스로 확인하지만, server-side
                # COPY는 파티션 하나가 단일 명령이라 중간에 끼어들 수 없다.
                # 이 확인이 없으면 server/auto 모드에서 '일시정지'를 눌러도
                # 작업이 끝까지 그냥 돈다 — 화면만 '일시정지'라고 말한다.
                self._check_pause()
                if not self.is_running:
                    break

                self.current_partition_index = i

                # O(1) 체크포인트 조회
                checkpoint = checkpoints_dict.get(partition)

                if checkpoint and checkpoint.status == "completed":
                    self.log.emit(f"{partition} - 이미 완료됨, 건너뛰기", "INFO")
                    log_emitter.emit_log("INFO", f"{partition} - 이미 완료됨, 건너뛰기")
                    self.performance_metrics.completed_partitions += 1
                    self._emit_performance_metrics(force=True)
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
                        skipped.append(partition)
                        continue
                    raise

            if self.is_running and skipped:
                # 건너뛴 파티션을 두고 '완료'로 끝내면 이력이 completed가 되어 재개 대상에서
                # 사라진다(감사 H-01). 실패로 끝내 '이어서 시작'이 실패 파티션을 다시 잡게 한다.
                raise RuntimeError(
                    f"{len(skipped)}개 파티션이 실패해 건너뛰었습니다: {', '.join(skipped)}. "
                    "나머지는 완료되었습니다. '이어서 시작'으로 실패한 파티션만 다시 시도할 수 있습니다."
                )

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
            # 화면 로그(self.log)는 run()이 공통으로 한 번 남긴다. 여기서 같이 emit하면
            # 다시 던진 예외를 run()이 또 찍어 같은 줄이 두 번 보인다.
            # 영속 로그는 run()이 남기지 않으므로 여기서만 기록한다.
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
            # 공용 빌더: TLS 서버 신원 검증, connect_timeout, search_path=public
            conn = connect_psycopg2(config)
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
                self._emit_performance_metrics(force=True)
                return
            total_rows, is_empty = self._resolve_total_rows(partition_name, table_info)
            total_mb = table_info["total_size_mb"]
            if is_empty:
                self._log(f"{partition_name} - 데이터 없음", "WARNING")
                self._update_checkpoint_completed(checkpoint, 0, copy_method="COPY")
                self.performance_metrics.completed_partitions += 1
                self._emit_performance_metrics(force=True)
                return
            self._log(f"{partition_name} - {total_rows:,}개 행, {total_mb:.1f}MB")
            # 성능 지표 시작
            self.performance_metrics.start_partition(partition_name, total_rows)
            self._emit_performance_metrics(force=True)
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
                # 원본이 보낸 데이터가 전부 대상 COPY로 넘어갔을 때만 커밋한다(감사 C-01).
                stream_buffer.assert_fully_consumed()
                self.target_conn.commit()
                # 행 수·마지막 키는 소비자(대상에 넘긴 데이터) 기준, CSV 레코드 경계로 센 값이다.
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
            # 전송 경로가 센 값이 아니라 원본·대상 COUNT(*)로 완료를 판정한다.
            accumulated_rows = self._verify_partition_row_count(partition_name)
            self.performance_metrics.complete_partition()
            self._update_checkpoint_completed(
                checkpoint,
                accumulated_rows,
                last_path_id=last_path_id,
                last_issued_date=last_issued_date,
                last_issued_date_text=last_issued_date_text,
                copy_method="COPY",
            )
            self._emit_performance_metrics(force=True)
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
                self._emit_performance_metrics(force=True)
                return

            total_rows, is_empty = self._resolve_total_rows(partition_name, table_info)
            total_mb = table_info["total_size_mb"]

            if is_empty:
                self.log.emit(f"{partition_name} - 데이터 없음", "WARNING")
                self._update_checkpoint_completed(checkpoint, 0)
                self.performance_metrics.completed_partitions += 1
                self._emit_performance_metrics(force=True)
                return

            self.log.emit(f"{partition_name} - {total_rows:,}개 행, {total_mb:.1f}MB", "INFO")

            # 2. 성능 지표 시작
            self.performance_metrics.start_partition(partition_name, total_rows)
            self._emit_performance_metrics(force=True)

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

            # 단일 COPY 명령 안에서는 정확한 행 진행률을 알 수 없으므로 UI에
            # 무한 진행 상태를 알린 뒤 블로킹 스트리밍을 시작한다.
            self._emit_performance_metrics(force=True, current_indeterminate=True)
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
            self._emit_performance_metrics(force=True)

            # 10. 행 수 검증 후 파티션 완료 (원본·대상 COUNT(*))
            copied_rows = self._verify_partition_row_count(partition_name)
            self.performance_metrics.complete_partition()
            self._update_checkpoint_completed(checkpoint, copied_rows, copy_method="COPY_SRV")
            self._emit_performance_metrics(force=True)

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

    def _verify_partition_row_count(self, partition_name: str) -> int:
        """원본과 대상 파티션의 COUNT(*)가 정확히 같은지 확인하고 그 값을 돌려준다.

        완료 판정의 유일한 근거다. 워커 카운터·rows_processed는 전송 경로가 센 값이라
        전송 중 누락이 생기면 같이 틀린다. 다르면 예외 → 체크포인트는 failed로 남는다.
        원본 파티션이 이관 중에 바뀌었을 때(오늘 날짜 등)도 여기서 드러난다.
        """
        query = sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(partition_name))

        with self.source_conn.cursor() as cur:
            cur.execute(query)
            row = cur.fetchone()
            source_rows = int(row[0]) if row else 0
        with self.target_conn.cursor() as cur:
            cur.execute(query)
            row = cur.fetchone()
            target_rows = int(row[0]) if row else 0
        try:
            self.target_conn.commit()  # 조회 트랜잭션을 닫는다
        except Exception:
            pass

        if source_rows != target_rows:
            raise RuntimeError(
                f"{partition_name} 행 수 불일치 — 원본 {source_rows:,} / 대상 {target_rows:,}. "
                "완료로 기록하지 않습니다. 원본이 이관 중 바뀌지 않았는지 확인하고, "
                "이 파티션을 처음부터 다시 이관하세요(기존 데이터 삭제 후 새 작업)."
            )
        self._log(f"{partition_name} 행 수 검증 통과: 원본 = 대상 = {source_rows:,}", "SUCCESS")
        return source_rows

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

    def _resolve_total_rows(self, partition_name: str, table_info: dict) -> tuple[int, bool]:
        """(행 수, 비어 있는가)를 돌려준다.

        두 값을 나누는 이유: 행 수를 못 세는 경우에도 '비었다'고 단정하면 안 되는데,
        그렇다고 진행률 계산에 음수를 넘길 수도 없다.

        `estimate_table_size`의 row_count는 `pg_class.reltuples`, 즉 **추정치**다.
        이 값은 VACUUM/ANALYZE 전에는 0(PG14+는 -1)이라, 방금 만들어져 아직
        분석되지 않은 파티션은 **데이터가 있어도 0으로 보고된다.**

        그대로 믿고 건너뛰면 체크포인트가 완료로 마킹되어 재개해도 다시
        시도하지 않는다 — 조용한 데이터 누락이다. 이 도구는 최근 날짜
        파티션을 옮기므로 갓 생성된 파티션이 정확히 이 조건에 해당한다.

        추정이 0/음수일 때만 실제로 세어 확인한다(진짜 빈 테이블이면 COUNT도 싸다).
        """
        estimated = int(table_info.get("row_count") or 0)
        if estimated > 0:
            return estimated, False

        try:
            with self.source_conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(partition_name))
                )
                row = cur.fetchone()
                actual = int(row[0]) if row else 0
        except Exception as e:
            # 실패한 문장이 트랜잭션을 abort 상태로 남긴다. 되돌리지 않으면
            # 이어지는 COPY가 전부 실패한다("current transaction is aborted").
            try:
                self.source_conn.rollback()
            except Exception:
                pass
            # 셀 수 없으면 '비었다'고 단정하지 않는다. 건너뛰지 말고 진행시킨다.
            self._log(
                f"{partition_name} - 행 수 확인 실패({e}). 비어 있지 않다고 보고 진행합니다.",
                "WARNING",
            )
            return 0, False

        if actual > 0:
            self._log(
                f"{partition_name} - 통계상 0행이지만 실제 {actual:,}행입니다"
                " (ANALYZE 전 파티션). 건너뛰지 않습니다.",
                "WARNING",
            )
        return actual, actual == 0

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

    def _emit_performance_metrics(
        self, *, force: bool = False, current_indeterminate: bool = False
    ):
        """성능 지표 시그널 전송"""
        current_time = time.time()
        if not force and current_time - self.last_metric_update < self.metric_update_interval:
            return

        stats = self.performance_metrics.get_stats()
        self.performance.emit(stats)

        # 완료 직후에는 current_partition이 비어 있다. 이때 현재 바를 0으로
        # 되돌리지 않고 전체 진행률만 갱신한다.
        progress_data: dict[str, Any] = {
            "total_progress": int(stats["total_progress"]),
            "total_partitions": stats["total_partitions"],
            "completed_partitions": stats["completed_partitions"],
            "speed": stats["instant_rows_per_sec"],
        }
        if stats["current_partition"] is not None:
            progress_data.update(
                {
                    "current_progress": int(stats["partition_progress"]),
                    "current_partition": stats["current_partition"],
                    "current_rows": stats["current_partition_rows"],
                    "current_indeterminate": current_indeterminate,
                }
            )
        self.progress.emit(progress_data)

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
