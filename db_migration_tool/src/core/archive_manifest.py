"""File archive manifest helpers.

동시 저장(M-02)과 무결성·인증(M-04)의 규칙은 ``docs/base/archive-trust-boundary.md``에 있다.

요약
- 저장은 파일 락 안에서 디스크의 최신 manifest를 다시 읽어 **항목 단위로 병합**한다.
  이 writer가 바꾸지 않은 항목은 디스크 값을 그대로 쓰고, 바꾼 항목은 읽을 당시의
  ``entry_version``과 디스크 값이 같을 때만(CAS) 새 version으로 기록한다. 다르면
  :class:`ManifestConflictError`로 실패한다 — 조용히 덮지 않는다.
- 새로 기록되는 파티션 항목은 ``checksum_sha256``(SHA-256 hex)이 필수다.
- passphrase를 주면 canonical manifest에 PBKDF2-SHA256 파생 키로 HMAC-SHA256을 붙인다.
  키는 passphrase와 manifest에 저장된 salt만으로 정해지므로 다른 PC로 옮긴 아카이브도
  같은 passphrase로 검증된다(기계별 키를 쓰지 않는다).
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.core.table_types import TableType, infer_partition_range

logger = logging.getLogger(__name__)


class ScanCancelled(Exception):
    """조회가 사용자 요청으로 중단됐다.

    실패가 아니라 취소다. UI가 이 둘을 구분해야 '오류'로 잘못 알리지 않는다.
    """


class ManifestConflictError(RuntimeError):
    """다른 writer가 같은 항목을 먼저 바꿨다(CAS 실패).

    manifest는 바뀌지 않았다. 최신 manifest를 다시 읽고 작업을 재시도해야 한다.
    """


class ManifestAuthError(ValueError):
    """manifest 인증 실패, 또는 인증/신뢰 정책 위반."""


ARCHIVE_FORMAT_NAME = "psql93-migration-archive"
# 3: entry_version / revision / auth 추가, 신규 항목 checksum 필수.
# 2 이하(1.2.7 이하가 만든 아카이브)는 읽을 수 있다. 1.2.7 이하 앱은 3을 거부하므로
# 인증을 검사하지 못하는 구버전이 서명된 아카이브를 가져오는 우회로가 생기지 않는다.
ARCHIVE_FORMAT_VERSION = 3
MANIFEST_FILENAME = "manifest.json"
MANIFEST_BACKUP_FILENAME = "manifest.json.bak"
PARTITIONS_DIRNAME = "partitions"

AUTH_SCHEME = "hmac-sha256"
AUTH_KDF = "pbkdf2-sha256"
DEFAULT_KDF_ITERATIONS = 600_000
MIN_KDF_ITERATIONS = 1_000
MAX_KDF_ITERATIONS = 10_000_000
_SALT_BYTES = 16
_MAC_DOMAIN = b"psql93-migration-archive/manifest/hmac-sha256/v1\x00"
_KEY_CHECK_DOMAIN = b"psql93-migration-archive/key-check/v1"

_SAFE_PARTITION_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_LOCK_TIMEOUT_SECONDS = 60.0


@dataclass
class ArchivePartitionEntry:
    partition_name: str
    table_type: str
    parent_table: str
    row_count: int
    file_path: str
    columns: list[str]
    from_timestamp: int | None = None
    to_timestamp: int | None = None
    bytes_written: int = 0
    exported_at: str | None = None
    copy_method: str = "FILE_ARCHIVE"
    checksum_sha256: str | None = None
    verified_at: str | None = None
    # CAS 토큰. 저장할 때 store가 올린다. 직접 바꾸지 않는다.
    entry_version: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchivePartitionEntry:
        allowed = {item.name for item in fields(cls)}
        payload = {key: value for key, value in dict(data).items() if key in allowed}
        return cls(**payload)


@dataclass
class ArchiveManifest:
    format: str = ARCHIVE_FORMAT_NAME
    version: int = ARCHIVE_FORMAT_VERSION
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    source: dict[str, Any] = field(default_factory=dict)
    target: dict[str, Any] = field(default_factory=dict)
    parent_tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    partitions: list[dict[str, Any]] = field(default_factory=list)
    revision: int = 0
    auth: dict[str, Any] | None = None
    # ── 직렬화하지 않는 상태 ──
    # 마지막으로 디스크와 맞춘 시점의 스냅샷(3-way 병합의 base).
    _base: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    # 이 객체의 내용이 passphrase로 검증됐는지.
    _authenticated: bool = field(default=False, repr=False, compare=False)

    @property
    def is_signed(self) -> bool:
        return bool(self.auth)

    @property
    def is_authenticated(self) -> bool:
        return self.is_signed and self._authenticated

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in [key for key in data if key.startswith("_")]:
            data.pop(key)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchiveManifest:
        allowed = {item.name for item in fields(cls) if not item.name.startswith("_")}
        payload = {key: value for key, value in dict(data).items() if key in allowed}
        return cls(**payload)


@dataclass(frozen=True)
class ArchiveSecurityInfo:
    """UI가 실행 전에 묻는 데 쓰는 요약. 검증 결과가 아니라 '적혀 있는 것'이다."""

    exists: bool
    signed: bool
    partition_count: int
    partitions_without_checksum: tuple[str, ...]


# ── canonical form / snapshot ────────────────────────────────────────────


def _canon(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def canonical_manifest_bytes(data: dict[str, Any]) -> bytes:
    """HMAC 입력. ``auth.mac``만 빼고 나머지 전부(인증 파라미터 포함)를 정규화 JSON으로."""
    body = {key: value for key, value in data.items() if key != "auth"}
    auth = data.get("auth")
    if isinstance(auth, dict):
        body["auth"] = {key: value for key, value in auth.items() if key != "mac"}
    return _MAC_DOMAIN + _canon(body).encode("utf-8")


def _entry_version(item: dict[str, Any] | None) -> int:
    if not item:
        return 0
    value = item.get("entry_version", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _entry_content(item: dict[str, Any]) -> str:
    return _canon({key: value for key, value in item.items() if key != "entry_version"})


def _snapshot(data: dict[str, Any]) -> dict[str, Any]:
    partitions: dict[str, str] = {}
    versions: dict[str, int] = {}
    for item in data.get("partitions") or []:
        name = item.get("partition_name")
        partitions[name] = _entry_content(item)
        versions[name] = _entry_version(item)
    return {
        "partitions": partitions,
        "versions": versions,
        "parent_tables": {
            name: _canon(value) for name, value in (data.get("parent_tables") or {}).items()
        },
        "source": _canon(data.get("source") or {}),
        "target": _canon(data.get("target") or {}),
    }


def _normalize_checksum(value: Any) -> str:
    return str(value or "").strip().lower()


class ArchiveManifestStore:
    """Archive manifest read/write helper."""

    def __init__(
        self,
        archive_path: str | Path,
        *,
        passphrase: str | None = None,
        allow_legacy_unverified: bool = False,
        kdf_iterations: int = DEFAULT_KDF_ITERATIONS,
        warn: Callable[[str], None] | None = None,
    ):
        self.archive_dir = self.resolve_archive_dir(archive_path)
        self.manifest_path = self.archive_dir / MANIFEST_FILENAME
        self.backup_path = self.archive_dir / MANIFEST_BACKUP_FILENAME
        self.partitions_dir = self.archive_dir / PARTITIONS_DIRNAME
        self._lock_path = self.archive_dir / ".manifest.lock"
        if not MIN_KDF_ITERATIONS <= int(kdf_iterations) <= MAX_KDF_ITERATIONS:
            raise ValueError(f"kdf_iterations 범위 밖: {kdf_iterations}")
        self._kdf_iterations = int(kdf_iterations)
        self._key_cache: dict[tuple[bytes, int], bytes] = {}
        self._warn_handler = warn
        self._passphrase: str | None = None
        self.allow_legacy_unverified = False
        self.configure_security(
            passphrase=passphrase, allow_legacy_unverified=allow_legacy_unverified
        )

    # ── 보안 설정 ──────────────────────────────────────────────────────

    def configure_security(
        self, *, passphrase: str | None = None, allow_legacy_unverified: bool = False
    ) -> None:
        """passphrase(없으면 인증 없이 읽기/쓰기)와 legacy 허용 여부를 정한다.

        passphrase는 메모리에만 둔다. 저장·로그하지 않는다.
        """
        self._passphrase = passphrase or None
        self.allow_legacy_unverified = bool(allow_legacy_unverified)
        self._key_cache.clear()

    @property
    def has_passphrase(self) -> bool:
        return self._passphrase is not None

    def set_warning_handler(self, warn: Callable[[str], None] | None) -> None:
        self._warn_handler = warn

    def _warn(self, message: str) -> None:
        logger.warning(message)
        if self._warn_handler is not None:
            self._warn_handler(message)

    def _derive_key(self, salt: bytes, iterations: int) -> bytes:
        if self._passphrase is None:
            raise ManifestAuthError("passphrase가 없어 manifest 인증 키를 만들 수 없습니다.")
        cache_key = (salt, iterations)
        cached = self._key_cache.get(cache_key)
        if cached is not None:
            return cached
        # OS/입력기마다 한글 조합형(NFD)·완성형(NFC)이 달라질 수 있어 NFC로 맞춘다.
        secret = unicodedata.normalize("NFC", self._passphrase).encode("utf-8")
        key = hashlib.pbkdf2_hmac("sha256", secret, salt, iterations, dklen=32)
        self._key_cache[cache_key] = key
        return key

    @staticmethod
    def _key_check(key: bytes) -> str:
        return hmac.new(key, _KEY_CHECK_DOMAIN, hashlib.sha256).hexdigest()[:32]

    @staticmethod
    def _compute_mac(key: bytes, data: dict[str, Any]) -> str:
        return hmac.new(key, canonical_manifest_bytes(data), hashlib.sha256).hexdigest()

    @staticmethod
    def _parse_auth_params(auth: Any) -> tuple[bytes, int]:
        if not isinstance(auth, dict):
            raise ManifestAuthError("manifest 인증 블록 형식이 올바르지 않습니다.")
        if auth.get("scheme") != AUTH_SCHEME or auth.get("kdf") != AUTH_KDF:
            raise ManifestAuthError(
                f"지원하지 않는 manifest 인증 방식입니다: scheme={auth.get('scheme')!r}, "
                f"kdf={auth.get('kdf')!r}"
            )
        iterations = auth.get("iterations")
        if (
            not isinstance(iterations, int)
            or isinstance(iterations, bool)
            or not MIN_KDF_ITERATIONS <= iterations <= MAX_KDF_ITERATIONS
        ):
            raise ManifestAuthError(f"manifest 인증 파라미터가 올바르지 않습니다: {iterations!r}")
        try:
            salt = bytes.fromhex(str(auth.get("salt", "")))
        except ValueError as exc:
            raise ManifestAuthError("manifest 인증 salt 형식이 올바르지 않습니다.") from exc
        if len(salt) < _SALT_BYTES:
            raise ManifestAuthError("manifest 인증 salt가 너무 짧습니다.")
        return salt, iterations

    def _verify_auth(self, data: dict[str, Any]) -> None:
        auth = data.get("auth")
        salt, iterations = self._parse_auth_params(auth)
        assert isinstance(auth, dict)
        key = self._derive_key(salt, iterations)
        key_check = auth.get("key_check")
        if key_check is not None and not hmac.compare_digest(self._key_check(key), str(key_check)):
            raise ManifestAuthError(
                "passphrase가 일치하지 않습니다(또는 manifest 인증 정보가 변조되었습니다)."
            )
        if not hmac.compare_digest(self._compute_mac(key, data), str(auth.get("mac", ""))):
            raise ManifestAuthError(
                "manifest 인증(HMAC-SHA256) 검증 실패: manifest가 변조되었거나 "
                "passphrase가 다릅니다."
            )

    def _build_auth(
        self, data: dict[str, Any], previous_auth: dict[str, Any] | None
    ) -> dict[str, Any]:
        """``data``(auth 제외)에 붙일 새 인증 블록. 검증된 이전 블록의 salt는 재사용한다."""
        salt: bytes | None = None
        iterations = self._kdf_iterations
        if previous_auth:
            try:
                salt, iterations = self._parse_auth_params(previous_auth)
            except ManifestAuthError:
                salt = None
                iterations = self._kdf_iterations
        if salt is None:
            salt = os.urandom(_SALT_BYTES)
        key = self._derive_key(salt, iterations)
        auth: dict[str, Any] = {
            "scheme": AUTH_SCHEME,
            "kdf": AUTH_KDF,
            "iterations": iterations,
            "salt": salt.hex(),
            "key_check": self._key_check(key),
        }
        auth["mac"] = self._compute_mac(key, {**data, "auth": auth})
        return auth

    def require_trusted(self, manifest: ArchiveManifest) -> list[str]:
        """가져오기(import) 전에 manifest를 믿어도 되는지 판정한다.

        Returns:
            사용자가 명시적으로 허용해 진행하는 경우 남겨야 할 경고 문구 목록.
            인증된 manifest면 빈 목록.

        Raises:
            ManifestAuthError: 서명됐는데 passphrase가 없거나, 인증 정보가 없는데
                명시적 허용(``allow_legacy_unverified``)이 없을 때.
        """
        if manifest.is_signed:
            if manifest.is_authenticated:
                return []
            raise ManifestAuthError(
                "이 아카이브는 passphrase로 보호되어 있습니다. export 때 지정한 "
                "passphrase를 입력해야 가져올 수 있습니다."
            )
        message = (
            "manifest에 인증 정보가 없습니다(1.2.7 이하에서 만들었거나 passphrase 없이 "
            "export한 아카이브). manifest와 데이터 파일이 함께 바뀌어도 알아챌 수 없습니다."
        )
        if not self.allow_legacy_unverified:
            raise ManifestAuthError(
                message + " 계속하려면 '인증 없는 아카이브 가져오기'를 명시적으로 확인해야 합니다."
            )
        warnings = [message + " 사용자가 명시적으로 확인하여 진행합니다."]
        missing = [
            str(item.get("partition_name"))
            for item in manifest.partitions
            if not _SHA256_HEX_RE.match(_normalize_checksum(item.get("checksum_sha256")))
        ]
        if missing:
            warnings.append(
                f"checksum이 없는 파티션 {len(missing)}개는 파일 무결성을 검증할 수 없습니다"
                f"(크기·행 수만 확인): {', '.join(missing[:10])}"
                + (" …" if len(missing) > 10 else "")
            )
        return warnings

    def inspect_security(self) -> ArchiveSecurityInfo:
        """실행 전 확인용 요약(검증하지 않음). manifest가 없으면 exists=False."""
        if not (self.manifest_path.exists() or self.backup_path.exists()):
            return ArchiveSecurityInfo(False, False, 0, ())
        raw, _ = self._read_raw(verify=False)
        partitions = raw.get("partitions") or []
        missing = tuple(
            str(item.get("partition_name"))
            for item in partitions
            if not _SHA256_HEX_RE.match(_normalize_checksum(item.get("checksum_sha256")))
        )
        return ArchiveSecurityInfo(True, bool(raw.get("auth")), len(partitions), missing)

    # ── 경로/파일 유틸 ────────────────────────────────────────────────

    @staticmethod
    def resolve_archive_dir(archive_path: str | Path) -> Path:
        path = Path(archive_path).expanduser()
        if path.name == MANIFEST_FILENAME:
            return path.parent
        return path

    @staticmethod
    def sanitize_endpoint(config: dict[str, Any] | None) -> dict[str, Any]:
        endpoint = dict(config or {})
        endpoint.pop("password", None)
        return endpoint

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if not path.exists():
            return
        dir_fd = None
        try:
            flags = getattr(os, "O_RDONLY", 0)
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            dir_fd = os.open(str(path), flags)
            os.fsync(dir_fd)
        except Exception:
            # 플랫폼별 제약(예: Windows) 때문에 디렉터리 fsync가 실패할 수 있다.
            pass
        finally:
            if dir_fd is not None:
                os.close(dir_fd)

    @staticmethod
    def _replace_with_retry(src: Path, dst: Path) -> None:
        # Windows는 다른 프로세스가 대상 파일을 읽는 동안 교체가 PermissionError로 실패한다.
        # 읽기는 짧으므로 잠시 재시도한다.
        attempts = 40 if sys.platform == "win32" else 1
        for attempt in range(attempts):
            try:
                os.replace(src, dst)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.05)

    def _atomic_write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex[:12]}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as fp:
                fp.write(text)
                fp.flush()
                os.fsync(fp.fileno())
            self._replace_with_retry(tmp_path, path)
            self._fsync_directory(path.parent)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            raise

    def replace_file_atomically(self, temp_path: str | Path, final_path: str | Path) -> None:
        src = Path(temp_path)
        dst = Path(final_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        self._replace_with_retry(src, dst)
        self._fsync_directory(dst.parent)

    def ensure_archive(self) -> None:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.partitions_dir.mkdir(parents=True, exist_ok=True)

    # ── 읽기 ──────────────────────────────────────────────────────────

    @staticmethod
    def _validate_manifest_schema(data: Any) -> None:
        if not isinstance(data, dict):
            raise ValueError("manifest 최상위가 객체가 아닙니다.")
        fmt = data.get("format")
        if fmt != ARCHIVE_FORMAT_NAME:
            raise ValueError(
                f"지원하지 않는 manifest 형식입니다: {fmt!r} (expected={ARCHIVE_FORMAT_NAME!r})"
            )
        version = data.get("version")
        if not isinstance(version, int) or version < 1 or version > ARCHIVE_FORMAT_VERSION:
            raise ValueError(
                f"지원하지 않는 manifest 버전입니다: {version!r} (max supported={ARCHIVE_FORMAT_VERSION})"
            )
        if not isinstance(data.get("partitions", []), list):
            raise ValueError("manifest partitions 필드가 list가 아닙니다.")
        if not isinstance(data.get("parent_tables", {}), dict):
            raise ValueError("manifest parent_tables 필드가 dict가 아닙니다.")

    def _read_raw(self, *, verify: bool = True) -> tuple[dict[str, Any], Path]:
        """manifest → backup 순으로 읽는다.

        - 둘 다 없으면 FileNotFoundError, 있는데 모두 못 읽으면 ValueError.
        - JSON/스키마 오류는 백업으로 넘어가지만, **인증 실패는 백업으로 넘어가지 않는다**
          (변조를 오래된 사본으로 조용히 덮지 않기 위해).
        - passphrase가 있고 서명된 manifest면 여기서 HMAC을 검증한다.
        """
        errors: list[str] = []
        found = False
        for candidate in (self.manifest_path, self.backup_path):
            if not candidate.exists():
                continue
            found = True
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                self._validate_manifest_schema(data)
            except Exception as exc:
                errors.append(f"{candidate}: {exc}")
                continue
            if verify and self._passphrase is not None and data.get("auth"):
                self._verify_auth(data)
            if candidate != self.manifest_path:
                self._warn(f"manifest.json을 읽지 못해 백업({candidate.name})을 사용합니다.")
            return data, candidate

        if found:
            raise ValueError("manifest.json 로드 실패: " + " | ".join(errors))
        raise FileNotFoundError(f"manifest.json 파일을 찾을 수 없습니다: {self.manifest_path}")

    def _manifest_from_raw(self, raw: dict[str, Any]) -> ArchiveManifest:
        manifest = ArchiveManifest.from_dict(copy.deepcopy(raw))
        manifest._base = _snapshot(raw)
        manifest._authenticated = self._passphrase is not None and bool(raw.get("auth"))
        return manifest

    def load(self) -> ArchiveManifest:
        raw, _ = self._read_raw()
        return self._manifest_from_raw(raw)

    def load_or_create(
        self,
        *,
        source: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
    ) -> ArchiveManifest:
        if self.manifest_path.exists() or self.backup_path.exists():
            manifest = self.load()
        else:
            manifest = ArchiveManifest()
            manifest._base = _snapshot({})

        if source is not None:
            manifest.source = self.sanitize_endpoint(source)
        if target is not None:
            manifest.target = self.sanitize_endpoint(target)
        manifest.updated_at = datetime.now().isoformat()
        self.save(manifest)
        return manifest

    # ── 쓰기 ──────────────────────────────────────────────────────────

    def _acquire_lock(self):
        """manifest 쓰기 직렬화를 위한 파일 락 획득(프로세스·스레드 모두). 실패 시 예외."""
        self.ensure_archive()
        lock_fd = open(self._lock_path, "a+")
        try:
            # os.name 대신 sys.platform 으로 분기해야 타입 체커가 플랫폼별 모듈
            # (msvcrt / fcntl)을 각각 해당 플랫폼에서만 검사한다.
            if sys.platform == "win32":
                import msvcrt

                lock_fd.seek(0)
                deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
                while True:
                    try:
                        msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise TimeoutError(
                                f"manifest 잠금을 {_LOCK_TIMEOUT_SECONDS:.0f}초 안에 얻지 못했습니다: "
                                f"{self._lock_path}"
                            ) from None
                        time.sleep(0.02)
            else:
                import fcntl

                fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except BaseException:
            lock_fd.close()
            raise
        return lock_fd

    @staticmethod
    def _release_lock(lock_fd):
        try:
            if sys.platform == "win32":
                import msvcrt

                lock_fd.seek(0)
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            lock_fd.close()
        except Exception:
            pass

    def _check_write_policy(self, disk_raw: dict[str, Any] | None) -> None:
        """다운그레이드·세탁(laundering) 방지. 디스크는 이미 ``_read_raw``에서 검증됐다."""
        if disk_raw is None:
            return
        signed = bool(disk_raw.get("auth"))
        if signed and self._passphrase is None:
            raise ManifestAuthError(
                "이 아카이브는 passphrase로 보호되어 있습니다. passphrase 없이 쓰면 인증이 "
                "사라지므로 거부합니다. export 때 쓴 passphrase를 입력하세요."
            )
        existing = disk_raw.get("partitions") or []
        if not signed and self._passphrase is not None and existing:
            if not self.allow_legacy_unverified:
                raise ManifestAuthError(
                    f"인증 정보가 없는 기존 아카이브(파티션 {len(existing)}개)에 passphrase 서명을 "
                    "더하려면 명시적 확인이 필요합니다. 서명하면 지금 폴더에 있는 내용이 그대로 "
                    "신뢰됩니다. 새 폴더로 export 하는 것을 권장합니다."
                )
            self._warn(
                f"인증 정보가 없던 기존 파티션 {len(existing)}개를 이 passphrase로 서명합니다"
                "(사용자 확인에 따른 채택). 채택 시점의 폴더 내용이 그대로 신뢰됩니다."
            )

    @staticmethod
    def _require_checksum(item: dict[str, Any]) -> None:
        checksum = _normalize_checksum(item.get("checksum_sha256"))
        if not _SHA256_HEX_RE.match(checksum):
            raise ValueError(
                "새로 기록하는 파티션 항목에는 checksum_sha256(SHA-256 hex 64자)이 필수입니다: "
                f"{item.get('partition_name')!r}"
            )
        item["checksum_sha256"] = checksum

    def _merge(self, manifest: ArchiveManifest, disk_raw: dict[str, Any] | None) -> dict[str, Any]:
        """3-way 병합(base=마지막 동기화 스냅샷, mine=메모리, theirs=디스크 최신)."""
        mine = manifest.to_dict()
        base = manifest._base or _snapshot({})
        theirs = disk_raw or {}

        merged: dict[str, Any] = {
            "format": ARCHIVE_FORMAT_NAME,
            "version": ARCHIVE_FORMAT_VERSION,
            "created_at": theirs.get("created_at") or mine.get("created_at"),
            "updated_at": datetime.now().isoformat(),
        }

        # source/target: 설명용 메타데이터. 이 writer가 바꿨으면 그 값, 아니면 디스크 값.
        for key in ("source", "target"):
            mine_value = mine.get(key) or {}
            if _canon(mine_value) != base[key] or key not in theirs:
                merged[key] = copy.deepcopy(mine_value)
            else:
                merged[key] = copy.deepcopy(theirs.get(key) or {})

        # parent_tables: 키 단위 3-way. 양쪽이 서로 다르게 바꿨으면 충돌.
        theirs_parents: dict[str, Any] = theirs.get("parent_tables") or {}
        parents = copy.deepcopy(theirs_parents)
        for name, value in (mine.get("parent_tables") or {}).items():
            mine_c = _canon(value)
            base_c = base["parent_tables"].get(name)
            if mine_c == base_c:
                continue
            their_value = theirs_parents.get(name)
            their_c = None if their_value is None else _canon(their_value)
            if their_c not in (base_c, mine_c):
                raise ManifestConflictError(
                    f"parent_table '{name}' 메타데이터를 다른 작업이 먼저 다르게 변경했습니다."
                )
            parents[name] = copy.deepcopy(value)
        merged["parent_tables"] = parents

        # partitions: 항목별 entry_version CAS.
        theirs_list: list[dict[str, Any]] = theirs.get("partitions") or []
        order = [item.get("partition_name") for item in theirs_list]
        by_name = {item.get("partition_name"): copy.deepcopy(item) for item in theirs_list}
        for item in mine.get("partitions") or []:
            name = item.get("partition_name")
            mine_c = _entry_content(item)
            base_c = base["partitions"].get(name)
            if mine_c == base_c:
                # 이 writer가 바꾸지 않은 항목 → 디스크의 최신 값을 유지한다.
                continue
            their = by_name.get(name)
            their_c = None if their is None else _entry_content(their)
            if their_c == mine_c:
                continue  # 같은 값을 이미 누가 썼다 — 충돌 아님
            base_v = base["versions"].get(name, 0)
            their_v = _entry_version(their)
            if their_c != base_c or their_v != base_v:
                raise ManifestConflictError(
                    f"파티션 '{name}' 항목을 다른 작업이 먼저 변경했습니다 "
                    f"(읽은 version={base_v}, 현재 version={their_v}). "
                    "manifest를 다시 읽은 뒤 재시도하세요."
                )
            new_item = copy.deepcopy(item)
            self._require_checksum(new_item)
            new_item["entry_version"] = their_v + 1
            if name not in by_name:
                order.append(name)
            by_name[name] = new_item
        merged["partitions"] = [by_name[name] for name in order]

        their_revision = theirs.get("revision", 0)
        if not isinstance(their_revision, int) or isinstance(their_revision, bool):
            their_revision = 0
        merged["revision"] = their_revision + 1
        return merged

    def _adopt_written(self, manifest: ArchiveManifest, written: dict[str, Any]) -> None:
        fresh = ArchiveManifest.from_dict(copy.deepcopy(written))
        for item in fields(ArchiveManifest):
            if not item.name.startswith("_"):
                setattr(manifest, item.name, getattr(fresh, item.name))
        manifest._base = _snapshot(written)
        manifest._authenticated = bool(written.get("auth"))

    def _save_locked(
        self,
        manifest: ArchiveManifest,
        *,
        create_backup: bool,
        before_write: Callable[[], None] | None,
    ) -> None:
        lock_fd = self._acquire_lock()
        try:
            try:
                disk_raw, disk_path = self._read_raw()
            except FileNotFoundError:
                disk_raw, disk_path = None, None
            # 읽을 수 없는 manifest(ValueError)·인증 실패(ManifestAuthError)는 그대로 올린다.
            # 예전에는 여기서 예외를 삼키고 메모리 값으로 덮어 다른 writer의 항목을 잃었다.
            self._check_write_policy(disk_raw)
            merged = self._merge(manifest, disk_raw)

            if before_write is not None:
                before_write()

            if self._passphrase is not None:
                previous_auth = (disk_raw or {}).get("auth") or manifest.auth
                merged["auth"] = self._build_auth(merged, previous_auth)
            payload = json.dumps(merged, ensure_ascii=False, indent=2)

            # 백업은 '읽을 수 있었던 기본 manifest'만 옮긴다. 깨진 파일로 좋은 백업을 덮지 않는다.
            if create_backup and disk_path == self.manifest_path:
                self._atomic_write_text(
                    self.backup_path, self.manifest_path.read_text(encoding="utf-8")
                )

            self._atomic_write_text(self.manifest_path, payload)
            self._adopt_written(manifest, merged)
        finally:
            self._release_lock(lock_fd)

    def save(self, manifest: ArchiveManifest, *, create_backup: bool = True) -> None:
        """잠금 안에서 디스크 최신본과 병합(CAS)해 원자적으로 교체한다.

        성공하면 ``manifest``는 기록된 최신 내용으로 갱신된다.

        Raises:
            ManifestConflictError: 같은 항목을 다른 writer가 먼저 바꿨다.
            ManifestAuthError: 서명 정책 위반(다운그레이드/세탁) 또는 디스크 manifest 인증 실패.
            ValueError: checksum 없는 신규 항목, 또는 디스크 manifest를 읽을 수 없음.
        """
        self._save_locked(manifest, create_backup=create_backup, before_write=None)

    def commit_partition(
        self,
        manifest: ArchiveManifest,
        entry: ArchivePartitionEntry,
        *,
        temp_path: str | Path,
        final_path: str | Path,
    ) -> None:
        """데이터 파일 교체와 manifest 항목 기록을 같은 잠금 안에서 한다.

        CAS가 통과한 뒤에만 ``temp_path``를 ``final_path``로 옮기므로, 동시에 같은
        파티션을 내보내도 manifest와 파일이 서로 다른 writer의 것으로 섞이지 않는다.
        실패하면 메모리의 항목을 되돌린다(임시 파일 정리는 호출자 몫).
        """
        name = entry.partition_name
        previous_index = next(
            (
                idx
                for idx, item in enumerate(manifest.partitions)
                if item.get("partition_name") == name
            ),
            None,
        )
        previous = (
            copy.deepcopy(manifest.partitions[previous_index])
            if previous_index is not None
            else None
        )
        self.upsert_partition(manifest, entry)
        try:
            self._save_locked(
                manifest,
                create_backup=True,
                before_write=lambda: self.replace_file_atomically(temp_path, final_path),
            )
        except BaseException:
            manifest.partitions = [
                item for item in manifest.partitions if item.get("partition_name") != name
            ]
            if previous is not None and previous_index is not None:
                manifest.partitions.insert(previous_index, previous)
            raise

    def upsert_parent_table(
        self,
        manifest: ArchiveManifest,
        *,
        parent_table: str,
        table_type: TableType,
        columns: list[dict[str, Any]],
    ) -> None:
        manifest.parent_tables[parent_table] = {
            "table_type": table_type.value,
            "table_name": table_type.table_name,
            "date_column": table_type.date_column,
            "date_is_timestamp": table_type.date_is_timestamp,
            "columns": columns,
        }

    def upsert_partition(self, manifest: ArchiveManifest, entry: ArchivePartitionEntry) -> None:
        payload = asdict(entry)
        for idx, existing in enumerate(manifest.partitions):
            if existing.get("partition_name") == entry.partition_name:
                # CAS 토큰은 store가 관리한다. 호출자가 만든 entry의 기본값(0)으로 덮지 않는다.
                payload["entry_version"] = _entry_version(existing)
                manifest.partitions[idx] = payload
                break
        else:
            manifest.partitions.append(payload)

    def resolve_safe_path(self, relative_path: str) -> Path:
        """아카이브 루트 하위 경로만 허용, 절대경로/.. 탈출 차단."""
        if not relative_path:
            raise ValueError("빈 파일 경로입니다.")
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise ValueError(f"절대 경로는 허용되지 않습니다: {relative_path}")
        resolved = (self.archive_dir / candidate).resolve()
        archive_root = self.archive_dir.resolve()
        if not str(resolved).startswith(str(archive_root) + os.sep) and resolved != archive_root:
            raise ValueError(f"아카이브 루트를 벗어나는 경로입니다: {relative_path}")
        return resolved

    @staticmethod
    def _validate_partition_name(partition_name: str) -> str:
        if not partition_name or not _SAFE_PARTITION_NAME_RE.match(partition_name):
            raise ValueError(f"안전하지 않은 파티션 이름입니다: {partition_name!r}")
        return partition_name

    def build_partition_file_path(self, partition_name: str) -> Path:
        self._validate_partition_name(partition_name)
        return self.partitions_dir / f"{partition_name}.csv"

    def build_partition_temp_file_path(self, partition_name: str) -> Path:
        self._validate_partition_name(partition_name)
        return self.partitions_dir / f".{partition_name}.csv.tmp"

    # ── 파일 무결성 ───────────────────────────────────────────────────

    def compute_file_metadata(
        self,
        file_path: str | Path,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """파일의 크기와 SHA-256을 구한다.

        Args:
            should_stop: 중간에 멈춰야 하는지 묻는 콜백. 수 GB 파일이면 이 루프가
                수 분씩 돌기 때문에, 취소 훅이 없으면 창을 닫아도 워커가 안 멈춘다.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"아카이브 데이터 파일이 없습니다: {path}")

        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as fp:
            while True:
                if should_stop is not None and should_stop():
                    raise ScanCancelled(f"체크섬 계산이 취소되었습니다: {path.name}")
                chunk = fp.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)

        return {
            "bytes_written": size,
            "checksum_sha256": digest.hexdigest(),
            "verified_at": datetime.now().isoformat(),
        }

    @staticmethod
    def _verify_entry_against_metadata(
        entry: ArchivePartitionEntry,
        metadata: dict[str, Any],
        *,
        allow_missing_checksum: bool = False,
    ) -> None:
        expected_size = int(entry.bytes_written or 0)
        if expected_size and metadata["bytes_written"] != expected_size:
            raise ValueError(
                f"파일 크기 불일치: expected={expected_size}, actual={metadata['bytes_written']}"
            )
        expected_checksum = _normalize_checksum(entry.checksum_sha256)
        if not expected_checksum:
            if allow_missing_checksum:
                return
            raise ValueError(
                f"checksum_sha256이 없어 파일 무결성을 검증할 수 없습니다: {entry.partition_name} "
                "(legacy 아카이브는 명시적 확인이 있어야 가져올 수 있습니다)"
            )
        if not _SHA256_HEX_RE.match(expected_checksum):
            raise ValueError(
                f"checksum_sha256 형식이 올바르지 않습니다(파일 체크섬 검증 불가): {entry.partition_name}"
            )
        if metadata["checksum_sha256"] != expected_checksum:
            raise ValueError(
                "파일 체크섬 불일치: "
                f"expected={expected_checksum}, actual={metadata['checksum_sha256']}"
            )

    def verify_partition_file(
        self,
        entry: ArchivePartitionEntry,
        *,
        should_stop: Callable[[], bool] | None = None,
        allow_missing_checksum: bool = False,
    ) -> dict[str, Any]:
        file_path = self.resolve_safe_path(entry.file_path)
        metadata = self.compute_file_metadata(file_path, should_stop=should_stop)
        self._verify_entry_against_metadata(
            entry, metadata, allow_missing_checksum=allow_missing_checksum
        )
        return metadata

    def verify_open_file(
        self,
        entry: ArchivePartitionEntry,
        fp,
        *,
        allow_missing_checksum: bool = False,
    ) -> dict[str, Any]:
        """열린 파일 핸들을 통해 무결성 검증 (TOCTOU 방어)."""
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            size += len(chunk)
            digest.update(chunk)
        metadata = {
            "bytes_written": size,
            "checksum_sha256": digest.hexdigest(),
            "verified_at": datetime.now().isoformat(),
        }
        self._verify_entry_against_metadata(
            entry, metadata, allow_missing_checksum=allow_missing_checksum
        )
        return metadata

    # ── 조회 ──────────────────────────────────────────────────────────

    def get_partition_entry(self, partition_name: str) -> ArchivePartitionEntry | None:
        manifest = self.load()
        for item in manifest.partitions:
            if item.get("partition_name") == partition_name:
                return ArchivePartitionEntry.from_dict(item)
        return None

    def get_completed_status(
        self,
        partition_names: list[str],
        *,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, bool]:
        """파티션별로 아카이브에 온전히 들어 있는지 확인한다.

        파일마다 체크섬을 다시 계산하므로 아카이브가 크면 오래 걸린다.
        크기 비교로 대체하지 않는다 — '완료' 오탐은 사용자가 그 파티션을
        건너뛰게 만들고, 손상된 데이터가 조용히 남는다. checksum이 없는 legacy
        항목도 같은 이유로 '미완료'로 본다(다시 export하면 checksum이 생긴다).

        Args:
            should_stop: 중간에 멈춰야 하는지 묻는 콜백.
            on_progress: (done, total) 진행 상황 콜백.
        """
        manifest = self.load()
        by_name = {item.get("partition_name"): item for item in manifest.partitions}
        results: dict[str, bool] = {}
        total = len(partition_names)
        for index, name in enumerate(partition_names, start=1):
            if should_stop is not None and should_stop():
                raise ScanCancelled("완료 여부 확인이 취소되었습니다")

            item = by_name.get(name)
            if not item:
                results[name] = False
            else:
                try:
                    self.verify_partition_file(
                        ArchivePartitionEntry.from_dict(item),
                        should_stop=should_stop,
                    )
                    results[name] = True
                except ScanCancelled:
                    raise
                except Exception:
                    results[name] = False

            if on_progress is not None:
                on_progress(index, total)
        return results

    def filter_partitions(
        self,
        *,
        start_date: date,
        end_date: date,
        table_types: list[TableType],
    ) -> list[dict[str, Any]]:
        manifest = self.load()
        selected_codes = {table_type.value for table_type in table_types}
        partitions: list[dict[str, Any]] = []

        for item in manifest.partitions:
            entry = ArchivePartitionEntry.from_dict(item)
            if entry.table_type not in selected_codes:
                continue

            try:
                table_type = TableType(entry.table_type)
            except ValueError:
                continue

            from_ts = entry.from_timestamp
            to_ts = entry.to_timestamp
            if from_ts is None or to_ts is None:
                inferred_from, inferred_to = infer_partition_range(table_type, entry.partition_name)
                from_ts = from_ts if from_ts is not None else inferred_from
                to_ts = to_ts if to_ts is not None else inferred_to

            if from_ts is None or to_ts is None:
                continue

            partition_start = datetime.fromtimestamp(from_ts / 1000).date()
            partition_end = datetime.fromtimestamp(to_ts / 1000).date()
            if partition_start > end_date or partition_end < start_date:
                continue

            partitions.append(
                {
                    "table_name": entry.partition_name,
                    "table_type": table_type,
                    "table_type_code": table_type.value,
                    "start_date": partition_start,
                    "end_date": partition_end,
                    "row_count": int(entry.row_count or 0),
                    "from_timestamp": from_ts,
                    "to_timestamp": to_ts,
                    "file_path": entry.file_path,
                }
            )

        partitions.sort(key=lambda part: (part["table_type_code"], part["from_timestamp"] or 0))
        return partitions

    def get_parent_table_metadata(self, parent_table: str) -> dict[str, Any]:
        manifest = self.load()
        metadata = manifest.parent_tables.get(parent_table)
        if not metadata:
            raise KeyError(f"parent_table metadata not found: {parent_table}")
        return metadata
