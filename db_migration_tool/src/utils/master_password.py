"""마스터 비밀번호 기반 인증 및 암호화 키 관리"""

from __future__ import annotations

import base64
import hmac
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from src.utils.app_paths import AppPaths


class AuthenticationError(RuntimeError):
    """인증이 필요한 작업에서 발생하는 예외"""


class MasterPasswordService:
    """마스터 비밀번호 설정/검증 및 세션 암호화 키 관리"""

    CONFIG_VERSION = 1
    DEFAULT_ITERATIONS = 600_000
    LEGACY_HARDCODED_KEY = b"ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="
    LEGACY_KEY_FILENAME = ".encryption_key"

    _active_cipher_suite: Fernet | None = None

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or AppPaths.get_config_path()

    def is_configured(self) -> bool:
        auth = self._load_config().get("auth")
        return isinstance(auth, dict) and auth.get("enabled") is True

    def setup_master_password(self, password: str) -> None:
        self.validate_password(password)

        if self.is_configured():
            raise RuntimeError("마스터 비밀번호가 이미 설정되어 있습니다.")

        encryption_salt = os.urandom(16)
        verifier_salt = os.urandom(16)
        verifier_bytes = self._derive_key_bytes(password, verifier_salt, self.DEFAULT_ITERATIONS)

        config = self._load_config()
        config["auth"] = {
            "enabled": True,
            "version": self.CONFIG_VERSION,
            "kdf": "pbkdf2_sha256",
            "iterations": self.DEFAULT_ITERATIONS,
            "encryption_salt": self._b64encode(encryption_salt),
            "verifier_salt": self._b64encode(verifier_salt),
            "password_verifier": self._b64encode(verifier_bytes),
            "created_at": datetime.now().isoformat(),
        }
        self._save_config(config)

    def unlock(self, password: str) -> bool:
        auth = self._get_auth_config()
        iterations = int(auth.get("iterations", self.DEFAULT_ITERATIONS))

        verifier_salt = self._b64decode(auth["verifier_salt"])
        expected_verifier = auth["password_verifier"]
        actual_verifier = self._b64encode(
            self._derive_key_bytes(password, verifier_salt, iterations)
        )

        if not hmac.compare_digest(actual_verifier, expected_verifier):
            self.__class__._active_cipher_suite = None
            return False

        encryption_salt = self._b64decode(auth["encryption_salt"])
        fernet_key = self._derive_fernet_key(password, encryption_salt, iterations)
        self.__class__._active_cipher_suite = Fernet(fernet_key)
        return True

    @classmethod
    def lock(cls) -> None:
        cls._active_cipher_suite = None

    @classmethod
    def is_authenticated(cls) -> bool:
        return cls._active_cipher_suite is not None

    @classmethod
    def get_active_cipher_suite(cls) -> Fernet:
        if cls._active_cipher_suite is None:
            raise AuthenticationError("마스터 비밀번호 인증이 필요합니다.")
        return cls._active_cipher_suite

    @classmethod
    def get_legacy_cipher_suites(cls) -> list[Fernet]:
        keys: list[bytes] = []

        legacy_key_file = AppPaths.get_app_data_dir() / cls.LEGACY_KEY_FILENAME
        if legacy_key_file.exists():
            key_data = legacy_key_file.read_bytes().strip()
            if key_data:
                keys.append(key_data)

        keys.append(cls.LEGACY_HARDCODED_KEY)

        unique_keys: list[bytes] = []
        seen = set()
        for key in keys:
            if key not in seen:
                seen.add(key)
                unique_keys.append(key)

        return [Fernet(key) for key in unique_keys]

    @classmethod
    def get_legacy_key_file_path(cls) -> Path:
        return AppPaths.get_app_data_dir() / cls.LEGACY_KEY_FILENAME

    @staticmethod
    def validate_password(password: str) -> None:
        if not password or not password.strip():
            raise ValueError("마스터 비밀번호를 입력해 주세요.")
        if len(password) < 4:
            raise ValueError("마스터 비밀번호는 4자 이상이어야 합니다.")

    def _get_auth_config(self) -> dict[str, Any]:
        config = self._load_config()
        auth = config.get("auth")
        if not isinstance(auth, dict) or auth.get("enabled") is not True:
            raise AuthenticationError("마스터 비밀번호가 설정되지 않았습니다.")
        return auth

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}

        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save_config(self, config: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _derive_key_bytes(password: str, salt: bytes, iterations: int) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=iterations,
        )
        return kdf.derive(password.encode("utf-8"))

    @classmethod
    def _derive_fernet_key(cls, password: str, salt: bytes, iterations: int) -> bytes:
        return base64.urlsafe_b64encode(cls._derive_key_bytes(password, salt, iterations))

    @staticmethod
    def _b64encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii")

    @staticmethod
    def _b64decode(data: str) -> bytes:
        return base64.urlsafe_b64decode(data.encode("ascii"))
