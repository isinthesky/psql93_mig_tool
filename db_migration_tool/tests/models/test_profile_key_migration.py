"""프로필 암호화 키 — H-06(공개 legacy 키 제거)과 M-05(OS 보호 저장소 래핑).

감사 문서 §5 단계 3 필수 테스트: legacy migration, key/db 동시 복제.
여기서 "머신"은 FakeDpapi의 machine_secret으로 모사한다(실제 DPAPI의 사용자·장치 범위).
"""

from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.local_db import Base, LocalDatabase, Profile, SavedConnection
from src.models import profile as profile_module
from src.models.profile import (
    ConnectionProfile,
    ProfileDecryptError,
    ProfileKeyUnavailableError,
    ProfileManager,
)
from src.models.saved_connection import SavedConnectionManager
from src.utils import secret_store
from src.utils.secret_store import (
    BACKUP_SUFFIX,
    KEY_FILE_HEADER,
    PlainFileProtector,
    WrappedKeyFile,
)
from tests.utils.fake_protectors import FakeDpapi

# 과거 소스에 하드코딩되어 공개된 키. 공격자도 알고 있다고 가정한다.
PUBLIC_LEGACY_KEY = b"ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="

SOURCE = {
    "host": "src.example",
    "port": 5446,
    "database": "bms93",
    "username": "u",
    "password": "src-pw",
}
TARGET = {
    "host": "dst.example",
    "port": 5445,
    "database": "temp",
    "username": "u",
    "password": "dst-pw",
}


# --- 헬퍼 ------------------------------------------------------------------------------


def make_db(path: Path) -> LocalDatabase:
    db = LocalDatabase()
    db.db_path = str(path)
    db.engine = create_engine(f"sqlite:///{path}", echo=False)
    Base.metadata.create_all(db.engine)
    db.Session = sessionmaker(bind=db.engine)
    return db


def seed_profile(db: LocalDatabase, name: str, key: bytes) -> None:
    cipher = Fernet(key)
    with db.session_scope() as session:
        session.add(
            Profile(
                name=name,
                source_config=cipher.encrypt(json.dumps(SOURCE).encode()).decode(),
                target_config=cipher.encrypt(json.dumps(TARGET).encode()).decode(),
            )
        )


def seed_saved(db: LocalDatabase, key: bytes, password: str = "preset-pw") -> None:
    with db.session_scope() as session:
        session.add(
            SavedConnection(
                host="h",
                port=5432,
                database="d",
                username="u",
                password=Fernet(key).encrypt(password.encode()).decode(),
            )
        )


def all_tokens(db: LocalDatabase) -> list[str]:
    with db.session_scope() as session:
        tokens = []
        for p in session.query(Profile).order_by(Profile.id):
            tokens += [p.source_config, p.target_config]
        for s in session.query(SavedConnection).order_by(SavedConnection.id):
            if s.password:
                tokens.append(s.password)
        return tokens


def decrypts(key: bytes, token: str) -> bool:
    try:
        Fernet(key).decrypt(token.encode())
        return True
    except (InvalidToken, ValueError):
        return False


def snapshot(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir()) if p.is_file()}


class Env:
    def __init__(self, root: Path, machine: bytes = b"machine-A"):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.db_path = root / "db_migration.db"
        self.key_path = root / ".encryption_key"
        self.db = make_db(self.db_path)
        self.machine = machine

    def key_file(self, protector=None) -> WrappedKeyFile:
        return WrappedKeyFile(self.key_path, protector or FakeDpapi(self.machine))

    def profiles(self, **kw) -> ProfileManager:
        return ProfileManager(db=self.db, key_file=self.key_file(**kw))

    def saved(self, **kw) -> SavedConnectionManager:
        return SavedConnectionManager(db=self.db, key_file=self.key_file(**kw))

    def stored_key(self) -> bytes | None:
        return self.key_file().read().key

    def close(self) -> None:
        if self.db.engine is not None:
            self.db.engine.dispose()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path / "machine-A")
    yield e
    e.close()


# --- H-06: 공개 legacy 키 제거 -----------------------------------------------------------


class TestLegacyMigration:
    def test_legacy_key_file_and_rows_are_rotated(self, env):
        """구버전이 기존 프로필이 있을 때 키 파일에 공개 키 자체를 써 두던 상태."""
        env.key_path.write_bytes(PUBLIC_LEGACY_KEY)
        seed_profile(env.db, "p1", PUBLIC_LEGACY_KEY)
        seed_profile(env.db, "p2", PUBLIC_LEGACY_KEY)
        seed_saved(env.db, PUBLIC_LEGACY_KEY)

        profiles = env.profiles().get_all_profiles()

        assert [p.name for p in profiles] == ["p1", "p2"]
        assert profiles[0].source_config["password"] == "src-pw"
        assert profiles[1].target_config["password"] == "dst-pw"
        assert env.saved().get_all()[0]["password"] == "preset-pw"

        new_key = env.stored_key()
        assert new_key is not None and new_key != PUBLIC_LEGACY_KEY
        assert PUBLIC_LEGACY_KEY not in env.key_path.read_bytes()
        tokens = all_tokens(env.db)
        assert len(tokens) == 5
        assert not any(decrypts(PUBLIC_LEGACY_KEY, t) for t in tokens), "공개 키로 풀리면 안 된다"
        assert all(decrypts(new_key, t) for t in tokens)

    def test_legacy_rows_without_key_file_get_fresh_key(self, env):
        seed_profile(env.db, "p1", PUBLIC_LEGACY_KEY)
        seed_saved(env.db, PUBLIC_LEGACY_KEY)

        assert env.profiles().get_all_profiles()[0].source_config["password"] == "src-pw"

        new_key = env.stored_key()
        assert new_key is not None and new_key != PUBLIC_LEGACY_KEY
        assert not any(decrypts(PUBLIC_LEGACY_KEY, t) for t in all_tokens(env.db))

    def test_saved_connection_manager_also_runs_migration(self, env):
        """프리셋 관리자가 먼저 열려도 legacy 암호문을 남기거나 키를 따로 만들지 않는다."""
        seed_profile(env.db, "p1", PUBLIC_LEGACY_KEY)
        seed_saved(env.db, PUBLIC_LEGACY_KEY)

        assert env.saved().get_all()[0]["password"] == "preset-pw"

        assert not any(decrypts(PUBLIC_LEGACY_KEY, t) for t in all_tokens(env.db))
        assert env.profiles().get_all_profiles()[0].target_config["password"] == "dst-pw"

    def test_mixed_rows_keep_existing_key(self, env):
        """프리셋 관리자가 랜덤 키를 먼저 만들고 프로필은 legacy로 남은 혼합 상태."""
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "current", k0)
        seed_profile(env.db, "legacy", PUBLIC_LEGACY_KEY)

        names = [p.name for p in env.profiles().get_all_profiles()]

        assert names == ["current", "legacy"]
        assert env.stored_key() == k0
        tokens = all_tokens(env.db)
        assert all(decrypts(k0, t) for t in tokens)
        assert not any(decrypts(PUBLIC_LEGACY_KEY, t) for t in tokens)

    def test_read_path_has_no_legacy_fallback(self, env):
        seed_profile(env.db, "legacy", PUBLIC_LEGACY_KEY)
        with env.db.session_scope() as session:
            row = session.query(Profile).one()
            with pytest.raises(ProfileDecryptError):
                ConnectionProfile.from_db_model(row, Fernet(Fernet.generate_key()))

    def test_new_writes_never_use_legacy_key(self, env):
        seed_profile(env.db, "old", PUBLIC_LEGACY_KEY)
        manager = env.profiles()

        manager.create_profile({"name": "new", "source_config": SOURCE, "target_config": TARGET})
        env.saved().save_connection(
            {"host": "h2", "database": "d", "username": "u", "password": "x"}
        )

        assert env.stored_key() != PUBLIC_LEGACY_KEY
        assert not any(decrypts(PUBLIC_LEGACY_KEY, t) for t in all_tokens(env.db))

    def test_fresh_install_has_zero_legacy_ciphertext(self, env):
        manager = env.profiles()
        manager.create_profile({"name": "a", "source_config": SOURCE, "target_config": TARGET})
        manager.create_profile({"name": "b", "source_config": SOURCE, "target_config": TARGET})
        env.saved().save_connection(
            {"host": "h", "database": "d", "username": "u", "password": "p"}
        )

        tokens = all_tokens(env.db)
        assert len(tokens) == 5
        assert sum(decrypts(PUBLIC_LEGACY_KEY, t) for t in tokens) == 0
        assert env.key_path.read_bytes().startswith(KEY_FILE_HEADER)
        # 신규 설치에는 되돌릴 이전 상태가 없으므로 백업을 만들지 않는다.
        assert not [p for p in env.root.iterdir() if BACKUP_SUFFIX in p.name]

    def test_legacy_constant_is_read_only_inside_migration_routine(self):
        tree = ast.parse(Path(profile_module.__file__).read_text(encoding="utf-8"))
        readers: set[str] = set()
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Name) and node.id == "_LEGACY_PUBLIC_KEY":
                    readers.add(func.name)
        assert readers == {"_legacy_cipher"}

        callers: set[str] = set()
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef):
                continue
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_legacy_cipher"
                ):
                    callers.add(func.name)
        assert callers == {"ensure_profile_cipher"}

        assert not hasattr(ProfileManager, "_LEGACY_KEY")
        saved_src = Path(sys.modules[SavedConnectionManager.__module__].__file__).read_text(
            encoding="utf-8"
        )
        assert "LEGACY" not in saved_src


# --- M-05: OS 보호 저장소 래핑 ------------------------------------------------------------


class TestKeyWrap:
    def test_plain_key_file_is_wrapped_and_backed_up(self, env):
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p1", k0)

        assert env.profiles().get_all_profiles()[0].name == "p1"

        raw = env.key_path.read_bytes()
        assert raw.startswith(KEY_FILE_HEADER)
        assert k0 not in raw
        assert env.stored_key() == k0
        key_backup = env.root / (".encryption_key" + BACKUP_SUFFIX)
        db_backup = env.root / ("db_migration.db" + BACKUP_SUFFIX)
        assert key_backup.read_bytes() == k0
        assert db_backup.exists()

    def test_key_and_db_copied_to_other_machine_cannot_decrypt(self, env, tmp_path):
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p1", k0)
        seed_saved(env.db, k0)
        env.profiles()  # machine-A에서 래핑 완료
        env.close()

        stolen = Env(tmp_path / "machine-B", machine=b"machine-B")
        stolen.db.engine.dispose()
        shutil.copy2(env.key_path, stolen.key_path)
        shutil.copy2(env.db_path, stolen.db_path)
        stolen.db = make_db(stolen.db_path)
        before = snapshot(stolen.root)
        try:
            manager = stolen.profiles()  # 앱은 뜬다
            with pytest.raises(ProfileKeyUnavailableError):
                manager.get_all_profiles()
            saved = stolen.saved()
            assert saved.get_all()[0]["password"] == ""
            with pytest.raises(ProfileKeyUnavailableError):
                saved.save_connection({"host": "x", "database": "d", "username": "u"})

            # 키 파일 내용 자체로는 어떤 암호문도 풀 수 없다.
            for line in stolen.key_path.read_bytes().split(b":"):
                assert not any(decrypts(line.strip(), t) for t in all_tokens(stolen.db))
            # 열지 못했다고 지우거나 덮어쓰지 않는다.
            assert snapshot(stolen.root) == before
        finally:
            stolen.close()

    def test_non_windows_fallback_keeps_owner_only_plain_file_and_warns(self, env, caplog):
        caplog.set_level(logging.WARNING)
        env.key_path.write_bytes(Fernet.generate_key())
        if sys.platform != "win32":
            os.chmod(env.key_path, 0o644)

        manager = env.profiles(protector=PlainFileProtector())
        manager.create_profile({"name": "a", "source_config": SOURCE, "target_config": TARGET})

        assert not env.key_path.read_bytes().startswith(KEY_FILE_HEADER)
        if sys.platform != "win32":
            assert (env.key_path.stat().st_mode & 0o777) == 0o600
        assert any("OS 보호 저장소" in r.getMessage() for r in caplog.records)

    def test_second_start_is_a_no_op(self, env):
        seed_profile(env.db, "p1", PUBLIC_LEGACY_KEY)
        env.profiles()
        before = snapshot(env.root)

        env.profiles()
        env.saved()

        assert snapshot(env.root) == before


# --- 안전성: 도중 실패, 복구, 데이터 보존 --------------------------------------------------


class TestFailureSafety:
    def test_failure_mid_reencryption_rolls_back_and_next_start_recovers(self, env, monkeypatch):
        for i in range(3):
            seed_profile(env.db, f"p{i}", PUBLIC_LEGACY_KEY)
        before_tokens = all_tokens(env.db)

        real = profile_module._reencrypt_token
        calls = {"n": 0}

        def flaky(legacy, target, token):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("주입된 중간 실패")
            return real(legacy, target, token)

        monkeypatch.setattr(profile_module, "_reencrypt_token", flaky)
        manager = env.profiles()  # 예외가 앱 밖으로 새지 않는다

        assert all_tokens(env.db) == before_tokens, "트랜잭션이 통째로 되돌아가야 한다"
        persisted = env.stored_key()
        assert persisted is not None and persisted != PUBLIC_LEGACY_KEY
        assert (env.root / ("db_migration.db" + BACKUP_SUFFIX)).exists()
        with pytest.raises(ProfileDecryptError):
            manager.get_all_profiles()

        monkeypatch.setattr(profile_module, "_reencrypt_token", real)
        recovered = env.profiles().get_all_profiles()

        assert [p.name for p in recovered] == ["p0", "p1", "p2"]
        assert env.stored_key() == persisted
        assert not any(decrypts(PUBLIC_LEGACY_KEY, t) for t in all_tokens(env.db))

    def test_key_wrap_write_failure_keeps_plain_key_and_app_runs(self, env, monkeypatch):
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p1", k0)
        tokens = all_tokens(env.db)

        real_replace = os.replace

        def fail_on_key(src, dst):
            if Path(dst).name == ".encryption_key":
                raise OSError("주입된 교체 실패")
            return real_replace(src, dst)

        monkeypatch.setattr(secret_store.os, "replace", fail_on_key)
        manager = env.profiles()

        assert env.key_path.read_bytes() == k0
        assert all_tokens(env.db) == tokens
        assert manager.get_all_profiles()[0].name == "p1"
        assert not [p for p in env.root.iterdir() if p.name.endswith(".tmp")]

        monkeypatch.setattr(secret_store.os, "replace", real_replace)
        env.profiles()
        assert env.key_path.read_bytes().startswith(KEY_FILE_HEADER)

    def test_key_write_failure_during_rotation_changes_nothing(self, env, monkeypatch):
        seed_profile(env.db, "p1", PUBLIC_LEGACY_KEY)
        tokens = all_tokens(env.db)

        real_replace = os.replace

        def fail_on_key(src, dst):
            if Path(dst).name == ".encryption_key":
                raise OSError("주입된 교체 실패")
            return real_replace(src, dst)

        monkeypatch.setattr(secret_store.os, "replace", fail_on_key)
        manager = env.profiles()

        assert not env.key_path.exists()
        assert all_tokens(env.db) == tokens
        with pytest.raises(ProfileKeyUnavailableError):
            manager.get_all_profiles()
        with pytest.raises(ProfileKeyUnavailableError):
            manager.create_profile({"name": "x", "source_config": SOURCE, "target_config": TARGET})

        monkeypatch.setattr(secret_store.os, "replace", real_replace)
        assert env.profiles().get_all_profiles()[0].name == "p1"

    def test_backup_failure_aborts_before_any_change(self, env, monkeypatch):
        env.key_path.write_bytes(PUBLIC_LEGACY_KEY)
        seed_profile(env.db, "p1", PUBLIC_LEGACY_KEY)
        before = snapshot(env.root)
        tokens = all_tokens(env.db)

        def boom(*args, **kwargs):
            raise OSError("주입된 백업 실패")

        monkeypatch.setattr(profile_module, "backup_sqlite_database", boom)
        manager = env.profiles()

        assert all_tokens(env.db) == tokens
        assert env.key_path.read_bytes() == PUBLIC_LEGACY_KEY
        # 백업 사본이 생길 수는 있어도 원본 파일은 그대로다.
        after = {k: v for k, v in snapshot(env.root).items() if BACKUP_SUFFIX not in k}
        assert after == before
        with pytest.raises(ProfileKeyUnavailableError):
            manager.get_all_profiles()

    def test_undecryptable_rows_are_never_deleted(self, env):
        lost = Fernet.generate_key()
        seed_profile(env.db, "orphan", lost)
        tokens = all_tokens(env.db)

        manager = env.profiles()

        assert all_tokens(env.db) == tokens
        with pytest.raises(ProfileDecryptError):
            manager.get_all_profiles()

    def test_corrupt_key_file_is_left_untouched(self, env):
        env.key_path.write_bytes(b"garbage")
        seed_profile(env.db, "p1", Fernet.generate_key())
        before = snapshot(env.root)

        manager = env.profiles()

        assert snapshot(env.root) == before
        with pytest.raises(ProfileKeyUnavailableError):
            manager.get_all_profiles()
