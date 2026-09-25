"""암호화 키를 OS 보호 저장소로 감싸 파일에 보관한다 (감사 M-05).

프로필 암호화 키를 평문으로 DB 옆에 두면 사용자 데이터 디렉터리를 복사하는 것만으로
키와 암호문이 함께 새어 나간다. Windows에서는 DPAPI(현재 사용자 범위)로 키를 감싸
다른 사용자·다른 장치에서는 키 파일을 가져가도 풀리지 않게 한다.

- Windows: `DpapiProtector` — CryptProtectData/CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN.
- 그 밖의 플랫폼(개발용 Mac 등): `PlainFileProtector` — 소유자 전용(0600) 평문 파일. 보호가
  아니므로 호출 측이 경고를 남긴다.

키 파일 형식
- 평문(구버전 호환): Fernet 키 44바이트 그대로.
- 래핑: ``DBMT-KEY1:<scheme>:<base64(blob)>``.

모든 쓰기는 같은 디렉터리의 임시 파일에 쓴 뒤 `os.replace`로 교체한다. 도중에 실패하면
기존 파일은 그대로 남는다.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import ctypes
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from cryptography.fernet import Fernet

KEY_FILE_HEADER = b"DBMT-KEY1"
BACKUP_SUFFIX = ".bak-pre-keywrap"

# DPAPI 앱 전용 보조 엔트로피. 비밀이 아니다 — 같은 사용자의 다른 앱이 DPAPI blob을
# 우연히 풀어 쓰는 것을 막는 용도다.
DEFAULT_DPAPI_ENTROPY = b"DBMigrationTool/profile-key/v1"

_CRYPTPROTECT_UI_FORBIDDEN = 0x01
# CRYPTPROTECT_LOCAL_MACHINE(0x04)은 장치의 모든 사용자가 풀 수 있게 하므로 쓰지 않는다.
_DPAPI_FLAGS = _CRYPTPROTECT_UI_FORBIDDEN


class SecretStoreError(Exception):
    """키 저장소 오류의 공통 부모."""


class KeyProtectError(SecretStoreError):
    """키를 보호(래핑)하지 못했다."""


class KeyUnprotectError(SecretStoreError):
    """보호된 키를 풀지 못했다 — 다른 사용자·장치이거나 blob이 손상됐다."""


class KeyFileCorruptError(SecretStoreError):
    """키 파일 형식을 알아볼 수 없다."""


class KeyProtector(Protocol):
    scheme: str
    os_protected: bool

    def protect(self, data: bytes) -> bytes: ...

    def unprotect(self, blob: bytes) -> bytes: ...


class PlainFileProtector:
    """보호 없음. 비Windows 개발 환경 폴백 — 키는 0600 평문 파일로 남는다."""

    scheme = "plain"
    os_protected = False

    def protect(self, data: bytes) -> bytes:
        return data

    def unprotect(self, blob: bytes) -> bytes:
        return blob


# --- DPAPI (ctypes) -----------------------------------------------------------------------


class DataBlob(ctypes.Structure):
    """Win32 DATA_BLOB."""

    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


@dataclass(frozen=True)
class DpapiFunctions:
    """DPAPI 호출 지점. 테스트는 가짜 구현을 주입한다."""

    protect: Callable[..., Any]
    unprotect: Callable[..., Any]
    local_free: Callable[[Any], Any]
    last_error: Callable[[], int]


def _load_windows_dpapi() -> DpapiFunctions:
    if sys.platform != "win32":
        raise KeyProtectError("DPAPI는 Windows에서만 사용할 수 있습니다")
    win_dll = ctypes.WinDLL
    crypt32 = win_dll("crypt32", use_last_error=True)
    kernel32 = win_dll("kernel32", use_last_error=True)

    blob_p = ctypes.POINTER(DataBlob)
    protect = crypt32.CryptProtectData
    protect.argtypes = [
        blob_p,
        ctypes.c_wchar_p,
        blob_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        blob_p,
    ]
    protect.restype = ctypes.c_int
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        blob_p,
        ctypes.c_void_p,
        blob_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        blob_p,
    ]
    unprotect.restype = ctypes.c_int
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    return DpapiFunctions(
        protect=protect,
        unprotect=unprotect,
        local_free=local_free,
        last_error=ctypes.get_last_error,
    )


class DpapiProtector:
    """Windows DPAPI, 현재 사용자 범위. DLL은 첫 사용 때 불러온다."""

    scheme = "dpapi"
    os_protected = True

    def __init__(self, api: DpapiFunctions | None = None, entropy: bytes = DEFAULT_DPAPI_ENTROPY):
        self._api = api
        self._entropy = entropy

    def _functions(self) -> DpapiFunctions:
        if self._api is None:
            self._api = _load_windows_dpapi()
        return self._api

    @staticmethod
    def _in_blob(data: bytes) -> tuple[DataBlob, ctypes.Array[ctypes.c_char]]:
        buf = ctypes.create_string_buffer(data, len(data))
        return DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _call(self, which: str, data: bytes) -> bytes:
        api = self._functions()
        func = api.protect if which == "protect" else api.unprotect
        in_blob, _in_buf = self._in_blob(data)
        ent_blob, _ent_buf = self._in_blob(self._entropy)
        out = DataBlob()
        ok = func(
            ctypes.pointer(in_blob),
            None,
            ctypes.pointer(ent_blob),
            None,
            None,
            _DPAPI_FLAGS,
            ctypes.pointer(out),
        )
        if not ok:
            code = api.last_error()
            if which == "protect":
                raise KeyProtectError(f"CryptProtectData 실패 (Win32 오류 {code})")
            raise KeyUnprotectError(
                f"CryptUnprotectData 실패 (Win32 오류 {code}) — 다른 사용자·장치의 키이거나 손상됨"
            )
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            api.local_free(ctypes.cast(out.pbData, ctypes.c_void_p))

    def protect(self, data: bytes) -> bytes:
        return self._call("protect", data)

    def unprotect(self, blob: bytes) -> bytes:
        return self._call("unprotect", blob)


def default_protector(platform: str | None = None) -> KeyProtector:
    """현재 플랫폼의 키 보호기. Windows면 DPAPI, 아니면 평문 파일 폴백."""
    if (platform or sys.platform) == "win32":
        return DpapiProtector()
    return PlainFileProtector()


# --- 파일 쓰기·백업 ------------------------------------------------------------------------


def _restrict_to_owner(path: Path | str) -> None:
    """POSIX에서 0600. Windows ACL은 사용자 프로필 디렉터리 권한을 따른다."""
    if os.name == "posix":
        os.chmod(path, 0o600)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """임시 파일에 쓰고 fsync 뒤 교체한다. 실패하면 기존 파일과 디렉터리는 그대로."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _restrict_to_owner(tmp)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _backup_destination(path: Path, suffix: str) -> Path:
    dest = path.with_name(path.name + suffix)
    if not dest.exists():
        return dest
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    for n in range(1000):
        candidate = path.with_name(f"{path.name}{suffix}-{stamp}{f'-{n}' if n else ''}")
        if not candidate.exists():
            return candidate
    raise OSError(f"백업 파일 이름을 정할 수 없습니다: {dest}")


def backup_file(path: Path, suffix: str = BACKUP_SUFFIX) -> Path | None:
    """파일을 소유자 전용 사본으로 백업한다. 기존 백업은 덮어쓰지 않는다."""
    if not path.exists():
        return None
    dest = _backup_destination(path, suffix)
    atomic_write_bytes(dest, path.read_bytes())
    return dest


def backup_sqlite_database(path: Path, suffix: str = BACKUP_SUFFIX) -> Path | None:
    """SQLite online backup API로 일관된 사본을 만든다(열린 연결이 있어도 안전)."""
    if not path.exists():
        return None
    dest = _backup_destination(path, suffix)
    fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    try:
        src = sqlite3.connect(str(path))
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        _restrict_to_owner(tmp)
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return dest


# --- 키 파일 -------------------------------------------------------------------------------


KeyForm = Literal["absent", "plain", "wrapped"]


@dataclass(frozen=True)
class StoredKey:
    key: bytes | None
    form: KeyForm
    scheme: str | None = None


def _validate_fernet_key(key: bytes) -> bytes:
    try:
        Fernet(key)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise KeyFileCorruptError("유효한 Fernet 키가 아닙니다") from exc
    return key


class WrappedKeyFile:
    """보호기로 감싼 키 한 개를 담는 파일."""

    def __init__(self, path: Path, protector: KeyProtector):
        self.path = path
        self.protector = protector

    def read(self) -> StoredKey:
        if not self.path.exists():
            return StoredKey(None, "absent")
        raw = self.path.read_bytes().strip()
        if raw.startswith(KEY_FILE_HEADER + b":"):
            parts = raw.split(b":", 2)
            if len(parts) != 3:
                raise KeyFileCorruptError("래핑된 키 파일 형식이 잘못되었습니다")
            scheme = parts[1].decode("ascii", "replace")
            if scheme != self.protector.scheme:
                raise KeyUnprotectError(
                    f"키가 '{scheme}' 방식으로 보호되어 있어 이 환경"
                    f"('{self.protector.scheme}')에서 열 수 없습니다"
                )
            try:
                blob = base64.b64decode(parts[2], validate=True)
            except binascii.Error as exc:
                raise KeyFileCorruptError("래핑된 키 파일 본문이 손상되었습니다") from exc
            key = self.protector.unprotect(blob)
            return StoredKey(_validate_fernet_key(key), "wrapped", scheme)
        return StoredKey(_validate_fernet_key(raw), "plain")

    def _encode(self, key: bytes) -> bytes:
        if not self.protector.os_protected:
            return key
        blob = self.protector.protect(key)
        try:
            roundtrip = self.protector.unprotect(blob)
        except SecretStoreError as exc:
            raise KeyProtectError("보호한 키를 다시 풀 수 없습니다") from exc
        if roundtrip != key:
            raise KeyProtectError("보호한 키를 되풀었더니 원래 키와 다릅니다")
        scheme = self.protector.scheme.encode("ascii")
        return KEY_FILE_HEADER + b":" + scheme + b":" + base64.b64encode(blob)

    def write(self, key: bytes) -> None:
        """보호기로 감싸 원자적으로 기록한다. 되풀기 검증에 실패하면 파일을 건드리지 않는다."""
        _validate_fernet_key(key)
        atomic_write_bytes(self.path, self._encode(key))

    def write_plain(self, key: bytes) -> None:
        """보호 없이 0600 평문으로 기록한다(보호기 실패 시 최후 수단)."""
        _validate_fernet_key(key)
        atomic_write_bytes(self.path, key)

    def harden_permissions(self) -> None:
        if self.path.exists():
            _restrict_to_owner(self.path)
