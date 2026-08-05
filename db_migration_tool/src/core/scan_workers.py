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
from datetime import date
from typing import Any

import psycopg
from psycopg import sql
from PySide6.QtCore import QThread, Signal

from src.core.archive_manifest import ArchiveManifestStore, ScanCancelled
from src.core.partition_discovery import PartitionDiscovery
from src.database.postgres_utils import PostgresOptimizer
from src.models.profile import ENDPOINT_KIND_POSTGRES
from src.utils.validators import ConnectionValidator

# 커넥션 수립 대기 상한. 이게 없으면 방화벽이 패킷을 버릴 때
# OS 타임아웃(수십 초)까지 취소 플래그조차 읽지 못한다.
CONNECT_TIMEOUT_SECONDS = 10


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
        except ScanCancelled:
            # 취소는 실패가 아니다. 아무 신호도 보내지 않는다.
            # (정리는 QThread.finished가 맡는다)
            return
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


class TargetCompletedScanWorker(ScanWorker):
    """대상에 이미 들어 있는 파티션을 찾는다.

    payload는 `{파티션명: 이미_있음}`.

    이 결과가 틀리면 데이터가 상한다. 값을 못 믿을 상황에서는 결과를 내지 말고
    실패로 알려야 한다 — 호출부가 '전부 미완료'로 읽으면 이미 옮긴 파티션까지
    TRUNCATE 후 다시 적재한다.
    """

    def __init__(
        self,
        generation: int,
        target_kind: str,
        target_config: dict,
        partition_names: list[str],
    ):
        super().__init__(generation)
        self._target_kind = target_kind
        self._target_config = dict(target_config)
        self._names = list(partition_names)

    def execute(self) -> dict[str, bool]:
        if self._target_kind == ENDPOINT_KIND_POSTGRES:
            return self._check_postgres()
        return self._check_archive()

    # ── File Archive ────────────────────────────────────────
    def _check_archive(self) -> dict[str, bool]:
        store = ArchiveManifestStore(self._target_config["archive_path"])
        try:
            return store.get_completed_status(
                self._names,
                should_stop=self.should_stop,
                on_progress=self._emit_progress,
            )
        except FileNotFoundError:
            # manifest가 아직 없다 = 아무것도 완료되지 않았다. 정상 상태다.
            return dict.fromkeys(self._names, False)

    # ── PostgreSQL ──────────────────────────────────────────
    def _check_postgres(self) -> dict[str, bool]:
        # 기본값은 ConnectionProfile이 정규화로 넣어 주지만, 워커는 임의의 dict를
        # 받을 수 있으므로 여기서도 한 겹 더 둔다(키 누락 시 None이 넘어가면
        # libpq 기본값으로 엉뚱한 DB에 붙을 수 있다).
        conn_params: dict[str, Any] = {
            "host": self._target_config.get("host", "localhost"),
            "port": self._target_config.get("port", 5432),
            "dbname": self._target_config.get("database", ""),
            "user": self._target_config.get("username", ""),
            "password": self._target_config.get("password", ""),
            # 없으면 방화벽이 패킷을 버릴 때 OS 타임아웃까지 취소도 안 먹는다.
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        }
        if self._target_config.get("ssl"):
            conn_params["sslmode"] = "require"

        results = dict.fromkeys(self._names, False)
        conn = psycopg.connect(**conn_params)
        # 취소는 다른 스레드에서 conn.cancel()로 들어온다.
        self._track_connection(conn)
        try:
            # 루프 전체가 한 트랜잭션으로 묶이면 대상 DB에 idle-in-transaction이
            # 남는다. 읽기만 하므로 자동 커밋으로 둔다.
            conn.autocommit = True
            total = len(self._names)
            with conn.cursor() as cur:
                for index, name in enumerate(self._names, start=1):
                    if self.should_stop():
                        raise ScanCancelled("대상 확인이 취소되었습니다")

                    cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_schema = 'public' AND table_name = %s
                        )
                        """,
                        (name,),
                    )
                    row = cur.fetchone()
                    if not row or not row[0]:
                        self._emit_progress(index, total)
                        continue

                    try:
                        # '데이터가 있는가'만 알면 되므로 COUNT(*)로 전수를 세지 않는다.
                        # 대용량 파티션에서 확인이 수 분씩 걸리던 원인.
                        cur.execute(
                            sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(name))
                        )
                        results[name] = cur.fetchone() is not None
                    except Exception:
                        # 권한 부족 등은 '없음'으로 본다(다시 적재하는 쪽이 안전).
                        results[name] = False

                    self._emit_progress(index, total)
        finally:
            self._release_connection()
            try:
                conn.close()
            except Exception:
                pass
        return results

    def _emit_progress(self, done: int, total: int) -> None:
        self.progress.emit(self.generation, done, total)


class PartitionScanWorker(ScanWorker):
    """소스에서 날짜 범위에 해당하는 파티션을 찾는다.

    payload는 `list[dict]` — `PartitionDiscovery`/`ArchiveManifestStore`가 주는
    원본 항목 그대로다. UI 표현으로 바꾸는 일은 다이얼로그가 한다.

    PostgreSQL 소스는 파티션마다 `COUNT(*)` 전수 스캔이 돌기 때문에 상한이 없다.
    취소 훅이 이 워커에서 가장 중요한 이유다.
    """

    def __init__(
        self,
        generation: int,
        source_kind: str,
        source_config: dict,
        start_date: date,
        end_date: date,
        table_types: list,
    ):
        super().__init__(generation)
        self._source_kind = source_kind
        self._source_config = dict(source_config)
        self._start_date = start_date
        self._end_date = end_date
        self._table_types = list(table_types)

    def execute(self) -> list:
        if self._source_kind == ENDPOINT_KIND_POSTGRES:
            return self._discover_postgres()
        return self._discover_archive()

    def _discover_postgres(self) -> list:
        discovery = PartitionDiscovery(self._source_config)
        return discovery.discover_partitions(
            self._start_date,
            self._end_date,
            self._table_types,
            should_stop=self.should_stop,
            on_progress=self._emit_progress,
            # COUNT(*) 전수 스캔은 플래그로 못 멈춘다. 커넥션을 받아 둬야
            # cancel_query()가 실제로 쿼리를 끊을 수 있다.
            on_connection=self._track_connection,
        )

    def _discover_archive(self) -> list:
        store = ArchiveManifestStore(self._source_config["archive_path"])
        partitions = store.filter_partitions(
            start_date=self._start_date,
            end_date=self._end_date,
            table_types=self._table_types,
        )
        if self.should_stop():
            raise ScanCancelled("파티션 탐색이 취소되었습니다")
        return list(partitions)

    def _emit_progress(self, done: int, total: int) -> None:
        self.progress.emit(self.generation, done, total)
