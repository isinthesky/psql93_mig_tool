"""저장된 연결 프리셋 관리"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from cryptography.fernet import InvalidToken

from src.database.local_db import LocalDatabase, SavedConnection, get_db
from src.models.profile import default_profile_key_file, shared_profile_key
from src.utils.secret_store import WrappedKeyFile

logger = logging.getLogger(__name__)


class SavedConnectionManager:
    """PostgreSQL 연결 프리셋 CRUD

    프로필과 같은 암호화 키를 쓴다. 키 준비·마이그레이션은 `ensure_profile_cipher()`
    하나로 모아, 이 관리자가 먼저 열려도 키를 따로 만들거나 legacy 암호문을 남기지 않는다.
    같은 프로세스에서는 `shared_profile_key()`로 ProfileManager와 같은 키 핸들을 공유해,
    나중에 열린 이 관리자가 키를 다시 교체해 앞선 매니저와 키가 갈라지지 않는다(H-06 2차 리뷰).
    """

    def __init__(
        self,
        db: LocalDatabase | None = None,
        key_file: WrappedKeyFile | None = None,
    ):
        self.db = db if db is not None else get_db()
        self._key_file = key_file if key_file is not None else default_profile_key_file()
        # 키를 쓸 수 없어도 앱을 멈추지 않는다 — 저장 시 오류로 알린다.
        self._keys = shared_profile_key(self.db, self._key_file)

    @property
    def key_available(self) -> bool:
        return self._keys.cipher is not None

    def _decrypt(self, token: str) -> str:
        cipher = self._keys.cipher
        if not token or cipher is None:
            return ""
        try:
            return cipher.decrypt(token.encode()).decode()
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
        # 쓰기 직전에 키 파일이 이 세션의 키와 같은지 확인한다(다르면 거부).
        cipher = self._keys.for_write()

        def encrypt(value: str) -> str:
            return cipher.encrypt(value.encode()).decode()

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
                existing.password = encrypt(password)
                existing.compat_mode = compat_mode
                existing.last_used = datetime.now()
                session.flush()
                return existing

            conn = SavedConnection(
                host=host,
                port=port,
                database=database,
                username=username,
                password=encrypt(password),
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
