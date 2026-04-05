"""
연결 프로필 모델 및 관리자
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from cryptography.fernet import Fernet

from src.database.local_db import Profile, get_db

ENDPOINT_KIND_POSTGRES = "postgres"
ENDPOINT_KIND_FILE = "file"
SUPPORTED_ENDPOINT_KINDS = {ENDPOINT_KIND_POSTGRES, ENDPOINT_KIND_FILE}


def normalize_endpoint_config(config: Optional[dict[str, Any]]) -> dict[str, Any]:
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
        id: Optional[int] = None,
        name: str = "",
        source_config: Optional[dict[str, Any]] = None,
        target_config: Optional[dict[str, Any]] = None,
        created_at: Optional[datetime] = None,
        updated_at: Optional[datetime] = None,
    ):
        self.id = id
        self.name = name
        self.source_config = normalize_endpoint_config(source_config)
        self.target_config = normalize_endpoint_config(target_config)
        self.created_at = created_at
        self.updated_at = updated_at

    @property
    def source_kind(self) -> str:
        return self.source_config.get("kind", ENDPOINT_KIND_POSTGRES)

    @property
    def target_kind(self) -> str:
        return self.target_config.get("kind", ENDPOINT_KIND_POSTGRES)

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
    def from_db_model(cls, db_profile: Profile, cipher_suite: Fernet) -> "ConnectionProfile":
        """DB 모델에서 생성

        현재 키로 복호화 실패 시 레거시 키로 재시도합니다.
        """
        source_encrypted = db_profile.source_config.encode()
        target_encrypted = db_profile.target_config.encode()

        try:
            source_config = json.loads(cipher_suite.decrypt(source_encrypted).decode())
            target_config = json.loads(cipher_suite.decrypt(target_encrypted).decode())
        except Exception:
            legacy = Fernet(ProfileManager._LEGACY_KEY)
            source_config = json.loads(legacy.decrypt(source_encrypted).decode())
            target_config = json.loads(legacy.decrypt(target_encrypted).decode())

        return cls(
            id=db_profile.id,
            name=db_profile.name,
            source_config=source_config,
            target_config=target_config,
            created_at=db_profile.created_at,
            updated_at=db_profile.updated_at,
        )


class ProfileManager:
    """프로필 관리자 클래스"""

    def __init__(self):
        self.db = get_db()
        self._cipher_suite = self._get_or_create_cipher()

    _LEGACY_KEY = b"ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="

    def _get_or_create_cipher(self) -> Fernet:
        """암호화 키 가져오기 또는 생성"""
        from src.utils.app_paths import AppPaths

        key_file = AppPaths.get_app_data_dir() / ".encryption_key"
        if key_file.exists():
            key = key_file.read_bytes().strip()
        else:
            has_existing_profiles = False
            try:
                with self.db.session_scope() as session:
                    has_existing_profiles = session.query(Profile).first() is not None
            except Exception:
                pass

            if has_existing_profiles:
                key = self._LEGACY_KEY
            else:
                key = Fernet.generate_key()

            key_file.write_bytes(key)
            try:
                import os

                os.chmod(key_file, 0o600)
            except (OSError, AttributeError):
                pass
        return Fernet(key)

    def _encrypt_config(self, config: dict[str, Any]) -> str:
        """설정 암호화"""
        json_str = json.dumps(normalize_endpoint_config(config))
        encrypted = self._cipher_suite.encrypt(json_str.encode())
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

            return ConnectionProfile.from_db_model(db_profile, self._cipher_suite)

    def get_profile(self, profile_id: int) -> Optional[ConnectionProfile]:
        """프로필 조회"""
        with self.db.session_scope() as session:
            db_profile = session.query(Profile).filter_by(id=profile_id).first()
            if db_profile:
                return ConnectionProfile.from_db_model(db_profile, self._cipher_suite)
            return None

    def get_all_profiles(self) -> list[ConnectionProfile]:
        """모든 프로필 조회"""
        with self.db.session_scope() as session:
            db_profiles = session.query(Profile).order_by(Profile.name).all()
            return [ConnectionProfile.from_db_model(p, self._cipher_suite) for p in db_profiles]

    def update_profile(self, profile_id: int, profile_data: dict[str, Any]) -> ConnectionProfile:
        """프로필 수정"""
        with self.db.session_scope() as session:
            db_profile = session.query(Profile).filter_by(id=profile_id).first()
            if not db_profile:
                raise ValueError(f"프로필을 찾을 수 없습니다: {profile_id}")

            db_profile.name = profile_data["name"]
            db_profile.source_config = self._encrypt_config(profile_data["source_config"])
            db_profile.target_config = self._encrypt_config(profile_data["target_config"])

            return ConnectionProfile.from_db_model(db_profile, self._cipher_suite)

    def delete_profile(self, profile_id: int) -> bool:
        """프로필 삭제"""
        with self.db.session_scope() as session:
            db_profile = session.query(Profile).filter_by(id=profile_id).first()
            if db_profile:
                session.delete(db_profile)
                return True
            return False
