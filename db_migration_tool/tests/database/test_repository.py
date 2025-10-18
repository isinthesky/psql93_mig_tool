"""repository.py 단위 테스트"""

from datetime import datetime

import pytest

from src.database.repository import CheckpointRepository, HistoryRepository


class TestHistoryRepository:
    """HistoryRepository 테스트"""

    @pytest.fixture
    def history_repo(self, temp_db):
        """HistoryRepository 픽스처"""
        return HistoryRepository(db=temp_db)

    def test_create_history(self, history_repo):
        """이력 생성 테스트"""
        history = history_repo.create(
            profile_id=1,
            start_date="2025-01-01",
            end_date="2025-01-31",
            started_at=datetime.now(),
            status="running",
        )

        assert history.id is not None
        assert history.profile_id == 1
        assert history.start_date == "2025-01-01"
        assert history.status == "running"

    def test_get_by_id(self, history_repo):
        """ID로 이력 조회 테스트"""
        # Given: 이력 생성
        created = history_repo.create(
            profile_id=1,
            start_date="2025-01-01",
            end_date="2025-01-31",
            started_at=datetime.now(),
            status="running",
        )

        # When: ID로 조회
        found = history_repo.get_by_id(created.id)

        # Then: 동일한 이력이 조회됨
        assert found is not None
        assert found.id == created.id
        assert found.profile_id == created.profile_id

    def test_get_by_id_not_found(self, history_repo):
        """존재하지 않는 ID 조회 테스트"""
        found = history_repo.get_by_id(99999)
        assert found is None

    def test_update_by_id(self, history_repo):
        """이력 업데이트 테스트"""
        # Given: 이력 생성
        history = history_repo.create(
            profile_id=1,
            start_date="2025-01-01",
            end_date="2025-01-31",
            started_at=datetime.now(),
            status="running",
            processed_rows=0,
        )

        # When: 상태 업데이트
        success = history_repo.update_by_id(
            history.id, status="completed", processed_rows=1000, completed_at=datetime.now()
        )

        # Then: 업데이트 성공 및 값 확인
        assert success is True
        updated = history_repo.get_by_id(history.id)
        assert updated.status == "completed"
        assert updated.processed_rows == 1000
        assert updated.completed_at is not None

    def test_update_nonexistent(self, history_repo):
        """존재하지 않는 이력 업데이트 시도"""
        success = history_repo.update_by_id(99999, status="completed")
        assert success is False

    def test_delete_by_id(self, history_repo):
        """이력 삭제 테스트"""
        # Given: 이력 생성
        history = history_repo.create(
            profile_id=1,
            start_date="2025-01-01",
            end_date="2025-01-31",
            started_at=datetime.now(),
            status="running",
        )

        # When: 삭제
        success = history_repo.delete_by_id(history.id)

        # Then: 삭제 성공 및 조회 실패
        assert success is True
        assert history_repo.get_by_id(history.id) is None

    def test_delete_nonexistent(self, history_repo):
        """존재하지 않는 이력 삭제 시도"""
        success = history_repo.delete_by_id(99999)
        assert success is False

    def test_get_all(self, history_repo):
        """전체 이력 조회 테스트"""
        # Given: 여러 이력 생성
        for i in range(3):
            history_repo.create(
                profile_id=i + 1,
                start_date="2025-01-01",
                end_date="2025-01-31",
                started_at=datetime.now(),
                status="running",
            )

        # When: 전체 조회
        all_histories = history_repo.get_all()

        # Then: 3개 조회됨
        assert len(all_histories) == 3

    def test_get_all_desc(self, history_repo):
        """최신순 전체 조회 테스트"""
        # Given: 시간 차이를 두고 이력 생성
        import time

        h1 = history_repo.create(
            profile_id=1,
            start_date="2025-01-01",
            end_date="2025-01-31",
            started_at=datetime.now(),
            status="running",
        )
        time.sleep(0.01)
        h2 = history_repo.create(
            profile_id=2,
            start_date="2025-02-01",
            end_date="2025-02-28",
            started_at=datetime.now(),
            status="running",
        )

        # When: 최신순 조회
        histories = history_repo.get_all_desc()

        # Then: 최신 것이 먼저
        assert len(histories) == 2
        assert histories[0].id == h2.id
        assert histories[1].id == h1.id

    def test_get_incomplete_by_profile(self, history_repo):
        """미완료 이력 조회 테스트"""
        # Given: 완료된 이력과 미완료 이력
        history_repo.create(
            profile_id=1,
            start_date="2025-01-01",
            end_date="2025-01-31",
            started_at=datetime.now(),
            status="completed",
        )

        import time

        time.sleep(0.01)

        running = history_repo.create(
            profile_id=1,
            start_date="2025-02-01",
            end_date="2025-02-28",
            started_at=datetime.now(),
            status="running",
        )

        # When: 미완료 이력 조회
        incomplete = history_repo.get_incomplete_by_profile(1)

        # Then: running 상태의 이력이 반환됨
        assert incomplete is not None
        assert incomplete.id == running.id
        assert incomplete.status == "running"

    def test_get_incomplete_by_profile_no_result(self, history_repo):
        """미완료 이력이 없는 경우"""
        # Given: 완료된 이력만 존재
        history_repo.create(
            profile_id=1,
            start_date="2025-01-01",
            end_date="2025-01-31",
            started_at=datetime.now(),
            status="completed",
        )

        # When: 미완료 이력 조회
        incomplete = history_repo.get_incomplete_by_profile(1)

        # Then: None 반환
        assert incomplete is None

    def test_exists(self, history_repo):
        """존재 여부 확인 테스트"""
        # Given: 이력 생성
        history_repo.create(
            profile_id=1,
            start_date="2025-01-01",
            end_date="2025-01-31",
            started_at=datetime.now(),
            status="running",
        )

        # When/Then: 존재 여부 확인
        assert history_repo.exists(profile_id=1) is True
        assert history_repo.exists(profile_id=999) is False

    def test_count(self, history_repo):
        """개수 세기 테스트"""
        # Given: 여러 이력 생성
        for i in range(5):
            history_repo.create(
                profile_id=1 if i < 3 else 2,
                start_date="2025-01-01",
                end_date="2025-01-31",
                started_at=datetime.now(),
                status="running",
            )

        # When/Then: 개수 확인
        assert history_repo.count() == 5
        assert history_repo.count(profile_id=1) == 3
        assert history_repo.count(profile_id=2) == 2


class TestCheckpointRepository:
    """CheckpointRepository 테스트"""

    @pytest.fixture
    def checkpoint_repo(self, temp_db):
        """CheckpointRepository 픽스처"""
        return CheckpointRepository(db=temp_db)

    @pytest.fixture
    def sample_history_id(self, temp_db):
        """테스트용 이력 ID"""
        history_repo = HistoryRepository(db=temp_db)
        history = history_repo.create(
            profile_id=1,
            start_date="2025-01-01",
            end_date="2025-01-31",
            started_at=datetime.now(),
            status="running",
        )
        return history.id

    def test_create_checkpoint(self, checkpoint_repo, sample_history_id):
        """체크포인트 생성 테스트"""
        checkpoint = checkpoint_repo.create(
            history_id=sample_history_id, partition_name="partition_240101", status="pending"
        )

        assert checkpoint.id is not None
        assert checkpoint.history_id == sample_history_id
        assert checkpoint.partition_name == "partition_240101"
        assert checkpoint.status == "pending"

    def test_get_by_history(self, checkpoint_repo, sample_history_id):
        """이력별 체크포인트 조회 테스트"""
        # Given: 여러 체크포인트 생성
        checkpoint_repo.create(
            history_id=sample_history_id, partition_name="partition_1", status="pending"
        )
        checkpoint_repo.create(
            history_id=sample_history_id, partition_name="partition_2", status="pending"
        )

        # When: 이력별 조회
        checkpoints = checkpoint_repo.get_by_history(sample_history_id)

        # Then: 2개 조회됨
        assert len(checkpoints) == 2
        partition_names = [cp.partition_name for cp in checkpoints]
        assert "partition_1" in partition_names
        assert "partition_2" in partition_names

    def test_get_pending_by_history(self, checkpoint_repo, sample_history_id):
        """미완료 체크포인트 조회 테스트"""
        # Given: 완료 및 미완료 체크포인트
        checkpoint_repo.create(
            history_id=sample_history_id, partition_name="completed_partition", status="completed"
        )
        pending_cp = checkpoint_repo.create(
            history_id=sample_history_id, partition_name="pending_partition", status="pending"
        )

        # When: 미완료 체크포인트 조회
        pending_checkpoints = checkpoint_repo.get_pending_by_history(sample_history_id)

        # Then: pending만 조회됨
        assert len(pending_checkpoints) == 1
        assert pending_checkpoints[0].id == pending_cp.id
        assert pending_checkpoints[0].status == "pending"

    def test_update_checkpoint(self, checkpoint_repo, sample_history_id):
        """체크포인트 업데이트 테스트"""
        # Given: 체크포인트 생성
        checkpoint = checkpoint_repo.create(
            history_id=sample_history_id,
            partition_name="partition_1",
            status="pending",
            rows_processed=0,
        )

        # When: 상태 업데이트
        success = checkpoint_repo.update_by_id(
            checkpoint.id, status="completed", rows_processed=1000
        )

        # Then: 업데이트 성공
        assert success is True
        updated = checkpoint_repo.get_by_id(checkpoint.id)
        assert updated.status == "completed"
        assert updated.rows_processed == 1000

    def test_count_checkpoints(self, checkpoint_repo, sample_history_id):
        """체크포인트 개수 세기 테스트"""
        # Given: 여러 체크포인트 생성
        for i in range(3):
            checkpoint_repo.create(
                history_id=sample_history_id, partition_name=f"partition_{i}", status="pending"
            )

        # When/Then: 개수 확인
        assert checkpoint_repo.count(history_id=sample_history_id) == 3


class TestBaseRepositoryRollback:
    """BaseRepository 트랜잭션 롤백 테스트"""

    def test_create_rollback_on_error(self, temp_db):
        """생성 중 오류 발생 시 롤백 테스트"""
        repo = HistoryRepository(db=temp_db)

        # 초기 개수
        initial_count = repo.count()

        # 잘못된 데이터로 생성 시도 (started_at이 datetime이 아님)
        try:
            repo.create(
                profile_id=1,
                start_date="2025-01-01",
                end_date="2025-01-31",
                started_at="invalid_datetime",  # 잘못된 타입
                status="running",
            )
        except Exception:
            pass

        # 롤백되어 개수 변화 없어야 함
        final_count = repo.count()
        assert final_count == initial_count

    def test_update_partial_rollback(self, temp_db):
        """업데이트 중 오류 발생 시 부분 롤백 테스트"""
        repo = HistoryRepository(db=temp_db)

        # Given: 이력 생성
        history = repo.create(
            profile_id=1,
            start_date="2025-01-01",
            end_date="2025-01-31",
            started_at=datetime.now(),
            status="running",
            processed_rows=0,
        )


        # When: 잘못된 필드로 업데이트 시도
        # (존재하지 않는 필드는 무시되지만, hasattr 체크로 안전)
        repo.update_by_id(history.id, status="updated", nonexistent_field="value")

        # Then: 유효한 필드만 업데이트됨
        updated = repo.get_by_id(history.id)
        assert updated.status == "updated"
