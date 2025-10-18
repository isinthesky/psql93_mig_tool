"""
마이그레이션 워커 추상 기반 클래스
"""

import time
from abc import ABCMeta, abstractmethod
from typing import Any

from PySide6.QtCore import QThread, Signal

from src.models.history import CheckpointManager, HistoryManager
from src.models.profile import ConnectionProfile
from src.utils.enhanced_logger import enhanced_logger, log_emitter


class QThreadABCMeta(type(QThread), ABCMeta):
    """QThread와 ABC를 동시에 상속하기 위한 메타클래스"""

    pass


class BaseMigrationWorker(QThread, metaclass=QThreadABCMeta):
    """마이그레이션 워커의 추상 기반 클래스

    MigrationWorker와 CopyMigrationWorker의 공통 로직을 제공합니다.
    하위 클래스는 _execute_migration() 메서드를 구현해야 합니다.
    """

    # 공통 시그널
    progress = Signal(dict)  # 진행 상황
    log = Signal(str, str)  # 메시지, 레벨
    error = Signal(str)  # 오류 메시지
    finished = Signal()  # 완료

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
        self.resume = resume

        # 공통 상태 필드
        self.is_running = False
        self.is_paused = False
        self.current_partition_index = 0
        self.total_rows_processed = 0
        self.start_time = None

        # 공통 매니저
        self.history_manager = HistoryManager()
        self.checkpoint_manager = CheckpointManager()

    def run(self):
        """워커 실행 (템플릿 메서드)

        공통 초기화와 마무리 로직을 수행하고,
        하위 클래스의 _execute_migration()을 호출합니다.
        """
        self.is_running = True
        self.start_time = time.time()

        # 세션 ID 초기화
        session_id = enhanced_logger.generate_session_id()
        log_emitter.logger.set_session_id(session_id)

        try:
            # 하위 클래스 구현 실행
            self._execute_migration()

            # 정상 완료
            if self.is_running:
                self.finished.emit()

        except Exception as e:
            error_msg = str(e)
            log_emitter.emit_log("ERROR", f"마이그레이션 오류: {error_msg}")
            self.error.emit(error_msg)

    @abstractmethod
    def _execute_migration(self):
        """마이그레이션 실행 (하위 클래스에서 구현)

        각 워커의 고유한 마이그레이션 로직을 구현합니다.
        - MigrationWorker: INSERT 기반 마이그레이션
        - CopyMigrationWorker: COPY 기반 마이그레이션
        """
        pass

    def pause(self):
        """마이그레이션 일시정지"""
        self.is_paused = True
        self.log.emit("마이그레이션 일시정지", "INFO")
        log_emitter.emit_log("INFO", "마이그레이션 일시정지")

    def resume(self):
        """마이그레이션 재개"""
        self.is_paused = False
        self.log.emit("마이그레이션 재개", "INFO")
        log_emitter.emit_log("INFO", "마이그레이션 재개")

    def stop(self):
        """마이그레이션 중지"""
        self.is_running = False
        self.is_paused = False
        self.log.emit("마이그레이션 중지 요청", "WARNING")
        log_emitter.emit_log("WARNING", "마이그레이션 중지 요청")

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
        eta_seconds = 0
        if speed > 0:
            eta_seconds = estimated_remaining_rows / speed

        return {
            "elapsed_seconds": elapsed,
            "total_rows_processed": self.total_rows_processed,
            "speed": speed,
            "eta_seconds": eta_seconds,
        }
