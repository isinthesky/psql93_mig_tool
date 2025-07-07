"""
마이그레이션 워커 스레드
"""
import time
from datetime import datetime
from typing import List, Dict, Any
from PySide6.QtCore import QThread, Signal
import psycopg
from psycopg import sql

from src.models.profile import ConnectionProfile
from src.models.history import HistoryManager, CheckpointManager
from src.core.table_creator import TableCreator
from src.utils.enhanced_logger import enhanced_logger, log_emitter


class MigrationWorker(QThread):
    """마이그레이션 작업을 수행하는 워커 스레드
    
    Note: 이 클래스는 레거시 INSERT 기반 마이그레이션을 위해 유지됩니다.
    고성능 마이그레이션을 위해서는 CopyMigrationWorker를 사용하세요.
    """
    
    # 시그널 정의
    progress = Signal(dict)  # 진행 상황
    log = Signal(str, str)   # 메시지, 레벨
    error = Signal(str)      # 오류 메시지
    finished = Signal()      # 완료
    truncate_requested = Signal(str, int)  # 테이블명, 행 수
    
    def __init__(self, profile: ConnectionProfile, partitions: List[str],
                 history_id: int, resume: bool = False):
        super().__init__()
        self.profile = profile
        self.partitions = partitions
        self.history_id = history_id
        self.resume = resume
        
        self.history_manager = HistoryManager()
        self.checkpoint_manager = CheckpointManager()
        
        self.is_running = False
        self.is_paused = False
        self.is_interrupted = False
        self.current_partition_index = 0
        self.total_rows_processed = 0
        self.start_time = None
        self.truncate_permission = None  # None, True, False
        
        # 배치 크기 설정
        self.batch_size = 100000  # 초기 배치 크기
        self.min_batch_size = 1000
        self.max_batch_size = 500000
        
    def run(self):
        """워커 실행"""
        self.is_running = True
        self.start_time = time.time()
        
        # 세션 ID 생성
        session_id = enhanced_logger.generate_session_id()
        
        try:
            # 소스 및 대상 연결 생성
            self.source_conn = self._create_connection(self.profile.source_config)
            target_conn = self._create_connection(self.profile.target_config)
            
            # 각 파티션 처리
            for i, partition in enumerate(self.partitions):
                if not self.is_running:
                    break
                    
                self.current_partition_index = i
                
                # 체크포인트 확인
                checkpoints = self.checkpoint_manager.get_checkpoints(self.history_id)
                checkpoint = next((cp for cp in checkpoints if cp.partition_name == partition), None)
                
                if checkpoint and checkpoint.status == 'completed':
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
                self.finished.emit()
            
        except Exception as e:
            log_emitter.emit_log("ERROR", f"마이그레이션 오류: {str(e)}")
            self.error.emit(str(e))
            
    def _create_connection(self, config: Dict[str, Any]) -> psycopg.Connection:
        """데이터베이스 연결 생성"""
        conn_params = {
            'host': config['host'],
            'port': config['port'],
            'dbname': config['database'],
            'user': config['username'],
            'password': config['password'],
        }
        
        if config.get('ssl'):
            conn_params['sslmode'] = 'require'
            
        return psycopg.connect(**conn_params)
        
    def _migrate_partition(self, source_conn: psycopg.Connection,
                          target_conn: psycopg.Connection,
                          partition_name: str,
                          checkpoint: Any):
        """단일 파티션 마이그레이션"""
        self.log.emit(f"{partition_name} 마이그레이션 시작", "INFO")
        log_emitter.emit_log("INFO", f"{partition_name} 마이그레이션 시작")
        
        try:
            # 소스에서 총 행 수 확인
            with source_conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT COUNT(*) FROM {}").format(
                        sql.Identifier(partition_name)
                    )
                )
                total_rows = cur.fetchone()[0]
                
            if total_rows == 0:
                self.log.emit(f"{partition_name} - 데이터 없음", "WARNING")
                log_emitter.emit_log("WARNING", f"{partition_name} - 데이터 없음")
                if checkpoint:
                    self.checkpoint_manager.update_checkpoint_status(
                        checkpoint.id, 'completed', 0
                    )
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
                while self.is_paused and self.is_running:
                    time.sleep(0.1)
                    
                try:
                    # 데이터 복사
                    rows_copied = self._copy_batch(
                        source_conn, target_conn, partition_name,
                        offset, current_batch_size
                    )
                    
                    offset += rows_copied
                    self.total_rows_processed += rows_copied
                    
                    # 진행 상황 업데이트
                    partition_progress = int((offset / total_rows) * 100)
                    total_progress = int(
                        ((self.current_partition_index + partition_progress / 100) 
                         / len(self.partitions)) * 100
                    )
                    
                    self.progress.emit({
                        'total_progress': total_progress,
                        'current_progress': partition_progress,
                        'total_partitions': len(self.partitions),
                        'completed_partitions': self.current_partition_index,
                        'current_partition': partition_name,
                        'current_rows': offset,
                        'speed': self._calculate_speed()
                    })
                    
                    # 체크포인트 업데이트
                    if checkpoint:
                        self.checkpoint_manager.update_checkpoint_status(
                            checkpoint.id, 'pending', offset
                        )
                        
                    # 배치 크기 조정 (성공 시 증가)
                    if current_batch_size < self.max_batch_size:
                        current_batch_size = min(
                            int(current_batch_size * 1.1),
                            self.max_batch_size
                        )
                        
                except psycopg.errors.InsufficientResources:
                    # 메모리 부족 시 배치 크기 감소
                    current_batch_size = max(
                        int(current_batch_size * 0.5),
                        self.min_batch_size
                    )
                    self.log.emit(
                        f"메모리 부족, 배치 크기 조정: {current_batch_size:,}",
                        "WARNING"
                    )
                    log_emitter.emit_log("WARNING", f"메모리 부족, 배치 크기 조정: {current_batch_size:,}")
                    continue
                    
            # 파티션 완료
            if self.is_running and offset >= total_rows:
                if checkpoint:
                    self.checkpoint_manager.update_checkpoint_status(
                        checkpoint.id, 'completed', total_rows
                    )
                self.log.emit(f"{partition_name} 완료 ({total_rows:,} rows)", "SUCCESS")
                log_emitter.emit_log("SUCCESS", f"{partition_name} 완료 ({total_rows:,} rows)")
                
        except Exception as e:
            if checkpoint:
                self.checkpoint_manager.update_checkpoint_status(
                    checkpoint.id, 'failed', error_message=str(e)
                )
            raise
            
    def _prepare_target_table(self, conn: psycopg.Connection, partition_name: str):
        """대상 테이블 준비"""
        with conn.cursor() as cur:
            # 테이블 존재 확인
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_name = %s
                )
            """, (partition_name,))
            
            table_exists = cur.fetchone()[0]
            
            if not table_exists:
                # 테이블이 없으면 생성
                self.log.emit(f"{partition_name} 테이블이 대상에 없어 생성합니다", "INFO")
                log_emitter.emit_log("INFO", f"{partition_name} 테이블이 대상에 없어 생성합니다")
                
                # 테이블 생성기 사용
                creator = TableCreator(self.source_conn, conn)
                creator.create_partition_table(partition_name)
                
                self.log.emit(f"{partition_name} 테이블 생성 완료", "SUCCESS")
                log_emitter.emit_log("SUCCESS", f"{partition_name} 테이블 생성 완료")
            else:
                # 기존 데이터가 있는지 확인
                cur.execute(
                    sql.SQL("SELECT COUNT(*) FROM {}").format(
                        sql.Identifier(partition_name)
                    )
                )
                row_count = cur.fetchone()[0]
                
                if row_count > 0:
                    self.log.emit(f"{partition_name} 테이블에 {row_count:,}개의 기존 데이터가 있습니다", "WARNING")
                    log_emitter.emit_log("WARNING", f"{partition_name} 테이블에 {row_count:,}개의 기존 데이터가 있습니다")
                    self.truncate_requested.emit(partition_name, row_count)
                    
                    # 사용자 응답 대기
                    while self.truncate_permission is None:
                        if self.is_interrupted:
                            raise Exception("사용자가 작업을 취소했습니다")
                        time.sleep(0.1)
                    
                    if self.truncate_permission:
                        self.log.emit(f"{partition_name} 테이블 데이터 삭제 중...", "INFO")
                        log_emitter.emit_log("INFO", f"{partition_name} 테이블 데이터 삭제 중...")
                        cur.execute(
                            sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY").format(
                                sql.Identifier(partition_name)
                            )
                        )
                        conn.commit()
                        self.log.emit(f"{partition_name} 테이블 데이터 삭제 완료", "SUCCESS")
                        log_emitter.emit_log("SUCCESS", f"{partition_name} 테이블 데이터 삭제 완료")
                    else:
                        raise Exception(f"{partition_name} 테이블의 기존 데이터 처리가 취소되었습니다")
                    
                    # 권한 초기화
                    self.truncate_permission = None
            
    def _copy_batch(self, source_conn: psycopg.Connection,
                   target_conn: psycopg.Connection,
                   partition_name: str,
                   offset: int, limit: int) -> int:
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
                        target_cur.execute("""
                            SELECT column_name 
                            FROM information_schema.columns 
                            WHERE table_name = %s 
                            ORDER BY ordinal_position
                        """, (partition_name,))
                        columns = [row[0] for row in target_cur.fetchall()]
                        
                        # INSERT 문 생성
                        insert_sql = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                            sql.Identifier(partition_name),
                            sql.SQL(', ').join(map(sql.Identifier, columns)),
                            sql.SQL(', ').join(sql.Placeholder() for _ in columns)
                        )
                        
                        # 배치 삽입
                        target_cur.executemany(insert_sql, rows)
                        target_conn.commit()
                        
        except Exception as e:
            self.log.emit(f"배치 복사 오류: {str(e)}", "ERROR")
            log_emitter.emit_log("ERROR", f"배치 복사 오류: {str(e)}")
            raise
            
        return rows_copied
                
    def _calculate_speed(self) -> int:
        """처리 속도 계산 (rows/sec)"""
        if not self.start_time or self.total_rows_processed == 0:
            return 0
            
        elapsed = time.time() - self.start_time
        if elapsed > 0:
            return int(self.total_rows_processed / elapsed)
        return 0
        
    def get_stats(self) -> Dict[str, Any]:
        """통계 정보 반환"""
        elapsed = time.time() - self.start_time if self.start_time else 0
        speed = self._calculate_speed()
        
        # 남은 행 수 추정
        remaining_partitions = len(self.partitions) - self.current_partition_index - 1
        estimated_remaining_rows = remaining_partitions * 4000000  # 하루 400만 rows
        
        # 예상 완료 시간
        eta_seconds = 0
        if speed > 0:
            eta_seconds = estimated_remaining_rows / speed
            
        return {
            'elapsed_seconds': elapsed,
            'total_rows_processed': self.total_rows_processed,
            'speed': speed,
            'eta_seconds': eta_seconds
        }
        
    def pause(self):
        """일시정지"""
        self.is_paused = True
        
    def resume(self):
        """재개"""
        self.is_paused = False
        
    def stop(self):
        """중지"""
        self.is_running = False
        self.is_paused = False
        self.is_interrupted = True