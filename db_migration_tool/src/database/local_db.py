"""
로컬 SQLite 데이터베이스 관리
"""

from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from src.utils.app_paths import AppPaths

Base = declarative_base()


class Profile(Base):
    """연결 프로필 테이블"""

    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    source_config = Column(Text, nullable=False)  # JSON (암호화)
    target_config = Column(Text, nullable=False)  # JSON (암호화)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MigrationHistory(Base):
    """작업 이력 테이블"""

    __tablename__ = "migration_history"

    id = Column(Integer, primary_key=True)
    profile_id = Column(Integer, nullable=False)
    start_date = Column(String(10))  # YYYY-MM-DD
    end_date = Column(String(10))  # YYYY-MM-DD
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    status = Column(String(20))  # completed, failed, cancelled, running
    total_rows = Column(Integer)
    processed_rows = Column(Integer)
    # 연결 상태 필드 추가
    source_connection_status = Column(Text)  # 연결 성공/실패 메시지
    target_connection_status = Column(Text)  # 연결 성공/실패 메시지
    connection_check_time = Column(DateTime)  # 연결 확인 시간


class Checkpoint(Base):
    """체크포인트 테이블 (재개용)"""

    __tablename__ = "checkpoints"

    id = Column(Integer, primary_key=True)
    history_id = Column(Integer, nullable=False)
    partition_name = Column(String(100), nullable=False)
    status = Column(String(20))  # pending, completed, failed
    rows_processed = Column(Integer, default=0)
    error_message = Column(Text)
    # 새 필드: COPY 방식 재개를 위한 마지막 처리 키
    last_path_id = Column(Integer)
    last_issued_date = Column(Integer)
    copy_method = Column(String(10), default="INSERT")  # COPY or INSERT
    bytes_transferred = Column(Integer, default=0)


class LogEntry(Base):
    """로그 엔트리 테이블"""

    __tablename__ = "logs"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    session_id = Column(String(20), index=True)
    level = Column(String(10), nullable=False, index=True)
    logger_name = Column(String(50))
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now)


class LocalDatabase:
    """로컬 데이터베이스 관리 클래스"""

    def __init__(self):
        self.db_path = self._get_db_path()
        self.engine = None
        self.Session = None

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

    def get_session(self):
        """데이터베이스 세션 반환"""
        if not self.Session:
            raise RuntimeError("데이터베이스가 초기화되지 않았습니다.")
        return self.Session()

    @contextmanager
    def session_scope(self):
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

    def _migrate_schema(self):
        """기존 데이터베이스 스키마 마이그레이션"""
        try:
            with self.engine.connect() as conn:
                # checkpoints 테이블에 새 컬럼 추가
                migrations = [
                    "ALTER TABLE checkpoints ADD COLUMN last_path_id INTEGER",
                    "ALTER TABLE checkpoints ADD COLUMN last_issued_date INTEGER",
                    "ALTER TABLE checkpoints ADD COLUMN copy_method VARCHAR(10) DEFAULT 'INSERT'",
                    "ALTER TABLE checkpoints ADD COLUMN bytes_transferred INTEGER DEFAULT 0",
                    # migration_history 테이블에 연결 상태 컬럼 추가
                    "ALTER TABLE migration_history ADD COLUMN source_connection_status TEXT",
                    "ALTER TABLE migration_history ADD COLUMN target_connection_status TEXT",
                    "ALTER TABLE migration_history ADD COLUMN connection_check_time DATETIME",
                ]

                for migration in migrations:
                    try:
                        conn.execute(text(migration))
                        conn.commit()
                    except Exception:
                        # 컬럼이 이미 존재하는 경우 무시
                        pass

        except Exception:
            # 마이그레이션 실패는 무시 (새 DB는 이미 올바른 스키마)
            pass


# 전역 데이터베이스 인스턴스
_db_instance = None


def get_db():
    """데이터베이스 인스턴스 반환"""
    global _db_instance
    if _db_instance is None:
        _db_instance = LocalDatabase()
        _db_instance.initialize()
    return _db_instance
