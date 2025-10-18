"""
마이그레이션 워커 스레드
"""

import time
from typing import Any

import psycopg
from psycopg import sql
from PySide6.QtCore import Signal

from src.core.base_migration_worker import BaseMigrationWorker
from src.core.table_creator import TableCreator
from src.models.profile import ConnectionProfile
from src.utils.enhanced_logger import log_emitter


class MigrationWorker(BaseMigrationWorker):
    """INSERT 기반 마이그레이션 워커

    Note: 이 클래스는 레거시 INSERT 기반 마이그레이션을 위해 유지됩니다.
    고성능 마이그레이션을 위해서는 CopyMigrationWorker를 사용하세요.
    """

    # MigrationWorker 전용 시그널
    truncate_requested = Signal(str, int)  # 테이블명, 행 수

    def __init__(
        self,
        profile: ConnectionProfile,
        partitions: list[str],
        history_id: int,
        resume: bool = False,
    ):
        super().__init__(profile, partitions, history_id, resume)

        # INSERT 워커 전용 필드
        self.is_interrupted = False
        self.truncate_permission = None  # None, True, False

        # 배치 크기 설정
        self.batch_size = 100000  # 초기 배치 크기
        self.min_batch_size = 1000
        self.max_batch_size = 500000

    def _execute_migration(self):
        """INSERT 기반 마이그레이션 실행"""
        try:
            # 소스 및 대상 연결 생성
            self.source_conn = self._create_connection(self.profile.source_config)
            target_conn = self._create_connection(self.profile.target_config)

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
                    continue

                # 파티션 마이그레이션
                self._migrate_partition(self.source_conn, target_conn, partition, checkpoint)

            # 연결 종료
            self.source_conn.close()
            target_conn.close()

            if self.is_running:  # 정상 완료
                log_emitter.emit_log("SUCCESS", "마이그레이션이 정상적으로 완료되었습니다")

        except Exception as e:
            log_emitter.emit_log("ERROR", f"마이그레이션 오류: {str(e)}")
            raise

    def _create_connection(self, config: dict[str, Any]) -> psycopg.Connection:
        """데이터베이스 연결 생성"""
        conn_params = {
            "host": config["host"],
            "port": config["port"],
            "dbname": config["database"],
            "user": config["username"],
            "password": config["password"],
        }

        if config.get("ssl"):
            conn_params["sslmode"] = "require"

        return psycopg.connect(**conn_params)

    def _migrate_partition(
        self,
        source_conn: psycopg.Connection,
        target_conn: psycopg.Connection,
        partition_name: str,
        checkpoint: Any,
    ):
        """단일 파티션 마이그레이션"""
        self.log.emit(f"{partition_name} 마이그레이션 시작", "INFO")
        log_emitter.emit_log("INFO", f"{partition_name} 마이그레이션 시작")

        try:
            # 소스에서 총 행 수 확인
            with source_conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(partition_name))
                )
                total_rows = cur.fetchone()[0]

            if total_rows == 0:
                self.log.emit(f"{partition_name} - 데이터 없음", "WARNING")
                log_emitter.emit_log("WARNING", f"{partition_name} - 데이터 없음")
                if checkpoint:
                    self.checkpoint_manager.update_checkpoint_status(checkpoint.id, "completed", 0)
                return

            # 대상 테이블 준비
            self._prepare_target_table(target_conn, partition_name)

            # 배치 단위로 데이터 복사
            offset = checkpoint.rows_processed if checkpoint else 0
            current_batch_size = self.batch_size

            while offset < total_rows:
                if not self.is_running:
                    break

                # 일시정지 확인
                self._check_pause()

                try:
                    # 데이터 복사
                    rows_copied = self._copy_batch(
                        source_conn, target_conn, partition_name, offset, current_batch_size
                    )

                    offset += rows_copied
                    self.total_rows_processed += rows_copied

                    # 진행 상황 업데이트
                    partition_progress = int((offset / total_rows) * 100)
                    total_progress = int(
                        (
                            (self.current_partition_index + partition_progress / 100)
                            / len(self.partitions)
                        )
                        * 100
                    )

                    self.progress.emit(
                        {
                            "total_progress": total_progress,
                            "current_progress": partition_progress,
                            "total_partitions": len(self.partitions),
                            "completed_partitions": self.current_partition_index,
                            "current_partition": partition_name,
                            "current_rows": offset,
                            "speed": self._calculate_speed(),
                        }
                    )

                    # 체크포인트 업데이트
                    if checkpoint:
                        self.checkpoint_manager.update_checkpoint_status(
                            checkpoint.id, "pending", offset
                        )

                    # 배치 크기 조정 (성공 시 증가)
                    if current_batch_size < self.max_batch_size:
                        current_batch_size = min(int(current_batch_size * 1.1), self.max_batch_size)

                except psycopg.errors.InsufficientResources:
                    # 메모리 부족 시 배치 크기 감소
                    current_batch_size = max(int(current_batch_size * 0.5), self.min_batch_size)
                    self.log.emit(f"메모리 부족, 배치 크기 조정: {current_batch_size:,}", "WARNING")
                    log_emitter.emit_log(
                        "WARNING", f"메모리 부족, 배치 크기 조정: {current_batch_size:,}"
                    )
                    continue

            # 파티션 완료
            if self.is_running and offset >= total_rows:
                if checkpoint:
                    self.checkpoint_manager.update_checkpoint_status(
                        checkpoint.id, "completed", total_rows
                    )
                self.log.emit(f"{partition_name} 완료 ({total_rows:,} rows)", "SUCCESS")
                log_emitter.emit_log("SUCCESS", f"{partition_name} 완료 ({total_rows:,} rows)")

        except Exception as e:
            if checkpoint:
                self.checkpoint_manager.update_checkpoint_status(
                    checkpoint.id, "failed", error_message=str(e)
                )
            raise

    def _prepare_target_table(self, conn: psycopg.Connection, partition_name: str):
        """대상 테이블 준비"""

        def confirm_truncate(partition_name: str, row_count: int) -> bool:
            """사용자에게 TRUNCATE 확인 요청"""
            self.log.emit(
                f"{partition_name} 테이블에 {row_count:,}개의 기존 데이터가 있습니다", "WARNING"
            )
            log_emitter.emit_log(
                "WARNING", f"{partition_name} 테이블에 {row_count:,}개의 기존 데이터가 있습니다"
            )
            self.truncate_requested.emit(partition_name, row_count)

            # 사용자 응답 대기
            self.truncate_permission = None
            while self.truncate_permission is None:
                if self.is_interrupted:
                    return False
                time.sleep(0.1)

            return self.truncate_permission

        # TableCreator를 사용하여 테이블 준비
        creator = TableCreator(self.source_conn, conn)
        try:
            created, row_count = creator.ensure_partition_ready(
                partition_name, truncate_mode="ask", confirm_callback=confirm_truncate
            )

            # 결과에 따른 로그 출력
            if created:
                self.log.emit(f"{partition_name} 테이블 생성 완료", "SUCCESS")
                log_emitter.emit_log("SUCCESS", f"{partition_name} 테이블 생성 완료")
            elif row_count > 0:
                self.log.emit(f"{partition_name} 테이블 데이터 삭제 완료", "SUCCESS")
                log_emitter.emit_log("SUCCESS", f"{partition_name} 테이블 데이터 삭제 완료")

        finally:
            # 권한 초기화
            self.truncate_permission = None

    def _copy_batch(
        self,
        source_conn: psycopg.Connection,
        target_conn: psycopg.Connection,
        partition_name: str,
        offset: int,
        limit: int,
    ) -> int:
        """배치 단위 데이터 복사"""
        rows_copied = 0

        try:
            with source_conn.cursor() as source_cur:
                # 데이터 조회
                select_sql = sql.SQL("""
                    SELECT * FROM {}
                    ORDER BY path_id, issued_date
                    LIMIT %s OFFSET %s
                """).format(sql.Identifier(partition_name))

                source_cur.execute(select_sql, (limit, offset))
                rows = source_cur.fetchall()
                rows_copied = len(rows)

                if rows_copied > 0:
                    # 대상에 삽입
                    with target_conn.cursor() as target_cur:
                        # 컬럼 정보 가져오기
                        target_cur.execute(
                            """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_name = %s
                            ORDER BY ordinal_position
                        """,
                            (partition_name,),
                        )
                        columns = [row[0] for row in target_cur.fetchall()]

                        # INSERT 문 생성
                        insert_sql = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                            sql.Identifier(partition_name),
                            sql.SQL(", ").join(map(sql.Identifier, columns)),
                            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                        )

                        # 배치 삽입
                        target_cur.executemany(insert_sql, rows)
                        target_conn.commit()

        except Exception as e:
            self.log.emit(f"배치 복사 오류: {str(e)}", "ERROR")
            log_emitter.emit_log("ERROR", f"배치 복사 오류: {str(e)}")
            raise

        return rows_copied

    def stop(self):
        """중지 (오버라이드)"""
        super().stop()
        self.is_interrupted = True
