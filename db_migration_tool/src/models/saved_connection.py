"""저장된 연결 프리셋 관리"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from cryptography.fernet import Fernet

from src.database.local_db import SavedConnection, get_db
from src.utils.master_password import MasterPasswordService


class SavedConnectionManager:
    """PostgreSQL 연결 프리셋 CRUD"""

    def __init__(self):
        self.db = get_db()
        self._cipher = self._get_cipher()

    @staticmethod
    def _get_cipher() -> Fernet:
        auth_service = MasterPasswordService()
        if auth_service.is_configured() and MasterPasswordService.is_authenticated():
            return MasterPasswordService.get_active_cipher_suite()

        legacy_ciphers = MasterPasswordService.get_legacy_cipher_suites()
        if legacy_ciphers:
            return legacy_ciphers[0]

        return Fernet.generate_key()

    def _encrypt(self, text: str) -> str:
        return self._cipher.encrypt(text.encode()).decode()

    def _decrypt(self, token: str) -> str:
        if not token:
            return ""
        try:
            return self._cipher.decrypt(token.encode()).decode()
        except Exception:
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
            rows = (
                session.query(SavedConnection)
                .order_by(SavedConnection.last_used.desc())
                .all()
            )
            return [self._to_dict(row) for row in rows]

    def delete(self, connection_id: int) -> bool:
        with self.db.session_scope() as session:
            row = session.query(SavedConnection).filter_by(id=connection_id).first()
            if row:
                session.delete(row)
                return True
            return False

    def reencrypt_all_saved_connections(self, target_cipher: Fernet) -> int:
        """저장된 연결 프리셋을 현재 활성 암호화 키로 재암호화합니다."""
        migrated_count = 0

        with self.db.session_scope() as session:
            rows = session.query(SavedConnection).all()

            for row in rows:
                decrypted_password = None

                for source_cipher in MasterPasswordService.get_legacy_cipher_suites():
                    try:
                        decrypted_password = source_cipher.decrypt(row.password.encode()).decode()
                        break
                    except Exception:
                        continue

                if decrypted_password is None:
                    continue

                row.password = target_cipher.encrypt(decrypted_password.encode()).decode()
                migrated_count += 1

        return migrated_count

    def _to_dict(self, row: SavedConnection) -> dict[str, Any]:
        return {
            "id": row.id,
            "host": row.host,
            "port": row.port,
            "database": row.database,
            "username": row.username,
            "password": self._decrypt(row.password),
            "ssl": bool(row.ssl),
            "compat_mode": row.compat_mode or "auto",
            "last_used": row.last_used,
            "label": f"{row.username}@{row.host}:{row.port}/{row.database}",
        }
