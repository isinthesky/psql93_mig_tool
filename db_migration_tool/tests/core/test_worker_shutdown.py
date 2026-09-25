"""애플리케이션 종료 시 워커 정리(감사 M-13).

결함: 트레이 '종료'가 실행 중인 워커를 stop/cancel/wait 없이 ``app.quit()``으로 끝냈다.
실행 중인 QThread가 파괴되거나(프로세스 abort), 진행 중 배치·manifest·로그가 정리되지 않았다.

여기서 고정하는 규약
- ``WorkerRegistry``: migration/scan/archive 워커는 시작할 때 등록되고 끝나면 해제된다.
- ``ShutdownCoordinator``: 전체 stop() → 실행 중 쿼리 cancel(H-02) → 제한 시간 대기 →
  flush(logger, 로컬 DB) → quit. 제한 시간을 넘기면 경고를 남기고 강제 종료(forced)로 끝낸다.
- 종료 뒤 임시 파일·manifest 잠금·스레드(생산자, cancel 반복, 워커)가 남지 않는다.

필수 테스트(감사 §5 단계 4): 각 worker 단계의 종료, timeout 뒤 강제 종료, 임시 파일·lock·thread 누수.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg2
import psycopg2.errors
import psycopg2.sql
import pytest

from src.core import worker_registry as registry_module
from src.core.archive_manifest import ScanCancelled
from src.core.base_migration_worker import BaseMigrationWorker
from src.core.file_archive_workers import (
    FileToPostgresArchiveWorker,
    PostgresToFileArchiveWorker,
)
from src.core.scan_workers import ScanWorker
from src.core.worker_registry import ShutdownCoordinator, WorkerRegistry
from src.models.profile import ConnectionProfile
from tests.core.test_copy_cancel_and_snapshot import (
    FakeSource,
    FakeTarget,
    _checkpoint_calls,
    _fake_quote_ident,
    _make_worker,
    _rows,
)

# 종료가 '제한 시간 안에' 끝났다고 볼 기준(초). 가짜 연결이 취소 없이 스스로 풀리는
# 시간(_BLOCK_SECONDS)보다 충분히 짧아야 한다 — 취소가 안 먹으면 이 기준으로 실패한다.
_BOUND_SECONDS = 3.0
_BLOCK_SECONDS = 8.0


@pytest.fixture(autouse=True)
def _quote_without_server(monkeypatch):
    monkeypatch.setattr(psycopg2.sql.ext, "quote_ident", _fake_quote_ident)


@pytest.fixture
def registry(monkeypatch):
    """전역 레지스트리를 테스트마다 새것으로 바꾼다(다른 테스트의 워커와 섞이지 않게)."""
    fresh = WorkerRegistry()
    monkeypatch.setattr(registry_module, "worker_registry", fresh)
    return fresh


@pytest.fixture
def estimate():
    with patch(
        "src.core.copy_migration_worker.PostgresOptimizer.estimate_table_size",
        return_value={"row_count": 20, "total_size_mb": 1.0, "exists": True},
    ) as m:
        yield m


def _coordinator(registry: WorkerRegistry, **kwargs: Any) -> ShutdownCoordinator:
    kwargs.setdefault("quit_app", MagicMock())
    kwargs.setdefault("timeout_seconds", 5.0)
    kwargs.setdefault("log", lambda level, message: None)
    return ShutdownCoordinator(registry, **kwargs)


_AUX_THREAD_PREFIXES = ("copy-producer", "db-cancel", "shutdown-cancel")
# 이 테스트가 시작되기 전부터 있던 보조 스레드(다른 테스트가 남긴 것)는 누수 판정에서 뺀다.
_baseline_threads: set[int] = set()


@pytest.fixture(autouse=True)
def _thread_baseline():
    _baseline_threads.clear()
    _baseline_threads.update(t.ident for t in threading.enumerate() if t.ident is not None)
    yield


def _leftover_threads() -> list[str]:
    """이 테스트에서 생겨 종료 뒤에도 남은 보조 스레드."""
    return [
        t.name
        for t in threading.enumerate()
        if t.name.startswith(_AUX_THREAD_PREFIXES)
        and t.is_alive()
        and t.ident not in _baseline_threads
    ]


def _wait_threads_gone(timeout: float = 2.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        left = _leftover_threads()
        if not left:
            return []
        time.sleep(0.02)
    return _leftover_threads()


def _profile() -> ConnectionProfile:
    return ConnectionProfile(
        id=1, name="p", source_config={"host": "s"}, target_config={"host": "t"}
    )


# ---------------------------------------------------------------------------
# 가짜 워커
# ---------------------------------------------------------------------------


class _CancelableConn:
    """cancel()이 오면 막힌 '쿼리'를 푼다."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.cancel_calls = 0

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.cancelled.set()


class _BlockingScan(ScanWorker):
    """쿼리에서 막혀 cancel_query()가 와야 풀리는 조회 워커."""

    def __init__(self) -> None:
        super().__init__(generation=1)
        self.conn = _CancelableConn()
        self.entered = threading.Event()
        self.outcome: str | None = None

    def execute(self) -> Any:
        self._track_connection(self.conn)
        self.entered.set()
        if self.conn.cancelled.wait(_BLOCK_SECONDS):
            self.outcome = "cancelled"
            raise ScanCancelled("조회 취소")
        self.outcome = "timeout"
        return None


class _QuickMigration(BaseMigrationWorker):
    def _execute_migration(self) -> None:
        return None


class _StuckMigration(BaseMigrationWorker):
    """stop()을 무시하고 release가 올 때까지 끝나지 않는 워커(강제 종료 경로)."""

    def __init__(self) -> None:
        with (
            patch("src.core.base_migration_worker.HistoryManager"),
            patch("src.core.base_migration_worker.CheckpointManager"),
        ):
            super().__init__(_profile(), ["p"], 1)
        self.entered = threading.Event()
        self.release = threading.Event()
        self._log = lambda message, level="INFO": None  # type: ignore[method-assign]

    def _execute_migration(self) -> None:
        self.entered.set()
        self.release.wait(30)


def _quick_migration() -> _QuickMigration:
    with (
        patch("src.core.base_migration_worker.HistoryManager"),
        patch("src.core.base_migration_worker.CheckpointManager"),
    ):
        worker = _QuickMigration(_profile(), ["p"], 1)
    worker._log = lambda message, level="INFO": None  # type: ignore[method-assign]
    return worker


# ---------------------------------------------------------------------------
# 1. 레지스트리: 시작 시 등록, 끝나면 해제
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_migration_worker_registers_on_start_and_unregisters_on_finish(self, registry, qtbot):
        worker = _StuckMigration()
        worker.start()
        assert worker.entered.wait(5)
        assert worker in registry.active_workers(), "실행 중인 migration 워커가 등록되지 않았습니다"

        worker.release.set()
        assert worker.wait(5000)
        assert registry.active_workers() == [], "끝난 워커가 레지스트리에 남았습니다"

    def test_scan_worker_registers_on_start_and_unregisters_on_finish(self, registry, qtbot):
        worker = _BlockingScan()
        worker.start()
        assert worker.entered.wait(5)
        assert worker in registry.active_workers(), "실행 중인 조회 워커가 등록되지 않았습니다"

        worker.cancel_query()
        assert worker.wait(5000)
        assert registry.active_workers() == []

    @pytest.mark.parametrize(
        "worker_cls,mode",
        [
            (PostgresToFileArchiveWorker, "postgres_to_file"),
            (FileToPostgresArchiveWorker, "file_to_postgres"),
        ],
    )
    def test_archive_workers_register_through_base(self, registry, tmp_path, worker_cls, mode):
        worker = _archive_worker(worker_cls, tmp_path, mode)
        entered = threading.Event()
        release = threading.Event()

        def body() -> None:
            entered.set()
            release.wait(10)

        worker._execute_migration = body  # type: ignore[method-assign]
        worker.start()
        try:
            assert entered.wait(5)
            assert worker in registry.active_workers()
        finally:
            release.set()
            assert worker.wait(5000)
        assert registry.active_workers() == []

    def test_registry_does_not_keep_finished_workers_alive(self, registry):
        """레지스트리는 수명을 늘리지 않는다(약한 참조) — 끝난 워커 객체가 쌓이지 않는다."""
        import gc
        import weakref

        worker = _quick_migration()
        worker.start()
        assert worker.wait(5000)
        ref = weakref.ref(worker)
        del worker
        gc.collect()
        assert ref() is None, "레지스트리가 끝난 워커를 붙잡고 있습니다"
        assert registry.active_workers() == []

    def test_worker_started_during_shutdown_is_stopped_immediately(self, registry, qtbot):
        """종료 도중 새로 시작한 워커도 곧바로 멈춘다(창이 늦게 띄운 작업이 종료를 붙잡지 않게)."""
        coordinator = _coordinator(registry)
        coordinator.request_shutdown()

        worker = _StuckMigration()

        def body() -> None:
            worker.entered.set()
            while worker.is_running:
                time.sleep(0.01)

        worker._execute_migration = body  # type: ignore[method-assign]
        worker.start()
        assert worker.wait(3000), "종료 중에 시작한 워커가 멈추지 않았습니다"
        assert worker.stop_reason == registry_module.SHUTDOWN_REASON


# ---------------------------------------------------------------------------
# 2. 조정자: 순서, 제한 시간, 강제 종료
# ---------------------------------------------------------------------------


class TestCoordinator:
    def test_no_workers_flushes_then_quits_once(self, registry, qtbot):
        order: list[str] = []
        quit_app = MagicMock(side_effect=lambda forced: order.append(f"quit:{forced}"))
        coordinator = _coordinator(
            registry,
            quit_app=quit_app,
            flush_steps=[
                ("logger", lambda: order.append("logger")),
                ("local_db", lambda: order.append("local_db")),
            ],
        )

        coordinator.request_shutdown()
        coordinator.request_shutdown()  # 두 번 눌러도 한 번만 종료한다

        assert order == ["logger", "local_db", "quit:False"]
        assert coordinator.is_finished and not coordinator.forced

    def test_stops_scan_and_waits_without_blocking_event_loop(self, registry, qtbot):
        """request_shutdown()은 UI를 막지 않고, 워커가 멈춘 뒤에 flush·quit한다."""
        worker = _BlockingScan()
        worker.start()
        assert worker.entered.wait(5)

        order: list[str] = []
        quit_app = MagicMock(side_effect=lambda forced: order.append("quit"))
        coordinator = _coordinator(
            registry,
            quit_app=quit_app,
            flush_steps=[("logger", lambda: order.append("flush"))],
        )

        t0 = time.monotonic()
        coordinator.request_shutdown()
        assert time.monotonic() - t0 < 0.5, "request_shutdown()이 UI 스레드를 붙잡았습니다"

        # 워커가 아주 빨리 멈추면 request_shutdown() 안에서 끝날 수도 있다 — 시그널 대신 상태를 기다린다.
        qtbot.waitUntil(lambda: coordinator.is_finished, timeout=int(_BOUND_SECONDS * 1000))
        assert worker.outcome == "cancelled", "실행 중 쿼리를 cancel하지 않았습니다"
        assert worker.conn.cancel_calls >= 1
        assert worker.isFinished()
        assert order == ["flush", "quit"], "워커가 멈추기 전에 flush/quit했습니다"
        assert not coordinator.forced
        assert _wait_threads_gone() == []

    def test_timeout_forces_shutdown_with_warning(self, registry, qtbot):
        worker = _StuckMigration()
        worker.start()
        assert worker.entered.wait(5)
        logs: list[tuple[str, str]] = []
        flushed: list[str] = []
        quit_app = MagicMock()
        coordinator = _coordinator(
            registry,
            quit_app=quit_app,
            timeout_seconds=0.5,
            log=lambda level, message: logs.append((level, message)),
            flush_steps=[("logger", lambda: flushed.append("logger"))],
        )
        try:
            t0 = time.monotonic()
            with qtbot.waitSignal(coordinator.shutdown_finished, timeout=5000) as blocker:
                coordinator.request_shutdown()
            elapsed = time.monotonic() - t0

            assert blocker.args == [True]
            assert coordinator.forced
            assert 0.4 <= elapsed < _BOUND_SECONDS, f"제한 시간 처리가 {elapsed:.1f}초 걸렸습니다"
            assert worker.stop_reason == registry_module.SHUTDOWN_REASON, (
                "stop()을 먼저 보내지 않았습니다"
            )
            warnings = [m for level, m in logs if level == "WARNING"]
            assert any("_StuckMigration" in m for m in warnings), (
                f"강제 종료 경고가 없습니다: {logs}"
            )
            assert flushed == ["logger"], "강제 종료에서도 flush는 해야 합니다"
            quit_app.assert_called_once_with(True)
            # 강제 종료는 아직 도는 스레드 객체를 붙들고 있어야 한다(파괴되면 프로세스가 abort).
            assert worker in coordinator.stuck_workers
        finally:
            worker.release.set()
            worker.wait(5000)

    def test_flush_failure_does_not_skip_remaining_steps_or_quit(self, registry, qtbot):
        done: list[str] = []
        logs: list[tuple[str, str]] = []

        def broken() -> None:
            raise OSError("disk full")

        quit_app = MagicMock()
        coordinator = _coordinator(
            registry,
            quit_app=quit_app,
            log=lambda level, message: logs.append((level, message)),
            flush_steps=[("logger", broken), ("local_db", lambda: done.append("local_db"))],
        )
        coordinator.request_shutdown()

        assert done == ["local_db"]
        quit_app.assert_called_once_with(False)
        assert any(level == "ERROR" and "logger" in m for level, m in logs)

    def test_blocking_shutdown_for_quit_paths_outside_tray(self, registry, qtbot):
        """트레이를 거치지 않은 종료(창 닫기·OS 종료)는 이벤트 루프 없이 막고 기다린다."""
        worker = _BlockingScan()
        worker.start()
        assert worker.entered.wait(5)
        quit_app = MagicMock()
        coordinator = _coordinator(registry, quit_app=quit_app)

        t0 = time.monotonic()
        forced = coordinator.shutdown_blocking()
        elapsed = time.monotonic() - t0

        assert forced is False
        assert elapsed < _BOUND_SECONDS
        assert worker.isFinished()
        quit_app.assert_not_called()  # 이미 끝나는 중이다 — 다시 quit하지 않는다
        # 끝난 뒤 다시 불러도 같은 결과(멱등)
        assert coordinator.shutdown_blocking() is False

    def test_blocking_shutdown_times_out(self, registry, qtbot):
        worker = _StuckMigration()
        worker.start()
        assert worker.entered.wait(5)
        coordinator = _coordinator(registry, timeout_seconds=0.3)
        try:
            t0 = time.monotonic()
            assert coordinator.shutdown_blocking() is True
            assert time.monotonic() - t0 < _BOUND_SECONDS
        finally:
            worker.release.set()
            worker.wait(5000)


# ---------------------------------------------------------------------------
# 3. COPY 워커의 각 단계에서 종료
# ---------------------------------------------------------------------------


def _start_copy(source: FakeSource, target: FakeTarget, **kwargs: Any):
    worker = _make_worker(source, target, **kwargs)
    worker.start()
    return worker


def _assert_clean_copy_shutdown(worker, source, target, coordinator, elapsed) -> None:
    assert not coordinator.forced, "정상 중지가 강제 종료로 끝났습니다"
    assert elapsed < _BOUND_SECONDS, f"종료가 {elapsed:.1f}초 걸렸습니다"
    assert worker.isFinished(), "워커 스레드가 남았습니다"
    assert source.closed and target.closed, "DB 연결이 닫히지 않았습니다"
    statuses = [s for s, _ in _checkpoint_calls(worker)]
    assert "failed" not in statuses, "종료를 오류로 기록했습니다"
    assert "completed" not in statuses
    assert target.pending == [], "커밋되지 않은 배치가 남았습니다"
    assert _wait_threads_gone() == [], f"보조 스레드가 남았습니다: {_leftover_threads()}"


class TestCopyWorkerStages:
    def _shutdown(self, registry) -> tuple[ShutdownCoordinator, float]:
        coordinator = _coordinator(registry)
        t0 = time.monotonic()
        coordinator.shutdown_blocking()
        return coordinator, time.monotonic() - t0

    def test_during_python_copy_batch(self, registry, estimate, qtbot):
        source, target = FakeSource(_rows(20)), FakeTarget()
        source.block_copy_no = 3
        worker = _start_copy(source, target)
        assert source.copy_blocked.wait(5)

        coordinator, elapsed = self._shutdown(registry)

        _assert_clean_copy_shutdown(worker, source, target, coordinator, elapsed)
        assert source.cancel_calls >= 1
        assert len(target.committed) == 8, "커밋된 배치까지만 남아야 합니다"
        running = [kw for s, kw in _checkpoint_calls(worker) if s == "running"]
        assert running[-1]["rows_processed"] == 8
        assert worker.stop_reason == registry_module.SHUTDOWN_REASON

    def test_during_blocked_commit(self, registry, estimate, qtbot):
        source, target = FakeSource(_rows(20)), FakeTarget()
        target.block_commit_no = 2
        worker = _start_copy(source, target)
        assert target.commit_blocked.wait(5)

        coordinator, elapsed = self._shutdown(registry)

        _assert_clean_copy_shutdown(worker, source, target, coordinator, elapsed)
        assert target.cancel_calls >= 1
        assert len(target.committed) == 4

    def test_during_server_copy(self, registry, estimate, qtbot):
        source, target = FakeSource(_rows(20)), FakeTarget()
        source.block_copy_no = 1
        worker = _start_copy(source, target, copy_mode="server")
        assert source.copy_blocked.wait(5)

        coordinator, elapsed = self._shutdown(registry)

        _assert_clean_copy_shutdown(worker, source, target, coordinator, elapsed)
        assert target.committed == [] and target.commits == 0

    def test_while_connecting(self, registry, estimate, qtbot):
        source, target = FakeSource(_rows(4)), FakeTarget()
        worker = _make_worker(source, target)
        connecting = threading.Event()
        conns = iter([source, target])

        def connect(_cfg):
            conn = next(conns)
            if conn is target:
                connecting.set()
                time.sleep(0.3)  # connect_timeout에만 묶이는 구간
            return conn

        worker._create_psycopg2_connection = MagicMock(side_effect=connect)
        worker.start()
        assert connecting.wait(5)

        coordinator, elapsed = self._shutdown(registry)

        _assert_clean_copy_shutdown(worker, source, target, coordinator, elapsed)
        assert source.copy_calls == 0, "종료 요청 뒤 작업을 시작했습니다"

    def test_while_paused(self, registry, estimate, qtbot):
        source, target = FakeSource(_rows(20)), FakeTarget()
        worker = _make_worker(source, target)
        paused = threading.Event()
        original = worker._check_pause

        def check_pause() -> None:
            if worker.is_paused:
                paused.set()
            original()

        worker._check_pause = check_pause  # type: ignore[method-assign]
        worker.is_paused = True
        worker.start()
        assert paused.wait(5)

        coordinator, elapsed = self._shutdown(registry)

        _assert_clean_copy_shutdown(worker, source, target, coordinator, elapsed)
        assert source.copy_calls == 0


# ---------------------------------------------------------------------------
# 4. 아카이브 워커: 취소 훅(H-02 잔여)과 임시 파일·잠금 누수
# ---------------------------------------------------------------------------

_PARENT_COLUMNS = [
    {
        "name": "path_id",
        "data_type": "integer",
        "character_maximum_length": None,
        "is_nullable": "NO",
        "column_default": None,
    },
    {
        "name": "issued_date",
        "data_type": "bigint",
        "character_maximum_length": None,
        "is_nullable": "NO",
        "column_default": None,
    },
]


def _archive_worker(worker_cls, tmp_path, mode):
    archive = str(tmp_path / "archive")
    pg = {"kind": "postgres", "host": "h", "port": 5432, "database": "d", "username": "u"}
    if mode == "postgres_to_file":
        profile = ConnectionProfile(
            id=1,
            name="a",
            source_config=pg,
            target_config={"kind": "file", "archive_path": archive},
        )
    else:
        profile = ConnectionProfile(
            id=1,
            name="a",
            source_config={"kind": "file", "archive_path": archive},
            target_config=pg,
        )
    with (
        patch("src.core.base_migration_worker.HistoryManager"),
        patch("src.core.base_migration_worker.CheckpointManager"),
    ):
        worker = worker_cls(profile, ["point_history_260517"], 1)
    worker._log = lambda message, level="INFO": None
    return worker


class _SlowCancelConn:
    """cancel()이 네트워크 왕복처럼 느린 연결."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.closed = 0
        self.cancel_calls = 0
        self.rollback_calls = 0

    def cancel(self) -> None:
        time.sleep(self.delay)
        self.cancel_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.closed = 1


@pytest.mark.parametrize(
    "worker_cls,mode,attr",
    [
        (PostgresToFileArchiveWorker, "postgres_to_file", "source_conn"),
        (FileToPostgresArchiveWorker, "file_to_postgres", "target_conn"),
    ],
)
class TestArchiveStopHook:
    def test_stop_does_not_block_caller_on_network_calls(self, tmp_path, worker_cls, mode, attr):
        """stop()은 UI 스레드에서 불린다. cancel·rollback 네트워크 호출로 막히면 안 된다."""
        worker = _archive_worker(worker_cls, tmp_path, mode)
        conn = _SlowCancelConn(delay=1.0)
        setattr(worker, attr, conn)
        worker._work_finished.clear()
        try:
            t0 = time.monotonic()
            worker.stop("app_shutdown")
            elapsed = time.monotonic() - t0
            assert elapsed < 0.3, f"stop()이 호출 스레드를 {elapsed:.1f}초 붙잡았습니다"
            assert conn.rollback_calls == 0, "다른 스레드에서 워커 연결을 rollback했습니다"
        finally:
            worker._work_finished.set()

    def test_stop_keeps_cancelling_until_work_finishes(self, tmp_path, worker_cls, mode, attr):
        """cancel은 그 순간 실행 중인 문장에만 듣는다. 작업이 끝날 때까지 반복해야 한다."""
        worker = _archive_worker(worker_cls, tmp_path, mode)
        conn = _SlowCancelConn(delay=0.0)
        setattr(worker, attr, conn)
        worker._work_finished.clear()
        worker.CANCEL_RETRY_INTERVAL = 0.05
        try:
            worker.stop("app_shutdown")
            deadline = time.monotonic() + 2.0
            while conn.cancel_calls < 3 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert conn.cancel_calls >= 3, "cancel을 한 번만 보냈습니다"
        finally:
            worker._work_finished.set()
        assert worker._cancel_thread is not None
        worker._cancel_thread.join(2)
        assert not worker._cancel_thread.is_alive()


class _BlockingCopySource:
    """export용 원본 연결. COPY가 cancel()될 때까지(문장 단위) 막힌다.

    실제 PostgreSQL처럼 cancel은 '그 순간 실행 중인 문장'에만 듣는다 — 문장이 시작될 때
    이전 cancel 표시를 지운다.
    """

    def __init__(self) -> None:
        self.closed = 0
        self.cancel_calls = 0
        self._cancel = threading.Event()
        self._in_statement = False
        self.copy_started = threading.Event()
        # close() 순간 cancel 반복 스레드가 살아 있었는가(psycopg2 cancel·close 경합 검사)
        self.cancel_thread_alive_at_close: bool | None = None
        self.worker: Any = None

    def set_isolation_level(self, _level: int) -> None:
        return None

    def cancel(self) -> None:
        self.cancel_calls += 1
        if self._in_statement:
            self._cancel.set()

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        thread = getattr(self.worker, "_cancel_thread", None)
        self.cancel_thread_alive_at_close = bool(thread is not None and thread.is_alive())
        self.closed = 1

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def copy_expert(self, _query: str, fp: Any) -> None:
        self._cancel.clear()
        self._in_statement = True
        try:
            fp.write("1,1\n")
            fp.flush()
            self.copy_started.set()
            if self._cancel.wait(_BLOCK_SECONDS):
                raise psycopg2.errors.QueryCanceled("canceling statement due to user request")
            raise psycopg2.errors.QueryCanceled("fake: 취소 없이 풀림(statement_timeout)")
        finally:
            self._in_statement = False


def _lock_is_free(lock_path) -> bool:
    """다른 열린 파일로 manifest 잠금을 바로 잡을 수 있으면 True."""
    if not lock_path.exists():
        return True
    with open(lock_path, "a+") as fd:
        if sys.platform == "win32":  # pragma: no cover - Windows 전용
            import msvcrt

            fd.seek(0)
            try:
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            return True
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True


class TestArchiveExportShutdown:
    def test_shutdown_mid_export_removes_temp_file_releases_lock_and_saves_manifest(
        self, registry, tmp_path, qtbot
    ):
        worker = _archive_worker(PostgresToFileArchiveWorker, tmp_path, "postgres_to_file")
        source = _BlockingCopySource()
        source.worker = worker
        worker._create_psycopg2_connection = MagicMock(return_value=source)
        worker._query_row_count = MagicMock(return_value=1)
        worker._query_parent_columns = MagicMock(return_value=_PARENT_COLUMNS)
        worker._query_partition_meta = MagicMock(return_value={})
        worker.checkpoint_manager.get_checkpoints.return_value = []
        checkpoint = MagicMock(id=3, partition_name="point_history_260517")
        worker.checkpoint_manager.create_checkpoint.return_value = checkpoint
        store = worker.archive_store

        worker.start()
        assert source.copy_started.wait(5), "export COPY에 도달하지 못했습니다"
        temp_files = list(store.partitions_dir.glob("*.tmp"))
        assert temp_files, "전제: 진행 중 임시 파일이 있어야 합니다"

        coordinator = _coordinator(registry)
        t0 = time.monotonic()
        forced = coordinator.shutdown_blocking()
        elapsed = time.monotonic() - t0

        assert forced is False
        assert elapsed < _BOUND_SECONDS, f"export 종료가 {elapsed:.1f}초 걸렸습니다"
        assert worker.isFinished()
        assert source.cancel_calls >= 1 and source.closed
        assert source.cancel_thread_alive_at_close is False, (
            "cancel 반복 스레드가 도는 중에 연결을 닫았습니다(psycopg2 cancel·close 경합)"
        )
        assert list(store.partitions_dir.glob("*.tmp")) == [], "임시 파일이 남았습니다"
        assert not store.build_partition_file_path("point_history_260517").exists()
        assert store.manifest_path.exists(), "종료 전에 manifest를 저장(flush)하지 않았습니다"
        assert _lock_is_free(store._lock_path), "manifest 잠금이 풀리지 않았습니다"
        statuses = [
            c.args[1] for c in worker.checkpoint_manager.update_checkpoint_status.call_args_list
        ]
        assert statuses[-1] == "pending", f"중단은 재개 가능한 pending이어야 합니다: {statuses}"
        assert _wait_threads_gone() == []

    def test_stop_between_check_and_copy_start_is_not_lost(self, registry, tmp_path, qtbot):
        """플래그 확인 뒤·COPY 시작 전에 온 종료도 COPY를 끊는다(cancel 1회로는 놓친다)."""
        worker = _archive_worker(PostgresToFileArchiveWorker, tmp_path, "postgres_to_file")
        source = _BlockingCopySource()
        worker._create_psycopg2_connection = MagicMock(return_value=source)
        worker._query_row_count = MagicMock(return_value=1)
        worker._query_parent_columns = MagicMock(return_value=_PARENT_COLUMNS)
        worker._query_partition_meta = MagicMock(return_value={})
        worker.checkpoint_manager.get_checkpoints.return_value = []
        worker.checkpoint_manager.create_checkpoint.return_value = MagicMock(
            id=3, partition_name="point_history_260517"
        )
        original_mark = worker._mark_partition_running
        stopped = threading.Event()

        def mark(checkpoint, **kwargs):
            original_mark(checkpoint, **kwargs)
            if kwargs.get("phase") == "export" and not stopped.is_set():
                stopped.set()
                worker.stop(registry_module.SHUTDOWN_REASON)  # 확인은 지났고 COPY는 아직

        worker._mark_partition_running = mark  # type: ignore[method-assign]
        worker.start()
        assert stopped.wait(5)
        t0 = time.monotonic()
        assert worker.wait(int(_BLOCK_SECONDS * 1000) + 5000)
        elapsed = time.monotonic() - t0

        assert elapsed < _BOUND_SECONDS, f"COPY가 취소되지 않고 {elapsed:.1f}초 돌았습니다"
        assert list(worker.archive_store.partitions_dir.glob("*.tmp")) == []
        assert _wait_threads_gone() == []
