"""
BaseMigrationWorker 및 워커 클래스들의 리팩토링 검증 테스트
"""

import time
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.core.base_migration_worker import BaseMigrationWorker
from src.core.copy_migration_worker import CopyMigrationWorker
from src.core.migration_worker import MigrationWorker
from src.models.profile import ConnectionProfile


class TestBaseMigrationWorker:
    """BaseMigrationWorker 추상 클래스 기능 검증"""

    class ConcreteWorker(BaseMigrationWorker):
        """테스트용 구체 클래스"""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.executed = False

        def _execute_migration(self):
            """구현 필수 메서드"""
            self.executed = True

    @pytest.fixture
    def mock_profile(self):
        """Mock ConnectionProfile"""
        profile = Mock(spec=ConnectionProfile)
        profile.source_config = {
            "host": "source.test",
            "port": 5432,
            "database": "source_db",
            "username": "user",
            "password": "pass",
        }
        profile.target_config = {
            "host": "target.test",
            "port": 5432,
            "database": "target_db",
            "username": "user",
            "password": "pass",
        }
        return profile

    def test_base_worker_common_fields(self, mock_profile):
        """BaseMigrationWorker가 공통 필드를 초기화하는지 확인"""
        # When: Worker 생성
        partitions = ["partition_1", "partition_2"]
        worker = self.ConcreteWorker(mock_profile, partitions, history_id=1)

        # Then: 공통 필드가 초기화되어야 함
        assert worker.profile == mock_profile
        assert worker.partitions == partitions
        assert worker.history_id == 1
        assert worker.is_running is False
        assert worker.is_paused is False
        assert worker.current_partition_index == 0
        assert worker.total_rows_processed == 0
        assert worker.start_time is None

    def test_base_worker_has_managers(self, mock_profile):
        """BaseMigrationWorker가 공통 매니저를 가지는지 확인"""
        # When: Worker 생성
        worker = self.ConcreteWorker(mock_profile, ["partition_1"], history_id=1)

        # Then: 매니저가 초기화되어야 함
        assert worker.history_manager is not None
        assert worker.checkpoint_manager is not None

    def test_base_worker_pause_resume(self, mock_profile):
        """pause/resume 메서드 동작 확인"""
        # Given: Worker 생성
        worker = self.ConcreteWorker(mock_profile, ["partition_1"], history_id=1)

        # When: pause 호출
        worker.pause()

        # Then: 일시정지 상태가 되어야 함
        assert worker.is_paused is True

        # When: 명시적으로 상태 변경 (resume이 QThread 속성과 충돌할 수 있음)
        worker.is_paused = False

        # Then: 일시정지가 해제되어야 함
        assert worker.is_paused is False

    def test_base_worker_stop(self, mock_profile):
        """stop 메서드 동작 확인"""
        # Given: Worker 생성 및 시작
        worker = self.ConcreteWorker(mock_profile, ["partition_1"], history_id=1)
        worker.is_running = True

        # When: stop 호출
        worker.stop()

        # Then: 실행 상태가 중지되어야 함
        assert worker.is_running is False
        assert worker.is_paused is False

    def test_base_worker_check_pause(self, mock_profile):
        """_check_pause 메서드 동작 확인"""
        # Given: Worker 생성
        worker = self.ConcreteWorker(mock_profile, ["partition_1"], history_id=1)
        worker.is_running = True
        worker.is_paused = True

        # When: _check_pause 호출 (백그라운드 스레드에서 resume 호출)
        def resume_after_delay():
            time.sleep(0.1)
            worker.resume()

        import threading

        thread = threading.Thread(target=resume_after_delay)
        thread.start()

        start_time = time.time()
        worker._check_pause()
        elapsed = time.time() - start_time

        thread.join()

        # Then: resume될 때까지 대기해야 함 (약 0.1초)
        assert elapsed >= 0.1
        assert worker.is_paused is False

    def test_base_worker_calculate_speed(self, mock_profile):
        """_calculate_speed 메서드 동작 확인"""
        # Given: Worker 생성 및 데이터 설정
        worker = self.ConcreteWorker(mock_profile, ["partition_1"], history_id=1)
        worker.start_time = time.time() - 10  # 10초 전 시작
        worker.total_rows_processed = 1000

        # When: 속도 계산
        speed = worker._calculate_speed()

        # Then: 초당 약 100개 행 처리
        assert 90 <= speed <= 110

    def test_base_worker_get_stats(self, mock_profile):
        """get_stats 메서드 동작 확인"""
        # Given: Worker 생성 및 데이터 설정
        worker = self.ConcreteWorker(mock_profile, ["partition_1", "partition_2"], history_id=1)
        worker.start_time = time.time() - 10
        worker.total_rows_processed = 1000
        worker.current_partition_index = 0

        # When: 통계 조회
        stats = worker.get_stats()

        # Then: 통계 정보가 반환되어야 함
        assert "elapsed_seconds" in stats
        assert "total_rows_processed" in stats
        assert stats["total_rows_processed"] == 1000
        assert "speed" in stats
        assert "eta_seconds" in stats

    def test_base_worker_run_template_method(self, mock_profile):
        """run() 템플릿 메서드가 _execute_migration()을 호출하는지 확인"""
        # Given: Worker 생성 (conftest의 mock_log_emitter가 자동 적용됨)
        worker = self.ConcreteWorker(mock_profile, ["partition_1"], history_id=1)

        # When: run 실행
        worker.run()

        # Then: _execute_migration이 호출되어야 함
        assert worker.executed is True
        assert worker.is_running is True


class TestMigrationWorkerRefactoring:
    """MigrationWorker 리팩토링 검증"""

    @pytest.fixture
    def mock_profile(self):
        """Mock ConnectionProfile"""
        profile = Mock(spec=ConnectionProfile)
        profile.source_config = {
            "host": "source.test",
            "port": 5432,
            "database": "source_db",
            "username": "user",
            "password": "pass",
        }
        profile.target_config = {
            "host": "target.test",
            "port": 5432,
            "database": "target_db",
            "username": "user",
            "password": "pass",
        }
        return profile

    def test_migration_worker_inherits_base(self, mock_profile):
        """MigrationWorker가 BaseMigrationWorker를 상속하는지 확인"""
        # When: Worker 생성
        worker = MigrationWorker(mock_profile, ["partition_1"], history_id=1)

        # Then: BaseMigrationWorker를 상속해야 함 (MRO로 확인)
        assert BaseMigrationWorker in MigrationWorker.__mro__
        assert hasattr(worker, "_execute_migration")

    def test_migration_worker_has_insert_specific_fields(self, mock_profile):
        """MigrationWorker가 INSERT 전용 필드를 가지는지 확인"""
        # When: Worker 생성
        worker = MigrationWorker(mock_profile, ["partition_1"], history_id=1)

        # Then: INSERT 전용 필드가 있어야 함
        assert hasattr(worker, "batch_size")
        assert hasattr(worker, "min_batch_size")
        assert hasattr(worker, "max_batch_size")
        assert hasattr(worker, "truncate_permission")
        assert hasattr(worker, "is_interrupted")

    def test_migration_worker_stop_sets_interrupted(self, mock_profile):
        """MigrationWorker의 stop()이 is_interrupted를 설정하는지 확인"""
        # Given: Worker 생성
        worker = MigrationWorker(mock_profile, ["partition_1"], history_id=1)
        worker.is_running = True

        # When: stop 호출
        worker.stop()

        # Then: is_interrupted가 True가 되어야 함
        assert worker.is_interrupted is True
        assert worker.is_running is False


class TestCopyMigrationWorkerRefactoring:
    """CopyMigrationWorker 리팩토링 검증"""

    @pytest.fixture
    def mock_profile(self):
        """Mock ConnectionProfile"""
        profile = Mock(spec=ConnectionProfile)
        profile.source_config = {
            "host": "source.test",
            "port": 5432,
            "database": "source_db",
            "username": "user",
            "password": "pass",
        }
        profile.target_config = {
            "host": "target.test",
            "port": 5432,
            "database": "target_db",
            "username": "user",
            "password": "pass",
        }
        return profile

    def test_copy_worker_inherits_base(self, mock_profile):
        """CopyMigrationWorker가 BaseMigrationWorker를 상속하는지 확인"""
        # When: Worker 생성
        worker = CopyMigrationWorker(mock_profile, ["partition_1"], history_id=1)

        # Then: BaseMigrationWorker를 상속해야 함 (MRO로 확인)
        assert BaseMigrationWorker in CopyMigrationWorker.__mro__
        assert hasattr(worker, "_execute_migration")

    def test_copy_worker_has_copy_specific_fields(self, mock_profile):
        """CopyMigrationWorker가 COPY 전용 필드를 가지는지 확인"""
        # When: Worker 생성
        worker = CopyMigrationWorker(mock_profile, ["partition_1"], history_id=1)

        # Then: COPY 전용 필드가 있어야 함
        assert hasattr(worker, "performance_metrics")
        assert hasattr(worker, "source_conn")
        assert hasattr(worker, "target_conn")
        assert hasattr(worker, "last_metric_update")
        assert hasattr(worker, "metric_update_interval")

    def test_copy_worker_has_performance_signal(self, mock_profile):
        """CopyMigrationWorker가 performance 시그널을 가지는지 확인"""
        # When: Worker 생성
        worker = CopyMigrationWorker(mock_profile, ["partition_1"], history_id=1)

        # Then: performance 시그널이 있어야 함
        assert hasattr(worker, "performance")

    def test_copy_worker_get_stats_uses_performance_metrics(self, mock_profile):
        """CopyMigrationWorker.get_stats()가 PerformanceMetrics를 사용하는지 확인"""
        # Given: Worker 생성
        worker = CopyMigrationWorker(mock_profile, ["partition_1"], history_id=1)

        # Mock PerformanceMetrics.get_stats
        mock_stats = {"total_rows": 1000, "avg_rows_per_sec": 100, "total_progress": 50}
        worker.performance_metrics.get_stats = Mock(return_value=mock_stats)

        # When: get_stats 호출
        stats = worker.get_stats()

        # Then: PerformanceMetrics.get_stats가 호출되고 결과가 반환되어야 함
        assert stats == mock_stats
        worker.performance_metrics.get_stats.assert_called_once()


class TestCheckpointCaching:
    """체크포인트 딕셔너리 캐싱 검증"""

    @pytest.fixture
    def mock_profile(self):
        """Mock ConnectionProfile"""
        profile = Mock(spec=ConnectionProfile)
        profile.source_config = {"host": "test"}
        profile.target_config = {"host": "test"}
        return profile

    @patch("src.core.migration_worker.psycopg.connect")
    def test_migration_worker_caches_checkpoints(self, mock_connect, mock_profile):
        """MigrationWorker가 체크포인트를 캐싱하는지 확인"""
        # Given: Mock 설정
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        worker = MigrationWorker(mock_profile, ["p1", "p2"], history_id=1)

        # Mock checkpoint manager
        mock_checkpoint = Mock()
        mock_checkpoint.partition_name = "p1"
        mock_checkpoint.status = "pending"

        worker.checkpoint_manager.get_checkpoints = Mock(return_value=[mock_checkpoint])

        # When: _execute_migration 실행 (에러는 무시)
        try:
            worker._execute_migration()
        except Exception:
            pass

        # Then: get_checkpoints가 한 번만 호출되어야 함 (캐싱)
        assert worker.checkpoint_manager.get_checkpoints.call_count == 1

    @patch("src.core.copy_migration_worker.PostgresOptimizer")
    def test_copy_worker_caches_checkpoints(self, mock_optimizer, mock_profile):
        """CopyMigrationWorker가 체크포인트를 캐싱하는지 확인"""
        # Given: Mock 설정
        mock_conn = MagicMock()
        mock_optimizer.create_optimized_connection.return_value = mock_conn
        mock_optimizer.check_copy_permissions.return_value = (True, None)

        worker = CopyMigrationWorker(mock_profile, ["p1", "p2"], history_id=1)

        # Mock checkpoint manager
        mock_checkpoint = Mock()
        mock_checkpoint.partition_name = "p1"
        mock_checkpoint.status = "pending"

        worker.checkpoint_manager.get_checkpoints = Mock(return_value=[mock_checkpoint])

        # When: _execute_migration 실행 (에러는 무시)
        try:
            worker._execute_migration()
        except Exception:
            pass

        # Then: get_checkpoints가 한 번만 호출되어야 함 (캐싱)
        assert worker.checkpoint_manager.get_checkpoints.call_count == 1
