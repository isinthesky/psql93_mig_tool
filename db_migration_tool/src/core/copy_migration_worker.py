"""
PostgreSQL COPY 명령 기반 고성능 마이그레이션 워커
"""

import threading
import time
from queue import Empty, Queue
from typing import Any, Union

import psycopg2
from PySide6.QtCore import Signal

from src.core.base_migration_worker import BaseMigrationWorker
from src.core.performance_metrics import PerformanceMetrics
from src.core.table_creator import TableCreator
from src.core.table_types import TABLE_TYPE_CONFIG, get_table_type
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

    def __init__(self, max_queue_size: int = 8):
        self.queue: Queue[Union[str, bytes, None]] = Queue(maxsize=max_queue_size)
        self.last_key: str | None = None
        self.last_date: str | None = None
        self.row_count: int = 0
        self.total_bytes: int = 0
        self._partial_line: str = ""
        self._closed = False
        self.error: Exception | None = None

    def write(self, data: Union[str, bytes]):
        """COPY OUT이 호출하는 write; 청크를 큐에 적재"""
        if self._closed:
            return

        # psycopg2는 bytes를 줄 수 있으므로 문자열로 변환
        if isinstance(data, bytes):
            data_str = data.decode("utf-8")
        else:
            data_str = data

        self._track_last_row(data_str)
        self.total_bytes += len(data_str.encode("utf-8"))
        self.queue.put(data)

    def read(self, size: int = -1) -> str:
        """COPY IN이 호출하는 read; 큐에서 꺼내 전달"""
        if self.error:
            raise self.error

        if self._closed and self.queue.empty():
            return ""

        chunks: list[str] = []
        bytes_read = 0

        while size < 0 or bytes_read < size:
            try:
                chunk = self.queue.get(timeout=1)
            except Empty:
                if self._closed:
                    break
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
        try:
            self.queue.put_nowait(None)
        except Exception:
            pass

    def set_error(self, exc: Exception):
        """프로듀서에서 발생한 오류를 기록"""
        self.error = exc
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
            except (IndexError):
                continue

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
        except (IndexError):
            pass


class CopyMigrationWorker(BaseMigrationWorker):
    """COPY 명령 기반 고성능 마이그레이션 워커"""

    # CopyMigrationWorker 전용 시그널
    performance = Signal(dict)  # 성능 지표
    connection_checking = Signal()  # 연결 확인 시작
    source_connection_status = Signal(bool, str)  # 연결 성공 여부, 메시지
    target_connection_status = Signal(bool, str)  # 연결 성공 여부, 메시지

    def __init__(
        self,
        profile: ConnectionProfile,
        partitions: list[str],
        history_id: int,
        resume: bool = False,
        batch_size: int = 100000,
    ):
        super().__init__(profile, partitions, history_id, resume)
        self.batch_size = batch_size

        # COPY 워커 전용 필드
        self.performance_metrics = PerformanceMetrics()

        # psycopg2 연결 (COPY 명령용)
        self.source_conn = None
        self.target_conn = None

        # 버전 정보 (연결 후 감지)
        self.source_version: PgVersionInfo | None = None
        self.target_version: PgVersionInfo | None = None

        # 성능 지표 업데이트 타이머
        self.last_metric_update = 0
        self.metric_update_interval = 1.0  # 1초마다 업데이트

    def _execute_migration(self):
        """COPY 기반 마이그레이션 실행"""
        # 연결 확인만 수행하는 경우
        if hasattr(self, "check_connections_only") and self.check_connections_only:
            self._check_connections()
            return

        try:
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

            # 체크포인트를 딕셔너리로 캐싱 (성능 개선)
            checkpoints_list = self.checkpoint_manager.get_checkpoints(self.history_id)
            checkpoints_dict = {cp.partition_name: cp for cp in checkpoints_list}

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
                self._migrate_partition_with_copy(partition, checkpoint)

            # 연결 종료
            self.source_conn.close()
            self.target_conn.close()

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

    def _create_psycopg2_connection(self, config: dict[str, Any]) -> psycopg2.extensions.connection:
        """psycopg2 연결 생성 (COPY 명령용)

        Note: 기존 최적화 대신 버전별 최적화를 나중에 적용합니다.
        """
        try:
            # 연결 파라미터 준비
            conn_params = {
                "host": config["host"],
                "port": config["port"],
                "database": config["database"],
                "user": config["username"],
                "password": config["password"],
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
        if not is_timestamp:
            try:
                int_val = int(value)
                return str(int_val)
            except (TypeError, ValueError):
                pass
        safe = str(value).replace("'", "''")
        if is_timestamp:
            return f"'{safe}'::timestamp"
        return f"'{safe}'"

    def _migrate_partition_with_copy(self, partition_name: str, checkpoint: Any):
        """COPY 명령을 사용한 파티션 마이그레이션 (청크 단위 처리)"""
        self.log.emit(f"{partition_name} COPY 마이그레이션 시작 (배치 크기: {self.batch_size:,})", "INFO")
        log_emitter.emit_log("INFO", f"{partition_name} COPY 마이그레이션 시작")

        accumulated_rows = 0
        last_path_id = None
        last_issued_date = None

        try:
            # 테이블 크기 추정
            table_info = PostgresOptimizer.estimate_table_size(
                self.source_conn, partition_name, self.source_version
            )

            # 테이블이 존재하지 않는 경우
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

            # 성능 지표 시작
            self.performance_metrics.start_partition(partition_name, total_rows)

            # 대상 테이블 준비
            self._prepare_target_table(partition_name)

            # 재개 지점 결정
            if checkpoint:
                # 체크포인트 필드 우선 사용
                if checkpoint.last_path_id is not None:
                    last_path_id = checkpoint.last_path_id
                    last_issued_date = checkpoint.last_issued_date
                    accumulated_rows = checkpoint.rows_processed
                    self.log.emit(
                        f"재개 지점 (DB): path_id={last_path_id}, issued_date={last_issued_date}",
                        "INFO",
                    )
                # 하위 호환성: error_message JSON 파싱
                elif checkpoint.rows_processed > 0 and checkpoint.error_message:
                    import json

                    try:
                        data = json.loads(checkpoint.error_message)
                        last_path_id = data.get("last_path_id")
                        last_issued_date = data.get("last_issued_date")
                        accumulated_rows = checkpoint.rows_processed
                        self.log.emit(
                            f"재개 지점 (JSON): path_id={last_path_id}, issued_date={last_issued_date}",
                            "INFO",
                        )
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass

            # 체크포인트가 없으면 생성하여 진행 중 상태를 기록할 수 있게 함
            if not checkpoint:
                checkpoint = self.checkpoint_manager.create_checkpoint(
                    self.history_id, partition_name
                )

            # 진행률 계산이 재개 지점부터 시작되도록 반영
            if accumulated_rows:
                self.performance_metrics.current_partition_rows = accumulated_rows

            # 테이블 타입/컬럼 구성
            table_type = self._detect_table_type(partition_name)
            table_config = TABLE_TYPE_CONFIG[table_type]
            columns_csv = ", ".join(table_config.columns)
            key_column = table_config.columns[0]
            date_column = table_config.date_column
            is_timestamp_date = table_config.date_is_timestamp

            # 청크 단위 처리 루프
            while self.is_running:
                # WHERE 절 구성
                where_clause = ""
                if last_path_id is not None:
                    key_literal = self._format_literal(last_path_id, is_timestamp=False)
                    date_literal = self._format_literal(last_issued_date, is_timestamp_date)
                    where_clause = (
                        f"WHERE {key_column} > {key_literal} OR "
                        f"({key_column} = {key_literal} AND {date_column} > {date_literal})"
                    )

                # LIMIT를 사용한 부분 쿼리
                copy_to_query = f"""
                    COPY (
                        SELECT {columns_csv}
                        FROM {partition_name}
                        {where_clause}
                        ORDER BY {key_column}, {date_column}
                        LIMIT {self.batch_size}
                    ) TO STDOUT WITH (FORMAT CSV, HEADER FALSE, NULL 'NULL')
                """

                copy_from_query = f"""
                    COPY {partition_name} ({columns_csv})
                    FROM STDIN WITH (FORMAT CSV, HEADER FALSE, NULL 'NULL')
                """

                stream_buffer = CopyStreamBuffer()

                # 소스 → 대상 스트리밍 (큐 기반)
                def copy_out():
                    try:
                        with self.source_conn.cursor() as source_cursor:
                            source_cursor.copy_expert(copy_to_query, stream_buffer)
                    except Exception as exc:
                        stream_buffer.set_error(exc)
                    finally:
                        stream_buffer.close()

                producer_thread = threading.Thread(target=copy_out, daemon=True)
                producer_thread.start()

                # 대상 COPY IN (stream_buffer.read 사용)
                with self.target_conn.cursor() as target_cursor:
                    try:
                        target_cursor.copy_expert(copy_from_query, stream_buffer)
                    except Exception as exc:
                        stream_buffer.set_error(exc)
                        raise

                producer_thread.join()

                if stream_buffer.error:
                    raise stream_buffer.error

                # COPY FROM 트랜잭션 커밋
                self.target_conn.commit()

                copied_rows = stream_buffer.row_count

                # 데이터가 없으면 완료
                if copied_rows == 0:
                    break

                # 트래커에서 마지막 키 값 가져오기
                if stream_buffer.last_key is not None:
                    last_path_id = stream_buffer.last_key
                    last_issued_date = stream_buffer.last_date

                # 누적 행 수 업데이트
                accumulated_rows += copied_rows

                # 성능 지표 업데이트
                self.performance_metrics.update(copied_rows, stream_buffer.total_bytes)

                # 체크포인트 업데이트 (중간 저장)
                if checkpoint:
                    self.checkpoint_manager.update_checkpoint_status(
                        checkpoint.id,
                        "running",
                        rows_processed=accumulated_rows,
                        last_path_id=last_path_id,
                        last_issued_date=last_issued_date,
                        copy_method="COPY",
                        bytes_transferred=self.performance_metrics.total_bytes,
                    )

                    # 성능 지표 전송
                    self._emit_performance_metrics()

                    # 로그 (너무 빈번하지 않게)
                    # self.log.emit(f"{partition_name} 배치 완료: {copied_rows:,}행", "DEBUG")

                # 파티션 완료 처리
                self.performance_metrics.complete_partition()
                self._update_checkpoint_completed(
                    checkpoint, accumulated_rows, last_path_id=last_path_id, last_issued_date=last_issued_date
                )

                # 로그 출력
                self.log.emit(
                    f"{partition_name} 완료: 총 {accumulated_rows:,}개 행",
                    "SUCCESS",
                )
                log_emitter.emit_log(
                    "SUCCESS",
                    f"{partition_name} COPY 완료: 총 {accumulated_rows:,}개 행",
                )

        except Exception as e:
            if checkpoint is None:
                checkpoint = self.checkpoint_manager.create_checkpoint(self.history_id, partition_name)
            if checkpoint:
                self.checkpoint_manager.update_checkpoint_status(
                    checkpoint.id,
                    "failed",
                    rows_processed=accumulated_rows,
                    error_message=str(e),
                    last_path_id=last_path_id,
                    last_issued_date=last_issued_date,
                    copy_method="COPY",
                    bytes_transferred=self.performance_metrics.total_bytes,
                )
            raise Exception(f"{partition_name} COPY 실패: {str(e)}")

    def _prepare_target_table(self, partition_name: str):
        """대상 테이블 준비"""
        # TableCreator를 사용하여 테이블 준비 (자동 TRUNCATE 모드)
        creator = TableCreator(self.source_conn, self.target_conn)
        created, row_count = creator.ensure_partition_ready(partition_name, truncate_mode="auto")

        # 결과에 따른 로그 출력
        if created:
            self.log.emit(f"{partition_name} 테이블 생성 완료", "SUCCESS")
            log_emitter.emit_log("SUCCESS", f"{partition_name} 테이블 생성 완료")
        elif row_count > 0:
            self.log.emit(f"{partition_name} 기존 데이터 삭제 완료", "INFO")
            log_emitter.emit_log("INFO", f"{partition_name} 기존 데이터 삭제 완료")

    def _update_checkpoint_completed(
        self,
        checkpoint: Any,
        rows: int,
        last_path_id: int | None = None,
        last_issued_date: int | None = None,
    ):
        """체크포인트 완료 업데이트"""
        if checkpoint:
            self.checkpoint_manager.update_checkpoint_status(
                checkpoint.id,
                "completed",
                rows_processed=rows,
                copy_method="COPY",
                bytes_transferred=self.performance_metrics.total_bytes,
                last_path_id=last_path_id,
                last_issued_date=last_issued_date,
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
