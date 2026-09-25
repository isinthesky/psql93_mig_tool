"""애플리케이션 수준 워커 레지스트리와 종료 조정자 (감사 M-13).

트레이 '종료'가 실행 중인 워커를 stop/cancel/wait 없이 ``app.quit()``으로 끝내던 결함을 막는다.
실행 중인 QThread가 파괴되면 프로세스가 abort하고, 진행 중 배치·manifest·로그가 정리되지 않는다.

## 레지스트리
- migration/scan/archive 워커(`BaseMigrationWorker`, `ScanWorker` 계열)는 `start()`와 `run()` 시작에서
  `register_worker()`로 등록되고, `run()`이 끝날 때 `unregister_worker()`로 해제된다.
- 약한 참조로 들고 있다. 레지스트리가 워커 수명을 늘리지 않는다 — 워커 스레드 안에서 마지막 참조가
  풀려 QThread가 제 스레드에서 파괴되는 사고를 만들지 않기 위해서다. 종료 중에는 조정자가 강한 참조를 쥔다.
- 종료가 시작된 뒤 등록되는(늦게 시작한) 워커는 곧바로 중지 요청을 받는다.

## 종료 순서 (`ShutdownCoordinator`)
1. 등록된 모든 워커에 중지 요청: `requestInterruption()` + `stop(reason="app_shutdown")`
   (`BaseMigrationWorker` — H-02의 `_on_stop_requested()`가 실행 중 문장을 백그라운드에서 cancel) +
   `cancel_query()`(`ScanWorker` — 네트워크 호출이라 백그라운드 스레드에서).
2. 제한 시간(기본 30초) 안에서 모두 끝나기를 기다린다. `request_shutdown()`은 이벤트 루프를 돌리며
   (QTimer 폴링) 기다리므로 워커의 finished 처리(이력 상태 기록 등)가 그대로 실행된다.
   워커의 `finally`가 연결을 닫고 아카이브 manifest를 저장(flush)한다.
3. flush 단계(로거 DB 큐 → 로컬 DB)를 순서대로 실행한다. 한 단계가 실패해도 나머지는 한다.
4. `quit_app(forced)`로 이벤트 루프를 끝낸다.

제한 시간을 넘기면 멈추지 않은 워커 이름을 WARNING으로 남기고 `forced=True`로 끝낸다. 이때 아직 도는
QThread를 파괴하면 abort하므로 `stuck_workers`로 붙들어 두고, 호출부(`main.finalize_exit`)가 파이썬
종료 절차 없이 프로세스를 끝낸다. 커밋되지 않은 배치는 DB 서버가 연결 종료로 롤백하고, checkpoint는
커밋된 배치까지만 남아 '이어서 시작'으로 재개할 수 있다.
"""

from __future__ import annotations

import threading
import time
import weakref
from collections.abc import Callable, Sequence

from PySide6.QtCore import QObject, QThread, QTimer, Signal

SHUTDOWN_REASON = "app_shutdown"
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 30.0

FlushStep = tuple[str, Callable[[], object]]
LogFn = Callable[[str, str], None]  # (level, message)


def _is_alive(worker: QThread) -> bool:
    try:
        return bool(worker.isRunning())
    except RuntimeError:  # C++ 객체가 이미 파괴됐다
        return False


def _worker_label(worker: QThread) -> str:
    return type(worker).__name__


def request_worker_stop(worker: QThread, reason: str = SHUTDOWN_REASON) -> None:
    """워커 하나에 멈추라고 알린다. 기다리지 않고, 호출 스레드(UI)를 막지 않는다."""
    try:
        worker.requestInterruption()
    except RuntimeError:
        return
    stop = getattr(worker, "stop", None)
    if callable(stop):
        try:
            stop(reason=reason)
        except Exception:  # noqa: BLE001 — 한 워커의 실패가 다른 워커의 정지를 막으면 안 된다
            pass
    cancel_query = getattr(worker, "cancel_query", None)
    if callable(cancel_query):

        def cancel() -> None:
            try:
                cancel_query()
            except Exception:  # noqa: BLE001 — 취소는 최선 노력이다
                pass

        threading.Thread(target=cancel, name="shutdown-cancel", daemon=True).start()


class WorkerRegistry:
    """실행 중인 워커의 약한 참조 집합. 워커 스레드와 UI 스레드에서 함께 불린다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: weakref.WeakSet[QThread] = weakref.WeakSet()
        self._shutdown_reason: str | None = None

    @property
    def shutting_down(self) -> bool:
        return self._shutdown_reason is not None

    def register(self, worker: QThread) -> None:
        with self._lock:
            self._workers.add(worker)
            reason = self._shutdown_reason
        # 종료 중에 시작한 워커는 곧바로 멈춘다. 아직 스레드가 안 떴으면(start() 직전 등록)
        # 중지 요청이 먹지 않으므로(requestInterruption은 실행 중에만 유효, run()이 is_running을
        # 다시 켠다) run() 시작에서 다시 등록될 때 멈춘다.
        if reason is not None and _is_alive(worker):
            request_worker_stop(worker, reason)

    def unregister(self, worker: QThread) -> None:
        with self._lock:
            self._workers.discard(worker)

    def active_workers(self) -> list[QThread]:
        """실행 중인 등록 워커. 끝난 항목은 이 기회에 정리한다."""
        with self._lock:
            workers = list(self._workers)
            for worker in workers:
                if not _is_alive(worker):
                    try:
                        finished = worker.isFinished()
                    except RuntimeError:
                        finished = True
                    if finished:
                        self._workers.discard(worker)
        return [w for w in workers if _is_alive(w)]

    def begin_shutdown(self, reason: str = SHUTDOWN_REASON) -> list[QThread]:
        """이후 등록되는 워커까지 멈추게 표시하고, 지금 실행 중인 워커에 중지를 요청한다."""
        with self._lock:
            self._shutdown_reason = reason
        workers = self.active_workers()
        for worker in workers:
            request_worker_stop(worker, reason)
        return workers


# 애플리케이션 전역 레지스트리. 워커 훅은 호출 시점에 이 이름을 찾는다(테스트가 교체할 수 있게).
worker_registry = WorkerRegistry()


def register_worker(worker: QThread) -> None:
    worker_registry.register(worker)


def unregister_worker(worker: QThread) -> None:
    worker_registry.unregister(worker)


def _default_log(level: str, message: str) -> None:
    from src.utils.enhanced_logger import log_emitter

    log_emitter.emit_log(level, message)


class ShutdownCoordinator(QObject):
    """앱 종료를 한 번만, 정해진 순서로 수행한다."""

    shutdown_started = Signal()
    shutdown_finished = Signal(bool)  # forced 여부

    POLL_INTERVAL_MS = 100

    def __init__(
        self,
        registry: WorkerRegistry,
        *,
        quit_app: Callable[[bool], object],
        flush_steps: Sequence[FlushStep] = (),
        timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        log: LogFn | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._registry = registry
        self._quit_app = quit_app
        self._flush_steps = list(flush_steps)
        self.timeout_seconds = float(timeout_seconds)
        self._log_fn = log or _default_log
        self._state = "idle"  # idle → stopping → done
        self._deadline = 0.0
        self._tracked: list[QThread] = []
        self.forced = False
        # 강제 종료 때 아직 도는 워커. 파괴되지 않게 프로세스가 끝날 때까지 붙든다.
        self.stuck_workers: list[QThread] = []
        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    # ── 상태 ────────────────────────────────────────────────
    @property
    def is_active(self) -> bool:
        return self._state == "stopping"

    @property
    def is_finished(self) -> bool:
        return self._state == "done"

    # ── 진입점 ──────────────────────────────────────────────
    def request_shutdown(self, reason: str = SHUTDOWN_REASON) -> None:
        """UI를 막지 않는 종료. 워커가 멈추면(또는 제한 시간이 지나면) flush 후 quit한다."""
        if self._state != "idle":
            return
        self._begin(reason)
        self._tick()
        if self._state == "stopping":
            self._timer.start()

    def shutdown_blocking(self, reason: str = SHUTDOWN_REASON) -> bool:
        """이벤트 루프 없이 막고 기다리는 종료(루프가 이미 끝난 경로용). quit은 부르지 않는다.

        Returns:
            강제 종료(제한 시간 초과)였으면 True.
        """
        if self._state == "done":
            return self.forced
        self._timer.stop()
        if self._state == "idle":
            self._begin(reason)
        while True:
            alive = self._collect_alive()
            if not alive:
                self._finish(forced=False, quit_app=False)
                break
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                self._finish(forced=True, quit_app=False)
                break
            alive[0].wait(max(1, int(min(remaining, 0.1) * 1000)))
        return self.forced

    # ── 내부 ────────────────────────────────────────────────
    def _log(self, level: str, message: str) -> None:
        try:
            self._log_fn(level, message)
        except Exception:  # noqa: BLE001 — 종료 로그 실패로 종료가 멈추면 안 된다
            pass

    def _begin(self, reason: str) -> None:
        self._state = "stopping"
        self._deadline = time.monotonic() + self.timeout_seconds
        self._tracked = list(self._registry.begin_shutdown(reason))
        if self._tracked:
            names = ", ".join(_worker_label(w) for w in self._tracked)
            self._log(
                "INFO",
                f"앱 종료: 실행 중인 작업 {len(self._tracked)}개를 멈춥니다({names}). "
                f"최대 {self.timeout_seconds:.0f}초 기다립니다.",
            )
        self.shutdown_started.emit()

    def _collect_alive(self) -> list[QThread]:
        # 종료가 시작된 뒤 늦게 등록된 워커도 기다린다(등록 시 이미 중지 요청을 받았다).
        for worker in self._registry.active_workers():
            if not any(worker is tracked for tracked in self._tracked):
                self._tracked.append(worker)
        return [w for w in self._tracked if _is_alive(w)]

    def _tick(self) -> None:
        if self._state != "stopping":
            self._timer.stop()
            return
        alive = self._collect_alive()
        if not alive:
            self._finish(forced=False, quit_app=True)
        elif time.monotonic() >= self._deadline:
            self._finish(forced=True, quit_app=True)

    def _finish(self, *, forced: bool, quit_app: bool) -> None:
        self._timer.stop()
        self._state = "done"
        self.forced = forced
        if forced:
            self.stuck_workers = [w for w in self._tracked if _is_alive(w)]
            names = ", ".join(_worker_label(w) for w in self.stuck_workers)
            self._log(
                "WARNING",
                f"앱 종료: {self.timeout_seconds:.0f}초 안에 멈추지 않은 작업({names})을 두고 "
                "강제 종료합니다. 커밋되지 않은 배치는 DB가 롤백하며 '이어서 시작'으로 재개할 수 있습니다.",
            )
        else:
            for worker in self._tracked:
                # isRunning()이 꺼진 뒤 OS 스레드가 완전히 끝나기까지의 짧은 꼬리를 기다린다.
                try:
                    worker.wait(1000)
                except RuntimeError:
                    pass
            if self._tracked:
                self._log("INFO", "앱 종료: 모든 작업이 멈췄습니다.")
        self._tracked = []
        self._run_flush_steps()
        self.shutdown_finished.emit(forced)
        if quit_app:
            self._quit_app(forced)

    def _run_flush_steps(self) -> None:
        for name, step in self._flush_steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 — 한 단계 실패가 나머지 flush·종료를 막으면 안 된다
                self._log("ERROR", f"앱 종료: {name} flush 실패: {exc}")
