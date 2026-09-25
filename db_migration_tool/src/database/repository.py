"""베이스 리포지토리 패턴

CRUD 공통 로직을 제공하는 베이스 리포지토리와
엔티티별 전용 리포지토리를 정의합니다.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import func, insert
from sqlalchemy.orm import Session

from .local_db import Checkpoint, MigrationHistory, get_db


class BaseRepository[T]:
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
    def _session_scope(self) -> Iterator[Session]:
        """트랜잭션 컨텍스트 매니저

        자동으로 commit/rollback/close를 처리합니다.

        반환 타입을 명시해야 self.db가 Any여도 세션 이후 체인이 T로 좁혀진다.
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
    def get_by_id(self, id: int) -> T | None:
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


# 재개 대상이 될 수 있는(=끝나지 않은) 이력 상태. 'partial'은 일부 파티션 실패로
# 끝난 실행을 위한 값이다(H-01 결과 상태). 프로필 삭제 차단(M-12)도 같은 집합을 쓴다.
INCOMPLETE_STATUSES: tuple[str, ...] = ("running", "failed", "partial")

# 사용자가 명시적으로 폐기한 이력의 상태. 이력 화면이 이미 '취소'로 표시한다.
ABANDONED_STATUS = "cancelled"


class HistoryRepository(BaseRepository[MigrationHistory]):
    """MigrationHistory 전용 리포지토리"""

    def __init__(self, db=None):
        """HistoryRepository 초기화

        Args:
            db: 데이터베이스 인스턴스 (테스트용 주입 가능)
        """
        super().__init__(MigrationHistory, db)

    def get_incomplete_by_profile(self, profile_id: int) -> MigrationHistory | None:
        """프로필의 미완료 이력 조회

        running/failed/partial 상태의 최신 이력을 반환합니다.

        Args:
            profile_id: 프로필 ID

        Returns:
            미완료 이력 또는 None
        """
        with self._session_scope() as session:
            obj = (
                session.query(MigrationHistory)
                .filter_by(profile_id=profile_id)
                .filter(MigrationHistory.status.in_(INCOMPLETE_STATUSES))
                .order_by(MigrationHistory.started_at.desc())
                .first()
            )
            if obj:
                session.expunge(obj)
            return obj

    def create_with_checkpoints(
        self, partition_names: Iterable[str], **history_fields: Any
    ) -> MigrationHistory:
        """이력과 계획된 모든 checkpoint를 **한 트랜잭션**으로 만든다(H-09).

        하나라도 실패하면 이력까지 전부 rollback된다 — 일부 checkpoint만 남은
        이력이 재개 대상으로 보이는 일이 없어야 한다.
        """
        names = list(partition_names)
        with self._session_scope() as session:
            history = MigrationHistory(**history_fields)
            session.add(history)
            session.flush()  # id 확보
            if names:
                session.execute(
                    insert(Checkpoint),
                    [
                        {
                            "history_id": history.id,
                            "partition_name": name,
                            "status": "pending",
                            "rows_processed": 0,
                        }
                        for name in names
                    ],
                )
            session.refresh(history)
            session.expunge(history)
            return history

    def add_missing_checkpoints(self, history_id: int, partition_names: Iterable[str]) -> list[str]:
        """계획에는 있는데 checkpoint가 없는 파티션을 pending으로 채운다.

        존재 여부를 같은 트랜잭션 안에서 다시 확인하므로 두 번 불려도 중복이 생기지 않는다.

        Returns:
            실제로 새로 만든 파티션 이름(정렬).
        """
        wanted = set(partition_names)
        with self._session_scope() as session:
            present = {
                name
                for (name,) in session.query(Checkpoint.partition_name).filter(
                    Checkpoint.history_id == history_id
                )
            }
            missing = sorted(wanted - present)
            if missing:
                session.execute(
                    insert(Checkpoint),
                    [
                        {
                            "history_id": history_id,
                            "partition_name": name,
                            "status": "pending",
                            "rows_processed": 0,
                        }
                        for name in missing
                    ],
                )
            return missing

    def get_checkpoint_states(self, history_id: int) -> list[tuple[str, str | None]]:
        """(partition_name, status) 목록. 엔티티를 만들지 않는다."""
        with self._session_scope() as session:
            rows = (
                session.query(Checkpoint.partition_name, Checkpoint.status)
                .filter(Checkpoint.history_id == history_id)
                .all()
            )
            return [(name, status) for name, status in rows]

    def bind_legacy_plan(
        self,
        history_id: int,
        expected_partitions: Iterable[str],
        *,
        add_partitions: Iterable[str] = (),
        **plan_fields: Any,
    ) -> bool:
        """계획이 없는(legacy) 이력에 계획·identity를 한 번만 기록한다.

        같은 트랜잭션에서 (1) 아직 legacy인지, (2) checkpoint 집합이 확인 시점과
        같은지를 다시 보고 기록한다. 둘 중 하나라도 어긋나면 아무것도 쓰지 않는다.
        `add_partitions`(범위 대비 누락 보충분)는 계획 기록과 **같은 트랜잭션**에서
        pending checkpoint로 만든다 — 보충이 실패하면 계획 기록도 rollback된다.
        `plan_fields`에는 보충한 이름의 기록(`legacy_supplemented`)도 들어간다 — 계획과 함께
        한 번만 쓰이므로, 아카이브 워커가 원본 부재를 허용하는 이름이 나중에 넓어지지 않는다.

        Returns:
            기록했으면 True, 이미 계획이 있거나 집합이 바뀌었으면 False.
        """
        expected = sorted(set(expected_partitions))
        extra = sorted(set(add_partitions) - set(expected))
        with self._session_scope() as session:
            names = sorted(
                {
                    name
                    for (name,) in session.query(Checkpoint.partition_name).filter(
                        Checkpoint.history_id == history_id
                    )
                }
            )
            if names != expected:
                return False
            # 조건부 UPDATE를 먼저 한다. 0건이면 아직 아무것도 쓰지 않았다.
            updated = (
                session.query(MigrationHistory)
                .filter(MigrationHistory.id == history_id)
                .filter(MigrationHistory.plan_version.is_(None))
                .update(dict[Any, Any](plan_fields), synchronize_session=False)
            )
            if updated != 1:
                return False
            if extra:
                session.execute(
                    insert(Checkpoint),
                    [
                        {
                            "history_id": history_id,
                            "partition_name": name,
                            "status": "pending",
                            "rows_processed": 0,
                        }
                        for name in extra
                    ],
                )
            return True

    def count_incomplete_by_profile(self, profile_id: int) -> int:
        """프로필에 남은 미완료 이력 수(M-12 삭제 차단 판정)."""
        with self._session_scope() as session:
            return (
                session.query(func.count(MigrationHistory.id))
                .filter(MigrationHistory.profile_id == profile_id)
                .filter(MigrationHistory.status.in_(INCOMPLETE_STATUSES))
                .scalar()
                or 0
            )

    def abandon(self, *, profile_id: int | None = None, history_id: int | None = None) -> int:
        """미완료 이력을 명시적으로 폐기(cancelled)한다. 이미 끝난 이력은 건드리지 않는다.

        Returns:
            폐기한 이력 수.
        """
        if profile_id is None and history_id is None:
            raise ValueError("profile_id 또는 history_id가 필요합니다")
        with self._session_scope() as session:
            query = session.query(MigrationHistory).filter(
                MigrationHistory.status.in_(INCOMPLETE_STATUSES)
            )
            if profile_id is not None:
                query = query.filter(MigrationHistory.profile_id == profile_id)
            if history_id is not None:
                query = query.filter(MigrationHistory.id == history_id)
            return query.update(
                {"status": ABANDONED_STATUS, "completed_at": datetime.now()},
                synchronize_session=False,
            )

    def get_all_desc(self) -> list[MigrationHistory]:
        """최신순 전체 조회

        started_at 기준 내림차순으로 정렬합니다.

        Returns:
            이력 리스트 (최신순)
        """
        return self.get_all(order_by=MigrationHistory.started_at.desc())

    def get_completed_by_profile(self, profile_id: int) -> list[MigrationHistory]:
        """프로필의 완료된 이력 조회"""
        with self._session_scope() as session:
            results = (
                session.query(MigrationHistory)
                .filter_by(profile_id=profile_id, status="completed")
                .all()
            )
            for obj in results:
                session.expunge(obj)
            return results


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

    def get_completed_names_by_history_ids(self, history_ids: list[int]) -> list[Checkpoint]:
        """여러 이력의 완료 체크포인트 조회"""
        if not history_ids:
            return []
        with self._session_scope() as session:
            results = (
                session.query(Checkpoint)
                .filter(Checkpoint.history_id.in_(history_ids))
                .filter(Checkpoint.status == "completed")
                .all()
            )
            for obj in results:
                session.expunge(obj)
            return results

    def sum_rows_processed(self, history_id: int) -> int:
        """이력의 누적 처리 행 수.

        워커의 성능 카운터는 실행 1회분만 센다. 재개하면 새 워커가 0부터
        다시 세므로 그 값을 이력에 쓰면 진행량이 뒤로 간다. 체크포인트는
        실행을 넘어 남으므로 여기서 합산한다.

        엔티티를 만들지 않고 SQL에서 더한다 — 체크포인트가 수만 개인
        이력에서 전부 hydrate하면 UI 스레드가 멈춘다.
        """
        with self._session_scope() as session:
            total = (
                session.query(func.coalesce(func.sum(Checkpoint.rows_processed), 0))
                .filter(Checkpoint.history_id == history_id)
                .scalar()
            )
            return int(total or 0)

    def count_pending(self, history_id: int) -> int:
        """완료되지 않은 체크포인트 수.

        엔티티를 만들지 않고 센다. "전부 끝났는가"만 알면 되는 곳에서
        목록을 통째로 불러올 이유가 없다.
        """
        with self._session_scope() as session:
            return (
                session.query(func.count(Checkpoint.id))
                .filter(Checkpoint.history_id == history_id)
                .filter(Checkpoint.status != "completed")
                .scalar()
                or 0
            )

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
