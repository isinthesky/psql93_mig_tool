"""master_password.py 단위 테스트"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import InvalidToken

from src.database.local_db import SavedConnection
from src.models.profile import ProfileManager
from src.models.saved_connection import SavedConnectionManager
from src.utils.app_paths import AppPaths
from src.utils.master_password import MasterPasswordService


class TestMasterPasswordService:
    """마스터 비밀번호 서비스 테스트"""

    @pytest.fixture(autouse=True)
    def isolated_app_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            AppPaths.set_custom_root(Path(tmpdir))
            MasterPasswordService.lock()
            yield
            MasterPasswordService.lock()
            AppPaths.set_custom_root(None)

    def test_setup_and_unlock_success(self):
        service = MasterPasswordService()

        service.setup_master_password("1234")

        assert service.is_configured() is True
        assert service.unlock("1234") is True
        assert service.is_authenticated() is True

    def test_unlock_with_wrong_password_returns_false(self):
        service = MasterPasswordService()
        service.setup_master_password("5678")

        assert service.unlock("9999") is False
        assert service.is_authenticated() is False

    def test_setup_master_password_rejects_short_password(self):
        service = MasterPasswordService()

        with pytest.raises(ValueError):
            service.setup_master_password("123")


class TestLegacyReencryption:
    """레거시 데이터 재암호화 테스트"""

    @pytest.fixture(autouse=True)
    def isolated_app_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            AppPaths.set_custom_root(Path(tmpdir))
            MasterPasswordService.lock()
            yield
            MasterPasswordService.lock()
            AppPaths.set_custom_root(None)

    @pytest.fixture
    def profile_manager(self, temp_db, monkeypatch):
        manager = ProfileManager()
        monkeypatch.setattr(manager, "db", temp_db)
        return manager

    @pytest.fixture
    def saved_connection_manager(self, temp_db, monkeypatch):
        manager = SavedConnectionManager()
        monkeypatch.setattr(manager, "db", temp_db)
        return manager

    def test_reencrypt_all_profiles_moves_legacy_data_to_master_password_key(
        self, profile_manager, sample_profile_data, monkeypatch
    ):
        created = profile_manager.create_profile(sample_profile_data)
        assert created.id is not None

        service = MasterPasswordService()
        service.setup_master_password("2468")
        assert service.unlock("2468") is True

        migrated_count = profile_manager.reencrypt_all_profiles(
            MasterPasswordService.get_active_cipher_suite()
        )

        assert migrated_count == 1

        new_manager = ProfileManager()
        monkeypatch.setattr(new_manager, "db", profile_manager.db)
        migrated_profile = new_manager.get_profile(created.id)

        assert migrated_profile is not None
        assert migrated_profile.source_config["host"] == sample_profile_data["source_config"]["host"]
        assert migrated_profile.target_config["host"] == sample_profile_data["target_config"]["host"]

        MasterPasswordService.lock()
        legacy_only_manager = ProfileManager()
        monkeypatch.setattr(legacy_only_manager, "db", profile_manager.db)
        with pytest.raises(InvalidToken):
            legacy_only_manager.get_profile(created.id)

    def test_reencrypt_saved_connections_moves_legacy_password_to_master_password_key(
        self, saved_connection_manager, temp_db
    ):
        legacy_password = saved_connection_manager._encrypt("legacy-pass")
        with temp_db.session_scope() as session:
            row = SavedConnection(
                host="localhost",
                port=5432,
                database="testdb",
                username="tester",
                password=legacy_password,
                ssl=0,
                compat_mode="auto",
            )
            session.add(row)
            session.flush()
            row_id = row.id

        service = MasterPasswordService()
        service.setup_master_password("2468")
        assert service.unlock("2468") is True

        migrated_count = saved_connection_manager.reencrypt_all_saved_connections(
            MasterPasswordService.get_active_cipher_suite()
        )
        assert migrated_count == 1

        new_manager = SavedConnectionManager()
        new_manager.db = temp_db
        rows = new_manager.get_all()
        assert len(rows) == 1
        assert rows[0]["password"] == "legacy-pass"

        MasterPasswordService.lock()
        legacy_manager = SavedConnectionManager()
        legacy_manager.db = temp_db
        with temp_db.session_scope() as session:
            encrypted_password = session.query(SavedConnection).filter_by(id=row_id).first().password
        with pytest.raises(InvalidToken):
            legacy_manager._cipher.decrypt(encrypted_password.encode())
