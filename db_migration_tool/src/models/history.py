"""
마이그레이션 이력 모델 및 관리자
"""
from datetime import datetime
from typing import List, Optional, Dict, Any
from src.database.local_db import get_db, MigrationHistory, Checkpoint


class MigrationHistoryItem:
    """마이그레이션 이력 데이터 클래스"""
    
    def __init__(self, id: Optional[int] = None, profile_id: int = 0,
                 start_date: str = "", end_date: str = "",
                 started_at: Optional[datetime] = None,
                 completed_at: Optional[datetime] = None,
                 status: str = "pending", total_rows: int = 0,
                 processed_rows: int = 0):
        self.id = id
        self.profile_id = profile_id
        self.start_date = start_date
        self.end_date = end_date
        self.started_at = started_at
        self.completed_at = completed_at
        self.status = status
        self.total_rows = total_rows
        self.processed_rows = processed_rows
        
    @classmethod
    def from_db_model(cls, db_history: MigrationHistory) -> 'MigrationHistoryItem':
        """DB 모델에서 생성"""
        return cls(
            id=db_history.id,
            profile_id=db_history.profile_id,
            start_date=db_history.start_date,
            end_date=db_history.end_date,
            started_at=db_history.started_at,
            completed_at=db_history.completed_at,
            status=db_history.status,
            total_rows=db_history.total_rows or 0,
            processed_rows=db_history.processed_rows or 0
        )


class CheckpointItem:
    """체크포인트 데이터 클래스"""
    
    def __init__(self, id: Optional[int] = None, history_id: int = 0,
                 partition_name: str = "", status: str = "pending",
                 rows_processed: int = 0, error_message: str = ""):
        self.id = id
        self.history_id = history_id
        self.partition_name = partition_name
        self.status = status
        self.rows_processed = rows_processed
        self.error_message = error_message
        
    @classmethod
    def from_db_model(cls, db_checkpoint: Checkpoint) -> 'CheckpointItem':
        """DB 모델에서 생성"""
        return cls(
            id=db_checkpoint.id,
            history_id=db_checkpoint.history_id,
            partition_name=db_checkpoint.partition_name,
            status=db_checkpoint.status,
            rows_processed=db_checkpoint.rows_processed or 0,
            error_message=db_checkpoint.error_message or ""
        )


class HistoryManager:
    """이력 관리자 클래스"""
    
    def __init__(self):
        self.db = get_db()
        
    def create_history(self, profile_id: int, start_date: str, 
                      end_date: str, source_status: str = None,
                      target_status: str = None) -> MigrationHistoryItem:
        """새 이력 생성"""
        session = self.db.get_session()
        try:
            db_history = MigrationHistory(
                profile_id=profile_id,
                start_date=start_date,
                end_date=end_date,
                started_at=datetime.now(),
                status="running",
                source_connection_status=source_status,
                target_connection_status=target_status,
                connection_check_time=datetime.now() if source_status or target_status else None
            )
            
            session.add(db_history)
            session.commit()
            
            return MigrationHistoryItem.from_db_model(db_history)
            
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
            
    def get_history(self, history_id: int) -> Optional[MigrationHistoryItem]:
        """이력 조회"""
        session = self.db.get_session()
        try:
            db_history = session.query(MigrationHistory).filter_by(id=history_id).first()
            if db_history:
                return MigrationHistoryItem.from_db_model(db_history)
            return None
        finally:
            session.close()
            
    def get_all_history(self) -> List[MigrationHistoryItem]:
        """모든 이력 조회"""
        session = self.db.get_session()
        try:
            db_histories = session.query(MigrationHistory)\
                .order_by(MigrationHistory.started_at.desc()).all()
            return [
                MigrationHistoryItem.from_db_model(h) 
                for h in db_histories
            ]
        finally:
            session.close()
            
    def update_history_status(self, history_id: int, status: str, 
                            processed_rows: int = None) -> bool:
        """이력 상태 업데이트"""
        session = self.db.get_session()
        try:
            db_history = session.query(MigrationHistory).filter_by(id=history_id).first()
            if not db_history:
                return False
                
            db_history.status = status
            if processed_rows is not None:
                db_history.processed_rows = processed_rows
                
            if status in ['completed', 'failed', 'cancelled']:
                db_history.completed_at = datetime.now()
                
            session.commit()
            return True
            
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
            
    def get_incomplete_history(self, profile_id: int) -> Optional[MigrationHistoryItem]:
        """미완료 이력 조회"""
        session = self.db.get_session()
        try:
            db_history = session.query(MigrationHistory)\
                .filter_by(profile_id=profile_id)\
                .filter(MigrationHistory.status.in_(['running', 'failed']))\
                .order_by(MigrationHistory.started_at.desc())\
                .first()
                
            if db_history:
                return MigrationHistoryItem.from_db_model(db_history)
            return None
        finally:
            session.close()


class CheckpointManager:
    """체크포인트 관리자 클래스"""
    
    def __init__(self):
        self.db = get_db()
        
    def create_checkpoint(self, history_id: int, partition_name: str) -> CheckpointItem:
        """체크포인트 생성"""
        session = self.db.get_session()
        try:
            db_checkpoint = Checkpoint(
                history_id=history_id,
                partition_name=partition_name,
                status="pending"
            )
            
            session.add(db_checkpoint)
            session.commit()
            
            return CheckpointItem.from_db_model(db_checkpoint)
            
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
            
    def get_checkpoints(self, history_id: int) -> List[CheckpointItem]:
        """이력별 체크포인트 조회"""
        session = self.db.get_session()
        try:
            db_checkpoints = session.query(Checkpoint)\
                .filter_by(history_id=history_id)\
                .order_by(Checkpoint.partition_name).all()
            return [
                CheckpointItem.from_db_model(c) 
                for c in db_checkpoints
            ]
        finally:
            session.close()
            
    def update_checkpoint_status(self, checkpoint_id: int, status: str,
                               rows_processed: int = None, 
                               error_message: str = None) -> bool:
        """체크포인트 상태 업데이트"""
        session = self.db.get_session()
        try:
            db_checkpoint = session.query(Checkpoint).filter_by(id=checkpoint_id).first()
            if not db_checkpoint:
                return False
                
            db_checkpoint.status = status
            if rows_processed is not None:
                db_checkpoint.rows_processed = rows_processed
            if error_message is not None:
                db_checkpoint.error_message = error_message
                
            session.commit()
            return True
            
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
            
    def get_pending_checkpoints(self, history_id: int) -> List[CheckpointItem]:
        """미완료 체크포인트 조회"""
        session = self.db.get_session()
        try:
            db_checkpoints = session.query(Checkpoint)\
                .filter_by(history_id=history_id)\
                .filter(Checkpoint.status != 'completed')\
                .order_by(Checkpoint.partition_name).all()
            return [
                CheckpointItem.from_db_model(c) 
                for c in db_checkpoints
            ]
        finally:
            session.close()