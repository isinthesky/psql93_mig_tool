"""
마이그레이션 이력 모델 및 관리자
"""

from datetime import datetime
from typing import Any

from src.database.local_db import Checkpoint, MigrationHistory
from src.database.repository import CheckpointRepository, HistoryRepository


class MigrationHistoryItem:
    """마이그레이션 이력 데이터 클래스"""

    def __init__(
        self,
        id: int | None = None,
        profile_id: int = 0,
        start_date: str = "",
        end_date: str = "",
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        status: str = "pending",
        total_rows: int = 0,
        processed_rows: int = 0,
    ):
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
    def from_db_model(cls, db_history: MigrationHistory) -> "MigrationHistoryItem":
        """DB 모델에서 생성"""
        return cls(
            id=db_history.id,
            profile_id=db_history.profile_id,
            start_date=db_history.start_date or "",
            end_date=db_history.end_date or "",
            started_at=db_history.started_at,
            completed_at=db_history.completed_at,
            status=db_history.status or "pending",
            total_rows=db_history.total_rows or 0,
            processed_rows=db_history.processed_rows or 0,
        )


class CheckpointItem:
    """체크포인트 데이터 클래스"""

    def __init__(
        self,
        id: int | None = None,
        history_id: int = 0,
        partition_name: str = "",
        status: str = "pending",
        rows_processed: int = 0,
        error_message: str = "",
        last_path_id: int | None = None,
        last_issued_date: int | None = None,
        last_issued_date_text: str | None = None,
        copy_method: str = "INSERT",
        bytes_transferred: int = 0,
    ):
        self.id = id
        self.history_id = history_id
        self.partition_name = partition_name
        self.status = status
        self.rows_processed = rows_processed
        self.error_message = error_message
        self.last_path_id = last_path_id
        self.last_issued_date = last_issued_date
        self.last_issued_date_text = last_issued_date_text
        self.copy_method = copy_method
        self.bytes_transferred = bytes_transferred

    @classmethod
    def from_db_model(cls, db_checkpoint: Checkpoint) -> "CheckpointItem":
        """DB 모델에서 생성"""
        return cls(
            id=db_checkpoint.id,
            history_id=db_checkpoint.history_id,
            partition_name=db_checkpoint.partition_name,
            status=db_checkpoint.status or "pending",
            rows_processed=db_checkpoint.rows_processed or 0,
            error_message=db_checkpoint.error_message or "",
            last_path_id=db_checkpoint.last_path_id,
            last_issued_date=db_checkpoint.last_issued_date,
            last_issued_date_text=getattr(db_checkpoint, "last_issued_date_text", None),
            copy_method=db_checkpoint.copy_method or "INSERT",
            bytes_transferred=db_checkpoint.bytes_transferred or 0,
        )


class HistoryManager:
    """이력 관리자 클래스 (HistoryRepository 활용)"""

    def __init__(self):
        self.repo = HistoryRepository()

    def create_history(
        self,
        profile_id: int,
        start_date: str,
        end_date: str,
        source_status: str | None = None,
        target_status: str | None = None,
        total_rows: int = 0,
    ) -> MigrationHistoryItem:
        """새 이력 생성

        Args:
            total_rows: 이 작업이 옮기기로 한 전체 행 수. 실행 시작 시점의
                범위를 기록해 둬야 나중에 "어디까지 갔나"를 답할 수 있다.
                PostgreSQL 소스라면 플래너 통계 기반 추정치다.
        """
        db_history = self.repo.create(
            profile_id=profile_id,
            start_date=start_date,
            end_date=end_date,
            started_at=datetime.now(),
            status="running",
            total_rows=total_rows,
            source_connection_status=source_status,
            target_connection_status=target_status,
            connection_check_time=datetime.now() if source_status or target_status else None,
        )
        return MigrationHistoryItem.from_db_model(db_history)

    def get_history(self, history_id: int) -> MigrationHistoryItem | None:
        """이력 조회"""
        db_history = self.repo.get_by_id(history_id)
        if db_history:
            return MigrationHistoryItem.from_db_model(db_history)
        return None

    def get_all_history(self) -> list[MigrationHistoryItem]:
        """모든 이력 조회 (최신순)"""
        db_histories = self.repo.get_all_desc()
        return [MigrationHistoryItem.from_db_model(h) for h in db_histories]

    def update_history_status(
        self,
        history_id: int,
        status: str,
        processed_rows: int | None = None,
        total_rows: int | None = None,
    ) -> bool:
        """이력 상태 업데이트

        Args:
            total_rows: 전체 행 수를 나중에 바로잡을 때만 넘긴다. 실행 시점의
                값은 추정치이므로, 실제로 다 옮기고 나면 정정할 수 있다.
        """
        updates: dict[str, Any] = {"status": status}
        if processed_rows is not None:
            updates["processed_rows"] = processed_rows
        if total_rows is not None:
            updates["total_rows"] = total_rows

        if status in ["completed", "failed", "cancelled"]:
            updates["completed_at"] = datetime.now()

        return self.repo.update_by_id(history_id, **updates)

    def get_incomplete_history(self, profile_id: int) -> MigrationHistoryItem | None:
        """미완료 이력 조회"""
        db_history = self.repo.get_incomplete_by_profile(profile_id)
        if db_history:
            return MigrationHistoryItem.from_db_model(db_history)
        return None

    def get_completed_histories(self, profile_id: int) -> list[MigrationHistoryItem]:
        """프로필의 완료된 이력 목록 조회"""
        db_histories = self.repo.get_completed_by_profile(profile_id)
        return [MigrationHistoryItem.from_db_model(h) for h in db_histories]


class CheckpointManager:
    """체크포인트 관리자 클래스 (CheckpointRepository 활용)"""

    def __init__(self):
        self.repo = CheckpointRepository()

    def create_checkpoint(self, history_id: int, partition_name: str) -> CheckpointItem:
        """체크포인트 생성"""
        db_checkpoint = self.repo.create(
            history_id=history_id, partition_name=partition_name, status="pending"
        )
        return CheckpointItem.from_db_model(db_checkpoint)

    def get_checkpoints(self, history_id: int) -> list[CheckpointItem]:
        """이력별 체크포인트 조회"""
        db_checkpoints = self.repo.get_by_history(history_id)
        return [CheckpointItem.from_db_model(c) for c in db_checkpoints]

    def update_checkpoint_status(
        self,
        checkpoint_id: int,
        status: str,
        rows_processed: int | None = None,
        error_message: str | None = None,
        last_path_id: int | None = None,
        last_issued_date: int | None = None,
        last_issued_date_text: str | None = None,
        copy_method: str | None = None,
        bytes_transferred: int | None = None,
    ) -> bool:
        """체크포인트 상태 업데이트"""
        updates: dict[str, Any] = {"status": status}
        if rows_processed is not None:
            updates["rows_processed"] = rows_processed
        if error_message is not None:
            updates["error_message"] = error_message
        if last_path_id is not None:
            updates["last_path_id"] = last_path_id
        if last_issued_date is not None:
            updates["last_issued_date"] = last_issued_date
        if last_issued_date_text is not None:
            updates["last_issued_date_text"] = last_issued_date_text
        if copy_method is not None:
            updates["copy_method"] = copy_method
        if bytes_transferred is not None:
            updates["bytes_transferred"] = bytes_transferred

        return self.repo.update_by_id(checkpoint_id, **updates)

    def get_processed_rows(self, history_id: int) -> int:
        """이력의 누적 처리 행 수(실행 횟수와 무관).

        워커의 성능 카운터는 실행 1회분만 센다. 재개 후 그 값을 이력에
        쓰면 진행량이 뒤로 간다(70만 처리 후 중단 → 30만 더 처리하고 완료 →
        이력에는 30만). 체크포인트는 실행을 넘어 남으므로 여기서 합산한다.
        """
        return self.repo.sum_rows_processed(history_id)

    def get_pending_checkpoints(self, history_id: int) -> list[CheckpointItem]:
        """미완료 체크포인트 조회"""
        db_checkpoints = self.repo.get_pending_by_history(history_id)
        return [CheckpointItem.from_db_model(c) for c in db_checkpoints]

    def get_completed_partition_names(self, history_ids: list[int]) -> set[str]:
        """여러 이력에서 완료된 파티션 이름 집합 반환"""
        if not history_ids:
            return set()
        checkpoints = self.repo.get_completed_names_by_history_ids(history_ids)
        return {c.partition_name for c in checkpoints}
