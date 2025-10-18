"""베이스 리포지토리 패턴

CRUD 공통 로직을 제공하는 베이스 리포지토리와
엔티티별 전용 리포지토리를 정의합니다.
"""

from contextlib import contextmanager
from typing import Generic, Optional, TypeVar

from .local_db import Checkpoint, MigrationHistory, get_db

T = TypeVar("T")


class BaseRepository(Generic[T]):
    """CRUD 공통 로직을 제공하는 베이스 리포지토리

    Args:
        model_class: SQLAlchemy 모델 클래스
        db: 데이터베이스 인스턴스 (테스트용 주입 가능)

    Examples:
        >>> from src.database.repository import HistoryRepository
        >>> repo = HistoryRepository()
        >>> history = repo.create(profile_id=1, start_date='2025-01-01', ...)
    """

    def __init__(self, model_class: type[T], db=None):
        """베이스 리포지토리 초기화

        Args:
            model_class: SQLAlchemy 모델 클래스
            db: 데이터베이스 인스턴스 (테스트용 주입 가능)
        """
        self.model_class = model_class
        self.db = db or get_db()

    @contextmanager
    def _session_scope(self):
        """트랜잭션 컨텍스트 매니저

        자동으로 commit/rollback/close를 처리합니다.
        """
        with self.db.session_scope() as session:
            yield session

    # CREATE
    def create(self, **kwargs) -> T:
        """엔티티 생성

        Args:
            **kwargs: 모델 필드와 값

        Returns:
            생성된 엔티티

        Examples:
            >>> repo = HistoryRepository()
            >>> history = repo.create(profile_id=1, start_date='2025-01-01', ...)
        """
        with self._session_scope() as session:
            obj = self.model_class(**kwargs)
            session.add(obj)
            session.flush()  # ID 생성
            session.refresh(obj)  # 생성된 값 로드
            # 세션이 닫히기 전에 모든 속성 로드 (Detached 방지)
            session.expunge(obj)
            return obj

    # READ
    def get_by_id(self, id: int) -> Optional[T]:
        """ID로 조회

        Args:
            id: 엔티티 ID

        Returns:
            엔티티 또는 None
        """
        with self._session_scope() as session:
            obj = session.query(self.model_class).filter_by(id=id).first()
            if obj:
                session.expunge(obj)
            return obj

    def get_one_by(self, **filters) -> Optional[T]:
        """조건으로 단건 조회

        Args:
            **filters: 필터 조건

        Returns:
            엔티티 또는 None

        Examples:
            >>> repo = HistoryRepository()
            >>> history = repo.get_one_by(profile_id=1, status='running')
        """
        with self._session_scope() as session:
            obj = session.query(self.model_class).filter_by(**filters).first()
            if obj:
                session.expunge(obj)
            return obj

    def get_all(self, order_by=None) -> list[T]:
        """전체 조회

        Args:
            order_by: 정렬 기준 (SQLAlchemy 컬럼)

        Returns:
            엔티티 리스트

        Examples:
            >>> from src.database.local_db import MigrationHistory
            >>> repo = HistoryRepository()
            >>> histories = repo.get_all(order_by=MigrationHistory.started_at.desc())
        """
        with self._session_scope() as session:
            query = session.query(self.model_class)
            if order_by is not None:
                query = query.order_by(order_by)
            results = query.all()
            for obj in results:
                session.expunge(obj)
            return results

    def get_many_by(self, order_by=None, **filters) -> list[T]:
        """조건으로 다건 조회

        Args:
            order_by: 정렬 기준 (SQLAlchemy 컬럼)
            **filters: 필터 조건

        Returns:
            엔티티 리스트

        Examples:
            >>> repo = CheckpointRepository()
            >>> checkpoints = repo.get_many_by(history_id=1, status='pending')
        """
        with self._session_scope() as session:
            query = session.query(self.model_class).filter_by(**filters)
            if order_by is not None:
                query = query.order_by(order_by)
            results = query.all()
            for obj in results:
                session.expunge(obj)
            return results

    # UPDATE
    def update_by_id(self, id: int, **updates) -> bool:
        """ID로 업데이트

        Args:
            id: 엔티티 ID
            **updates: 업데이트할 필드와 값

        Returns:
            성공 여부

        Examples:
            >>> repo = HistoryRepository()
            >>> success = repo.update_by_id(1, status='completed', processed_rows=1000)
        """
        with self._session_scope() as session:
            obj = session.query(self.model_class).filter_by(id=id).first()
            if not obj:
                return False

            for key, value in updates.items():
                if hasattr(obj, key):
                    setattr(obj, key, value)

            return True

    # DELETE
    def delete_by_id(self, id: int) -> bool:
        """ID로 삭제

        Args:
            id: 엔티티 ID

        Returns:
            성공 여부
        """
        with self._session_scope() as session:
            obj = session.query(self.model_class).filter_by(id=id).first()
            if obj:
                session.delete(obj)
                return True
            return False

    # 고급 쿼리
    def exists(self, **filters) -> bool:
        """존재 여부 확인

        Args:
            **filters: 필터 조건

        Returns:
            존재 여부
        """
        with self._session_scope() as session:
            return session.query(self.model_class).filter_by(**filters).first() is not None

    def count(self, **filters) -> int:
        """개수 세기

        Args:
            **filters: 필터 조건 (없으면 전체 개수)

        Returns:
            엔티티 개수
        """
        with self._session_scope() as session:
            query = session.query(self.model_class)
            if filters:
                query = query.filter_by(**filters)
            return query.count()


class HistoryRepository(BaseRepository[MigrationHistory]):
    """MigrationHistory 전용 리포지토리"""

    def __init__(self, db=None):
        """HistoryRepository 초기화

        Args:
            db: 데이터베이스 인스턴스 (테스트용 주입 가능)
        """
        super().__init__(MigrationHistory, db)

    def get_incomplete_by_profile(self, profile_id: int) -> Optional[MigrationHistory]:
        """프로필의 미완료 이력 조회

        running 또는 failed 상태의 최신 이력을 반환합니다.

        Args:
            profile_id: 프로필 ID

        Returns:
            미완료 이력 또는 None
        """
        with self._session_scope() as session:
            obj = (
                session.query(MigrationHistory)
                .filter_by(profile_id=profile_id)
                .filter(MigrationHistory.status.in_(["running", "failed"]))
                .order_by(MigrationHistory.started_at.desc())
                .first()
            )
            if obj:
                session.expunge(obj)
            return obj

    def get_all_desc(self) -> list[MigrationHistory]:
        """최신순 전체 조회

        started_at 기준 내림차순으로 정렬합니다.

        Returns:
            이력 리스트 (최신순)
        """
        return self.get_all(order_by=MigrationHistory.started_at.desc())


class CheckpointRepository(BaseRepository[Checkpoint]):
    """Checkpoint 전용 리포지토리"""

    def __init__(self, db=None):
        """CheckpointRepository 초기화

        Args:
            db: 데이터베이스 인스턴스 (테스트용 주입 가능)
        """
        super().__init__(Checkpoint, db)

    def get_by_history(self, history_id: int) -> list[Checkpoint]:
        """이력별 체크포인트 조회

        partition_name 기준 오름차순으로 정렬합니다.

        Args:
            history_id: 이력 ID

        Returns:
            체크포인트 리스트
        """
        return self.get_many_by(history_id=history_id, order_by=Checkpoint.partition_name)

    def get_pending_by_history(self, history_id: int) -> list[Checkpoint]:
        """미완료 체크포인트 조회

        completed가 아닌 체크포인트를 반환합니다.

        Args:
            history_id: 이력 ID

        Returns:
            미완료 체크포인트 리스트
        """
        with self._session_scope() as session:
            results = (
                session.query(Checkpoint)
                .filter_by(history_id=history_id)
                .filter(Checkpoint.status != "completed")
                .order_by(Checkpoint.partition_name)
                .all()
            )
            for obj in results:
                session.expunge(obj)
            return results
