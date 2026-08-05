"""조회(scan) 워커 — 다이얼로그를 멈추지 않고 확인/탐색을 수행한다.

여기 있는 워커는 **읽기 전용 조회**만 한다. 실제 데이터를 옮기는 워커는
`base_migration_worker.py` 계열이고 규약이 다르다.

## 규약

1. `QThread`를 직접 상속하고 **`finished`를 재정의하지 않는다.**
   `BaseMigrationWorker`는 `finished`를 가려서 "작업 종료"라는 다른 뜻으로 쓴다.
   여기서는 `QThread.finished`가 "스레드 종료"라는 원래 뜻 그대로 남아 있어야
   버튼 복구 같은 정리를 성공·실패 상관없이 정확히 한 번 걸 수 있다.

2. 결과 시그널의 **첫 인자는 항상 generation**이다. 조회 도중 조건이 바뀌면
   호출부가 세대를 올리고, 늦게 도착한 결과는 세대가 달라 버려진다.

3. 생성자는 **원시값(dict/list/str/bool/date)만** 받는다. 위젯, `ConnectionProfile`,
   `HistoryManager`/`CheckpointManager`, 커넥션/커서를 넘기지 않는다.
   워커 스레드에서 UI나 ORM 세션을 만지면 무작위로 깨진다.

4. 취소는 두 층이다. 루프 경계는 `should_stop()`(= `isInterruptionRequested()`),
   진행 중인 쿼리는 `cancel_query()`. 후자는 다른 스레드에서 불러도 되는
   몇 안 되는 psycopg API다.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QThread, Signal

from src.database.postgres_utils import PostgresOptimizer
from src.models.profile import ENDPOINT_KIND_POSTGRES
from src.utils.validators import ConnectionValidator


class ScanWorker(QThread):
    """조회 워커 공통 베이스.

    하위 클래스는 `execute()`만 구현한다. 예외를 `failed`로 바꾸고
    취소를 존중하는 일은 여기서 처리한다.
    """

    result = Signal(int, object)  # (generation, payload)
    failed = Signal(int, str)  # (generation, message)
    progress = Signal(int, int, int)  # (generation, done, total)

    def __init__(self, generation: int):
        super().__init__()
        self.generation = generation
        self._conn: Any = None
        self._conn_lock = threading.Lock()

    # ── 취소 ────────────────────────────────────────────────
    def should_stop(self) -> bool:
        return self.isInterruptionRequested()

    def cancel_query(self) -> None:
        """진행 중인 쿼리를 서버 쪽에서 끊는다(다른 스레드에서 호출 가능)."""
        with self._conn_lock:
            conn = self._conn
        if conn is None:
            return
        try:
            conn.cancel()
        except Exception:
            # 이미 닫혔거나 취소를 지원하지 않는다. 무시해도 안전하다.
            pass

    def _track_connection(self, conn: Any) -> None:
        with self._conn_lock:
            self._conn = conn

    def _release_connection(self) -> None:
        with self._conn_lock:
            self._conn = None

    # ── 실행 ────────────────────────────────────────────────
    def execute(self) -> Any:
        raise NotImplementedError

    def run(self) -> None:
        try:
            payload = self.execute()
        except Exception as e:  # noqa: BLE001 - 어떤 예외든 UI로 전달해야 한다
            if not self.should_stop():
                self.failed.emit(self.generation, str(e))
            return
        finally:
            self._release_connection()

        if not self.should_stop():
            self.result.emit(self.generation, payload)


@dataclass(frozen=True)
class EndpointCheckSpec:
    """엔드포인트 하나를 확인하는 데 필요한 값 묶음.

    위젯이나 프로필 객체가 아니라 값만 담는다(규약 3).
    """

    kind: str
    config: dict
    must_exist: bool


@dataclass(frozen=True)
class EndpointCheckResult:
    ok: bool
    message: str


class ConnectionCheckWorker(ScanWorker):
    """소스/대상 엔드포인트를 확인한다.

    PostgreSQL이면 접속을, File Archive면 경로를 확인한다.
    payload는 `{"source": EndpointCheckResult, "target": EndpointCheckResult}`.

    취소에 관하여: `check_connection_quick`이 커넥션을 자기 안에서 만들고 닫으므로
    이 워커는 `cancel_query()`로 끊을 대상을 갖지 못한다(호출해도 아무 일도 안 한다).
    대신 그쪽의 `connect_timeout`이 대기 시간을 유한하게 묶어 준다.
    파일 경로 확인은 즉시 끝나거나 OS가 알아서 끊는다.
    """

    def __init__(self, generation: int, source: EndpointCheckSpec, target: EndpointCheckSpec):
        super().__init__(generation)
        self._source = source
        self._target = target

    def execute(self) -> dict[str, EndpointCheckResult]:
        source = self._check(self._source)
        if self.should_stop():
            return {"source": source, "target": EndpointCheckResult(False, "취소됨")}
        target = self._check(self._target)
        return {"source": source, "target": target}

    def _check(self, spec: EndpointCheckSpec) -> EndpointCheckResult:
        if spec.kind == ENDPOINT_KIND_POSTGRES:
            ok, msg = PostgresOptimizer.check_connection_quick(spec.config)
            return EndpointCheckResult(bool(ok), str(msg))

        ok, msg = ConnectionValidator.validate_file_archive_config(
            spec.config,
            must_exist=spec.must_exist,
        )
        if ok:
            msg = "경로 확인 완료" if spec.must_exist else "출력 경로 사용 가능"
        return EndpointCheckResult(bool(ok), str(msg))
