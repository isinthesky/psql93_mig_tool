"""
연결 프로필 모델 및 관리자
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from src.database.local_db import LocalDatabase, Profile, SavedConnection, get_db
from src.utils.secret_store import (
    KeyProtectError,
    SecretStoreError,
    StoredKey,
    WrappedKeyFile,
    backup_file,
    backup_sqlite_database,
    default_protector,
)

logger = logging.getLogger(__name__)


class ProfileKeyUnavailableError(RuntimeError):
    """프로필 암호화 키를 쓸 수 없다(다른 사용자·장치의 키, 손상, 기록 실패 등)."""


class ProfileDecryptError(RuntimeError):
    """저장된 프로필 암호문을 현재 키로 풀 수 없다."""


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
    ):
        self.id = id
        self.name = name
        self.source_config = normalize_endpoint_config(source_config)
        self.target_config = normalize_endpoint_config(target_config)
        self.created_at = created_at
        self.updated_at = updated_at

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
    def from_db_model(cls, db_profile: Profile, cipher_suite: Fernet) -> ConnectionProfile:
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
                "키 파일과 DB가 다른 사용자·장치에서 복사되었거나 키 마이그레이션이 "
                "끝나지 않았습니다(다음 실행 때 다시 시도합니다)."
            ) from exc

        return cls(
            id=db_profile.id,
            name=db_profile.name,
            source_config=source_config,
            target_config=target_config,
            created_at=db_profile.created_at,
            updated_at=db_profile.updated_at,
        )


# --- 암호화 키 준비와 1회 마이그레이션 (감사 H-06, M-05) ---------------------------------

KEY_FILE_NAME = ".encryption_key"

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


def _can_decrypt(cipher: Fernet, token: str) -> bool:
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


def _reencrypt_token(legacy: Fernet, target: Fernet, token: str) -> str:
    return target.encrypt(legacy.decrypt(token.encode())).decode()


def _reencrypt_fields(
    db: LocalDatabase, fields: list[_EncryptedField], legacy: Fernet, target: Fernet
) -> None:
    """한 트랜잭션으로 재암호화한다. 도중 예외면 전부 롤백된다."""
    with db.session_scope() as session:
        for field in fields:
            row = session.get(field.model, field.row_id)
            if row is None or getattr(row, field.column) != field.token:
                continue  # 스캔 뒤 바뀐 행은 건드리지 않는다
            setattr(row, field.column, _reencrypt_token(legacy, target, field.token))


def _persist_key(key_file: WrappedKeyFile, key: bytes, *, allow_plain_fallback: bool) -> None:
    try:
        key_file.write(key)
    except KeyProtectError:
        if not allow_plain_fallback:
            raise
        logger.warning(
            "OS 보호 저장소로 암호화 키를 감싸지 못해 소유자 전용 평문 파일로 저장합니다. "
            "다음 실행 때 다시 감쌉니다."
        )
        key_file.write_plain(key)
    if key_file.read().key != key:
        raise SecretStoreError("기록한 키 파일을 다시 읽었더니 키가 다릅니다")


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


def ensure_profile_cipher(db: LocalDatabase, key_file: WrappedKeyFile) -> Fernet:
    """프로필 암호화 키를 준비하고, 필요하면 1회 마이그레이션한다.

    마이그레이션 대상
    - 키 파일이 없거나 과거 공개 키 자체인 경우 → 새 랜덤 키로 교체(rotate).
    - 평문 키 파일 + OS 보호 저장소 사용 가능 → 래핑.
    - 과거 공개 키로만 풀리는 암호문 → 현재 키로 재암호화.

    순서와 안전성
    1. DB·키 파일 백업(`*.bak-pre-keywrap`). 실패하면 아무것도 바꾸지 않는다.
    2. 키 파일을 원자적으로 교체한다. DB보다 먼저 — 반대 순서면 새 키로 바뀐 암호문만
       남고 키를 잃을 수 있다. 키를 새로 만드는 경우 기록에 실패하면 이 키를 쓰지 않는다.
    3. 암호문 재암호화는 한 트랜잭션. 실패하면 롤백되어 legacy 암호문이 그대로 남고
       다음 실행 때 다시 시도한다.
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

        legacy = _legacy_cipher()
        rotate = stored.key is None or _is_same_key(stored.key, legacy)
        target_key = Fernet.generate_key() if rotate or stored.key is None else stored.key
        target = Fernet(target_key)

        fields = _collect_encrypted_fields(db)
        unreadable = [f for f in fields if not _can_decrypt(target, f.token)]
        legacy_fields = [f for f in unreadable if _can_decrypt(legacy, f.token)]
        if len(unreadable) > len(legacy_fields):
            logger.warning(
                "현재 키로도 과거 공개 키로도 풀 수 없는 암호문 %d건이 있습니다. "
                "삭제하지 않고 그대로 둡니다.",
                len(unreadable) - len(legacy_fields),
            )

        wrap_needed = stored.form == "plain" and key_file.protector.os_protected
        if not (rotate or wrap_needed or legacy_fields):
            if not key_file.protector.os_protected:
                _warn_unprotected_key(key_file)
            return target

        # 1) 백업
        try:
            if fields and db.db_path:
                backup_sqlite_database(Path(db.db_path))
            backup_file(key_file.path)
        except Exception as exc:
            logger.error("키 마이그레이션 전 백업에 실패해 이번 실행에서는 건너뜁니다: %s", exc)
            if rotate:
                raise ProfileKeyUnavailableError(
                    f"암호화 키 마이그레이션 전 백업에 실패했습니다: {exc}"
                ) from exc
            return target

        # 2) 키 파일
        if rotate or wrap_needed:
            try:
                _persist_key(key_file, target_key, allow_plain_fallback=rotate)
            except Exception as exc:
                if rotate:
                    raise ProfileKeyUnavailableError(
                        f"새 암호화 키를 저장하지 못했습니다: {exc}"
                    ) from exc
                logger.error(
                    "키 파일을 OS 보호 저장소로 감싸지 못했습니다. 기존 키로 계속 동작하고 "
                    "다음 실행 때 다시 시도합니다: %s",
                    exc,
                )

        # 3) legacy 암호문 재암호화
        if legacy_fields:
            try:
                _reencrypt_fields(db, legacy_fields, legacy, target)
                logger.info("과거 공개 키로 된 암호문 %d건을 재암호화했습니다.", len(legacy_fields))
            except Exception as exc:
                logger.error(
                    "과거 공개 키 암호문 재암호화에 실패해 롤백했습니다. 다음 실행 때 다시 "
                    "시도합니다: %s",
                    exc,
                )

        if not key_file.protector.os_protected:
            _warn_unprotected_key(key_file)
        return target


class ProfileManager:
    """프로필 관리자 클래스"""

    def __init__(
        self,
        db: LocalDatabase | None = None,
        key_file: WrappedKeyFile | None = None,
    ):
        self.db = db if db is not None else get_db()
        self._key_file = key_file if key_file is not None else default_profile_key_file()
        self._cipher_suite: Fernet | None = None
        self._key_error: Exception | None = None
        try:
            self._cipher_suite = ensure_profile_cipher(self.db, self._key_file)
        except Exception as exc:  # 앱 시작을 막지 않는다 — 사용하는 순간 오류로 알린다
            logger.error("프로필 암호화 키를 준비하지 못했습니다: %s", exc)
            self._key_error = exc

    def _cipher(self) -> Fernet:
        if self._cipher_suite is None:
            raise ProfileKeyUnavailableError(
                f"프로필 암호화 키를 사용할 수 없습니다: {self._key_error}"
            ) from self._key_error
        return self._cipher_suite

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
        """프로필 조회"""
        with self.db.session_scope() as session:
            db_profile = session.query(Profile).filter_by(id=profile_id).first()
            if db_profile:
                return ConnectionProfile.from_db_model(db_profile, self._cipher())
            return None

    def get_all_profiles(self) -> list[ConnectionProfile]:
        """모든 프로필 조회"""
        with self.db.session_scope() as session:
            db_profiles = session.query(Profile).order_by(Profile.name).all()
            cipher = self._cipher()
            return [ConnectionProfile.from_db_model(p, cipher) for p in db_profiles]

    def update_profile(self, profile_id: int, profile_data: dict[str, Any]) -> ConnectionProfile:
        """프로필 수정"""
        with self.db.session_scope() as session:
            db_profile = session.query(Profile).filter_by(id=profile_id).first()
            if not db_profile:
                raise ValueError(f"프로필을 찾을 수 없습니다: {profile_id}")

            db_profile.name = profile_data["name"]
            db_profile.source_config = self._encrypt_config(profile_data["source_config"])
            db_profile.target_config = self._encrypt_config(profile_data["target_config"])

            return ConnectionProfile.from_db_model(db_profile, self._cipher())

    def delete_profile(self, profile_id: int) -> bool:
        """프로필 삭제"""
        with self.db.session_scope() as session:
            db_profile = session.query(Profile).filter_by(id=profile_id).first()
            if db_profile:
                session.delete(db_profile)
                return True
            return False
