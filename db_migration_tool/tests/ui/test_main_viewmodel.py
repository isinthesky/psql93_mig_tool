"""
MainViewModel 테스트
"""

from unittest.mock import Mock

import pytest

from src.ui.viewmodels.main_viewmodel import MainViewModel


class TestMainViewModel:
    """MainViewModel 테스트"""

    @pytest.fixture
    def mock_profile_manager(self):
        """ProfileManager 목 객체"""
        manager = Mock()
        manager.get_all_profiles = Mock(return_value=[])
        manager.get_profile = Mock(return_value=None)
        manager.create_profile = Mock()
        manager.update_profile = Mock()
        manager.delete_profile = Mock()
        return manager

    @pytest.fixture
    def mock_history_manager(self):
        """HistoryManager 목 객체"""
        manager = Mock()
        manager.get_all_history = Mock(return_value=[])
        return manager

    @pytest.fixture
    def viewmodel(self, mock_profile_manager, mock_history_manager):
        """MainViewModel 픽스처"""
        return MainViewModel(
            profile_manager=mock_profile_manager, history_manager=mock_history_manager
        )

    def test_initial_state(self, viewmodel):
        """초기 상태 확인"""
        assert viewmodel.profiles == []
        assert viewmodel.current_profile is None
        assert viewmodel.histories == []

    def test_load_profiles_emits_signal(self, viewmodel, mock_profile_manager, qtbot):
        """프로필 로드 시 시그널 발행 확인"""
        # Given: 목 프로필 데이터
        mock_profiles = [Mock(id=1, name="Profile 1"), Mock(id=2, name="Profile 2")]
        mock_profile_manager.get_all_profiles.return_value = mock_profiles

        # When: 프로필 로드
        with qtbot.waitSignal(viewmodel.profiles_changed, timeout=1000) as blocker:
            viewmodel.load_profiles()

        # Then: 시그널 발행 및 내부 상태 업데이트 확인
        assert blocker.args[0] == mock_profiles
        assert viewmodel.profiles == mock_profiles
        mock_profile_manager.get_all_profiles.assert_called_once()

    def test_select_profile_emits_signal(self, viewmodel, mock_profile_manager, qtbot):
        """프로필 선택 시 시그널 발행 확인"""
        # Given: 목 프로필
        mock_profile = Mock(id=1, name="Test Profile")
        mock_profile_manager.get_profile.return_value = mock_profile

        # When: 프로필 선택
        with qtbot.waitSignal(viewmodel.current_profile_changed, timeout=1000) as blocker:
            viewmodel.select_profile(1)

        # Then: 시그널 발행 및 현재 프로필 업데이트 확인
        assert blocker.args[0] == mock_profile
        assert viewmodel.current_profile == mock_profile
        mock_profile_manager.get_profile.assert_called_once_with(1)

    def test_create_profile_reloads_profiles(self, viewmodel, mock_profile_manager, qtbot):
        """프로필 생성 시 목록 자동 갱신 확인"""
        # Given: 생성 후 갱신된 프로필 목록
        mock_profile_manager.get_all_profiles.return_value = [Mock(id=1, name="New Profile")]

        # When: 프로필 생성
        with qtbot.waitSignal(viewmodel.profiles_changed, timeout=1000):
            result = viewmodel.create_profile({"name": "New Profile"})

        # Then: 생성 성공 및 목록 갱신 확인
        assert result is True
        mock_profile_manager.create_profile.assert_called_once()
        mock_profile_manager.get_all_profiles.assert_called()

    def test_create_profile_error_handling(self, viewmodel, mock_profile_manager, qtbot):
        """프로필 생성 실패 시 오류 처리 확인"""
        # Given: create_profile이 예외 발생
        mock_profile_manager.create_profile.side_effect = RuntimeError("Creation failed")

        # When: 프로필 생성 시도
        with qtbot.waitSignal(viewmodel.error_occurred, timeout=1000) as blocker:
            result = viewmodel.create_profile({"name": "Bad Profile"})

        # Then: 실패 반환 및 오류 시그널 발행
        assert result is False
        assert "Creation failed" in blocker.args[0]

    def test_update_profile_reloads_profiles(self, viewmodel, mock_profile_manager, qtbot):
        """프로필 업데이트 시 목록 자동 갱신 확인"""
        # Given: 업데이트 후 갱신된 프로필 목록
        mock_profile_manager.get_all_profiles.return_value = [Mock(id=1, name="Updated Profile")]

        # When: 프로필 업데이트
        with qtbot.waitSignal(viewmodel.profiles_changed, timeout=1000):
            result = viewmodel.update_profile(1, {"name": "Updated Profile"})

        # Then: 업데이트 성공 및 목록 갱신 확인
        assert result is True
        mock_profile_manager.update_profile.assert_called_once_with(1, {"name": "Updated Profile"})

    def test_delete_profile_reloads_and_clears_current(
        self, viewmodel, mock_profile_manager, qtbot
    ):
        """프로필 삭제 시 목록 갱신 및 current_profile 초기화 확인"""
        # Given: 현재 프로필이 선택된 상태
        mock_profile = Mock(id=1, name="To Delete")
        mock_profile_manager.get_profile.return_value = mock_profile
        viewmodel.select_profile(1)
        assert viewmodel.current_profile is not None

        # Given: 삭제 후 빈 프로필 목록
        mock_profile_manager.get_all_profiles.return_value = []

        # When: 현재 선택된 프로필 삭제
        with qtbot.waitSignal(viewmodel.current_profile_changed, timeout=1000) as blocker:
            result = viewmodel.delete_profile(1)

        # Then: 삭제 성공, 목록 갱신, current_profile이 None으로 변경
        assert result is True
        assert blocker.args[0] is None
        assert viewmodel.current_profile is None
        mock_profile_manager.delete_profile.assert_called_once_with(1)

    def test_delete_profile_does_not_clear_different_current(
        self, viewmodel, mock_profile_manager, qtbot
    ):
        """다른 프로필 삭제 시 current_profile 유지 확인"""
        # Given: 프로필 1이 선택된 상태
        mock_profile = Mock(id=1, name="Current")
        mock_profile_manager.get_profile.return_value = mock_profile
        viewmodel.select_profile(1)

        # Given: 삭제 후 프로필 목록
        mock_profile_manager.get_all_profiles.return_value = [mock_profile]

        # When: 프로필 2 삭제 (프로필 1과 다름)
        with qtbot.waitSignal(viewmodel.profiles_changed, timeout=1000):
            result = viewmodel.delete_profile(2)

        # Then: current_profile은 그대로 유지
        assert result is True
        assert viewmodel.current_profile == mock_profile

    def test_load_histories_emits_signal(self, viewmodel, mock_history_manager, qtbot):
        """이력 로드 시 시그널 발행 확인"""
        # Given: 목 이력 데이터
        mock_histories = [
            Mock(id=1, profile_id=1, status="completed"),
            Mock(id=2, profile_id=2, status="running"),
        ]
        mock_history_manager.get_all_history.return_value = mock_histories

        # When: 이력 로드
        with qtbot.waitSignal(viewmodel.histories_changed, timeout=1000) as blocker:
            viewmodel.load_histories()

        # Then: 시그널 발행 및 내부 상태 업데이트 확인
        assert blocker.args[0] == mock_histories
        assert viewmodel.histories == mock_histories

    def test_refresh_histories_alias(self, viewmodel, mock_history_manager, qtbot):
        """refresh_histories가 load_histories의 별칭인지 확인"""
        # Given: 목 이력 데이터
        mock_histories = [Mock(id=1)]
        mock_history_manager.get_all_history.return_value = mock_histories

        # When: refresh_histories 호출
        with qtbot.waitSignal(viewmodel.histories_changed, timeout=1000):
            viewmodel.refresh_histories()

        # Then: 이력이 로드됨
        assert viewmodel.histories == mock_histories

    def test_initialize_loads_both(
        self, viewmodel, mock_profile_manager, mock_history_manager, qtbot
    ):
        """initialize가 프로필과 이력을 모두 로드하는지 확인"""
        # Given: 목 데이터
        mock_profiles = [Mock(id=1)]
        mock_histories = [Mock(id=1)]
        mock_profile_manager.get_all_profiles.return_value = mock_profiles
        mock_history_manager.get_all_history.return_value = mock_histories

        # When: initialize 호출 (두 시그널 모두 발행됨)
        with qtbot.waitSignal(viewmodel.profiles_changed, timeout=1000):
            with qtbot.waitSignal(viewmodel.histories_changed, timeout=1000):
                viewmodel.initialize()

        # Then: 프로필과 이력 모두 로드됨
        assert viewmodel.profiles == mock_profiles
        assert viewmodel.histories == mock_histories
