"""이력 모델 테스트 공용 픽스처 — 파일 기반 임시 SQLite를 전역 DB로 끼운다."""

import os

import pytest

import src.database.local_db as local_db_module


@pytest.fixture
def history_db(tmp_path, monkeypatch):
    """실제 SQL 경로(트랜잭션·트리거)를 그대로 타는 파일 SQLite.

    `get_db()` 전역을 바꿔 끼우므로 매니저를 인자 없이 만들어도 이 DB를 쓴다.
    """
    path = str(tmp_path / "history.db")
    instance = local_db_module.LocalDatabase()
    instance.db_path = path
    instance.initialize()
    monkeypatch.setattr(local_db_module, "_db_instance", instance, raising=False)
    yield instance
    if instance.engine:
        instance.engine.dispose()
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
