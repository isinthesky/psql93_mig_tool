"""
연결 프로필 모델 및 관리자
"""

import json
from datetime import datetime
from typing import Any, Optional

from cryptography.fernet import Fernet

from src.database.local_db import Profile, get_db


class ConnectionProfile:
    """연결 프로필 데이터 클래스"""

    def __init__(
        self,
        id: Optional[int] = None,
        name: str = "",
        source_config: dict[str, Any] = None,
        target_config: dict[str, Any] = None,
        created_at: Optional[datetime] = None,
        updated_at: Optional[datetime] = None,
    ):
        self.id = id
        self.name = name
        self.source_config = source_config or {}
        self.target_config = target_config or {}
        self.created_at = created_at
        self.updated_at = updated_at

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
        """DB 모델에서 생성"""
        # 암호화된 설정 복호화
        source_config = json.loads(cipher_suite.decrypt(db_profile.source_config.encode()).decode())
        target_config = json.loads(cipher_suite.decrypt(db_profile.target_config.encode()).decode())

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

    def _get_or_create_cipher(self) -> Fernet:
        """암호화 키 가져오기 또는 생성"""
        # 실제 구현에서는 안전한 키 저장소 사용 권장
        # 여기서는 간단히 고정 키 사용
        key = b"ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="
        return Fernet(key)

    def _encrypt_config(self, config: dict[str, Any]) -> str:
        """설정 암호화"""
        json_str = json.dumps(config)
        encrypted = self._cipher_suite.encrypt(json_str.encode())
        return encrypted.decode()

    def create_profile(self, profile_data: dict[str, Any]) -> ConnectionProfile:
        """새 프로필 생성"""
        with self.db.session_scope() as session:
            # DB 모델 생성
            db_profile = Profile(
                name=profile_data["name"],
                source_config=self._encrypt_config(profile_data["source_config"]),
                target_config=self._encrypt_config(profile_data["target_config"]),
            )

            session.add(db_profile)
            session.flush()  # ID 생성을 위해 flush

            # 생성된 프로필 반환
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

            # 업데이트
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
