"""secret_store — 키 파일 래핑(M-05)과 원자적 파일 교체.

DPAPI는 Windows에서만 실제로 돌 수 있다. 그래서 두 층으로 검사한다.
- ctypes 마샬링(DATA_BLOB 구성·플래그·LocalFree)은 가짜 crypt32로 모든 플랫폼에서.
- 실제 CryptProtectData 왕복은 Windows에서만(`skipif`).
"""

from __future__ import annotations

import ctypes
import os
import sqlite3
import sys

import pytest
from cryptography.fernet import Fernet

from src.utils import secret_store
from src.utils.secret_store import (
    BACKUP_SUFFIX,
    KEY_FILE_HEADER,
    DataBlob,
    DpapiFunctions,
    DpapiProtector,
    KeyFileCorruptError,
    KeyProtectError,
    KeyUnprotectError,
    PlainFileProtector,
    WrappedKeyFile,
    atomic_write_bytes,
    backup_file,
    backup_sqlite_database,
    default_protector,
)
from tests.utils.fake_protectors import FakeDpapi

CRYPTPROTECT_UI_FORBIDDEN = 0x1
CRYPTPROTECT_LOCAL_MACHINE = 0x4


# --- 가짜 crypt32: 실제 C 시그니처대로 포인터를 받아 DATA_BLOB을 채운다 ---------------


class FakeCrypt32:
    ERROR_INVALID_DATA = 13

    def __init__(self, *, fail_protect: bool = False):
        self.fail_protect = fail_protect
        self.flags: list[int] = []
        self.allocated: set[int] = set()
        self.freed: list[int] = []
        self._buffers: dict[int, ctypes.Array[ctypes.c_char]] = {}
        self._last_error = 0

    @staticmethod
    def _read(ptr) -> bytes:
        if not ptr:
            return b""
        blob = ptr.contents
        return ctypes.string_at(blob.pbData, blob.cbData)

    def _emit(self, p_out, data: bytes) -> None:
        buf = ctypes.create_string_buffer(data, len(data))
        addr = ctypes.addressof(buf)
        self._buffers[addr] = buf
        self.allocated.add(addr)
        p_out.contents.cbData = len(data)
        p_out.contents.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))

    def protect(self, p_in, descr, p_entropy, reserved, prompt, flags, p_out) -> int:
        self.flags.append(flags)
        if self.fail_protect:
            self._last_error = 5
            return 0
        entropy = self._read(p_entropy)
        self._emit(p_out, b"WRAP|" + entropy.hex().encode() + b"|" + self._read(p_in)[::-1])
        return 1

    def unprotect(self, p_in, descr, p_entropy, reserved, prompt, flags, p_out) -> int:
        self.flags.append(flags)
        parts = self._read(p_in).split(b"|", 2)
        if len(parts) != 3 or parts[1] != self._read(p_entropy).hex().encode():
            self._last_error = self.ERROR_INVALID_DATA
            return 0
        self._emit(p_out, parts[2][::-1])
        return 1

    def local_free(self, ptr) -> None:
        self.freed.append(ctypes.cast(ptr, ctypes.c_void_p).value)

    def api(self) -> DpapiFunctions:
        return DpapiFunctions(
            protect=self.protect,
            unprotect=self.unprotect,
            local_free=self.local_free,
            last_error=lambda: self._last_error,
        )


class TestDpapiMarshalling:
    def test_roundtrip_uses_current_user_scope_and_frees_buffers(self):
        fake = FakeCrypt32()
        protector = DpapiProtector(api=fake.api())
        key = Fernet.generate_key()

        blob = protector.protect(key)
        assert key not in blob
        assert protector.unprotect(blob) == key

        # UI 금지, 그리고 LOCAL_MACHINE(장치 전체 범위)은 쓰지 않는다 → 현재 사용자 범위.
        assert fake.flags, "DPAPI가 호출되지 않았다"
        for flags in fake.flags:
            assert flags & CRYPTPROTECT_UI_FORBIDDEN
            assert not flags & CRYPTPROTECT_LOCAL_MACHINE
        # DPAPI가 할당한 출력 버퍼는 전부 LocalFree로 돌려준다.
        assert sorted(fake.freed) == sorted(fake.allocated)

    def test_entropy_mismatch_is_unprotect_error(self):
        fake = FakeCrypt32()
        blob = DpapiProtector(api=fake.api(), entropy=b"app-A").protect(b"secret")
        with pytest.raises(KeyUnprotectError):
            DpapiProtector(api=fake.api(), entropy=b"app-B").unprotect(blob)

    def test_protect_failure_is_protect_error(self):
        protector = DpapiProtector(api=FakeCrypt32(fail_protect=True).api())
        with pytest.raises(KeyProtectError):
            protector.protect(b"secret")

    def test_constructor_does_not_load_windows_dll(self):
        """비Windows에서 DpapiProtector를 만들기만 해도 터지면 안 된다(지연 로딩)."""
        DpapiProtector()

    def test_data_blob_layout(self):
        blob = DataBlob()
        assert blob.cbData == 0
        assert not blob.pbData


@pytest.mark.skipif(sys.platform != "win32", reason="실제 DPAPI는 Windows 전용")
class TestRealDpapi:
    def test_roundtrip(self):
        protector = DpapiProtector()
        key = Fernet.generate_key()
        blob = protector.protect(key)
        assert key not in blob
        assert protector.unprotect(blob) == key

    def test_other_entropy_cannot_unprotect(self):
        blob = DpapiProtector(entropy=b"A").protect(b"secret")
        with pytest.raises(KeyUnprotectError):
            DpapiProtector(entropy=b"B").unprotect(blob)


class TestDefaultProtector:
    def test_windows_uses_dpapi(self):
        protector = default_protector(platform="win32")
        assert isinstance(protector, DpapiProtector)
        assert protector.os_protected

    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_other_platforms_fall_back_to_plain_file(self, platform):
        protector = default_protector(platform=platform)
        assert isinstance(protector, PlainFileProtector)
        assert not protector.os_protected


# --- 키 파일 -------------------------------------------------------------------------


class TestWrappedKeyFile:
    def test_absent(self, tmp_path):
        stored = WrappedKeyFile(tmp_path / ".encryption_key", FakeDpapi()).read()
        assert stored.key is None
        assert stored.form == "absent"

    def test_plain_legacy_file_is_recognized(self, tmp_path):
        key = Fernet.generate_key()
        path = tmp_path / ".encryption_key"
        path.write_bytes(key + b"\r\n")
        stored = WrappedKeyFile(path, FakeDpapi()).read()
        assert stored.key == key
        assert stored.form == "plain"

    def test_write_wraps_and_never_stores_raw_key(self, tmp_path):
        key = Fernet.generate_key()
        path = tmp_path / ".encryption_key"
        store = WrappedKeyFile(path, FakeDpapi())

        store.write(key)

        raw = path.read_bytes()
        assert raw.startswith(KEY_FILE_HEADER)
        assert key not in raw
        stored = store.read()
        assert stored.key == key
        assert stored.form == "wrapped"
        assert stored.scheme == "dpapi"

    def test_other_machine_cannot_unwrap(self, tmp_path):
        path = tmp_path / ".encryption_key"
        WrappedKeyFile(path, FakeDpapi(b"machine-A")).write(Fernet.generate_key())
        with pytest.raises(KeyUnprotectError):
            WrappedKeyFile(path, FakeDpapi(b"machine-B")).read()

    def test_dpapi_file_on_platform_without_dpapi(self, tmp_path):
        path = tmp_path / ".encryption_key"
        WrappedKeyFile(path, FakeDpapi()).write(Fernet.generate_key())
        with pytest.raises(KeyUnprotectError):
            WrappedKeyFile(path, PlainFileProtector()).read()

    def test_garbage_is_corrupt(self, tmp_path):
        path = tmp_path / ".encryption_key"
        path.write_bytes(b"not-a-fernet-key")
        with pytest.raises(KeyFileCorruptError):
            WrappedKeyFile(path, FakeDpapi()).read()

    def test_plain_protector_writes_raw_key_compatible_with_old_versions(self, tmp_path):
        key = Fernet.generate_key()
        path = tmp_path / ".encryption_key"
        WrappedKeyFile(path, PlainFileProtector()).write(key)
        assert path.read_bytes().strip() == key

    def test_protect_failure_leaves_existing_file(self, tmp_path):
        path = tmp_path / ".encryption_key"
        original = Fernet.generate_key()
        path.write_bytes(original)
        with pytest.raises(KeyProtectError):
            WrappedKeyFile(path, FakeDpapi(fail_protect=True)).write(Fernet.generate_key())
        assert path.read_bytes() == original

    def test_unverifiable_blob_is_not_written(self, tmp_path):
        """보호는 됐는데 되풀 수 없는 blob이면 기존 파일을 바꾸지 않는다."""

        class Broken(FakeDpapi):
            def unprotect(self, blob: bytes) -> bytes:
                return b"something else"

        path = tmp_path / ".encryption_key"
        original = Fernet.generate_key()
        path.write_bytes(original)
        with pytest.raises(KeyProtectError):
            WrappedKeyFile(path, Broken()).write(Fernet.generate_key())
        assert path.read_bytes() == original

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX 권한")
    def test_written_file_is_owner_only(self, tmp_path):
        path = tmp_path / ".encryption_key"
        WrappedKeyFile(path, FakeDpapi()).write(Fernet.generate_key())
        assert (path.stat().st_mode & 0o777) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX 권한")
    def test_harden_plain_permissions(self, tmp_path):
        path = tmp_path / ".encryption_key"
        path.write_bytes(Fernet.generate_key())
        os.chmod(path, 0o644)
        WrappedKeyFile(path, PlainFileProtector()).harden_permissions()
        assert (path.stat().st_mode & 0o777) == 0o600


class TestAtomicWrite:
    def test_replace_failure_keeps_original_and_cleans_temp(self, tmp_path, monkeypatch):
        path = tmp_path / "target.bin"
        path.write_bytes(b"original")

        def boom(src, dst):
            raise OSError("주입된 rename 실패")

        monkeypatch.setattr(secret_store.os, "replace", boom)
        with pytest.raises(OSError):
            atomic_write_bytes(path, b"new")

        assert path.read_bytes() == b"original"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["target.bin"]

    def test_writes_new_content(self, tmp_path):
        path = tmp_path / "target.bin"
        atomic_write_bytes(path, b"new")
        assert path.read_bytes() == b"new"


class TestBackups:
    def test_backup_file_never_overwrites_previous_backup(self, tmp_path):
        path = tmp_path / ".encryption_key"
        path.write_bytes(b"v1")
        first = backup_file(path)
        path.write_bytes(b"v2")
        second = backup_file(path)

        assert first is not None and second is not None
        assert first.name == ".encryption_key" + BACKUP_SUFFIX
        assert first != second
        assert first.read_bytes() == b"v1"
        assert second.read_bytes() == b"v2"

    def test_backup_of_missing_file_is_none(self, tmp_path):
        assert backup_file(tmp_path / "missing") is None

    def test_sqlite_backup_is_consistent_copy(self, tmp_path):
        db_path = tmp_path / "db_migration.db"
        conn = sqlite3.connect(db_path)
        conn.execute("create table t (v text)")
        conn.execute("insert into t values ('a'), ('b')")
        conn.commit()

        backup = backup_sqlite_database(db_path)
        conn.close()

        assert backup is not None
        assert backup.name == "db_migration.db" + BACKUP_SUFFIX
        copy = sqlite3.connect(backup)
        try:
            assert copy.execute("select count(*) from t").fetchone()[0] == 2
        finally:
            copy.close()


class TestKeyFileJournal:
    """리뷰 반영: 키 교체 도중 이전 키를 보호된 채로 함께 두는 저널과 완료 표식."""

    def test_previous_key_and_migrated_flag_roundtrip_inside_protected_payload(self, tmp_path):
        path = tmp_path / ".encryption_key"
        current, previous = Fernet.generate_key(), Fernet.generate_key()
        store = WrappedKeyFile(path, FakeDpapi())

        store.write(current, previous=previous)

        raw = path.read_bytes()
        assert current not in raw and previous not in raw
        stored = store.read()
        assert (stored.key, stored.previous, stored.migrated) == (current, previous, False)

        store.write(current, migrated=True)
        stored = store.read()
        assert (stored.key, stored.previous, stored.migrated) == (current, None, True)

    def test_plain_file_cannot_hold_previous_key(self, tmp_path):
        path = tmp_path / ".encryption_key"
        with pytest.raises(KeyProtectError):
            WrappedKeyFile(path, PlainFileProtector()).write(
                Fernet.generate_key(), previous=Fernet.generate_key()
            )
        assert not path.exists()

    def test_plain_file_is_never_marked_migrated(self, tmp_path):
        path = tmp_path / ".encryption_key"
        store = WrappedKeyFile(path, PlainFileProtector())
        store.write(Fernet.generate_key(), migrated=True)
        assert store.read().migrated is False


class TestKeyBackupAndCleanup:
    def test_plain_key_backup_is_wrapped_when_os_protection_exists(self, tmp_path):
        key = Fernet.generate_key()
        path = tmp_path / ".encryption_key"
        path.write_bytes(key)
        store = WrappedKeyFile(path, FakeDpapi())

        dest = secret_store.backup_key_file(store, store.read())

        assert dest is not None and dest.name == ".encryption_key" + BACKUP_SUFFIX
        assert key not in dest.read_bytes()
        assert WrappedKeyFile(dest, FakeDpapi()).read().key == key
        assert path.read_bytes() == key, "원본 키 파일은 그대로"

    def test_wrapped_key_backup_is_a_byte_copy(self, tmp_path):
        path = tmp_path / ".encryption_key"
        store = WrappedKeyFile(path, FakeDpapi())
        store.write(Fernet.generate_key())

        dest = secret_store.backup_key_file(store, store.read())

        assert dest is not None and dest.read_bytes() == path.read_bytes()

    def test_remove_backups_only_touches_this_files_backups(self, tmp_path):
        path = tmp_path / "db_migration.db"
        path.write_bytes(b"db")
        keep = tmp_path / "other.db.bak-pre-keywrap"
        keep.write_bytes(b"x")
        first = backup_file(path)
        second = backup_file(path)

        removed = secret_store.remove_backups(path)

        assert sorted(removed) == sorted([first, second])
        assert sorted(p.name for p in tmp_path.iterdir()) == ["db_migration.db", keep.name]

    def test_quarantine_moves_file_aside_without_losing_bytes(self, tmp_path):
        path = tmp_path / ".encryption_key"
        path.write_bytes(b"foreign")

        dest = secret_store.quarantine_file(path)

        assert not path.exists()
        assert dest.read_bytes() == b"foreign"
        assert BACKUP_SUFFIX not in dest.name, "마이그레이션 백업 정리 대상이 아니어야 한다"
