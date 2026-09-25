"""저장된 연결 프리셋 관리"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from src.database.local_db import LocalDatabase, SavedConnection, get_db
from src.models.profile import (
    ProfileKeyUnavailableError,
    default_profile_key_file,
    ensure_profile_cipher,
)
from src.utils.secret_store import WrappedKeyFile

logger = logging.getLogger(__name__)


class SavedConnectionManager:
    """PostgreSQL 연결 프리셋 CRUD

    프로필과 같은 암호화 키를 쓴다. 키 준비·마이그레이션은 `ensure_profile_cipher()`
    하나로 모아, 이 관리자가 먼저 열려도 키를 따로 만들거나 legacy 암호문을 남기지 않는다.
    """

    def __init__(
        self,
        db: LocalDatabase | None = None,
        key_file: WrappedKeyFile | None = None,
    ):
        self.db = db if db is not None else get_db()
        self._key_file = key_file if key_file is not None else default_profile_key_file()
        self._cipher: Fernet | None = None
        self._key_error: Exception | None = None
        try:
            self._cipher = ensure_profile_cipher(self.db, self._key_file)
        except Exception as exc:  # 앱을 멈추지 않는다 — 저장 시 오류로 알린다
            logger.error("연결 프리셋 암호화 키를 준비하지 못했습니다: %s", exc)
            self._key_error = exc

    def _encrypt(self, text: str) -> str:
        if self._cipher is None:
            raise ProfileKeyUnavailableError(
                f"암호화 키를 사용할 수 없어 연결 정보를 저장할 수 없습니다: {self._key_error}"
            ) from self._key_error
        return self._cipher.encrypt(text.encode()).decode()

    def _decrypt(self, token: str) -> str:
        if not token or self._cipher is None:
            return ""
        try:
            return self._cipher.decrypt(token.encode()).decode()
        except InvalidToken:
            return ""

    def save_connection(self, config: dict[str, Any]) -> SavedConnection:
        """연결 정보 저장 (upsert by host+port+database+username+ssl)."""
        host = config.get("host", "localhost")
        port = int(config.get("port", 5432))
        database = config.get("database", "")
        username = config.get("username", "")
        ssl = bool(config.get("ssl", False))
        compat_mode = config.get("compat_mode", "auto")
        password = config.get("password", "")

        with self.db.session_scope() as session:
            existing = (
                session.query(SavedConnection)
                .filter_by(
                    host=host,
                    port=port,
                    database=database,
                    username=username,
                    ssl=int(ssl),
                )
                .first()
            )

            if existing:
                existing.password = self._encrypt(password)
                existing.compat_mode = compat_mode
                existing.last_used = datetime.now()
                session.flush()
                return existing

            conn = SavedConnection(
                host=host,
                port=port,
                database=database,
                username=username,
                password=self._encrypt(password),
                ssl=int(ssl),
                compat_mode=compat_mode,
                last_used=datetime.now(),
            )
            session.add(conn)
            session.flush()
            return conn

    def get_all(self) -> list[dict[str, Any]]:
        """저장된 연결 목록 (최근 사용순)."""
        with self.db.session_scope() as session:
            rows = session.query(SavedConnection).order_by(SavedConnection.last_used.desc()).all()
            return [self._to_dict(row) for row in rows]

    def delete(self, connection_id: int) -> bool:
        with self.db.session_scope() as session:
            row = session.query(SavedConnection).filter_by(id=connection_id).first()
            if row:
                session.delete(row)
                return True
            return False

    def _to_dict(self, row: SavedConnection) -> dict[str, Any]:
        return {
            "id": row.id,
            "host": row.host,
            "port": row.port,
            "database": row.database,
            "username": row.username,
            "password": self._decrypt(row.password or ""),
            "ssl": bool(row.ssl),
            "compat_mode": row.compat_mode or "auto",
            "last_used": row.last_used,
            "label": f"{row.username}@{row.host}:{row.port}/{row.database}",
        }
