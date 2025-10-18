"""
pytest 설정 및 공통 픽스처
"""

import os
import tempfile

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
