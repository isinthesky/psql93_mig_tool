"""
pytest 설정 및 공통 픽스처
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.local_db import Base, LocalDatabase


@pytest.fixture(scope="function")
def temp_db():
    """임시 테스트 데이터베이스 픽스처"""
    # 임시 데이터베이스 파일 생성
    temp_fd, temp_path = tempfile.mkstemp(suffix=".db")

    try:
        # 테스트용 LocalDatabase 인스턴스 생성
        test_db = LocalDatabase()
        test_db.db_path = temp_path
        test_db.engine = create_engine(f"sqlite:///{temp_path}", echo=False)

        # 테이블 생성
        Base.metadata.create_all(test_db.engine)

        # 세션 팩토리 생성
        test_db.Session = sessionmaker(bind=test_db.engine)

        yield test_db

    finally:
        # 정리
        if test_db.engine:
            test_db.engine.dispose()
        os.close(temp_fd)
        os.unlink(temp_path)


@pytest.fixture
def sample_profile_data():
    """샘플 프로필 데이터"""
    return {
        "name": "Test Profile",
        "source_config": {
            "host": "source.example.com",
            "port": 5432,
            "database": "source_db",
            "username": "source_user",
            "password": "source_pass",
            "ssl": False,
        },
        "target_config": {
            "host": "target.example.com",
            "port": 5432,
            "database": "target_db",
            "username": "target_user",
            "password": "target_pass",
            "ssl": False,
        },
    }


@pytest.fixture(scope="session", autouse=True)
def mock_log_emitter():
    """테스트 환경에서 log_emitter 모킹 (자동 적용)

    DB 로깅으로 인한 테스트 지연을 방지합니다.
    - DB 초기화 비용 제거 (0.5~1초)
    - 백그라운드 스레드 플러시 대기 제거 (0.1~0.2초/호출)
    """
    with patch("src.utils.enhanced_logger.log_emitter") as mock_emitter:
        # Mock logger 설정
        mock_logger = MagicMock()
        mock_logger.generate_session_id.return_value = "TEST_SESSION"
        mock_logger.set_session_id = MagicMock()

        # Mock emitter 설정
        mock_emitter.logger = mock_logger
        mock_emitter.emit_log = MagicMock()  # no-op

        yield mock_emitter
