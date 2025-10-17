"""
마이그레이션 이력 모델 및 관리자
"""
from datetime import datetime
from typing import List, Optional, Dict, Any
from src.database.local_db import get_db, MigrationHistory, Checkpoint
from src.database.repository import HistoryRepository, CheckpointRepository


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
    """이력 관리자 클래스 (HistoryRepository 활용)"""

    def __init__(self):
        self.repo = HistoryRepository()

    def create_history(self, profile_id: int, start_date: str,
                      end_date: str, source_status: str = None,
                      target_status: str = None) -> MigrationHistoryItem:
        """새 이력 생성"""
        db_history = self.repo.create(
            profile_id=profile_id,
            start_date=start_date,
            end_date=end_date,
            started_at=datetime.now(),
            status="running",
            source_connection_status=source_status,
            target_connection_status=target_status,
            connection_check_time=datetime.now() if source_status or target_status else None
        )
        return MigrationHistoryItem.from_db_model(db_history)

    def get_history(self, history_id: int) -> Optional[MigrationHistoryItem]:
        """이력 조회"""
        db_history = self.repo.get_by_id(history_id)
        if db_history:
            return MigrationHistoryItem.from_db_model(db_history)
        return None

    def get_all_history(self) -> List[MigrationHistoryItem]:
        """모든 이력 조회 (최신순)"""
        db_histories = self.repo.get_all_desc()
        return [
            MigrationHistoryItem.from_db_model(h)
            for h in db_histories
        ]

    def update_history_status(self, history_id: int, status: str,
                            processed_rows: int = None) -> bool:
        """이력 상태 업데이트"""
        updates = {'status': status}
        if processed_rows is not None:
            updates['processed_rows'] = processed_rows

        if status in ['completed', 'failed', 'cancelled']:
            updates['completed_at'] = datetime.now()

        return self.repo.update_by_id(history_id, **updates)

    def get_incomplete_history(self, profile_id: int) -> Optional[MigrationHistoryItem]:
        """미완료 이력 조회"""
        db_history = self.repo.get_incomplete_by_profile(profile_id)
        if db_history:
            return MigrationHistoryItem.from_db_model(db_history)
        return None


class CheckpointManager:
    """체크포인트 관리자 클래스 (CheckpointRepository 활용)"""

    def __init__(self):
        self.repo = CheckpointRepository()

    def create_checkpoint(self, history_id: int, partition_name: str) -> CheckpointItem:
        """체크포인트 생성"""
        db_checkpoint = self.repo.create(
            history_id=history_id,
            partition_name=partition_name,
            status="pending"
        )
        return CheckpointItem.from_db_model(db_checkpoint)

    def get_checkpoints(self, history_id: int) -> List[CheckpointItem]:
        """이력별 체크포인트 조회"""
        db_checkpoints = self.repo.get_by_history(history_id)
        return [
            CheckpointItem.from_db_model(c)
            for c in db_checkpoints
        ]

    def update_checkpoint_status(self, checkpoint_id: int, status: str,
                               rows_processed: int = None,
                               error_message: str = None) -> bool:
        """체크포인트 상태 업데이트"""
        updates = {'status': status}
        if rows_processed is not None:
            updates['rows_processed'] = rows_processed
        if error_message is not None:
            updates['error_message'] = error_message

        return self.repo.update_by_id(checkpoint_id, **updates)

    def get_pending_checkpoints(self, history_id: int) -> List[CheckpointItem]:
        """미완료 체크포인트 조회"""
        db_checkpoints = self.repo.get_pending_by_history(history_id)
        return [
            CheckpointItem.from_db_model(c)
            for c in db_checkpoints
        ]