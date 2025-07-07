"""
마이그레이션 성능 지표 수집 및 계산
"""
import time
from typing import Dict, Optional
from datetime import datetime, timedelta


class PerformanceMetrics:
    """실시간 성능 지표 추적 클래스"""
    
    def __init__(self):
        self.start_time = time.time()
        self.last_update_time = self.start_time
        
        # 누적 통계
        self.total_rows = 0
        self.total_bytes = 0
        self.total_partitions = 0
        self.completed_partitions = 0
        
        # 현재 파티션 정보
        self.current_partition = None
        self.current_partition_rows = 0
        self.current_partition_total_rows = 0
        self.current_partition_start_time = None
        
        # 순간 속도 계산을 위한 윈도우
        self.window_size = 5  # 5초 윈도우
        self.row_history = []  # (timestamp, rows) 튜플 리스트
        self.byte_history = []  # (timestamp, bytes) 튜플 리스트
        
    def start_partition(self, partition_name: str, total_rows: int) -> None:
        """새 파티션 처리 시작"""
        self.current_partition = partition_name
        self.current_partition_rows = 0
        self.current_partition_total_rows = total_rows
        self.current_partition_start_time = time.time()
        
    def update(self, rows: int, bytes_transferred: int) -> None:
        """진행 상황 업데이트"""
        current_time = time.time()
        
        # 누적 통계 업데이트
        self.total_rows += rows
        self.total_bytes += bytes_transferred
        self.current_partition_rows += rows
        
        # 히스토리에 추가 (윈도우 기반 속도 계산용)
        self.row_history.append((current_time, self.total_rows))
        self.byte_history.append((current_time, self.total_bytes))
        
        # 오래된 히스토리 제거
        cutoff_time = current_time - self.window_size
        self.row_history = [(t, r) for t, r in self.row_history if t > cutoff_time]
        self.byte_history = [(t, b) for t, b in self.byte_history if t > cutoff_time]
        
        self.last_update_time = current_time
        
    def complete_partition(self) -> None:
        """현재 파티션 완료"""
        self.completed_partitions += 1
        self.current_partition = None
        self.current_partition_rows = 0
        self.current_partition_total_rows = 0
        
    def get_stats(self) -> Dict[str, any]:
        """현재 성능 통계 반환"""
        current_time = time.time()
        elapsed_seconds = current_time - self.start_time
        
        # 전체 평균 속도
        avg_rows_per_sec = self.total_rows / elapsed_seconds if elapsed_seconds > 0 else 0
        avg_mb_per_sec = (self.total_bytes / (1024 * 1024)) / elapsed_seconds if elapsed_seconds > 0 else 0
        
        # 순간 속도 (최근 5초)
        instant_rows_per_sec = self._calculate_instant_rate(self.row_history)
        instant_mb_per_sec = self._calculate_instant_rate(self.byte_history) / (1024 * 1024)
        
        # 현재 파티션 진행률
        partition_progress = 0
        if self.current_partition_total_rows > 0:
            partition_progress = (self.current_partition_rows / self.current_partition_total_rows) * 100
            
        # 전체 진행률
        total_progress = 0
        if self.total_partitions > 0:
            total_progress = (self.completed_partitions / self.total_partitions) * 100
            
        # 예상 완료 시간
        eta_seconds = self._calculate_eta(instant_rows_per_sec)
        
        return {
            # 시간 정보
            'elapsed_seconds': elapsed_seconds,
            'elapsed_time': str(timedelta(seconds=int(elapsed_seconds))),
            'eta_seconds': eta_seconds,
            'eta_time': str(timedelta(seconds=int(eta_seconds))) if eta_seconds > 0 else "계산중...",
            
            # 진행 상황
            'total_rows': self.total_rows,
            'total_mb': self.total_bytes / (1024 * 1024),
            'completed_partitions': self.completed_partitions,
            'total_partitions': self.total_partitions,
            'total_progress': total_progress,
            'partition_progress': partition_progress,
            
            # 속도 (평균)
            'avg_rows_per_sec': avg_rows_per_sec,
            'avg_mb_per_sec': avg_mb_per_sec,
            
            # 속도 (순간)
            'instant_rows_per_sec': instant_rows_per_sec,
            'instant_mb_per_sec': instant_mb_per_sec,
            
            # 현재 파티션
            'current_partition': self.current_partition,
            'current_partition_rows': self.current_partition_rows,
            'current_partition_total_rows': self.current_partition_total_rows,
        }
        
    def _calculate_instant_rate(self, history: list) -> float:
        """순간 속도 계산 (최근 윈도우 기준)"""
        if len(history) < 2:
            return 0.0
            
        # 가장 오래된 것과 최신 것의 차이로 계산
        oldest_time, oldest_value = history[0]
        latest_time, latest_value = history[-1]
        
        time_diff = latest_time - oldest_time
        value_diff = latest_value - oldest_value
        
        if time_diff > 0:
            return value_diff / time_diff
        return 0.0
        
    def _calculate_eta(self, current_rate: float) -> float:
        """예상 완료 시간 계산"""
        if current_rate <= 0 or not self.current_partition_total_rows:
            return 0
            
        # 현재 파티션의 남은 행
        remaining_in_current = self.current_partition_total_rows - self.current_partition_rows
        
        # 남은 파티션들의 예상 행 수 (현재 파티션과 동일하다고 가정)
        remaining_partitions = self.total_partitions - self.completed_partitions - 1
        estimated_remaining_rows = remaining_in_current + (remaining_partitions * self.current_partition_total_rows)
        
        return estimated_remaining_rows / current_rate
        
    def format_speed(self, rows_per_sec: float, mb_per_sec: float) -> str:
        """속도를 보기 좋은 형식으로 포맷"""
        if rows_per_sec >= 1000000:
            rows_str = f"{rows_per_sec/1000000:.1f}M rows/sec"
        elif rows_per_sec >= 1000:
            rows_str = f"{rows_per_sec/1000:.1f}K rows/sec"
        else:
            rows_str = f"{rows_per_sec:.0f} rows/sec"
            
        mb_str = f"{mb_per_sec:.1f} MB/sec"
        
        return f"{rows_str}, {mb_str}"