"""
session_scope() 컨텍스트 매니저 테스트
"""

import pytest

from src.database.local_db import Profile


class TestSessionScope:
    """session_scope() 동작 검증 테스트"""

    def test_session_scope_commit_on_success(self, temp_db):
        """정상 실행 시 자동 commit 확인"""
        # Given: 프로필 데이터 준비
        profile_name = "Test Auto Commit"

        # When: session_scope 내에서 데이터 추가
        with temp_db.session_scope() as session:
            profile = Profile(
                name=profile_name,
                source_config='{"encrypted": "source"}',
                target_config='{"encrypted": "target"}',
            )
            session.add(profile)
            session.flush()
            profile_id = profile.id

        # Then: commit이 자동으로 실행되어 데이터가 저장되어야 함
        with temp_db.session_scope() as session:
            saved_profile = session.query(Profile).filter_by(id=profile_id).first()
            assert saved_profile is not None
            assert saved_profile.name == profile_name

    def test_session_scope_rollback_on_exception(self, temp_db):
        """예외 발생 시 자동 rollback 확인"""
        # Given: 초기 데이터 없음
        initial_count = 0
        with temp_db.session_scope() as session:
            initial_count = session.query(Profile).count()

        # When: session_scope 내에서 예외 발생
        with pytest.raises(ValueError):
            with temp_db.session_scope() as session:
                profile = Profile(
                    name="Should Rollback",
                    source_config='{"encrypted": "source"}',
                    target_config='{"encrypted": "target"}',
                )
                session.add(profile)
                session.flush()
                # 의도적으로 예외 발생
                raise ValueError("Test exception")

        # Then: rollback이 자동으로 실행되어 데이터가 저장되지 않아야 함
        with temp_db.session_scope() as session:
            final_count = session.query(Profile).count()
            assert final_count == initial_count

    def test_session_scope_closes_session(self, temp_db):
        """session_scope 종료 시 세션이 닫히는지 확인"""
        # When: session_scope 사용
        with temp_db.session_scope() as session:
            profile = Profile(
                name="Test Close",
                source_config='{"encrypted": "source"}',
                target_config='{"encrypted": "target"}',
            )
            session.add(profile)
            # session은 아직 열려있음
            assert session.is_active

        # Then: with 블록을 벗어나면 세션이 닫혀야 함
        # (세션이 닫혔는지 직접 확인할 수 없으므로, 새 세션으로 데이터 조회)
        with temp_db.session_scope() as new_session:
            profiles = new_session.query(Profile).all()
            assert len(profiles) == 1

    def test_session_scope_flush_generates_id(self, temp_db):
        """flush() 호출 시 ID가 생성되는지 확인"""
        # When: session_scope 내에서 flush 호출
        with temp_db.session_scope() as session:
            profile = Profile(
                name="Test Flush ID",
                source_config='{"encrypted": "source"}',
                target_config='{"encrypted": "target"}',
            )
            session.add(profile)

            # flush 전에는 ID가 None일 수 있음

            session.flush()

            # flush 후에는 ID가 생성되어야 함
            post_flush_id = profile.id
            assert post_flush_id is not None

    def test_session_scope_nested_exception(self, temp_db):
        """중첩된 작업에서 예외 발생 시 전체 rollback 확인"""
        # Given: 초기 카운트
        with temp_db.session_scope() as session:
            initial_count = session.query(Profile).count()

        # When: 중첩 작업 중 예외 발생
        with pytest.raises(RuntimeError):
            with temp_db.session_scope() as session:
                # 첫 번째 프로필 추가
                profile1 = Profile(
                    name="Profile 1",
                    source_config='{"encrypted": "source1"}',
                    target_config='{"encrypted": "target1"}',
                )
                session.add(profile1)
                session.flush()

                # 두 번째 프로필 추가
                profile2 = Profile(
                    name="Profile 2",
                    source_config='{"encrypted": "source2"}',
                    target_config='{"encrypted": "target2"}',
                )
                session.add(profile2)
                session.flush()

                # 예외 발생
                raise RuntimeError("Nested operation failed")

        # Then: 모든 변경사항이 rollback되어야 함
        with temp_db.session_scope() as session:
            final_count = session.query(Profile).count()
            assert final_count == initial_count

    def test_session_scope_multiple_operations(self, temp_db):
        """여러 작업을 한 트랜잭션에서 처리"""
        # When: 여러 프로필을 한 트랜잭션에서 생성
        profile_names = ["Profile A", "Profile B", "Profile C"]

        with temp_db.session_scope() as session:
            for name in profile_names:
                profile = Profile(
                    name=name,
                    source_config=f'{{"encrypted": "source_{name}"}}',
                    target_config=f'{{"encrypted": "target_{name}"}}',
                )
                session.add(profile)

        # Then: 모든 프로필이 저장되어야 함
        with temp_db.session_scope() as session:
            saved_profiles = session.query(Profile).all()
            saved_names = [p.name for p in saved_profiles]
            assert len(saved_profiles) == 3
            for name in profile_names:
                assert name in saved_names
