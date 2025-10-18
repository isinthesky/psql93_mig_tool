"""
MigrationViewModel 테스트
"""

import pytest
from unittest.mock import Mock

from src.ui.viewmodels.migration_viewmodel import MigrationViewModel


class TestMigrationViewModel:
    """MigrationViewModel 테스트"""

    @pytest.fixture
    def viewmodel(self):
        """MigrationViewModel 픽스처"""
        mock_profile = Mock(id=1, name='Test Profile')
        return MigrationViewModel(profile=mock_profile)

    def test_initial_state(self, viewmodel):
        """초기 상태 확인"""
        assert viewmodel.partition_count == 0
        assert viewmodel.source_connected is False
        assert viewmodel.target_connected is False
        assert viewmodel.both_connected is False
        assert viewmodel.is_running is False
        assert viewmodel.is_paused is False
        assert viewmodel.can_start is False

    def test_set_partitions_emits_signals(self, viewmodel, qtbot):
        """파티션 설정 시 시그널 발행 확인"""
        # Given: 파티션 목록
        partitions = ['partition_1', 'partition_2', 'partition_3']

        # When: 파티션 설정
        with qtbot.waitSignal(viewmodel.partition_list_changed, timeout=1000) as list_blocker:
            with qtbot.waitSignal(viewmodel.partition_count_changed, timeout=1000) as count_blocker:
                viewmodel.set_partitions(partitions, "총 3개 파티션")

        # Then: 시그널 발행 및 상태 업데이트 확인
        assert list_blocker.args == [partitions]
        assert count_blocker.args == [3, "총 3개 파티션"]
        assert viewmodel.partition_count == 3
        assert viewmodel.get_partitions() == partitions

    def test_update_progress_emits_signal(self, viewmodel, qtbot):
        """진행률 업데이트 시 시그널 발행 확인"""
        # Given: 진행률 데이터
        progress_data = {
            'total_progress': 50,
            'completed_partitions': 5,
            'total_partitions': 10,
            'current_partition': 'partition_6',
            'current_progress': 30,
            'current_rows': 150000,
            'speed': 5000
        }

        # When: 진행률 업데이트
        with qtbot.waitSignal(viewmodel.progress_changed, timeout=1000) as blocker:
            viewmodel.update_progress(progress_data)

        # Then: 시그널 발행 확인
        emitted_data = blocker.args[0]
        assert emitted_data['total_progress'] == 50
        assert emitted_data['completed_partitions'] == 5
        assert emitted_data['current_partition'] == 'partition_6'

    def test_update_performance_emits_signal(self, viewmodel, qtbot):
        """성능 지표 업데이트 시 시그널 발행 확인"""
        # Given: 성능 지표 데이터
        performance_data = {
            'instant_rows_per_sec': 10000,
            'instant_mb_per_sec': 2.5,
            'eta_time': '00:15:30',
            'elapsed_time': '00:05:00'
        }

        # When: 성능 지표 업데이트
        with qtbot.waitSignal(viewmodel.performance_changed, timeout=1000) as blocker:
            viewmodel.update_performance(performance_data)

        # Then: 시그널 발행 확인
        emitted_data = blocker.args[0]
        assert emitted_data['instant_rows_per_sec'] == 10000
        assert emitted_data['instant_mb_per_sec'] == 2.5

    def test_update_connection_status_emits_signal(self, viewmodel, qtbot):
        """연결 상태 업데이트 시 시그널 발행 확인"""
        # When: 소스 DB 연결 상태 업데이트
        with qtbot.waitSignal(viewmodel.connection_status_changed, timeout=1000) as blocker:
            viewmodel.update_connection_status('source', True, '연결됨')

        # Then: 시그널 발행 및 상태 업데이트 확인
        assert blocker.args == ['source', True, '연결됨']
        assert viewmodel.source_connected is True
        assert viewmodel.both_connected is False  # 대상은 아직 미연결

    def test_both_connected_property(self, viewmodel):
        """양쪽 DB 연결 상태 확인"""
        # Given: 소스만 연결
        viewmodel.update_connection_status('source', True, '연결됨')
        assert viewmodel.both_connected is False

        # When: 대상도 연결
        viewmodel.update_connection_status('target', True, '연결됨')

        # Then: 양쪽 모두 연결됨
        assert viewmodel.both_connected is True

    def test_start_migration_success(self, viewmodel, qtbot):
        """마이그레이션 시작 성공 케이스"""
        # Given: 연결 상태와 파티션 설정
        viewmodel.update_connection_status('source', True, '연결됨')
        viewmodel.update_connection_status('target', True, '연결됨')
        viewmodel.set_partitions(['partition_1', 'partition_2'])

        # When: 마이그레이션 시작
        with qtbot.waitSignal(viewmodel.migration_started, timeout=1000):
            viewmodel.start_migration()

        # Then: 실행 상태 확인
        assert viewmodel.is_running is True
        assert viewmodel.is_paused is False

    def test_start_migration_fails_without_connection(self, viewmodel, qtbot):
        """연결 없이 시작 시도 시 실패"""
        # Given: 파티션만 설정 (연결 안 됨)
        viewmodel.set_partitions(['partition_1'])

        # When: 마이그레이션 시작 시도
        with qtbot.waitSignal(viewmodel.error_occurred, timeout=1000) as blocker:
            viewmodel.start_migration()

        # Then: 오류 발생, 실행 안 됨
        assert "연결되지 않았습니다" in blocker.args[0]
        assert viewmodel.is_running is False

    def test_start_migration_fails_without_partitions(self, viewmodel, qtbot):
        """파티션 없이 시작 시도 시 실패"""
        # Given: 연결만 설정 (파티션 없음)
        viewmodel.update_connection_status('source', True, '연결됨')
        viewmodel.update_connection_status('target', True, '연결됨')

        # When: 마이그레이션 시작 시도
        with qtbot.waitSignal(viewmodel.error_occurred, timeout=1000) as blocker:
            viewmodel.start_migration()

        # Then: 오류 발생
        assert "파티션이 없습니다" in blocker.args[0]
        assert viewmodel.is_running is False

    def test_pause_migration(self, viewmodel, qtbot):
        """마이그레이션 일시정지"""
        # Given: 실행 중인 상태
        viewmodel.update_connection_status('source', True, '연결됨')
        viewmodel.update_connection_status('target', True, '연결됨')
        viewmodel.set_partitions(['partition_1'])
        viewmodel.start_migration()

        # When: 일시정지
        with qtbot.waitSignal(viewmodel.migration_paused, timeout=1000):
            viewmodel.pause_migration()

        # Then: 일시정지 상태 확인
        assert viewmodel.is_running is True
        assert viewmodel.is_paused is True

    def test_resume_migration(self, viewmodel, qtbot):
        """마이그레이션 재개"""
        # Given: 일시정지된 상태
        viewmodel.update_connection_status('source', True, '연결됨')
        viewmodel.update_connection_status('target', True, '연결됨')
        viewmodel.set_partitions(['partition_1'])
        viewmodel.start_migration()
        viewmodel.pause_migration()

        # When: 재개
        with qtbot.waitSignal(viewmodel.migration_resumed, timeout=1000):
            viewmodel.resume_migration()

        # Then: 실행 상태로 복귀
        assert viewmodel.is_running is True
        assert viewmodel.is_paused is False

    def test_complete_migration(self, viewmodel, qtbot):
        """마이그레이션 완료"""
        # Given: 실행 중인 상태
        viewmodel.update_connection_status('source', True, '연결됨')
        viewmodel.update_connection_status('target', True, '연결됨')
        viewmodel.set_partitions(['partition_1'])
        viewmodel.start_migration()

        # When: 완료
        with qtbot.waitSignal(viewmodel.migration_completed, timeout=1000):
            viewmodel.complete_migration()

        # Then: 종료 상태 확인
        assert viewmodel.is_running is False
        assert viewmodel.is_paused is False

    def test_fail_migration(self, viewmodel, qtbot):
        """마이그레이션 실패"""
        # Given: 실행 중인 상태
        viewmodel.update_connection_status('source', True, '연결됨')
        viewmodel.update_connection_status('target', True, '연결됨')
        viewmodel.set_partitions(['partition_1'])
        viewmodel.start_migration()

        # When: 실패
        with qtbot.waitSignal(viewmodel.migration_failed, timeout=1000) as blocker:
            viewmodel.fail_migration("테스트 오류 발생")

        # Then: 종료 상태 및 오류 메시지 확인
        assert blocker.args == ["테스트 오류 발생"]
        assert viewmodel.is_running is False

    def test_can_start_property(self, viewmodel):
        """시작 가능 여부 속성 테스트"""
        # Given: 초기 상태 (시작 불가)
        assert viewmodel.can_start is False

        # When: 연결 설정
        viewmodel.update_connection_status('source', True, '연결됨')
        viewmodel.update_connection_status('target', True, '연결됨')
        assert viewmodel.can_start is False  # 파티션 없음

        # When: 파티션 설정
        viewmodel.set_partitions(['partition_1'])
        assert viewmodel.can_start is True  # 시작 가능

        # When: 시작 후
        viewmodel.start_migration()
        assert viewmodel.can_start is False  # 이미 실행 중
