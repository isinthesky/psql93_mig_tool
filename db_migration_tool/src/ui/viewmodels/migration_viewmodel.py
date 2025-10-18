"""
MigrationDialog를 위한 ViewModel

마이그레이션 진행 상태와 UI 상태를 관리합니다.
"""

from PySide6.QtCore import Signal
from typing import List, Optional, Dict, Any
from datetime import date

from src.models.profile import ConnectionProfile
from .base_viewmodel import BaseViewModel


class MigrationViewModel(BaseViewModel):
    """마이그레이션 다이얼로그 ViewModel

    마이그레이션 상태 관리와 UI 업데이트 로직을 MigrationDialog에서 분리합니다.
    """

    # 파티션 관련 시그널
    partition_list_changed = Signal(list)  # 파티션 목록 변경 (파티션 이름 리스트)
    partition_count_changed = Signal(int, str)  # 파티션 개수, 메시지

    # 진행률 관련 시그널
    progress_changed = Signal(dict)  # 진행률 데이터 (total, completed, current_partition, etc.)
    performance_changed = Signal(dict)  # 성능 지표 (speed, data_rate, eta, elapsed)

    # 연결 상태 시그널
    connection_status_changed = Signal(str, bool, str)  # DB 타입(source/target), 연결 여부, 메시지

    # 마이그레이션 상태 시그널
    migration_started = Signal()  # 마이그레이션 시작됨
    migration_paused = Signal()  # 일시정지됨
    migration_resumed = Signal()  # 재개됨
    migration_completed = Signal()  # 완료됨
    migration_failed = Signal(str)  # 실패 (오류 메시지)

    def __init__(self, profile: Optional[ConnectionProfile] = None):
        super().__init__()

        self.profile = profile

        # 파티션 상태
        self._partitions: List[str] = []
        self._partition_count: int = 0

        # 진행률 상태
        self._progress_data: Dict[str, Any] = {
            'total_progress': 0,
            'completed_partitions': 0,
            'total_partitions': 0,
            'current_partition': '',
            'current_progress': 0,
            'current_rows': 0,
            'speed': 0
        }

        # 성능 지표 상태
        self._performance_data: Dict[str, Any] = {
            'instant_rows_per_sec': 0,
            'instant_mb_per_sec': 0.0,
            'eta_time': '계산중...',
            'elapsed_time': '00:00:00'
        }

        # 연결 상태
        self._source_connected: bool = False
        self._target_connected: bool = False
        self._source_status_message: str = '확인 중...'
        self._target_status_message: str = '확인 중...'

        # 마이그레이션 실행 상태
        self._is_running: bool = False
        self._is_paused: bool = False

    # --- 파티션 관련 메서드 ---

    def set_partitions(self, partitions: List[str], count_message: str = ""):
        """파티션 목록 설정

        Args:
            partitions: 파티션 이름 리스트
            count_message: 파티션 개수 표시 메시지 (예: "총 30개 파티션")
        """
        self._partitions = partitions
        self._partition_count = len(partitions)

        self.partition_list_changed.emit(partitions)
        self.partition_count_changed.emit(
            self._partition_count,
            count_message or f"총 {self._partition_count}개 파티션"
        )

    def get_partitions(self) -> List[str]:
        """파티션 목록 반환"""
        return self._partitions

    @property
    def partition_count(self) -> int:
        """파티션 개수"""
        return self._partition_count

    # --- 진행률 관련 메서드 ---

    def update_progress(self, progress_data: Dict[str, Any]):
        """진행률 업데이트

        Args:
            progress_data: 진행률 정보 딕셔너리
                - total_progress: 전체 진행률 (0-100)
                - completed_partitions: 완료된 파티션 수
                - total_partitions: 전체 파티션 수
                - current_partition: 현재 처리 중인 파티션 이름
                - current_progress: 현재 파티션 진행률 (0-100)
                - current_rows: 현재 파티션 처리 행 수
                - speed: 처리 속도 (rows/sec)
        """
        self._progress_data.update(progress_data)
        self.progress_changed.emit(self._progress_data)

    def update_performance(self, performance_data: Dict[str, Any]):
        """성능 지표 업데이트

        Args:
            performance_data: 성능 지표 딕셔너리
                - instant_rows_per_sec: 순간 처리 속도 (rows/sec)
                - instant_mb_per_sec: 순간 전송 속도 (MB/sec)
                - eta_time: 예상 완료 시간 (문자열)
                - elapsed_time: 경과 시간 (HH:MM:SS)
        """
        self._performance_data.update(performance_data)
        self.performance_changed.emit(self._performance_data)

    @property
    def progress_data(self) -> Dict[str, Any]:
        """현재 진행률 데이터"""
        return self._progress_data.copy()

    @property
    def performance_data(self) -> Dict[str, Any]:
        """현재 성능 지표 데이터"""
        return self._performance_data.copy()

    # --- 연결 상태 관련 메서드 ---

    def update_connection_status(self, db_type: str, connected: bool, message: str):
        """연결 상태 업데이트

        Args:
            db_type: 'source' 또는 'target'
            connected: 연결 성공 여부
            message: 상태 메시지
        """
        if db_type == 'source':
            self._source_connected = connected
            self._source_status_message = message
        elif db_type == 'target':
            self._target_connected = connected
            self._target_status_message = message

        self.connection_status_changed.emit(db_type, connected, message)

    @property
    def source_connected(self) -> bool:
        """소스 DB 연결 상태"""
        return self._source_connected

    @property
    def target_connected(self) -> bool:
        """대상 DB 연결 상태"""
        return self._target_connected

    @property
    def both_connected(self) -> bool:
        """양쪽 DB 모두 연결됨"""
        return self._source_connected and self._target_connected

    # --- 마이그레이션 실행 상태 관련 메서드 ---

    def start_migration(self):
        """마이그레이션 시작"""
        if not self.both_connected:
            self.handle_error(RuntimeError("양쪽 DB가 모두 연결되지 않았습니다."))
            return

        if self._partition_count == 0:
            self.handle_error(RuntimeError("선택된 파티션이 없습니다."))
            return

        self._is_running = True
        self._is_paused = False
        self.migration_started.emit()

    def pause_migration(self):
        """마이그레이션 일시정지"""
        if self._is_running and not self._is_paused:
            self._is_paused = True
            self.migration_paused.emit()

    def resume_migration(self):
        """마이그레이션 재개"""
        if self._is_running and self._is_paused:
            self._is_paused = False
            self.migration_resumed.emit()

    def complete_migration(self):
        """마이그레이션 완료"""
        self._is_running = False
        self._is_paused = False
        self.migration_completed.emit()

    def fail_migration(self, error_message: str):
        """마이그레이션 실패

        Args:
            error_message: 오류 메시지
        """
        self._is_running = False
        self._is_paused = False
        self.migration_failed.emit(error_message)
        self.handle_error(RuntimeError(error_message))

    @property
    def is_running(self) -> bool:
        """마이그레이션 실행 중 여부"""
        return self._is_running

    @property
    def is_paused(self) -> bool:
        """일시정지 상태 여부"""
        return self._is_paused

    @property
    def can_start(self) -> bool:
        """시작 가능 여부 (양쪽 DB 연결됨 + 파티션 있음 + 실행 중 아님)"""
        return self.both_connected and self._partition_count > 0 and not self._is_running
