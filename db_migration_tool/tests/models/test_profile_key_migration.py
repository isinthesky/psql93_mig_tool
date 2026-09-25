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
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.local_db import (
    Base,
    LocalDatabase,
    MigrationHistory,
    Profile,
    SavedConnection,
)
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

    def restart(self) -> None:
        """프로세스 재시작을 모사한다 — 이 프로세스가 공유하던 키 준비 결과를 잊는다.

        같은 프로세스 안에서는 키 준비·마이그레이션을 (DB, 키 파일)마다 한 번만 한다(H-06 2차
        리뷰). "다음 실행" 동작을 확인하는 테스트는 매니저를 새로 만들기 전에 이것을 부른다.
        """
        profile_module._KEY_HANDLES.clear()

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

    def test_mixed_rows_are_moved_to_one_fresh_key(self, env):
        """프리셋 관리자가 랜덤 평문 키를 먼저 만들고 프로필은 legacy로 남은 혼합 상태.

        평문 키는 래핑하면서 교체한다(리뷰 지적 1). 두 종류 암호문 모두 새 키 하나로 모인다.
        """
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "current", k0)
        seed_profile(env.db, "legacy", PUBLIC_LEGACY_KEY)

        profiles = env.profiles().get_all_profiles()

        assert [(p.name, p.locked) for p in profiles] == [("current", False), ("legacy", False)]
        new_key = env.stored_key()
        assert new_key is not None and new_key != k0
        tokens = all_tokens(env.db)
        assert all(decrypts(new_key, t) for t in tokens)
        assert not any(decrypts(k0, t) for t in tokens)
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
    def test_plain_key_file_is_rotated_and_wrapped(self, env):
        """평문 키는 래핑하면서 새 키로 교체한다. 백업은 검증 뒤 지운다(리뷰 지적 1)."""
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p1", k0)

        assert env.profiles().get_all_profiles()[0].source_config["password"] == "src-pw"

        raw = env.key_path.read_bytes()
        assert raw.startswith(KEY_FILE_HEADER)
        assert k0 not in raw
        stored = env.key_file().read()
        assert stored.key is not None and stored.key != k0
        assert stored.previous is None and stored.migrated
        assert not [p for p in env.root.iterdir() if BACKUP_SUFFIX in p.name]

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
            assert not manager.key_available
            profiles = manager.get_all_profiles()  # 이름만 보이는 잠긴 프로필
            assert [(p.name, p.locked) for p in profiles] == [("p1", True)]
            assert profiles[0].source_config.get("password", "") == ""
            with pytest.raises(ProfileKeyUnavailableError):
                manager.create_profile(
                    {"name": "x", "source_config": SOURCE, "target_config": TARGET}
                )
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

        env.restart()
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

        def flaky(source, target, token):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("주입된 중간 실패")
            return real(source, target, token)

        monkeypatch.setattr(profile_module, "_reencrypt_token", flaky)
        manager = env.profiles()  # 예외가 앱 밖으로 새지 않는다

        assert all_tokens(env.db) == before_tokens, "트랜잭션이 통째로 되돌아가야 한다"
        persisted = env.stored_key()
        assert persisted is not None and persisted != PUBLIC_LEGACY_KEY
        assert (env.root / ("db_migration.db" + BACKUP_SUFFIX)).exists()
        # 공개 키는 읽기 경로에 없다 — 재암호화가 끝나기 전 legacy 행은 잠긴 채 보인다.
        assert [p.locked for p in manager.get_all_profiles()] == [True, True, True]

        monkeypatch.setattr(profile_module, "_reencrypt_token", real)
        env.restart()
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
        env.restart()
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
        assert [p.locked for p in manager.get_all_profiles()] == [True]
        with pytest.raises(ProfileKeyUnavailableError):
            manager.create_profile({"name": "x", "source_config": SOURCE, "target_config": TARGET})

        monkeypatch.setattr(secret_store.os, "replace", real_replace)
        env.restart()
        recovered = env.profiles()
        assert recovered.key_available
        assert [(p.name, p.locked) for p in recovered.get_all_profiles()] == [("p1", False)]

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
        assert [p.locked for p in manager.get_all_profiles()] == [True]
        with pytest.raises(ProfileKeyUnavailableError):
            manager.create_profile({"name": "x", "source_config": SOURCE, "target_config": TARGET})

    def test_undecryptable_rows_are_never_deleted(self, env):
        lost = Fernet.generate_key()
        seed_profile(env.db, "orphan", lost)
        tokens = all_tokens(env.db)

        manager = env.profiles()

        assert all_tokens(env.db) == tokens
        assert [(p.name, p.locked) for p in manager.get_all_profiles()] == [("orphan", True)]
        assert all_tokens(env.db) == tokens

    def test_corrupt_key_file_is_left_untouched(self, env):
        env.key_path.write_bytes(b"garbage")
        seed_profile(env.db, "p1", Fernet.generate_key())
        before = snapshot(env.root)

        manager = env.profiles()

        assert snapshot(env.root) == before
        assert [p.locked for p in manager.get_all_profiles()] == [True]
        with pytest.raises(ProfileKeyUnavailableError):
            manager.create_profile({"name": "x", "source_config": SOURCE, "target_config": TARGET})
        assert snapshot(env.root) == before


# --- 독립 리뷰 지적 회귀 테스트 -------------------------------------------------------------
#
# 1) 평문 키 → 래핑 업그레이드에서 평문 키 사본이 남아 현재 DB를 풀 수 있었다(M-05 무력화).
# 2) legacy 설치 마이그레이션 뒤 DB 백업이 공개 상수로 풀렸다(H-06 잔존).
# 3) 마이그레이션 완료 뒤에도 매 시작마다 공개 키 암호문을 재암호화해 "세탁"했다.
# 4) 복호화 불가 행 하나 또는 키 불가 상태에서 목록 전체가 막히고 복구 경로가 없었다.


def sqlite_tokens_in(directory: Path) -> list[str]:
    """디렉터리 안 모든 SQLite 파일(백업 포함)의 암호문."""
    tokens: list[str] = []
    for p in sorted(directory.iterdir()):
        if not p.is_file() or p.read_bytes()[:16] != b"SQLite format 3\x00":
            continue
        con = sqlite3.connect(p)
        try:
            for src, dst in con.execute("SELECT source_config, target_config FROM profiles"):
                tokens += [src, dst]
            for (pw,) in con.execute("SELECT password FROM saved_connections"):
                if pw:
                    tokens.append(pw)
        finally:
            con.close()
    return tokens


def plain_keys_in(directory: Path) -> list[bytes]:
    """디렉터리 안에서 그대로 Fernet 키로 쓸 수 있는 파일 내용."""
    keys: list[bytes] = []
    for p in sorted(directory.iterdir()):
        if not p.is_file():
            continue
        raw = p.read_bytes().strip()
        try:
            Fernet(raw)
        except (ValueError, TypeError):
            continue
        keys.append(raw)
    return keys


def backups_in(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if BACKUP_SUFFIX in p.name)


def by_name(profiles) -> dict[str, ConnectionProfile]:
    return {p.name: p for p in profiles}


class TestReviewNoKeyMaterialLeftBehind:
    def test_plain_key_upgrade_rotates_and_leaves_no_plain_key_that_opens_any_db(self, env):
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p1", k0)
        seed_saved(env.db, k0)

        assert env.profiles().get_all_profiles()[0].source_config["password"] == "src-pw"

        new_key = env.stored_key()
        assert new_key is not None and new_key != k0, "래핑할 때도 키를 교체해야 한다"
        tokens = sqlite_tokens_in(env.root)
        assert len(tokens) >= 3
        assert all(decrypts(new_key, t) for t in all_tokens(env.db))
        assert sum(decrypts(k0, t) for t in tokens) == 0, "옛 평문 키로 풀리는 사본이 남았다"
        for key in plain_keys_in(env.root):
            assert sum(decrypts(key, t) for t in tokens) == 0
        assert backups_in(env.root) == [], "검증을 마친 마이그레이션 백업은 남기지 않는다"

    def test_whole_directory_copied_to_other_machine_exposes_nothing(self, env, tmp_path):
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p1", k0)
        seed_saved(env.db, k0)
        env.profiles()
        env.close()

        stolen_root = tmp_path / "machine-B"
        shutil.copytree(env.root, stolen_root)
        tokens = sqlite_tokens_in(stolen_root)
        assert tokens
        for key in [k0, PUBLIC_LEGACY_KEY, *plain_keys_in(stolen_root)]:
            assert sum(decrypts(key, t) for t in tokens) == 0

        stolen = Env(stolen_root, machine=b"machine-B")
        try:
            assert all(p.locked for p in stolen.profiles().get_all_profiles())
        finally:
            stolen.close()

    def test_legacy_upgrade_leaves_no_public_key_ciphertext_in_any_file(self, env):
        env.key_path.write_bytes(PUBLIC_LEGACY_KEY)
        seed_profile(env.db, "p1", PUBLIC_LEGACY_KEY)
        seed_profile(env.db, "p2", PUBLIC_LEGACY_KEY)
        seed_saved(env.db, PUBLIC_LEGACY_KEY)

        env.profiles().get_all_profiles()

        tokens = sqlite_tokens_in(env.root)
        assert len(tokens) >= 5
        assert sum(decrypts(PUBLIC_LEGACY_KEY, t) for t in tokens) == 0
        assert backups_in(env.root) == []

    def test_key_backup_taken_during_migration_is_wrapped(self, env, monkeypatch):
        """마이그레이션 도중(실패로 백업이 남는 동안)에도 키 백업은 평문이 아니다."""
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p1", k0)

        def boom(*args, **kwargs):
            raise RuntimeError("주입된 재암호화 실패")

        monkeypatch.setattr(profile_module, "_reencrypt_token", boom)
        env.profiles()

        names = backups_in(env.root)
        assert any(n.startswith(".encryption_key") for n in names)
        for name in names:
            assert k0 not in (env.root / name).read_bytes()


class TestReviewNoLaunderingAfterMigration:
    def test_forged_public_key_row_is_not_reencrypted_on_next_start(self, env):
        seed_profile(env.db, "p1", PUBLIC_LEGACY_KEY)
        env.profiles()  # 1회 마이그레이션
        env.restart()
        seed_profile(env.db, "forged", PUBLIC_LEGACY_KEY)
        forged_tokens = [t for t in all_tokens(env.db) if decrypts(PUBLIC_LEGACY_KEY, t)]
        assert len(forged_tokens) == 2
        files_before = sorted(p.name for p in env.root.iterdir())

        profiles = by_name(env.profiles().get_all_profiles())
        env.saved()

        assert profiles["forged"].locked
        assert "attacker" not in json.dumps(profiles["forged"].source_config)
        assert not profiles["p1"].locked
        assert profiles["p1"].source_config["password"] == "src-pw"
        assert [t for t in all_tokens(env.db) if decrypts(PUBLIC_LEGACY_KEY, t)] == forged_tokens
        assert sorted(p.name for p in env.root.iterdir()) == files_before, "백업이 쌓이면 안 된다"

    def test_foreign_plain_key_file_after_migration_is_refused(self, env):
        env.profiles().create_profile(
            {"name": "p1", "source_config": SOURCE, "target_config": TARGET}
        )
        attacker = Fernet.generate_key()
        env.key_path.write_bytes(attacker)
        seed_profile(env.db, "forged", attacker)
        before = snapshot(env.root)

        env.restart()
        manager = env.profiles()

        assert snapshot(env.root) == before, "낯선 평문 키를 감싸거나 수용하면 안 된다"
        assert not manager.key_available
        assert all(p.locked for p in manager.get_all_profiles())
        with pytest.raises(ProfileKeyUnavailableError):
            manager.create_profile({"name": "x", "source_config": SOURCE, "target_config": TARGET})

    def test_deleted_key_file_is_not_silently_replaced_when_data_exists(self, env):
        env.profiles().create_profile(
            {"name": "p1", "source_config": SOURCE, "target_config": TARGET}
        )
        env.key_path.unlink()
        before = snapshot(env.root)

        env.restart()
        manager = env.profiles()

        assert not env.key_path.exists()
        assert snapshot(env.root) == before
        assert [p.locked for p in manager.get_all_profiles()] == [True]
        with pytest.raises(ProfileKeyUnavailableError):
            manager.create_profile({"name": "x", "source_config": SOURCE, "target_config": TARGET})

    def test_current_key_reappearing_as_plain_file_is_treated_as_exposed(self, env, caplog):
        manager = env.profiles()
        manager.create_profile({"name": "p1", "source_config": SOURCE, "target_config": TARGET})
        exposed = env.stored_key()
        assert exposed is not None
        env.key_path.write_bytes(exposed)  # 누군가 현재 키를 평문으로 꺼내 놓았다
        caplog.set_level(logging.WARNING)
        env.restart()

        assert env.profiles().get_all_profiles()[0].source_config["password"] == "src-pw"

        assert env.key_path.read_bytes().startswith(KEY_FILE_HEADER)
        assert env.stored_key() != exposed, "노출된 키는 교체해야 한다"
        assert sum(decrypts(exposed, t) for t in sqlite_tokens_in(env.root)) == 0
        assert any("평문" in r.getMessage() for r in caplog.records)

    def test_db_marker_removed_does_not_reopen_legacy_migration(self, env):
        """DB 쪽 표식을 지워도 보호된 키 파일의 완료 표식이 legacy 수용을 막는다."""
        env.profiles().create_profile(
            {"name": "p1", "source_config": SOURCE, "target_config": TARGET}
        )
        with sqlite3.connect(env.db_path) as con:
            con.execute(f"DELETE FROM {profile_module.KEY_STATE_TABLE}")
        seed_profile(env.db, "forged", PUBLIC_LEGACY_KEY)

        env.restart()
        profiles = by_name(env.profiles().get_all_profiles())

        assert profiles["forged"].locked
        assert not profiles["p1"].locked


class TestReviewRecovery:
    def test_one_undecryptable_profile_does_not_hide_the_others(self, env):
        env.profiles().create_profile(
            {"name": "good", "source_config": SOURCE, "target_config": TARGET}
        )
        seed_profile(env.db, "foreign", Fernet.generate_key())

        profiles = by_name(env.profiles().get_all_profiles())

        assert set(profiles) == {"good", "foreign"}
        assert not profiles["good"].locked
        assert profiles["good"].target_config["password"] == "dst-pw"
        assert profiles["foreign"].locked
        assert profiles["foreign"].lock_reason
        assert profiles["foreign"].source_config.get("password", "") == ""

    def test_locked_profile_can_be_reentered_or_deleted(self, env):
        manager = env.profiles()
        seed_profile(env.db, "foreign-1", Fernet.generate_key())
        seed_profile(env.db, "foreign-2", Fernet.generate_key())
        ids = {p.name: p.id for p in manager.get_all_profiles()}

        selected = manager.get_profile(ids["foreign-1"])
        assert selected is not None and selected.locked  # 선택은 되어야 편집·삭제할 수 있다

        updated = manager.update_profile(
            ids["foreign-1"],
            {"name": "foreign-1", "source_config": SOURCE, "target_config": TARGET},
        )
        assert not updated.locked
        assert manager.delete_profile(ids["foreign-2"])
        assert [(p.name, p.locked) for p in manager.get_all_profiles()] == [("foreign-1", False)]

    def test_profile_name_lookup_needs_no_key(self, env, tmp_path):
        env.profiles().create_profile(
            {"name": "p1", "source_config": SOURCE, "target_config": TARGET}
        )
        env.close()
        other_root = tmp_path / "machine-B"
        shutil.copytree(env.root, other_root)
        other = Env(other_root, machine=b"machine-B")
        try:
            manager = other.profiles()
            assert not manager.key_available
            profile_id = manager.get_all_profiles()[0].id
            assert manager.get_profile_name(profile_id) == "p1"
            assert manager.get_profile_name(9999) is None
        finally:
            other.close()

    def test_key_reset_quarantines_old_key_and_keeps_history_and_rows(self, env, tmp_path):
        env.profiles().create_profile(
            {"name": "old", "source_config": SOURCE, "target_config": TARGET}
        )
        with env.db.session_scope() as session:
            session.add(MigrationHistory(profile_id=1, status="completed"))
        env.close()
        root_b = tmp_path / "machine-B"
        shutil.copytree(env.root, root_b)
        moved = Env(root_b, machine=b"machine-B")
        try:
            old_key_bytes = moved.key_path.read_bytes()
            old_tokens = all_tokens(moved.db)
            manager = moved.profiles()
            assert not manager.key_available

            manager.reset_encryption_key()

            quarantined = [p for p in root_b.iterdir() if ".unusable" in p.name]
            assert [p.read_bytes() for p in quarantined] == [old_key_bytes], "옛 키는 보관"
            assert all_tokens(moved.db) == old_tokens, "옛 암호문은 지우지 않는다"
            with moved.db.session_scope() as session:
                assert session.query(MigrationHistory).count() == 1
            manager.create_profile(
                {"name": "new", "source_config": SOURCE, "target_config": TARGET}
            )

            moved.restart()
            restarted = by_name(moved.profiles().get_all_profiles())
            assert restarted["old"].locked
            assert not restarted["new"].locked
        finally:
            moved.close()

    def test_key_reset_is_refused_while_key_is_usable(self, env):
        manager = env.profiles()
        manager.create_profile({"name": "p1", "source_config": SOURCE, "target_config": TARGET})
        before = snapshot(env.root)

        with pytest.raises(RuntimeError):
            manager.reset_encryption_key()

        assert snapshot(env.root) == before


class TestReviewResumableRotation:
    def test_wrap_rotation_failing_mid_reencryption_keeps_app_usable_and_resumes(
        self, env, monkeypatch
    ):
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p0", k0)
        seed_profile(env.db, "p1", k0)
        tokens = all_tokens(env.db)

        real = profile_module._reencrypt_token
        calls = {"n": 0}

        def flaky(*args):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("주입된 중간 실패")
            return real(*args)

        monkeypatch.setattr(profile_module, "_reencrypt_token", flaky)
        manager = env.profiles()

        assert all_tokens(env.db) == tokens, "트랜잭션이 통째로 되돌아가야 한다"
        assert [p.locked for p in manager.get_all_profiles()] == [False, False]
        assert k0 not in env.key_path.read_bytes()
        pending = env.key_file().read()
        assert pending.previous == k0 and pending.key != k0
        # 도중 상태에서도 새 기록은 새 키로만 한다.
        manager.create_profile({"name": "p2", "source_config": SOURCE, "target_config": TARGET})
        new_tokens = [t for t in all_tokens(env.db) if t not in tokens]
        assert len(new_tokens) == 2
        assert all(decrypts(pending.key, t) and not decrypts(k0, t) for t in new_tokens)
        assert backups_in(env.root), "실패한 동안에는 백업을 남긴다"

        monkeypatch.setattr(profile_module, "_reencrypt_token", real)
        env.restart()
        recovered = env.profiles().get_all_profiles()

        assert [p.name for p in recovered] == ["p0", "p1", "p2"]
        new_key = env.stored_key()
        assert new_key is not None and new_key != k0
        assert all(decrypts(new_key, t) for t in all_tokens(env.db))
        assert sum(decrypts(k0, t) for t in sqlite_tokens_in(env.root)) == 0
        assert env.key_file().read().previous is None
        assert backups_in(env.root) == []

    def test_crash_before_dropping_previous_key_is_completed_on_next_start(self, env, monkeypatch):
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p1", k0)

        real_write = WrappedKeyFile.write

        def write_then_crash(self, *args, **kwargs):
            # 재암호화 커밋 뒤 '이전 키를 버리는' 마지막 키 파일 쓰기에서만 실패시킨다.
            if self.path == env.key_path and kwargs.get("migrated"):
                raise OSError("주입된 마무리 실패")
            return real_write(self, *args, **kwargs)

        monkeypatch.setattr(WrappedKeyFile, "write", write_then_crash)
        env.profiles()
        assert env.key_file().read().previous == k0

        monkeypatch.setattr(WrappedKeyFile, "write", real_write)
        env.restart()
        assert env.profiles().get_all_profiles()[0].name == "p1"

        assert env.key_file().read().previous is None
        assert backups_in(env.root) == []
        assert sum(decrypts(k0, t) for t in sqlite_tokens_in(env.root)) == 0


# --- 2차 리뷰 major: 같은 프로세스의 복수 매니저 키 갈림 -----------------------------------
#
# 첫 ProfileManager(MainViewModel)가 백업·키 기록 실패로 fallback(k0)을 받은 뒤, 같은 세션의
# ConnectionDialog(SavedConnectionManager)나 HistoryDialog(ProfileManager)가 마이그레이션을
# 다시 돌려 K1로 교체·재암호화하면, 첫 매니저는 k0을 계속 쥔다. (1) 목록이 전부 잠기고
# (2) 이 세션에서 저장한 값이 k0으로 암호화돼 재시작 뒤 영구히 잠긴다.


def fail_first_db_backup(monkeypatch) -> dict[str, int]:
    """첫 DB 백업 한 번만 실패시켜 첫 매니저가 fallback 키로 끝나게 한다."""
    real = profile_module.backup_sqlite_database
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("주입된 첫 백업 실패")
        return real(*args, **kwargs)

    monkeypatch.setattr(profile_module, "backup_sqlite_database", flaky)
    return calls


def other_manager(env: Env, kind: str):
    """같은 세션에서 나중에 열리는 매니저 — ConnectionDialog 또는 HistoryDialog."""
    return env.saved() if kind == "connection_dialog" else env.profiles()


def saved_passwords(env: Env) -> list[str]:
    return sorted(c["password"] for c in env.saved().get_all())


NEW_SOURCE = {**SOURCE, "password": "new-src-pw"}


class TestReviewSameProcessKeyDivergence:
    @pytest.mark.parametrize("second", ["connection_dialog", "history_dialog"])
    def test_fallback_then_second_manager_then_create_survives_restart(
        self, env, monkeypatch, second
    ):
        """필수 회귀: 첫 생성 fallback → 둘째 매니저 → 첫 매니저로 create → 재시작 후 잠김 0."""
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p1", k0)
        seed_saved(env.db, k0)
        fail_first_db_backup(monkeypatch)

        main = env.profiles()  # MainViewModel — 백업 실패로 fallback(k0)
        assert main.key_available
        assert env.key_path.read_bytes() == k0

        other_manager(env, second)  # 같은 세션의 다이얼로그

        created = main.create_profile(
            {"name": "new", "source_config": NEW_SOURCE, "target_config": TARGET}
        )
        assert not created.locked

        env.restart()
        restarted = by_name(env.profiles().get_all_profiles())

        assert sorted(restarted) == ["new", "p1"]
        assert [n for n, p in restarted.items() if p.locked] == [], "재시작 뒤 잠긴 프로필"
        assert restarted["new"].source_config["password"] == "new-src-pw"
        assert restarted["p1"].source_config["password"] == "src-pw"
        assert saved_passwords(env) == ["preset-pw"]
        # 재시작한 프로세스가 마이그레이션을 끝냈다 — k0으로 풀리는 사본이 없다.
        assert env.stored_key() != k0
        assert sum(decrypts(k0, t) for t in sqlite_tokens_in(env.root)) == 0
        assert backups_in(env.root) == []

    @pytest.mark.parametrize("second", ["connection_dialog", "history_dialog"])
    def test_fallback_is_not_retried_in_same_process(self, env, monkeypatch, second):
        """fallback으로 끝났으면 같은 프로세스에서 백업·키 교체를 다시 시도하지 않는다."""
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p1", k0)
        calls = fail_first_db_backup(monkeypatch)

        env.profiles()
        other_manager(env, second)
        other_manager(env, second)

        assert calls["n"] == 1
        assert env.key_path.read_bytes() == k0, "같은 프로세스에서 키를 다시 교체하면 안 된다"
        assert backups_in(env.root) == []

    @pytest.mark.parametrize("second", ["connection_dialog", "history_dialog"])
    def test_first_manager_list_is_not_locked_after_second_manager_opens(
        self, env, monkeypatch, second
    ):
        """지적 (1): 둘째 매니저가 열린 뒤 첫 매니저의 목록 갱신에서 프로필이 잠기지 않는다."""
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p1", k0)
        seed_profile(env.db, "p2", k0)
        fail_first_db_backup(monkeypatch)

        main = env.profiles()
        assert [p.locked for p in main.get_all_profiles()] == [False, False]

        other_manager(env, second)

        assert [p.locked for p in main.get_all_profiles()] == [False, False]
        profile = main.get_profile(main.get_all_profiles()[0].id)
        assert profile is not None and profile.source_config["password"] == "src-pw"

    def test_update_and_preset_saved_in_fallback_session_survive_restart(self, env, monkeypatch):
        """지적 (2)의 update_profile·프리셋 저장 경로."""
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p1", k0)
        fail_first_db_backup(monkeypatch)

        main = env.profiles()
        dialog = env.saved()
        history = env.profiles()
        pid = main.get_all_profiles()[0].id

        main.update_profile(
            pid, {"name": "p1", "source_config": NEW_SOURCE, "target_config": TARGET}
        )
        dialog.save_connection({"host": "h2", "database": "d", "username": "u", "password": "p2"})
        assert history.get_profile(pid).source_config["password"] == "new-src-pw"

        env.restart()
        restarted = env.profiles().get_all_profiles()

        assert [(p.name, p.locked) for p in restarted] == [("p1", False)]
        assert restarted[0].source_config["password"] == "new-src-pw"
        assert saved_passwords(env) == ["p2"]

    def test_concurrent_managers_share_one_key(self, env, monkeypatch):
        """동시 생성: 여러 매니저가 한꺼번에 열려도 한 번만 준비하고 같은 키를 쓴다."""
        k0 = Fernet.generate_key()
        env.key_path.write_bytes(k0)
        seed_profile(env.db, "p1", k0)
        seed_saved(env.db, k0)
        calls = fail_first_db_backup(monkeypatch)

        count = 6
        barrier = threading.Barrier(count)
        managers: list = [None] * count
        errors: list[BaseException] = []

        def build(i: int) -> None:
            try:
                barrier.wait()
                managers[i] = env.profiles() if i % 2 == 0 else env.saved()
            except BaseException as exc:  # pragma: no cover - 실패 보고용
                errors.append(exc)

        threads = [threading.Thread(target=build, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors
        assert calls["n"] == 1, "마이그레이션(백업 시도)은 프로세스에서 한 번만 한다"
        assert env.key_path.read_bytes() == k0

        for i, manager in enumerate(managers):
            if i % 2 == 0:
                manager.create_profile(
                    {"name": f"new-{i}", "source_config": NEW_SOURCE, "target_config": TARGET}
                )
            else:
                manager.save_connection(
                    {"host": f"h{i}", "database": "d", "username": "u", "password": f"pw-{i}"}
                )

        env.restart()
        restarted = by_name(env.profiles().get_all_profiles())

        assert sorted(restarted) == ["new-0", "new-2", "new-4", "p1"]
        assert [n for n, p in restarted.items() if p.locked] == []
        assert saved_passwords(env) == ["preset-pw", "pw-1", "pw-3", "pw-5"]

    def test_plain_fallback_key_is_not_rotated_again_in_same_process(self, env):
        """DPAPI가 잠깐 실패해 평문 키로 시작한 세션에서, 뒤에 열린 매니저가 다시 교체하지 않는다."""
        first = env.profiles(protector=FakeDpapi(env.machine, fail_protect=True))
        assert first.key_available
        plain_key = env.key_path.read_bytes()
        assert not plain_key.startswith(KEY_FILE_HEADER)

        second = env.saved()  # 보호기는 이제 동작한다

        assert env.key_path.read_bytes() == plain_key
        first.create_profile({"name": "a", "source_config": SOURCE, "target_config": TARGET})
        second.save_connection({"host": "h", "database": "d", "username": "u", "password": "pw"})

        env.restart()
        restarted = env.profiles().get_all_profiles()

        assert [(p.name, p.locked) for p in restarted] == [("a", False)]
        assert saved_passwords(env) == ["pw"]
        assert env.key_path.read_bytes().startswith(KEY_FILE_HEADER), "재시작 때 감싼다"

    def test_key_reset_is_shared_by_every_manager_in_the_process(self, env, tmp_path):
        """키 재설정도 프로세스의 모든 매니저가 함께 본다(재설정 뒤 프리셋 저장이 막히지 않는다)."""
        env.profiles().create_profile(
            {"name": "old", "source_config": SOURCE, "target_config": TARGET}
        )
        env.close()
        root_b = tmp_path / "machine-B"
        shutil.copytree(env.root, root_b)
        moved = Env(root_b, machine=b"machine-B")
        try:
            main = moved.profiles()
            dialog = moved.saved()
            assert not main.key_available and not dialog.key_available

            main.reset_encryption_key()

            assert dialog.key_available
            dialog.save_connection({"host": "h", "database": "d", "username": "u", "password": "p"})
            moved.restart()
            assert saved_passwords(moved) == ["p"]
        finally:
            moved.close()


class TestReviewWriteGuardAgainstKeyFileChange:
    @staticmethod
    def _replace(env: Env) -> None:
        env.key_file().write(Fernet.generate_key(), migrated=True)

    @staticmethod
    def _delete(env: Env) -> None:
        env.key_path.unlink()

    @staticmethod
    def _corrupt(env: Env) -> None:
        env.key_path.write_bytes(b"garbage")

    @pytest.mark.parametrize("change", ["_replace", "_delete", "_corrupt"])
    def test_write_refused_when_key_file_changed_outside_session(self, env, change):
        manager = env.profiles()
        dialog = env.saved()
        created = manager.create_profile(
            {"name": "p1", "source_config": SOURCE, "target_config": TARGET}
        )
        tokens = all_tokens(env.db)

        getattr(self, change)(env)  # 다른 프로세스·사용자가 키 파일을 바꿨다

        with pytest.raises(ProfileKeyUnavailableError, match="키 파일"):
            manager.create_profile({"name": "x", "source_config": SOURCE, "target_config": TARGET})
        with pytest.raises(ProfileKeyUnavailableError, match="키 파일"):
            manager.update_profile(
                created.id, {"name": "p1", "source_config": NEW_SOURCE, "target_config": TARGET}
            )
        with pytest.raises(ProfileKeyUnavailableError, match="키 파일"):
            dialog.save_connection({"host": "h", "database": "d", "username": "u", "password": "p"})

        assert all_tokens(env.db) == tokens, "거부한 쓰기는 DB에 아무것도 남기지 않는다"
        with env.db.session_scope() as session:
            assert [p.name for p in session.query(Profile)] == ["p1"]

    def test_write_allowed_while_key_file_unchanged(self, env):
        manager = env.profiles()
        dialog = env.saved()
        manager.create_profile({"name": "p1", "source_config": SOURCE, "target_config": TARGET})
        dialog.save_connection({"host": "h", "database": "d", "username": "u", "password": "p"})
        env.restart()
        assert [p.locked for p in env.profiles().get_all_profiles()] == [False]
        assert saved_passwords(env) == ["p"]
