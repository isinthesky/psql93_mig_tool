"""
마이그레이션 워커 추상 기반 클래스
"""

import threading
import time
from abc import ABCMeta, abstractmethod
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThread, Signal

from src.core.worker_registry import register_worker, unregister_worker
from src.models.history import CheckpointManager, HistoryManager
from src.models.profile import ConnectionProfile
from src.utils.enhanced_logger import enhanced_logger, log_emitter


class QThreadABCMeta(type(QThread), ABCMeta):  # type: ignore[misc]
    """QThread와 ABC를 동시에 상속하기 위한 메타클래스

    type(QThread)는 런타임에만 결정되는 동적 베이스라 타입 체커가 해석하지 못한다.
    """

    pass


class BaseMigrationWorker(QThread, metaclass=QThreadABCMeta):
    """마이그레이션 워커의 추상 기반 클래스

    CopyMigrationWorker와 파일 아카이브 워커의 공통 로직을 제공합니다.
    하위 클래스는 _execute_migration() 메서드를 구현해야 합니다.

    취소 규약(감사 H-02): stop()은 플래그만 바꾸지 않고 `_on_stop_requested()`를 불러
    실행 중인 DB 문장을 끊게 한다. 플래그는 문장 **사이**에서만 읽히기 때문이다.
    """

    # stop() 뒤 연결 cancel()을 다시 보내는 간격·최대 시간(초).
    # cancel 요청은 그 순간 실행 중인 문장에만 듣는다. 플래그 확인과 다음 문장 시작 사이에
    # 들어온 stop()도 놓치지 않도록 작업이 끝날 때까지(최대 이 시간) 반복한다.
    CANCEL_RETRY_INTERVAL = 0.5
    CANCEL_RETRY_SECONDS = 15.0

    # 공통 시그널
    #
    # finished는 선언하지 않는다. QThread가 이미 같은 이름으로 '스레드 종료'를
    # 알리기 때문에, 여기서 다시 선언하고 직접 emit하면 같은 슬롯이 두 번 불린다
    # (직접 emit 1회 + run() 반환 시 QThread 자체 emit 1회).
    # 그러면 완료 핸들러가 두 번 돌면서 "완료" 다음에 "중단"이 이어 찍힌다.
    progress = Signal(dict)  # 진행 상황
    log = Signal(str, str)  # 메시지, 레벨
    error = Signal(str)  # 오류 메시지

    def __init__(
        self,
        profile: ConnectionProfile,
        partitions: list[str],
        history_id: int,
        resume: bool = False,
    ):
        """워커 초기화

        Args:
            profile: 연결 프로필
            partitions: 마이그레이션할 파티션 목록
            history_id: 이력 ID
            resume: 재개 여부
        """
        super().__init__()
        self.profile = profile
        self.partitions = partitions
        self.history_id = history_id
        # NOTE: `resume` 속성명은 resume() 메서드와 충돌하므로 다른 이름을 사용한다.
        self.should_resume = resume

        # 공통 상태 필드
        self.is_running = False
        self.is_paused = False
        self.stop_reason: str | None = None
        self.current_partition_index = 0
        self.total_rows_processed = 0
        self.start_time: float | None = None

        # 공통 매니저
        self.history_manager = HistoryManager()
        self.checkpoint_manager = CheckpointManager()

        # 취소 보조: 작업이 끝났음을 cancel 반복 스레드에 알린다.
        self._work_finished = threading.Event()
        self._cancel_thread: threading.Thread | None = None

    def start(self, priority: QThread.Priority = QThread.Priority.InheritPriority) -> None:
        """스레드를 띄우고 앱 종료 레지스트리에 등록한다(감사 M-13)."""
        register_worker(self)
        super().start(priority)

    def run(self):
        """워커 실행 (템플릿 메서드)

        공통 초기화와 마무리 로직을 수행하고,
        하위 클래스의 _execute_migration()을 호출합니다.
        """
        self.is_running = True
        self.start_time = time.time()
        # 앱 종료 레지스트리(M-13). 종료가 이미 시작됐으면 여기서 곧바로 stop()된다 —
        # 위에서 is_running을 켠 뒤라야 그 중지가 덮어쓰이지 않는다.
        register_worker(self)

        # 세션 ID 초기화
        session_id = enhanced_logger.generate_session_id()
        log_emitter.logger.set_session_id(session_id)

        self._work_finished.clear()
        try:
            # 하위 클래스 구현 실행
            self._execute_migration()
        except Exception as e:
            error_msg = str(e)
            self._log(f"마이그레이션 오류: {error_msg}", "ERROR")
            self.error.emit(error_msg)
        finally:
            self._work_finished.set()
            unregister_worker(self)

        # finished는 직접 발행하지 않는다. 예외를 여기서 모두 삼키므로 run()은 항상
        # 정상 반환하고, 그 시점에 QThread가 finished를 정확히 한 번 발행한다.
        # 완료 유형 판정은 handler 측에서 is_running 스냅샷으로 수행한다.

    @abstractmethod
    def _execute_migration(self):
        """마이그레이션 실행 (하위 클래스에서 구현)

        각 워커의 고유한 마이그레이션 로직을 구현합니다.
        - CopyMigrationWorker: COPY 기반 마이그레이션
        - 파일 아카이브 워커: export/import
        """
        pass

    def _log(self, message: str, level: str = "INFO"):
        """UI 로그 시그널 발행 (파일/DB 로깅은 UI 측 add_log에서 자동 수행)"""
        self.log.emit(message, level)

    def pause(self):
        """마이그레이션 일시정지"""
        self.is_paused = True
        self._log("마이그레이션 일시정지")

    def resume(self):
        """마이그레이션 재개"""
        self.is_paused = False
        self._log("마이그레이션 재개")

    def stop(self, reason: str = "user_stop"):
        """마이그레이션 중지

        Args:
            reason: 중지 사유 식별자 (예: user_cancel, app_stop, network_interrupt)
        """
        self.is_running = False
        self.is_paused = False
        self.stop_reason = reason
        self._log(f"마이그레이션 중지 요청 ({reason})", "WARNING")
        try:
            self._on_stop_requested()
        except Exception as exc:  # noqa: BLE001 — 취소 보조가 실패해도 플래그는 이미 섰다
            self._log(f"실행 중 작업 취소 요청 실패: {exc}", "WARNING")

    def _on_stop_requested(self) -> None:
        """실행 중인 DB 작업을 끊는다(하위 클래스 구현). 기본은 아무것도 하지 않는다.

        UI 스레드에서 불리므로 막히면 안 된다. 연결 cancel은 `_cancel_connections_async()`로
        백그라운드에서 보낸다.
        """

    def _cancel_connections_async(self, get_connections: Callable[[], list[Any]]) -> None:
        """열린 연결에 cancel()을 백그라운드로, 작업이 끝날 때까지 반복해 보낸다.

        Args:
            get_connections: 매번 현재 연결 목록을 돌려주는 함수(연결 참조가 바뀔 수 있다).

        psycopg2/psycopg의 cancel()은 다른 스레드에서 불러도 안전하지만 서버에 새 소켓을
        여는 네트워크 호출이라 UI 스레드에서 직접 부르지 않는다.
        """
        if self._cancel_thread is not None and self._cancel_thread.is_alive():
            return
        if self._work_finished.is_set():
            # 작업이 이미 끝나(연결을 닫는 중) — 닫히는 연결에 cancel을 겹쳐 보내지 않는다.
            return

        def run() -> None:
            deadline = time.monotonic() + self.CANCEL_RETRY_SECONDS
            while not self._work_finished.is_set():
                for conn in get_connections():
                    _cancel_quietly(conn)
                if self._work_finished.wait(self.CANCEL_RETRY_INTERVAL):
                    return
                if time.monotonic() > deadline:
                    return

        self._cancel_thread = threading.Thread(target=run, name="db-cancel", daemon=True)
        self._cancel_thread.start()

    def _stop_cancel_retries(self) -> None:
        """연결을 닫기 직전에 부른다: cancel 반복 스레드를 끝내고 잠깐 기다린다.

        psycopg2의 cancel()과 close()가 다른 스레드에서 겹치면 해제된 cancel 핸들을 쓸 수 있다.
        앱 종료(M-13)는 모든 워커에 cancel 반복을 거므로 닫기 전에 반드시 멈춘다.
        """
        self._work_finished.set()
        thread = self._cancel_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(self.CANCEL_RETRY_INTERVAL + 2.0)

    def _check_pause(self):
        """일시정지 상태 확인

        일시정지 중이면 재개될 때까지 대기합니다.
        파티션 처리 루프에서 호출하여 사용합니다.
        """
        while self.is_paused and self.is_running:
            time.sleep(0.1)

    def _calculate_speed(self) -> int:
        """처리 속도 계산 (rows/sec)

        Returns:
            초당 처리된 행 수
        """
        if not self.start_time or self.total_rows_processed == 0:
            return 0

        elapsed = time.time() - self.start_time
        if elapsed > 0:
            return int(self.total_rows_processed / elapsed)
        return 0

    def get_stats(self) -> dict[str, Any]:
        """통계 정보 반환

        Returns:
            경과 시간, 처리된 행 수, 속도 등의 통계
        """
        elapsed = time.time() - self.start_time if self.start_time else 0
        speed = self._calculate_speed()

        # 남은 파티션 추정
        remaining_partitions = len(self.partitions) - self.current_partition_index - 1
        estimated_remaining_rows = remaining_partitions * 4000000  # 하루 평균 400만 rows

        # 예상 완료 시간
        eta_seconds = 0.0
        if speed > 0:
            eta_seconds = estimated_remaining_rows / speed

        return {
            "elapsed_seconds": elapsed,
            "total_rows_processed": self.total_rows_processed,
            "speed": speed,
            "eta_seconds": eta_seconds,
        }


def _cancel_quietly(conn: Any) -> None:
    """열린 연결의 실행 중 문장을 취소한다. 닫혔거나 없거나 실패하면 조용히 넘어간다."""
    if conn is None:
        return
    # psycopg2: closed는 int(0=열림), psycopg(3): bool
    closed = getattr(conn, "closed", False)
    if isinstance(closed, (bool, int)) and closed:
        return
    try:
        conn.cancel()
    except Exception:  # noqa: BLE001 — 이미 끝난 연결 등. 취소는 최선 노력이다
        pass
