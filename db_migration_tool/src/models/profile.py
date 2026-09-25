"""
연결 프로필 모델 및 관리자
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import text

from src.database.local_db import LocalDatabase, Profile, SavedConnection, get_db
from src.utils.secret_store import (
    KeyProtectError,
    SecretStoreError,
    StoredKey,
    WrappedKeyFile,
    backup_key_file,
    backup_sqlite_database,
    default_protector,
    quarantine_file,
    remove_backups,
)

logger = logging.getLogger(__name__)


class ProfileKeyUnavailableError(RuntimeError):
    """프로필 암호화 키를 쓸 수 없다(다른 사용자·장치의 키, 손상, 기록 실패 등)."""


class ProfileDecryptError(RuntimeError):
    """저장된 프로필 암호문을 현재 키로 풀 수 없다."""


# 평상시에는 Fernet. 키 교체가 도중에 멈춘 동안만 새 키 + 이전 키를 읽는 MultiFernet이다
# (새 기록은 항상 첫 번째 키, 즉 새 키로 한다). 과거 공개 키는 절대 들어가지 않는다.
ProfileCipher = Fernet | MultiFernet


ENDPOINT_KIND_POSTGRES = "postgres"
ENDPOINT_KIND_FILE = "file"
SUPPORTED_ENDPOINT_KINDS = {ENDPOINT_KIND_POSTGRES, ENDPOINT_KIND_FILE}


def normalize_endpoint_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """엔드포인트 설정을 정규화합니다.

    레거시 프로필은 kind 필드가 없으므로 PostgreSQL로 간주합니다.
    """
    normalized = dict(config or {})
    kind = normalized.get("kind")

    if kind not in SUPPORTED_ENDPOINT_KINDS:
        if "archive_path" in normalized and not any(
            key in normalized for key in ("host", "database", "username")
        ):
            kind = ENDPOINT_KIND_FILE
        else:
            kind = ENDPOINT_KIND_POSTGRES

    normalized["kind"] = kind

    if kind == ENDPOINT_KIND_POSTGRES:
        normalized.setdefault("host", "localhost")
        normalized.setdefault("port", 5432)
        normalized.setdefault("database", "")
        normalized.setdefault("username", "")
        normalized.setdefault("password", "")
        normalized.setdefault("ssl", False)
        normalized.setdefault("compat_mode", "auto")
        normalized.pop("archive_path", None)
    else:
        normalized.setdefault("archive_path", "")
        normalized.pop("host", None)
        normalized.pop("port", None)
        normalized.pop("database", None)
        normalized.pop("username", None)
        normalized.pop("password", None)
        normalized.pop("ssl", None)
        normalized.pop("compat_mode", None)

    return normalized


class ConnectionProfile:
    """연결 프로필 데이터 클래스"""

    def __init__(
        self,
        id: int | None = None,
        name: str = "",
        source_config: dict[str, Any] | None = None,
        target_config: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        locked: bool = False,
        lock_reason: str = "",
    ):
        self.id = id
        self.name = name
        self.source_config = normalize_endpoint_config(source_config)
        self.target_config = normalize_endpoint_config(target_config)
        self.created_at = created_at
        self.updated_at = updated_at
        # 잠김: 저장된 연결 정보를 현재 키로 풀 수 없다. 이름만 보이고 설정은 기본값이다.
        # 연결 정보를 다시 입력(update)하거나 삭제할 수 있지만 작업을 시작할 수는 없다.
        self.locked = locked
        self.lock_reason = lock_reason

    @property
    def source_kind(self) -> str:
        return str(self.source_config.get("kind", ENDPOINT_KIND_POSTGRES))

    @property
    def target_kind(self) -> str:
        return str(self.target_config.get("kind", ENDPOINT_KIND_POSTGRES))

    @property
    def migration_mode(self) -> str:
        return f"{self.source_kind}_to_{self.target_kind}"

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환"""
        return {
            "id": self.id,
            "name": self.name,
            "source_config": self.source_config,
            "target_config": self.target_config,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_db_model(cls, db_profile: Profile, cipher_suite: ProfileCipher) -> ConnectionProfile:
        """DB 모델에서 생성

        현재 키로만 복호화합니다. 과거 공개 키로 된 암호문은 `ensure_profile_cipher()`의
        1회 마이그레이션이 재암호화하며, 읽기 경로에는 fallback이 없습니다(H-06).
        """
        try:
            source_config = json.loads(cipher_suite.decrypt(db_profile.source_config.encode()))
            target_config = json.loads(cipher_suite.decrypt(db_profile.target_config.encode()))
        except InvalidToken as exc:
            raise ProfileDecryptError(
                f"프로필 '{db_profile.name}'을(를) 현재 암호화 키로 복호화할 수 없습니다. "
                "다른 사용자·장치나 다른 키로 저장된 값입니다. 연결 정보를 다시 입력하거나 "
                "프로필을 삭제하십시오."
            ) from exc

        return cls(
            id=db_profile.id,
            name=db_profile.name,
            source_config=source_config,
            target_config=target_config,
            created_at=db_profile.created_at,
            updated_at=db_profile.updated_at,
        )

    @classmethod
    def locked_from_db_model(cls, db_profile: Profile, reason: str) -> ConnectionProfile:
        """복호화하지 못한 행 — 이름과 ID만 담은 잠긴 프로필."""
        return cls(
            id=db_profile.id,
            name=db_profile.name,
            created_at=db_profile.created_at,
            updated_at=db_profile.updated_at,
            locked=True,
            lock_reason=reason,
        )


# --- 암호화 키 준비와 1회 마이그레이션 (감사 H-06, M-05) ---------------------------------

KEY_FILE_NAME = ".encryption_key"
# DB 쪽 키 상태 표식. 현재 키의 확인값(HMAC)과 보호 방식을 담는다. 키 파일이 이 DB의 키와
# 맞는지 판정하고, 1회 마이그레이션이 끝났음을 기록한다.
KEY_STATE_TABLE = "profile_key_state"
_KEY_STATE_NAME = "profile_key"

# 과거 소스에 하드코딩되어 공개된 Fernet 키. 누구나 아는 값이라 보호 수단이 아니다.
# 이 키로만 풀리는 기존 암호문을 찾아 재암호화하는 용도로만 `_legacy_cipher()`가 읽는다.
# 새 기록이나 읽기 경로에 이 키를 쓰면 안 된다.
_LEGACY_PUBLIC_KEY = b"ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="

_migration_lock = threading.Lock()
_plain_key_warned: set[str] = set()


def default_profile_key_file() -> WrappedKeyFile:
    """앱 데이터 디렉터리의 키 파일. Windows는 DPAPI, 그 밖에는 0600 평문 폴백."""
    from src.utils.app_paths import AppPaths

    return WrappedKeyFile(AppPaths.get_app_data_dir() / KEY_FILE_NAME, default_protector())


def _legacy_cipher() -> Fernet:
    return Fernet(_LEGACY_PUBLIC_KEY)


@dataclass(frozen=True)
class _EncryptedField:
    model: type[Profile] | type[SavedConnection]
    row_id: int
    column: str
    token: str


@dataclass(frozen=True)
class _KeyState:
    check: str
    scheme: str


def _collect_encrypted_fields(db: LocalDatabase) -> list[_EncryptedField]:
    fields: list[_EncryptedField] = []
    with db.session_scope() as session:
        for p in session.query(Profile).order_by(Profile.id):
            fields.append(_EncryptedField(Profile, p.id, "source_config", p.source_config))
            fields.append(_EncryptedField(Profile, p.id, "target_config", p.target_config))
        for c in session.query(SavedConnection).order_by(SavedConnection.id):
            if c.password:
                fields.append(_EncryptedField(SavedConnection, c.id, "password", c.password))
    return fields


def _can_decrypt(cipher: Fernet | MultiFernet, token: str) -> bool:
    try:
        cipher.decrypt(token.encode())
        return True
    except InvalidToken:
        return False


def _is_same_key(key: bytes, other: Fernet) -> bool:
    """`key`가 `other`와 같은 키인지 — 상수를 직접 비교하지 않고 암호문 왕복으로 판정."""
    probe = b"dbmt-key-probe"
    try:
        return Fernet(key).decrypt(other.encrypt(probe)) == probe
    except InvalidToken:
        return False


def _key_check(key: bytes) -> str:
    """키 확인값. 키를 드러내지 않고 '같은 키인가'만 판정한다."""
    return hmac.new(key, b"DBMT profile key check v1", hashlib.sha256).hexdigest()


def _read_key_state(db: LocalDatabase) -> _KeyState | None:
    with db.session_scope() as session:
        table = session.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = :t"),
            {"t": KEY_STATE_TABLE},
        ).first()
        if table is None:
            return None
        row = session.execute(
            text(f"SELECT value FROM {KEY_STATE_TABLE} WHERE name = :n"),
            {"n": _KEY_STATE_NAME},
        ).first()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
        return _KeyState(check=str(data["check"]), scheme=str(data["scheme"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise ProfileKeyUnavailableError("DB에 기록된 암호화 키 상태가 손상되었습니다") from exc


def _write_key_state(session: Any, key: bytes, scheme: str) -> None:
    session.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {KEY_STATE_TABLE} "
            "(name TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
    )
    value = json.dumps({"version": 2, "check": _key_check(key), "scheme": scheme})
    session.execute(
        text(f"INSERT OR REPLACE INTO {KEY_STATE_TABLE} (name, value) VALUES (:n, :v)"),
        {"n": _KEY_STATE_NAME, "v": value},
    )


def _reencrypt_token(source: Fernet, target: Fernet, token: str) -> str:
    return target.encrypt(source.decrypt(token.encode())).decode()


def _warn_unprotected_key(key_file: WrappedKeyFile) -> None:
    try:
        key_file.harden_permissions()
    except OSError as exc:
        logger.warning("키 파일 권한을 소유자 전용으로 바꾸지 못했습니다: %s", exc)
    marker = str(key_file.path)
    if marker not in _plain_key_warned:
        _plain_key_warned.add(marker)
        logger.warning(
            "이 플랫폼에서는 OS 보호 저장소(DPAPI)를 쓸 수 없어 프로필 암호화 키를 "
            "소유자 전용(0600) 평문 파일로 보관합니다: %s",
            key_file.path,
        )


def _cleanup_migration_backups(db: LocalDatabase, key_file: WrappedKeyFile) -> None:
    """검증을 마친 마이그레이션의 백업을 지운다.

    백업에는 과거 공개 키 암호문이나 교체 전 키로 된 암호문이 들어 있다. 남겨 두면
    디렉터리를 복사하는 것만으로 H-06/M-05 노출이 되살아난다.
    """
    targets = [key_file.path]
    if db.db_path:
        targets.append(Path(db.db_path))
    for path in targets:
        try:
            removed = remove_backups(path)
        except OSError as exc:
            logger.warning("마이그레이션 백업을 지우지 못했습니다(다음 실행 때 다시 시도): %s", exc)
            continue
        if removed:
            logger.info("검증을 마친 키 마이그레이션 백업 %d개를 지웠습니다.", len(removed))


def _discard(paths: list[Path]) -> None:
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink()


def _check_key_matches_state(stored: StoredKey, state: _KeyState) -> None:
    candidates = [k for k in (stored.key, stored.previous) if k is not None]
    if not any(hmac.compare_digest(_key_check(k), state.check) for k in candidates):
        raise ProfileKeyUnavailableError(
            "키 파일이 이 DB에 기록된 암호화 키와 다릅니다(다른 설치의 키 파일로 바뀌었을 수 "
            "있습니다). 원래 키 파일을 되돌리거나 '키 재설정'으로 새 키를 만드십시오."
        )


def ensure_profile_cipher(db: LocalDatabase, key_file: WrappedKeyFile) -> ProfileCipher:
    """프로필 암호화 키를 준비하고, 필요하면 1회 마이그레이션한다.

    마이그레이션 대상(한 번만)
    - 키 파일이 없거나 과거 공개 키 자체 → 새 랜덤 키.
    - 평문 키 파일 + OS 보호 저장소 사용 가능 → 새 키로 교체하면서 래핑. 키 값을 유지하면
      평문 키의 사본(백업·옛 복사본)으로 현재 DB가 계속 풀리므로 반드시 교체한다.
    - 과거 공개 키로만 풀리는 암호문 → 현재 키로 재암호화.

    완료 표식은 두 곳에 남긴다. 보호된 키 파일 안의 `migrated`와 DB의 키 상태 행이다.
    둘 중 하나라도 있으면 legacy 암호문을 다시는 받아들이지 않는다 — 나중에 들어온 공개 키
    암호문은 잠긴 프로필로 남을 뿐 재암호화("세탁")되지 않는다. DB 표식의 키 확인값과
    다른 키 파일은 수용하지 않는다.

    순서와 안전성
    1. DB·키 파일 백업(`*.bak-pre-keywrap`, 평문 키는 보호기로 감싸 백업). 실패하면 아무것도
       바꾸지 않는다.
    2. 키 파일을 새 키 + 이전 키(저널)로 원자적 교체. DB보다 먼저 — 도중에 끊겨도 이전 키가
       보호된 파일 안에 남아 다음 실행에서 이어 간다.
    3. 암호문 재암호화와 DB 표식 기록을 한 트랜잭션으로. 실패하면 롤백.
    4. 다시 읽어 검증한 뒤 키 파일에서 이전 키를 버리고 완료 표식을 남긴다.
    5. 백업을 지운다(백업 자체가 노출 경로이므로).
    어떤 경우에도 풀 수 없는 암호문을 지우지 않는다.

    Raises:
        ProfileKeyUnavailableError: 쓸 수 있는 키가 없다(다른 장치의 키, 손상, 새 키 기록 실패).
    """
    with _migration_lock:
        try:
            stored: StoredKey = key_file.read()
        except (SecretStoreError, OSError) as exc:
            raise ProfileKeyUnavailableError(
                f"프로필 암호화 키를 열 수 없습니다({key_file.path.name}): {exc}"
            ) from exc

        state = _read_key_state(db)
        fields = _collect_encrypted_fields(db)
        protected = key_file.protector.os_protected
        legacy = _legacy_cipher()
        legacy_done = state is not None or stored.migrated
        legacy_to_convert = None if legacy_done else legacy

        if state is not None and stored.key is not None:
            _check_key_matches_state(stored, state)

        if stored.key is None and fields and state is not None:
            # 완료된 설치에서 키 파일만 사라졌다. 새 키를 조용히 만들면 기존 암호문과 어긋난
            # 키가 자리 잡으므로, 사용자가 명시적으로 재설정하게 한다.
            raise ProfileKeyUnavailableError(
                "암호화 키 파일이 없어 저장된 프로필을 열 수 없습니다. 원래 키 파일을 "
                "되돌리거나 '키 재설정'으로 새 키를 만드십시오."
            )

        if stored.key is None or _is_same_key(stored.key, legacy):
            # 신규 설치, 또는 구버전이 공개 키를 키 파일에 써 둔 상태 → 새 랜덤 키.
            return _migrate(
                db,
                key_file,
                stored,
                fields,
                target_key=Fernet.generate_key(),
                old_key=None,
                legacy=legacy_to_convert,
                fallback=None,
            )

        if stored.form == "plain" and protected:
            if state is not None and state.scheme != "plain":
                logger.warning(
                    "OS 보호 저장소로 옮겼던 암호화 키가 평문 파일로 다시 나타났습니다. "
                    "노출된 키로 보고 새 키로 교체합니다: %s",
                    key_file.path,
                )
            return _migrate(
                db,
                key_file,
                stored,
                fields,
                target_key=Fernet.generate_key(),
                old_key=stored.key,
                legacy=legacy_to_convert,
                fallback=Fernet(stored.key),
            )

        unfinished = (
            stored.previous is not None
            or not legacy_done
            or state is None
            or (protected and not stored.migrated)
        )
        if unfinished:
            current = Fernet(stored.key)
            fallback: ProfileCipher = (
                MultiFernet([current, Fernet(stored.previous)])
                if stored.previous is not None
                else current
            )
            return _migrate(
                db,
                key_file,
                stored,
                fields,
                target_key=stored.key,
                old_key=stored.previous,
                legacy=legacy_to_convert,
                fallback=fallback,
            )

        # 정상 상태: 아무것도 바꾸지 않는다.
        cipher = Fernet(stored.key)
        unreadable = [f for f in fields if not _can_decrypt(cipher, f.token)]
        if unreadable:
            public = sum(_can_decrypt(legacy, f.token) for f in unreadable)
            logger.warning(
                "현재 키로 풀 수 없는 암호문 %d건(그중 과거 공개 키 %d건)은 재암호화하지 않고 "
                "잠긴 프로필로 둡니다. 연결 정보를 다시 입력하거나 삭제하십시오.",
                len(unreadable),
                public,
            )
        _cleanup_migration_backups(db, key_file)
        if not protected:
            _warn_unprotected_key(key_file)
        return cipher


def _migrate(
    db: LocalDatabase,
    key_file: WrappedKeyFile,
    stored: StoredKey,
    fields: list[_EncryptedField],
    *,
    target_key: bytes,
    old_key: bytes | None,
    legacy: Fernet | None,
    fallback: ProfileCipher | None,
) -> ProfileCipher:
    """`ensure_profile_cipher`의 실행부. `fallback`은 아무것도 못 바꿨을 때 계속 쓸 키."""
    protected = key_file.protector.os_protected
    new_key = target_key != stored.key
    target = Fernet(target_key)
    old = Fernet(old_key) if old_key is not None else None
    sources = [c for c in (old, legacy) if c is not None]

    conversions: list[tuple[_EncryptedField, Fernet]] = []
    unreadable = 0
    for field in fields:
        if _can_decrypt(target, field.token):
            continue
        source = next((c for c in sources if _can_decrypt(c, field.token)), None)
        if source is None:
            unreadable += 1
        else:
            conversions.append((field, source))
    if unreadable:
        logger.warning(
            "현재 키로도 이전 키로도 풀 수 없는 암호문 %d건이 있습니다. 삭제하지 않고 잠긴 "
            "프로필로 둡니다.",
            unreadable,
        )

    def give_up(reason: str, exc: BaseException) -> ProfileCipher:
        if fallback is None:
            raise ProfileKeyUnavailableError(f"{reason}: {exc}") from exc
        logger.error("%s. 기존 키로 계속 동작하고 다음 실행 때 다시 시도합니다: %s", reason, exc)
        return fallback

    # 1) 백업
    backups: list[Path] = []
    try:
        if conversions and db.db_path:
            db_backup = backup_sqlite_database(Path(db.db_path))
            if db_backup is not None:
                backups.append(db_backup)
        if new_key:
            key_backup = backup_key_file(key_file, stored)
            if key_backup is not None:
                backups.append(key_backup)
    except Exception as exc:
        _discard(backups)
        return give_up("키 마이그레이션 전 백업에 실패했습니다", exc)

    # 2) 키 파일 — 새 키와 이전 키(저널)를 함께. 되읽어 확인한다.
    scheme = key_file.protector.scheme if protected else "plain"
    finished_in_file = not protected or (stored.previous is None and stored.migrated)
    if new_key:
        journal = old_key if protected else None
        done_now = not conversions and journal is None
        try:
            try:
                key_file.write(target_key, previous=journal, migrated=done_now)
            except KeyProtectError:
                if fallback is not None:
                    raise
                logger.warning(
                    "OS 보호 저장소로 암호화 키를 감싸지 못해 소유자 전용 평문 파일로 "
                    "저장합니다. 다음 실행 때 새 키로 교체하며 다시 감쌉니다."
                )
                key_file.write_plain(target_key)
                scheme = "plain"
                done_now = True
            if key_file.read().key != target_key:
                raise SecretStoreError("기록한 키 파일을 다시 읽었더니 키가 다릅니다")
        except Exception as exc:
            _discard(backups)
            return give_up("새 암호화 키를 저장하지 못했습니다", exc)
        finished_in_file = scheme == "plain" or done_now

    in_progress: ProfileCipher = MultiFernet([target, old]) if old is not None else target

    # 3) 재암호화와 DB 표식 — 한 트랜잭션
    try:
        with db.session_scope() as session:
            for field, source in conversions:
                row = session.get(field.model, field.row_id)
                if row is None or getattr(row, field.column) != field.token:
                    continue  # 스캔 뒤 바뀐 행은 건드리지 않는다
                setattr(row, field.column, _reencrypt_token(source, target, field.token))
            _write_key_state(session, target_key, scheme)
    except Exception as exc:
        logger.error(
            "암호문 재암호화에 실패해 롤백했습니다. 다음 실행 때 이어서 시도합니다: %s", exc
        )
        return in_progress

    # 4) 검증 — 옮긴 값은 새 키로 풀리고, 이전 키·공개 키로 풀리는 값은 남지 않았다.
    after = _collect_encrypted_fields(db)
    moved = {(f.model, f.row_id, f.column) for f, _ in conversions}
    broken = [
        f
        for f in after
        if (f.model, f.row_id, f.column) in moved and not _can_decrypt(target, f.token)
    ]
    leftover = [f for f in after if any(_can_decrypt(c, f.token) for c in sources)]
    if broken or leftover:
        logger.error(
            "재암호화 검증 실패(새 키로 안 풀림 %d건, 이전 키로 풀림 %d건). 백업과 이전 키를 "
            "남겨 두고 다음 실행 때 다시 시도합니다.",
            len(broken),
            len(leftover),
        )
        return in_progress

    # 5) 키 파일 마무리 — 이전 키를 버리고 완료 표식
    if not finished_in_file:
        try:
            key_file.write(target_key, migrated=True)
            final = key_file.read()
            if final.key != target_key or final.previous is not None or not final.migrated:
                raise SecretStoreError("마무리한 키 파일을 다시 읽었더니 내용이 다릅니다")
        except Exception as exc:
            logger.error(
                "키 파일 마무리(이전 키 제거)에 실패했습니다. 다음 실행 때 다시 시도합니다: %s",
                exc,
            )
            return in_progress

    if conversions:
        logger.info("암호문 %d건을 새 암호화 키로 재암호화했습니다.", len(conversions))

    # 6) 백업 정리
    _cleanup_migration_backups(db, key_file)
    if not protected:
        _warn_unprotected_key(key_file)
    return target


def reset_profile_key(db: LocalDatabase, key_file: WrappedKeyFile) -> Fernet:
    """쓸 수 없는 키를 치우고 새 키로 시작한다(사용자가 명시적으로 요청할 때만).

    옛 키 파일은 지우지 않고 `*.unusable` 이름으로 옮겨 보관한다. DB의 옛 암호문, 작업
    이력, 체크포인트는 그대로 둔다 — 옛 암호문은 잠긴 프로필로 남아 사용자가 다시 입력하거나
    삭제할 수 있다. 완료 표식을 남기므로 이 뒤로도 공개 키 암호문은 수용하지 않는다.
    """
    with _migration_lock:
        quarantined = quarantine_file(key_file.path) if key_file.path.exists() else None
        new_key = Fernet.generate_key()
        try:
            scheme = key_file.protector.scheme if key_file.protector.os_protected else "plain"
            try:
                key_file.write(new_key, migrated=True)
            except KeyProtectError:
                key_file.write_plain(new_key)
                scheme = "plain"
            if key_file.read().key != new_key:
                raise SecretStoreError("기록한 키 파일을 다시 읽었더니 키가 다릅니다")
            with db.session_scope() as session:
                _write_key_state(session, new_key, scheme)
        except Exception as exc:
            with contextlib.suppress(OSError):
                key_file.path.unlink()
            if quarantined is not None:
                os.replace(quarantined, key_file.path)
            raise ProfileKeyUnavailableError(f"암호화 키를 재설정하지 못했습니다: {exc}") from exc
        logger.warning(
            "프로필 암호화 키를 재설정했습니다. 옛 키 파일 보관: %s. 옛 키로 저장된 프로필은 "
            "잠긴 상태로 남습니다.",
            quarantined,
        )
        return Fernet(new_key)


class ProfileManager:
    """프로필 관리자 클래스"""

    def __init__(
        self,
        db: LocalDatabase | None = None,
        key_file: WrappedKeyFile | None = None,
    ):
        self.db = db if db is not None else get_db()
        self._key_file = key_file if key_file is not None else default_profile_key_file()
        self._cipher_suite: ProfileCipher | None = None
        self._key_error: Exception | None = None
        try:
            self._cipher_suite = ensure_profile_cipher(self.db, self._key_file)
        except Exception as exc:  # 앱 시작을 막지 않는다 — 사용하는 순간 오류로 알린다
            logger.error("프로필 암호화 키를 준비하지 못했습니다: %s", exc)
            self._key_error = exc

    @property
    def key_available(self) -> bool:
        return self._cipher_suite is not None

    @property
    def key_error(self) -> str | None:
        return None if self._key_error is None else str(self._key_error)

    def _cipher(self) -> ProfileCipher:
        if self._cipher_suite is None:
            raise ProfileKeyUnavailableError(
                f"프로필 암호화 키를 사용할 수 없습니다: {self._key_error}"
            ) from self._key_error
        return self._cipher_suite

    def reset_encryption_key(self) -> None:
        """키를 쓸 수 없을 때만 새 키로 재설정한다. 기존 행과 이력은 보존된다."""
        if self._cipher_suite is not None:
            raise RuntimeError(
                "암호화 키를 쓸 수 있는 상태에서는 재설정하지 않습니다(기존 프로필을 잃게 됩니다)."
            )
        self._cipher_suite = reset_profile_key(self.db, self._key_file)
        self._key_error = None

    def _to_profile(self, db_profile: Profile) -> ConnectionProfile:
        """복호화하지 못한 행은 목록에서 빼지 않고 잠긴 프로필로 돌려준다."""
        if self._cipher_suite is None:
            return ConnectionProfile.locked_from_db_model(
                db_profile, f"암호화 키를 사용할 수 없습니다: {self._key_error}"
            )
        try:
            return ConnectionProfile.from_db_model(db_profile, self._cipher_suite)
        except ProfileDecryptError as exc:
            return ConnectionProfile.locked_from_db_model(db_profile, str(exc))

    def _encrypt_config(self, config: dict[str, Any]) -> str:
        """설정 암호화"""
        json_str = json.dumps(normalize_endpoint_config(config))
        encrypted = self._cipher().encrypt(json_str.encode())
        return encrypted.decode()

    def create_profile(self, profile_data: dict[str, Any]) -> ConnectionProfile:
        """새 프로필 생성"""
        with self.db.session_scope() as session:
            db_profile = Profile(
                name=profile_data["name"],
                source_config=self._encrypt_config(profile_data["source_config"]),
                target_config=self._encrypt_config(profile_data["target_config"]),
            )

            session.add(db_profile)
            session.flush()

            return ConnectionProfile.from_db_model(db_profile, self._cipher())

    def get_profile(self, profile_id: int) -> ConnectionProfile | None:
        """프로필 조회 (복호화하지 못하면 잠긴 프로필)"""
        with self.db.session_scope() as session:
            db_profile = session.query(Profile).filter_by(id=profile_id).first()
            if db_profile:
                return self._to_profile(db_profile)
            return None

    def get_profile_name(self, profile_id: int) -> str | None:
        """복호화 없이 이름만 조회한다(이력 표시 등)."""
        with self.db.session_scope() as session:
            row = session.query(Profile.name).filter_by(id=profile_id).first()
            return row[0] if row else None

    def get_all_profiles(self) -> list[ConnectionProfile]:
        """모든 프로필 조회 — 복호화하지 못한 행도 잠긴 프로필로 포함한다."""
        with self.db.session_scope() as session:
            db_profiles = session.query(Profile).order_by(Profile.name).all()
            return [self._to_profile(p) for p in db_profiles]

    def update_profile(self, profile_id: int, profile_data: dict[str, Any]) -> ConnectionProfile:
        """프로필 수정 (잠긴 프로필은 연결 정보를 다시 입력하면 현재 키로 풀린다)"""
        with self.db.session_scope() as session:
            db_profile = session.query(Profile).filter_by(id=profile_id).first()
            if not db_profile:
                raise ValueError(f"프로필을 찾을 수 없습니다: {profile_id}")

            db_profile.name = profile_data["name"]
            db_profile.source_config = self._encrypt_config(profile_data["source_config"])
            db_profile.target_config = self._encrypt_config(profile_data["target_config"])

            return ConnectionProfile.from_db_model(db_profile, self._cipher())

    def delete_profile(self, profile_id: int) -> bool:
        """프로필 삭제 (복호화가 필요 없다)"""
        with self.db.session_scope() as session:
            db_profile = session.query(Profile).filter_by(id=profile_id).first()
            if db_profile:
                session.delete(db_profile)
                return True
            return False
