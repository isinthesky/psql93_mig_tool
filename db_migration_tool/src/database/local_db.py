"""
로컬 SQLite 데이터베이스 관리
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import DateTime, Engine, Integer, String, Text, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from src.utils.app_paths import AppPaths


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 선언적 베이스.

    Mapped[X] 는 NOT NULL, Mapped[X | None] 은 nullable 로 매핑된다.
    기존 스키마와 어긋나면 안 되므로 어노테이션은 원래 nullable 설정을 그대로 따른다.
    """


class Profile(Base):
    """연결 프로필 테이블"""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    source_config: Mapped[str] = mapped_column(Text, nullable=False)  # JSON (암호화)
    target_config: Mapped[str] = mapped_column(Text, nullable=False)  # JSON (암호화)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


class MigrationHistory(Base):
    """작업 이력 테이블"""

    __tablename__ = "migration_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[str | None] = mapped_column(String(10))  # YYYY-MM-DD
    end_date: Mapped[str | None] = mapped_column(String(10))  # YYYY-MM-DD
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # completed, failed, cancelled, running
    status: Mapped[str | None] = mapped_column(String(20))
    total_rows: Mapped[int | None] = mapped_column(Integer)
    processed_rows: Mapped[int | None] = mapped_column(Integer)
    # 연결 상태 필드 추가
    source_connection_status: Mapped[str | None] = mapped_column(Text)  # 연결 성공/실패 메시지
    target_connection_status: Mapped[str | None] = mapped_column(Text)  # 연결 성공/실패 메시지
    connection_check_time: Mapped[datetime | None] = mapped_column(DateTime)  # 연결 확인 시간

    # ── 불변 계획(H-08/H-09). 생성 트랜잭션에서 한 번만 쓰고 이후 바꾸지 않는다. ──
    # 모두 nullable: 이 컬럼이 생기기 전 이력은 NULL로 남아 legacy로 취급된다
    # (가짜 계획을 backfill하지 않는다 — 재개 시 명시적 확인 후 채택).
    plan_version: Mapped[int | None] = mapped_column(Integer)
    migration_mode: Mapped[str | None] = mapped_column(String(40))  # postgres_to_postgres 등
    schema_name: Mapped[str | None] = mapped_column(String(63))
    # 비밀을 뺀 endpoint identity(kind/host/port/database/user 또는 archive_path)의 SHA-256
    source_fingerprint: Mapped[str | None] = mapped_column(String(64))
    target_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # 거부 안내용 표시 문자열(비밀·사용자명 없음): "host:port/db → host:port/db"
    endpoint_label: Mapped[str | None] = mapped_column(Text)
    planned_partitions: Mapped[str | None] = mapped_column(Text)  # JSON, 정렬된 파티션 이름
    planned_count: Mapped[int | None] = mapped_column(Integer)
    planned_hash: Mapped[str | None] = mapped_column(String(64))
    plan_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # legacy 이력을 사용자가 확인하고 현재 endpoint에 묶은 시각
    legacy_adopted_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 채택 때 범위 대비 누락으로 보충한 파티션(JSON, 정렬). 명명 규칙으로 만든 '있을 수 있는
    # 이름'이라 원본에 없을 수 있다 — 아카이브 워커는 이 이름만 원본 부재를 0건 완료로 닫는다.
    legacy_supplemented: Mapped[str | None] = mapped_column(Text)


class Checkpoint(Base):
    """체크포인트 테이블 (재개용)"""

    __tablename__ = "checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    history_id: Mapped[int] = mapped_column(Integer, nullable=False)
    partition_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str | None] = mapped_column(String(20))  # pending, completed, failed
    rows_processed: Mapped[int | None] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    # 새 필드: COPY 방식 재개를 위한 마지막 처리 키
    last_path_id: Mapped[int | None] = mapped_column(Integer)
    last_issued_date: Mapped[int | None] = mapped_column(Integer)
    # timestamp 타입(예: energy_display)의 재개를 위해 문자열 컬럼을 추가
    last_issued_date_text: Mapped[str | None] = mapped_column(Text)
    copy_method: Mapped[str | None] = mapped_column(String(10), default="INSERT")  # COPY or INSERT
    bytes_transferred: Mapped[int | None] = mapped_column(Integer, default=0)


class SavedConnection(Base):
    """저장된 PostgreSQL 연결 프리셋"""

    __tablename__ = "saved_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=5432)
    database: Mapped[str] = mapped_column(String(100), nullable=False)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    password: Mapped[str | None] = mapped_column(Text, default="")  # 암호화
    ssl: Mapped[int | None] = mapped_column(Integer, default=0)  # 0=False, 1=True
    compat_mode: Mapped[str | None] = mapped_column(String(10), default="auto")
    last_used: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class LogEntry(Base):
    """로그 엔트리 테이블"""

    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(20), index=True)
    level: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    logger_name: Mapped[str | None] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class LocalDatabase:
    """로컬 데이터베이스 관리 클래스"""

    def __init__(self):
        self.db_path = self._get_db_path()
        self.engine: Engine | None = None
        self.Session: sessionmaker[Session] | None = None

    def _get_db_path(self):
        """데이터베이스 파일 경로 가져오기 (AppPaths 활용)"""
        return str(AppPaths.get_db_path())

    def initialize(self):
        """데이터베이스 초기화"""
        # SQLite 연결 문자열
        connection_string = f"sqlite:///{self.db_path}"

        # 엔진 생성
        self.engine = create_engine(connection_string, echo=False)

        # 테이블 생성
        Base.metadata.create_all(self.engine)

        # 스키마 마이그레이션 실행
        self._migrate_schema()

        # 세션 팩토리 생성
        self.Session = sessionmaker(bind=self.engine)

    def get_session(self) -> Session:
        """데이터베이스 세션 반환"""
        if not self.Session:
            raise RuntimeError("데이터베이스가 초기화되지 않았습니다.")
        return self.Session()

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        """트랜잭션 컨텍스트 매니저

        자동으로 commit/rollback/close를 처리합니다.

        Usage:
            with self.db.session_scope() as session:
                session.add(obj)
                # 정상 종료 시 자동 commit
                # 예외 발생 시 자동 rollback
        """
        session = self.get_session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self):
        """데이터베이스 연결 종료"""
        if self.engine:
            self.engine.dispose()

    # 기존 파일에 없을 수 있는 컬럼. create_all()은 이미 있는 테이블을 고치지 않는다.
    # 전부 nullable(또는 DEFAULT)이라 기존 행을 건드리지 않고 추가된다.
    _ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("checkpoints", "last_path_id", "INTEGER"),
        ("checkpoints", "last_issued_date", "INTEGER"),
        ("checkpoints", "last_issued_date_text", "TEXT"),
        ("checkpoints", "copy_method", "VARCHAR(10) DEFAULT 'INSERT'"),
        ("checkpoints", "bytes_transferred", "INTEGER DEFAULT 0"),
        ("migration_history", "source_connection_status", "TEXT"),
        ("migration_history", "target_connection_status", "TEXT"),
        ("migration_history", "connection_check_time", "DATETIME"),
        # H-08/H-09 불변 계획
        ("migration_history", "plan_version", "INTEGER"),
        ("migration_history", "migration_mode", "VARCHAR(40)"),
        ("migration_history", "schema_name", "VARCHAR(63)"),
        ("migration_history", "source_fingerprint", "VARCHAR(64)"),
        ("migration_history", "target_fingerprint", "VARCHAR(64)"),
        ("migration_history", "endpoint_label", "TEXT"),
        ("migration_history", "planned_partitions", "TEXT"),
        ("migration_history", "planned_count", "INTEGER"),
        ("migration_history", "planned_hash", "VARCHAR(64)"),
        ("migration_history", "plan_fingerprint", "VARCHAR(64)"),
        ("migration_history", "legacy_adopted_at", "DATETIME"),
        ("migration_history", "legacy_supplemented", "TEXT"),
    )

    def _migrate_schema(self):
        """기존 데이터베이스 스키마 마이그레이션.

        없는 컬럼만 골라 추가한다(PRAGMA table_info 기준, 몇 번 실행해도 같다).
        예전에는 모든 ALTER의 예외를 삼켜 '이미 있음'과 '추가 실패'를 구분하지 못했다.
        추가에 실패한 채 올라가면 이후 모든 이력 조회가 'no such column'으로 깨지므로,
        여기서는 한 트랜잭션으로 추가하고 실패하면 그대로 올린다.
        """
        if self.engine is None:
            raise RuntimeError("데이터베이스가 초기화되지 않았습니다.")
        with self.engine.begin() as conn:
            existing: dict[str, set[str]] = {}
            for table, column, ddl in self._ADDED_COLUMNS:
                if table not in existing:
                    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
                    existing[table] = {row[1] for row in rows}
                if column in existing[table]:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                existing[table].add(column)


# 전역 데이터베이스 인스턴스 (스레드 안전 초기화)
_db_instance: LocalDatabase | None = None
_db_lock = threading.Lock()


def get_db() -> LocalDatabase:
    """데이터베이스 인스턴스 반환 (스레드 안전)"""
    global _db_instance
    if _db_instance is None:
        with _db_lock:
            if _db_instance is None:
                db = LocalDatabase()
                db.initialize()
                _db_instance = db
    return _db_instance
