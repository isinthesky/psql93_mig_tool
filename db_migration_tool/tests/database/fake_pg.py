"""PostgreSQL 트랜잭션 의미를 흉내 내는 가짜 연결(테스트 전용).

MagicMock은 실패한 문장 뒤에도 다음 문장을 받아 준다. 실제 PostgreSQL은 그렇지 않다:

- 트랜잭션 안에서 한 문장이 실패하면 트랜잭션이 **중단(aborted)** 되고, 이후 모든 문장이
  `25P02 in_failed_sql_transaction`으로 거부된다. `ROLLBACK` 또는 `ROLLBACK TO SAVEPOINT`만 받는다.
- 중단된 트랜잭션에서 `COMMIT`은 **오류 없이 ROLLBACK으로 끝난다**(드라이버가 예외를 던지지 않는다).
  그래서 "커밋했다"고 믿는 사이 DDL이 조용히 사라질 수 있다.
- `SAVEPOINT`는 트랜잭션 블록 안에서만 쓸 수 있다(autocommit이면 `25P01`).

이 가짜는 위 규칙을 그대로 지켜 psycopg(3)·psycopg2 양쪽 예외 계층으로 재현한다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import psycopg
import psycopg2
import psycopg2.errors

IDLE = 0
INTRANS = 2
INERROR = 3

_SAVEPOINT_RE = re.compile(r"^SAVEPOINT\s+(\S+)$", re.IGNORECASE)
_ROLLBACK_TO_RE = re.compile(r"^ROLLBACK\s+TO\s+(?:SAVEPOINT\s+)?(\S+)$", re.IGNORECASE)
_RELEASE_RE = re.compile(r"^RELEASE\s+(?:SAVEPOINT\s+)?(\S+)$", re.IGNORECASE)

Responder = Callable[[str, Any], list[tuple] | None]


def normalize(sql_text: Any) -> str:
    return " ".join(str(sql_text).split()).rstrip(";").strip()


class _Info:
    def __init__(self, conn: FakePgConnection):
        self._conn = conn

    @property
    def transaction_status(self) -> int:
        return self._conn.status


class FakeCursor:
    def __init__(self, conn: FakePgConnection):
        self.connection = conn
        self._rows: list[tuple] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, query: Any, params: Any = None) -> None:
        self._rows = self.connection._execute(normalize(query), params)

    def fetchone(self) -> tuple | None:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[tuple]:
        rows, self._rows = self._rows, []
        return rows


class FakePgConnection:
    """문장을 기록하고 PostgreSQL 트랜잭션·SAVEPOINT·중단 규칙을 흉내 낸다.

    Args:
        responder: (정규화된 SQL, params) → 행 목록. DB 예외를 던지면 그 문장이 실패한다.
            `conn.error("DuplicateTable")`로 드라이버에 맞는 예외를 만들 수 있다.
        flavor: "psycopg"(3) 또는 "psycopg2" — 예외 계층을 고른다.
    """

    def __init__(
        self,
        responder: Responder | None = None,
        *,
        flavor: str = "psycopg",
        autocommit: bool = False,
    ):
        assert flavor in ("psycopg", "psycopg2")
        self.responder: Responder = responder or (lambda _sql, _params: [])
        self.flavor = flavor
        self.autocommit = autocommit
        self.status = IDLE
        self.info = _Info(self)
        self.closed = False
        # True면 ROLLBACK TO SAVEPOINT도 실패한다(연결이 끊긴 상황 재현).
        self.fail_rollback_to = False

        self.statements: list[str] = []  # 실행 시도한 모든 문장
        self.committed: list[str] = []  # 실제로 커밋되어 남은 문장
        self._pending: list[str] = []
        self._savepoints: list[tuple[str, int]] = []
        self.commits = 0
        self.rollbacks = 0
        self.silent_rollbacks = 0  # 중단된 트랜잭션에 COMMIT을 보내 조용히 사라진 횟수

    # ── 예외 헬퍼 ───────────────────────────────────────────
    def error(self, name: str, message: str = "injected") -> Exception:
        module = psycopg.errors if self.flavor == "psycopg" else psycopg2.errors
        exc: Exception = getattr(module, name)(message)
        return exc

    def _is_db_error(self, exc: BaseException) -> bool:
        return isinstance(exc, (psycopg.Error, psycopg2.Error))

    # ── DB-API ─────────────────────────────────────────────
    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1
        if self.status == INERROR:
            # 실제 서버 동작: 오류 없이 ROLLBACK. 드라이버도 예외를 던지지 않는다.
            self.silent_rollbacks += 1
        else:
            self.committed.extend(self._pending)
        self._reset()

    def rollback(self) -> None:
        self.rollbacks += 1
        self._reset()

    def close(self) -> None:
        self.closed = True

    def cancel(self) -> None:
        pass

    def _reset(self) -> None:
        self._pending = []
        self._savepoints = []
        self.status = IDLE

    # ── 실행 ───────────────────────────────────────────────
    def _execute(self, sql_text: str, params: Any) -> list[tuple]:
        self.statements.append(sql_text)

        if m := _ROLLBACK_TO_RE.match(sql_text):
            if self.fail_rollback_to:
                raise self.error("OperationalError", "server closed the connection unexpectedly")
            name = m.group(1)
            for index in range(len(self._savepoints) - 1, -1, -1):
                if self._savepoints[index][0] == name:
                    mark = self._savepoints[index][1]
                    del self._savepoints[index + 1 :]
                    del self._pending[mark:]
                    self.status = INTRANS
                    return []
            raise self.error("InvalidSavepointSpecification", f"savepoint {name} does not exist")

        if self.status == INERROR:
            raise self.error(
                "InFailedSqlTransaction",
                "current transaction is aborted, commands ignored until end of transaction block",
            )

        if m := _SAVEPOINT_RE.match(sql_text):
            if self.autocommit:
                raise self.error(
                    "NoActiveSqlTransaction", "SAVEPOINT can only be used in transaction blocks"
                )
            self.status = INTRANS
            self._savepoints.append((m.group(1), len(self._pending)))
            return []

        if m := _RELEASE_RE.match(sql_text):
            name = m.group(1)
            for index in range(len(self._savepoints) - 1, -1, -1):
                if self._savepoints[index][0] == name:
                    del self._savepoints[index:]
                    return []
            raise self.error("InvalidSavepointSpecification", f"savepoint {name} does not exist")

        if not self.autocommit and self.status == IDLE:
            self.status = INTRANS

        try:
            rows = self.responder(sql_text, params)
        except BaseException as exc:
            if self._is_db_error(exc) and not self.autocommit:
                self.status = INERROR
            raise

        if self.autocommit:
            self.committed.append(sql_text)
        else:
            self._pending.append(sql_text)
        return list(rows or [])
