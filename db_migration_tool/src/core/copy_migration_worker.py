"""
PostgreSQL COPY 명령 기반 고성능 마이그레이션 워커
"""
import time
import psycopg2
from psycopg2 import sql
from io import StringIO
from typing import Dict, Any, Optional, Tuple
from PySide6.QtCore import QThread, Signal
from datetime import datetime

from src.models.profile import ConnectionProfile
from src.models.history import HistoryManager, CheckpointManager
from src.core.table_creator import TableCreator
from src.database.postgres_utils import PostgresOptimizer
from src.core.performance_metrics import PerformanceMetrics
from src.utils.enhanced_logger import enhanced_logger, log_emitter


class CopyMigrationWorker(QThread):
    """COPY 명령을 사용한 고성능 마이그레이션 워커"""
    
    # 시그널 정의
    progress = Signal(dict)  # 진행 상황
    log = Signal(str, str)   # 메시지, 레벨
    error = Signal(str)      # 오류 메시지
    finished = Signal()      # 완료
    performance = Signal(dict)  # 성능 지표
    
    # 연결 상태 시그널
    connection_checking = Signal()  # 연결 확인 시작
    source_connection_status = Signal(bool, str)  # 연결 성공 여부, 메시지
    target_connection_status = Signal(bool, str)  # 연결 성공 여부, 메시지
    
    def __init__(self, profile: ConnectionProfile, partitions: list[str],
                 history_id: int, resume: bool = False):
        super().__init__()
        self.profile = profile
        self.partitions = partitions
        self.history_id = history_id
        self.resume = resume
        
        self.history_manager = HistoryManager()
        self.checkpoint_manager = CheckpointManager()
        self.performance_metrics = PerformanceMetrics()
        
        self.is_running = False
        self.is_paused = False
        self.current_partition_index = 0
        
        # psycopg2 연결 (COPY 명령용)
        self.source_conn = None
        self.target_conn = None
        
        # 성능 지표 업데이트 타이머
        self.last_metric_update = 0
        self.metric_update_interval = 1.0  # 1초마다 업데이트
        
    def run(self):
        """워커 실행"""
        self.is_running = True
        
        # 연결 확인만 수행하는 경우
        if hasattr(self, 'check_connections_only') and self.check_connections_only:
            self._check_connections()
            return
        
        # 세션 ID 생성
        session_id = enhanced_logger.generate_session_id()
        log_emitter.logger.set_session_id(session_id)
        
        try:
            # psycopg2 연결 생성 (COPY 명령용)
            self.log.emit("PostgreSQL 연결 생성 중...", "INFO")
            log_emitter.emit_log("INFO", "COPY 기반 마이그레이션 시작")
            
            self.source_conn = self._create_psycopg2_connection(self.profile.source_config)
            self.target_conn = self._create_psycopg2_connection(self.profile.target_config)
            
            # COPY 권한 확인
            self._check_copy_permissions()
            
            # 성능 지표 초기화
            self.performance_metrics.total_partitions = len(self.partitions)
            
            # 각 파티션 처리
            for i, partition in enumerate(self.partitions):
                if not self.is_running:
                    break
                    
                self.current_partition_index = i
                
                # 체크포인트 확인
                checkpoint = self._get_checkpoint(partition)
                
                if checkpoint and checkpoint.status == 'completed':
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
                    "SUCCESS"
                )
                log_emitter.emit_log("SUCCESS", "COPY 기반 마이그레이션이 정상적으로 완료되었습니다")
                self.finished.emit()
                
        except Exception as e:
            self.log.emit(f"마이그레이션 오류: {str(e)}", "ERROR")
            log_emitter.emit_log("ERROR", f"마이그레이션 오류: {str(e)}")
            self.error.emit(str(e))
            
    def _create_psycopg2_connection(self, config: Dict[str, Any]) -> psycopg2.extensions.connection:
        """psycopg2 연결 생성 (COPY 명령용)"""
        try:
            # PostgresOptimizer를 사용하여 최적화된 연결 생성
            conn = PostgresOptimizer.create_optimized_connection(config)
            self.log.emit("PostgreSQL 연결 생성 및 최적화 완료", "INFO")
            return conn
        except Exception as e:
            raise Exception(f"데이터베이스 연결 실패: {str(e)}")
            
    def _check_copy_permissions(self):
        """COPY 권한 확인"""
        # 소스 DB COPY TO 권한
        can_copy_from, error_msg = PostgresOptimizer.check_copy_permissions(
            self.source_conn, check_write=False
        )
        if not can_copy_from:
            raise PermissionError(f"소스 데이터베이스 COPY TO 권한 없음:\n{error_msg}")
            
        # 대상 DB COPY FROM 권한
        can_copy_to, error_msg = PostgresOptimizer.check_copy_permissions(
            self.target_conn, check_write=True
        )
        if not can_copy_to:
            raise PermissionError(f"대상 데이터베이스 COPY FROM 권한 없음:\n{error_msg}")
            
        self.log.emit("COPY 권한 확인 완료", "INFO")
        
    def _get_checkpoint(self, partition_name: str) -> Optional[Any]:
        """체크포인트 가져오기"""
        checkpoints = self.checkpoint_manager.get_checkpoints(self.history_id)
        return next((cp for cp in checkpoints if cp.partition_name == partition_name), None)
        
    def _migrate_partition_with_copy(self, partition_name: str, checkpoint: Any):
        """COPY 명령을 사용한 파티션 마이그레이션"""
        self.log.emit(f"{partition_name} COPY 마이그레이션 시작", "INFO")
        log_emitter.emit_log("INFO", f"{partition_name} COPY 마이그레이션 시작")
        
        try:
            # 테이블 크기 추정
            table_info = PostgresOptimizer.estimate_table_size(self.source_conn, partition_name)
            
            # 테이블이 존재하지 않는 경우
            if not table_info.get('exists', True):
                self.log.emit(f"{partition_name} - 소스 테이블이 존재하지 않음, 건너뛰기", "WARNING")
                log_emitter.emit_log("WARNING", f"{partition_name} - 소스 테이블이 존재하지 않음")
                self.performance_metrics.completed_partitions += 1
                self._update_checkpoint_completed(checkpoint, 0)
                return
            
            total_rows = table_info['row_count']
            total_mb = table_info['total_size_mb']
            
            if total_rows == 0:
                self.log.emit(f"{partition_name} - 데이터 없음", "WARNING")
                self._update_checkpoint_completed(checkpoint, 0)
                self.performance_metrics.completed_partitions += 1
                return
                
            self.log.emit(
                f"{partition_name} - {total_rows:,}개 행, {total_mb:.1f}MB",
                "INFO"
            )
            
            # 성능 지표 시작
            self.performance_metrics.start_partition(partition_name, total_rows)
            
            # 대상 테이블 준비
            self._prepare_target_table(partition_name)
            
            # COPY 실행
            start_time = time.time()
            
            # StringIO 버퍼 사용
            buffer = StringIO()
            
            # 초기 성능 지표 전송
            self._emit_performance_metrics()
            
            # 재개 지점 결정
            last_path_id = None
            last_issued_date = None
            if checkpoint and checkpoint.rows_processed > 0:
                # 체크포인트에서 마지막 처리 위치 로드
                checkpoint_data = checkpoint.error_message  # JSON으로 저장된 체크포인트 데이터
                if checkpoint_data:
                    import json
                    try:
                        data = json.loads(checkpoint_data)
                        last_path_id = data.get('last_path_id')
                        last_issued_date = data.get('last_issued_date')
                    except:
                        pass
            
            # COPY TO 실행
            with self.source_conn.cursor() as source_cursor:
                # WHERE 절 구성
                if last_path_id is not None:
                    # 재개 시 WHERE 절 포함
                    copy_to_query = f"""
                        COPY (
                            SELECT path_id, issued_date, changed_value, 
                                   COALESCE(connection_status::text, 'true') as connection_status
                            FROM {partition_name}
                            WHERE path_id > {last_path_id} OR (path_id = {last_path_id} AND issued_date > {last_issued_date})
                            ORDER BY path_id, issued_date
                        ) TO STDOUT WITH (FORMAT CSV, HEADER FALSE, NULL 'NULL')
                    """
                else:
                    # 전체 테이블 복사
                    copy_to_query = f"""
                        COPY (
                            SELECT path_id, issued_date, changed_value,
                                   COALESCE(connection_status::text, 'true') as connection_status
                            FROM {partition_name}
                            ORDER BY path_id, issued_date
                        ) TO STDOUT WITH (FORMAT CSV, HEADER FALSE, NULL 'NULL')
                    """
                
                # 버퍼로 데이터 복사
                source_cursor.copy_expert(copy_to_query, buffer)
                
            # 버퍼 크기 확인
            buffer_size = buffer.tell()
            buffer.seek(0)
            
            # COPY FROM 실행
            with self.target_conn.cursor() as target_cursor:
                copy_from_query = f"""
                    COPY {partition_name} (path_id, issued_date, changed_value, connection_status)
                    FROM STDIN WITH (FORMAT CSV, HEADER FALSE, NULL 'NULL')
                """
                target_cursor.copy_expert(copy_from_query, buffer)
                
                # 실제 복사된 행 수 확인
                target_cursor.execute(
                    sql.SQL("SELECT COUNT(*) FROM {}").format(
                        sql.Identifier(partition_name)
                    )
                )
                copied_rows = target_cursor.fetchone()[0]
                
            # 커밋
            self.target_conn.commit()
            
            # 소요 시간 계산
            copy_duration = time.time() - start_time
            rows_per_sec = copied_rows / copy_duration if copy_duration > 0 else 0
            mb_per_sec = (buffer_size / (1024 * 1024)) / copy_duration if copy_duration > 0 else 0
            
            # 성능 지표 업데이트
            self.performance_metrics.update(copied_rows, buffer_size)
            self.performance_metrics.complete_partition()
            
            # 성능 지표 전송
            self._emit_performance_metrics()
            
            # 체크포인트 업데이트
            self._update_checkpoint_completed(checkpoint, copied_rows)
            
            # 로그 출력
            self.log.emit(
                f"{partition_name} 완료: {copied_rows:,}개 행, "
                f"{copy_duration:.1f}초, {rows_per_sec:,.0f} rows/sec",
                "SUCCESS"
            )
            log_emitter.emit_log(
                "SUCCESS",
                f"{partition_name} COPY 완료: {copied_rows:,}개 행, {rows_per_sec:,.0f} rows/sec"
            )
            
            # 버퍼 정리
            buffer.close()
            
        except Exception as e:
            if checkpoint:
                self.checkpoint_manager.update_checkpoint_status(
                    checkpoint.id, 'failed', error_message=str(e)
                )
            raise Exception(f"{partition_name} COPY 실패: {str(e)}")
            
    def _prepare_target_table(self, partition_name: str):
        """대상 테이블 준비"""
        with self.target_conn.cursor() as cursor:
            # 테이블 존재 확인
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_name = %s
                )
            """, (partition_name,))
            
            table_exists = cursor.fetchone()[0]
            
            if not table_exists:
                # 테이블 생성
                self.log.emit(f"{partition_name} 테이블 생성 중...", "INFO")
                creator = TableCreator(self.source_conn, self.target_conn)
                creator.create_partition_table(partition_name)
                self.log.emit(f"{partition_name} 테이블 생성 완료", "SUCCESS")
                log_emitter.emit_log("SUCCESS", f"{partition_name} 테이블 생성 완료")
            else:
                # 기존 데이터 삭제 (TRUNCATE)
                self.log.emit(f"{partition_name} 기존 데이터 삭제 중...", "INFO")
                cursor.execute(
                    sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY").format(
                        sql.Identifier(partition_name)
                    )
                )
                self.target_conn.commit()
                
    def _update_checkpoint_completed(self, checkpoint: Any, rows: int):
        """체크포인트 완료 업데이트"""
        if checkpoint:
            # COPY 방식 관련 정보 추가
            checkpoint.copy_method = 'COPY'
            checkpoint.bytes_transferred = self.performance_metrics.total_bytes
            self.checkpoint_manager.update_checkpoint_status(
                checkpoint.id, 'completed', rows
            )
            
    def _emit_performance_metrics(self):
        """성능 지표 시그널 전송"""
        current_time = time.time()
        if current_time - self.last_metric_update >= self.metric_update_interval:
            stats = self.performance_metrics.get_stats()
            self.performance.emit(stats)
            
            # 진행 상황도 함께 업데이트
            self.progress.emit({
                'total_progress': int(stats['total_progress']),
                'current_progress': int(stats['partition_progress']),
                'total_partitions': stats['total_partitions'],
                'completed_partitions': stats['completed_partitions'],
                'current_partition': stats['current_partition'],
                'current_rows': stats['current_partition_rows'],
                'speed': stats['instant_rows_per_sec']
            })
            
            self.last_metric_update = current_time
            
    def pause(self):
        """일시정지"""
        self.is_paused = True
        self.log.emit("마이그레이션 일시정지", "INFO")
        
    def resume(self):
        """재개"""
        self.is_paused = False
        self.log.emit("마이그레이션 재개", "INFO")
        
    def stop(self):
        """중지"""
        self.is_running = False
        self.is_paused = False
        self.log.emit("마이그레이션 중지 요청", "WARNING")
        
    def get_stats(self) -> Dict[str, Any]:
        """통계 정보 반환 (MigrationDialog 호환성)"""
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