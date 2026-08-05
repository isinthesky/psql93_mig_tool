"""조회(scan) 워커를 띄우는 다이얼로그의 공통 골격.

두 마법사(`migration_wizard_dialog`, `file_archive_migration_dialog`)가 같은
문제를 푼다. 각자 따로 구현하면 한쪽에서 고친 결함이 다른 쪽에 남는다 —
실제로 그런 상태였다.

## 이 골격이 지키는 것

**세대(generation)** — 조회 도중 조건이 바뀌면 세대가 올라간다. 결과 시그널의
첫 인자가 세대이므로, 늦게 도착한 결과는 자기 세대가 아니면 버려진다.

**수명** — 실행 중인 QThread의 마지막 파이썬 참조가 사라지면 프로세스가 죽는다.
창을 닫을 때 워커를 정리하고, 끝내 안 멈추면 시그널을 끊고 전역에 맡긴다.
`terminate()`는 쓰지 않는다 — psycopg 커넥션과 아카이브 파일 락이 남는다.

**재진입** — `isRunning()`만으로는 run()이 끝나고 결과 슬롯이 아직 안 돈 구간을
놓친다. 그 틈에 두 번째 요청이 들어가면 워커가 둘이 된다.

## 쓰는 쪽이 할 일

- `_init_scan_host()`를 `__init__`에서 부른다.
- 조회를 시작할 때 `_is_scan_inflight(kind)`로 막고, 워커를 `_scan_workers[kind]`에
  넣고 `_mark_scan_started(kind)` 한다.
- `QThread.finished`에 `_mark_scan_finished(kind)`를 포함한 정리를 건다.
- 조건이 바뀌면 `_bump_generation()`을 부른다.
- 닫기 경로에서 `_prepare_close()`를 부른다.
- 필요하면 `_on_scan_generation_changed` / `_sync_scan_buttons` /
  `_set_scan_status`를 재정의한다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread

# 화면에 그릴 파티션 최대 개수. 이보다 많으면 목록에 담기지 않고,
# 담기지 않은 파티션은 체크할 수 없어 실행 대상에서도 빠진다.
# 조용히 자르면 '전부 옮겼다'고 믿게 되므로 반드시 사용자에게 알린다.
PARTITION_DISPLAY_LIMIT = 5000

# 끝내 멈추지 않은 조회 워커를 붙들어 두는 곳.
#
# 실행 중인 QThread의 마지막 파이썬 참조가 사라지면 프로세스가 즉사한다.
# 창을 닫을 때 워커가 아직 살아 있으면 여기로 옮겨서, 창이 사라져도
# 스레드가 제 발로 끝날 때까지 참조를 유지한다.
_ORPHANED_SCAN_WORKERS: set[QThread] = set()


def park_orphan_worker(worker: QThread) -> None:
    """멈추지 않는 워커를 전역에 맡기고 스스로 정리되게 한다."""
    _ORPHANED_SCAN_WORKERS.add(worker)
    worker.finished.connect(lambda: _ORPHANED_SCAN_WORKERS.discard(worker))


def build_log_retention_hint(max_blocks: int):
    """실행 로그 창 아래에 붙일 안내 라벨을 만든다.

    로그 창은 오래된 줄을 조용히 버린다(메모리 상한). 수천 개 파티션을
    옮기는 동안 초반의 경고가 화면 밖으로 밀려나면, 사용자는 그런 경고가
    없었다고 믿는다. 전체 기록은 파일에 남으므로 그 사실과 위치를 알린다.
    """
    from PySide6.QtWidgets import QLabel

    from src.utils.app_paths import get_logs_dir

    label = QLabel(
        f"화면에는 최근 {max_blocks:,}줄만 남습니다. 전체 기록은 로그 파일에 저장됩니다 — "
        f"{get_logs_dir()}"
    )
    label.setProperty("role", "hint")
    label.setWordWrap(True)
    label.setToolTip("경로를 드래그해 복사할 수 있습니다.")
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def format_row_count(count: int, estimated: bool) -> str:
    """행 수를 표기한다. 추정치는 추정치라고 밝힌다.

    PostgreSQL 소스의 행 수는 플래너 통계(`pg_class.reltuples`)에서 온다.
    파티션마다 `COUNT(*)` 전수 스캔을 돌리면 탐색이 분 단위로 늘어나는데,
    이 숫자는 목록·합계 표시에만 쓰이므로 추정으로 충분하다. 아카이브
    소스의 행 수는 내보낼 때 실제로 센 값이라 그대로 쓴다.

    통계가 없으면(ANALYZE 전) 0으로 온다. 이때 '0 rows'라고 쓰면 사용자가
    빈 파티션으로 읽고 체크를 푼다 — 실제로는 수백만 행일 수 있다.
    모른다는 것을 모른다고 쓴다.
    """
    if not estimated:
        return f"{count:,} rows"
    if count <= 0:
        return "행 수 미상"
    return f"약 {count:,} rows"


class ScanHostMixin:
    """조회 워커 수명·세대·재진입을 관리하는 믹스인."""

    # 창을 닫을 때 조회 워커를 기다리는 방식.
    # 한 번에 오래 붙잡으면 창이 굳어 보이므로 짧게 여러 번 시도하고,
    # 총 대기(약 2초)를 넘기면 워커를 떼어내고 창을 닫는다.
    SCAN_SHUTDOWN_STEP_MS = 200
    SCAN_SHUTDOWN_MAX_ATTEMPTS = 10

    def _init_scan_host(self) -> None:
        self._scan_gen: int = 0
        self._scan_workers: dict[str, QThread] = {}
        self._inflight: set[str] = set()
        self._close_attempts: int = 0
        # 창이 닫히는 중인가. 예약된 작업(QTimer.singleShot)이 닫힌 뒤에
        # 뒤늦게 발화해 워커를 띄우는 것을 막는다.
        self._closing: bool = False

    # ── 세대 ────────────────────────────────────────────────
    def _bump_generation(self) -> int:
        """조회 조건이 바뀌었음을 알린다.

        늦게 도착하는 이전 세대의 결과는 버려진다. 돌고 있는 조회는 결과가
        어차피 버려지므로 멈추라고 알린다(체크섬 확인은 수 분씩 걸린다 —
        날짜만 바꿔도 쓸모없어진 작업이 계속 도는 것을 막는다).
        """
        self._scan_gen += 1
        self._interrupt_active_scans()
        self._on_scan_generation_changed()
        return self._scan_gen

    def _is_current_generation(self, gen: int) -> bool:
        return gen == self._scan_gen

    def _on_scan_generation_changed(self) -> None:
        """세대가 바뀐 뒤 화면 상태를 되돌린다. 쓰는 쪽에서 재정의한다."""

    # ── 재진입 ──────────────────────────────────────────────
    def _mark_scan_started(self, kind: str) -> None:
        self._inflight.add(kind)

    def _mark_scan_finished(self, kind: str) -> None:
        self._inflight.discard(kind)

    def _is_scan_inflight(self, kind: str) -> bool:
        """해당 종류의 조회가 진행 중인가.

        `isRunning()`만으로는 run()이 끝나고 결과 슬롯이 아직 안 돈 구간을
        놓친다. 그 틈에 두 번째 요청이 들어가면 워커가 둘이 된다.
        """
        if kind in self._inflight:
            return True
        worker = self._scan_workers.get(kind)
        return bool(worker is not None and worker.isRunning())

    def _has_active_scan(self) -> bool:
        """조회 작업이 돌고 있는가.

        마이그레이션 실행과 구분한다. 실행은 닫기를 막지만, 조회는
        파괴적이지 않으므로 닫기를 허용하고 대신 정리한다.
        """
        return any(w is not None and w.isRunning() for w in self._scan_workers.values())

    def _sync_scan_buttons(self) -> None:
        """조회 버튼 활성화를 한 곳에서 정한다. 쓰는 쪽에서 재정의한다."""

    def _set_scan_status(self, text: str) -> None:
        """조회 상태 문구를 표시한다. 쓰는 쪽에서 재정의한다."""

    # ── 취소·정리 ───────────────────────────────────────────
    @staticmethod
    def _request_worker_stop(worker: QThread) -> None:
        """워커에게 멈추라고 알린다(기다리지 않는다).

        세 갈래를 모두 시도한다. `ScanWorker`는 `isInterruptionRequested()`를
        보지만, 연결 확인에 재사용되는 `BaseMigrationWorker` 계열은 자체
        `stop()` 플래그만 본다. 진행 중인 쿼리는 `cancel_query()`로 끊는다.
        """
        worker.requestInterruption()
        for name in ("cancel_query", "stop"):
            method = getattr(worker, name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass

    def _interrupt_active_scans(self) -> None:
        """돌고 있는 조회에 중단을 요청한다(기다리지는 않는다)."""
        for worker in self._scan_workers.values():
            if worker is None or not worker.isRunning():
                continue
            self._request_worker_stop(worker)

    def _shutdown_scans(self, timeout_ms: int = 3000) -> bool:
        """진행 중인 조회 워커를 정리한다.

        `terminate()`는 쓰지 않는다. psycopg 커넥션과 아카이브 파일 락이
        정리되지 않은 채 남아 이후 저장이 실패한다.

        Returns:
            제한 시간 안에 전부 멈췄으면 True.
        """
        workers = [w for w in self._scan_workers.values() if w is not None and w.isRunning()]
        for worker in workers:
            self._request_worker_stop(worker)

        all_stopped = True
        for worker in workers:
            if not worker.wait(timeout_ms):
                all_stopped = False

        if all_stopped:
            self._inflight.clear()
        return all_stopped

    def _abandon_scans(self) -> None:
        """멈추지 않는 워커를 떼어내고 창을 놓아준다.

        마지막 수단이다. 워커 시그널을 끊어 사라질 위젯을 건드리지 못하게 하고,
        스레드 객체는 전역에 맡겨 참조가 살아 있게 한다(참조가 끊기면 프로세스가 죽는다).
        """
        for worker in self._scan_workers.values():
            if worker is None:
                continue
            try:
                # 인자 없는 disconnect()는 이 객체의 모든 연결을 끊는 Qt의 유효한
                # 사용법이지만 PySide6 스텁에 해당 오버로드가 없다.
                worker.disconnect()  # type: ignore[call-overload]
            except (RuntimeError, TypeError):
                pass
            if worker.isRunning():
                park_orphan_worker(worker)
        self._scan_workers.clear()
        self._inflight.clear()

    def _prepare_close(self) -> bool:
        """닫기 전에 조회 워커를 정리한다.

        조회는 파괴적이지 않으므로 닫기를 막지 않는다. 다만 정리하지 않고 닫으면
        워커가 사라진 위젯을 갱신하거나, 실행 중인 QThread가 파괴돼 프로세스가 죽는다.

        Returns:
            닫아도 되면 True. 잠시 더 기다려야 하면 False.
        """
        # 예약된 작업이 닫힌 뒤에 워커를 띄우지 못하게 먼저 표시한다.
        self._closing = True

        if self._shutdown_scans(self.SCAN_SHUTDOWN_STEP_MS):
            self._close_attempts = 0
            return True

        self._close_attempts += 1
        if self._close_attempts >= self.SCAN_SHUTDOWN_MAX_ATTEMPTS:
            # 끝내 안 멈춘다. 창을 인질로 잡지 않는다 — 떼어내고 닫는다.
            self._on_scans_abandoned()
            self._abandon_scans()
            self._close_attempts = 0
            return True

        self._set_scan_status("조회 작업 정리 중...")
        return False

    def _on_scans_abandoned(self) -> None:
        """워커를 떼어내기 직전에 불린다(로그 등). 쓰는 쪽에서 재정의한다."""
