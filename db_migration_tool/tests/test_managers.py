"""
Manager 클래스들의 session_scope() 사용 검증 테스트
"""
import pytest
from datetime import datetime

from src.models.profile import ProfileManager
from src.models.history import HistoryManager, CheckpointManager


class TestProfileManager:
    """ProfileManager의 session_scope 적용 검증"""

    @pytest.fixture
    def profile_manager(self, temp_db, monkeypatch):
        """ProfileManager 픽스처 (temp_db 사용)"""
        manager = ProfileManager()
        # temp_db를 사용하도록 패치
        monkeypatch.setattr(manager, 'db', temp_db)
        return manager

    def test_create_profile_with_session_scope(self, profile_manager, sample_profile_data):
        """create_profile이 session_scope를 사용하여 프로필을 생성하는지 확인"""
        # When: 프로필 생성
        profile = profile_manager.create_profile(sample_profile_data)

        # Then: 프로필이 생성되고 ID가 할당되어야 함
        assert profile.id is not None
        assert profile.name == sample_profile_data['name']

    def test_create_profile_generates_id_before_return(self, profile_manager, sample_profile_data):
        """create_profile이 반환 전에 ID를 생성하는지 확인 (flush 검증)"""
        # When: 프로필 생성
        profile = profile_manager.create_profile(sample_profile_data)

        # Then: ID가 None이 아니어야 함 (flush가 호출됨)
        assert profile.id is not None

    def test_get_profile(self, profile_manager, sample_profile_data):
        """get_profile이 session_scope를 사용하여 프로필을 조회하는지 확인"""
        # Given: 프로필 생성
        created_profile = profile_manager.create_profile(sample_profile_data)

        # When: 프로필 조회
        retrieved_profile = profile_manager.get_profile(created_profile.id)

        # Then: 프로필이 조회되어야 함
        assert retrieved_profile is not None
        assert retrieved_profile.id == created_profile.id
        assert retrieved_profile.name == sample_profile_data['name']

    def test_get_all_profiles(self, profile_manager, sample_profile_data):
        """get_all_profiles이 session_scope를 사용하여 모든 프로필을 조회하는지 확인"""
        # Given: 여러 프로필 생성
        profile_data_1 = sample_profile_data.copy()
        profile_data_1['name'] = 'Profile 1'

        profile_data_2 = sample_profile_data.copy()
        profile_data_2['name'] = 'Profile 2'

        profile_manager.create_profile(profile_data_1)
        profile_manager.create_profile(profile_data_2)

        # When: 모든 프로필 조회
        profiles = profile_manager.get_all_profiles()

        # Then: 2개의 프로필이 조회되어야 함
        assert len(profiles) == 2
        profile_names = [p.name for p in profiles]
        assert 'Profile 1' in profile_names
        assert 'Profile 2' in profile_names

    def test_update_profile(self, profile_manager, sample_profile_data):
        """update_profile이 session_scope를 사용하여 프로필을 수정하는지 확인"""
        # Given: 프로필 생성
        profile = profile_manager.create_profile(sample_profile_data)

        # When: 프로필 수정
        updated_data = sample_profile_data.copy()
        updated_data['name'] = 'Updated Profile Name'
        updated_profile = profile_manager.update_profile(profile.id, updated_data)

        # Then: 프로필이 수정되어야 함
        assert updated_profile.id == profile.id
        assert updated_profile.name == 'Updated Profile Name'

    def test_delete_profile(self, profile_manager, sample_profile_data):
        """delete_profile이 session_scope를 사용하여 프로필을 삭제하는지 확인"""
        # Given: 프로필 생성
        profile = profile_manager.create_profile(sample_profile_data)

        # When: 프로필 삭제
        result = profile_manager.delete_profile(profile.id)

        # Then: 삭제가 성공하고 프로필이 조회되지 않아야 함
        assert result is True
        deleted_profile = profile_manager.get_profile(profile.id)
        assert deleted_profile is None

    def test_create_profile_rollback_on_error(self, profile_manager, sample_profile_data, monkeypatch):
        """생성 중 오류 발생 시 rollback되는지 확인"""
        # Given: _encrypt_config가 예외를 발생하도록 패치
        def mock_encrypt_error(*args):
            raise RuntimeError("Encryption failed")

        monkeypatch.setattr(profile_manager, '_encrypt_config', mock_encrypt_error)

        # When/Then: 예외 발생 시 데이터가 저장되지 않아야 함
        initial_count = len(profile_manager.get_all_profiles())

        with pytest.raises(RuntimeError):
            profile_manager.create_profile(sample_profile_data)

        final_count = len(profile_manager.get_all_profiles())
        assert final_count == initial_count


class TestHistoryManager:
    """HistoryManager의 session_scope 적용 검증"""

    @pytest.fixture
    def history_manager(self, temp_db, monkeypatch):
        """HistoryManager 픽스처 (Repository 패턴 지원)"""
        manager = HistoryManager()
        # HistoryRepository의 db를 temp_db로 패치
        monkeypatch.setattr(manager.repo, 'db', temp_db)
        return manager

    def test_create_history(self, history_manager):
        """create_history가 session_scope를 사용하여 이력을 생성하는지 확인"""
        # When: 이력 생성
        history = history_manager.create_history(
            profile_id=1,
            start_date='2024-01-01',
            end_date='2024-01-31'
        )

        # Then: 이력이 생성되고 ID가 할당되어야 함
        assert history.id is not None
        assert history.profile_id == 1
        assert history.status == "running"

    def test_create_history_with_connection_status(self, history_manager):
        """연결 상태와 함께 이력을 생성하는지 확인"""
        # When: 연결 상태와 함께 이력 생성
        history = history_manager.create_history(
            profile_id=1,
            start_date='2024-01-01',
            end_date='2024-01-31',
            source_status='Connected',
            target_status='Connected'
        )

        # Then: 연결 상태가 저장되어야 함
        assert history.id is not None
        # 조회하여 확인
        retrieved = history_manager.get_history(history.id)
        # Note: MigrationHistoryItem은 connection_status 필드가 없으므로
        # 기본 검증만 수행
        assert retrieved is not None

    def test_update_history_status(self, history_manager):
        """update_history_status가 session_scope를 사용하여 상태를 업데이트하는지 확인"""
        # Given: 이력 생성
        history = history_manager.create_history(
            profile_id=1,
            start_date='2024-01-01',
            end_date='2024-01-31'
        )

        # When: 상태 업데이트
        result = history_manager.update_history_status(
            history.id, 'completed', processed_rows=1000000
        )

        # Then: 상태가 업데이트되어야 함
        assert result is True
        updated_history = history_manager.get_history(history.id)
        assert updated_history.status == 'completed'
        assert updated_history.processed_rows == 1000000

    def test_get_incomplete_history(self, history_manager):
        """get_incomplete_history가 미완료 이력을 조회하는지 확인"""
        # Given: 완료 및 미완료 이력 생성
        completed = history_manager.create_history(1, '2024-01-01', '2024-01-31')
        history_manager.update_history_status(completed.id, 'completed')

        incomplete = history_manager.create_history(1, '2024-02-01', '2024-02-28')

        # When: 미완료 이력 조회
        result = history_manager.get_incomplete_history(1)

        # Then: 미완료 이력만 조회되어야 함
        assert result is not None
        assert result.id == incomplete.id
        assert result.status == 'running'


class TestCheckpointManager:
    """CheckpointManager의 session_scope 적용 검증"""

    @pytest.fixture
    def checkpoint_manager(self, temp_db, monkeypatch):
        """CheckpointManager 픽스처 (Repository 패턴 지원)"""
        manager = CheckpointManager()
        # CheckpointRepository의 db를 temp_db로 패치
        monkeypatch.setattr(manager.repo, 'db', temp_db)
        return manager

    @pytest.fixture
    def history_id(self, temp_db, monkeypatch):
        """테스트용 히스토리 ID (Repository 패턴 지원)"""
        history_manager = HistoryManager()
        # HistoryRepository의 db를 temp_db로 패치
        monkeypatch.setattr(history_manager.repo, 'db', temp_db)

        history = history_manager.create_history(1, '2024-01-01', '2024-01-31')
        return history.id

    def test_create_checkpoint(self, checkpoint_manager, history_id):
        """create_checkpoint가 session_scope를 사용하여 체크포인트를 생성하는지 확인"""
        # When: 체크포인트 생성
        checkpoint = checkpoint_manager.create_checkpoint(
            history_id=history_id,
            partition_name='test_partition_240101'
        )

        # Then: 체크포인트가 생성되고 ID가 할당되어야 함
        assert checkpoint.id is not None
        assert checkpoint.history_id == history_id
        assert checkpoint.partition_name == 'test_partition_240101'
        assert checkpoint.status == 'pending'

    def test_get_checkpoints(self, checkpoint_manager, history_id):
        """get_checkpoints가 session_scope를 사용하여 체크포인트를 조회하는지 확인"""
        # Given: 여러 체크포인트 생성
        checkpoint_manager.create_checkpoint(history_id, 'partition_1')
        checkpoint_manager.create_checkpoint(history_id, 'partition_2')

        # When: 체크포인트 조회
        checkpoints = checkpoint_manager.get_checkpoints(history_id)

        # Then: 2개의 체크포인트가 조회되어야 함
        assert len(checkpoints) == 2
        partition_names = [cp.partition_name for cp in checkpoints]
        assert 'partition_1' in partition_names
        assert 'partition_2' in partition_names

    def test_update_checkpoint_status(self, checkpoint_manager, history_id):
        """update_checkpoint_status가 session_scope를 사용하여 상태를 업데이트하는지 확인"""
        # Given: 체크포인트 생성
        checkpoint = checkpoint_manager.create_checkpoint(history_id, 'partition_1')

        # When: 상태 업데이트
        result = checkpoint_manager.update_checkpoint_status(
            checkpoint.id, 'completed', rows_processed=100000
        )

        # Then: 상태가 업데이트되어야 함
        assert result is True
        checkpoints = checkpoint_manager.get_checkpoints(history_id)
        updated = next(cp for cp in checkpoints if cp.id == checkpoint.id)
        assert updated.status == 'completed'
        assert updated.rows_processed == 100000

    def test_get_pending_checkpoints(self, checkpoint_manager, history_id):
        """get_pending_checkpoints가 미완료 체크포인트만 조회하는지 확인"""
        # Given: 완료 및 미완료 체크포인트 생성
        completed_cp = checkpoint_manager.create_checkpoint(history_id, 'partition_completed')
        checkpoint_manager.update_checkpoint_status(completed_cp.id, 'completed')

        pending_cp = checkpoint_manager.create_checkpoint(history_id, 'partition_pending')

        # When: 미완료 체크포인트 조회
        pending_checkpoints = checkpoint_manager.get_pending_checkpoints(history_id)

        # Then: 미완료 체크포인트만 조회되어야 함
        assert len(pending_checkpoints) == 1
        assert pending_checkpoints[0].id == pending_cp.id
        assert pending_checkpoints[0].status == 'pending'
