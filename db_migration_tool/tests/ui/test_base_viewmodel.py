"""
BaseViewModel 테스트
"""

import pytest

from src.ui.viewmodels.base_viewmodel import BaseViewModel


class TestBaseViewModel:
    """BaseViewModel 기본 동작 테스트"""

    @pytest.fixture
    def viewmodel(self):
        """BaseViewModel 픽스처"""
        return BaseViewModel()

    def test_initial_state(self, viewmodel):
        """초기 상태 확인"""
        assert viewmodel.is_loading is False
        assert viewmodel.error_message is None

    def test_loading_state_change_emits_signal(self, viewmodel, qtbot):
        """로딩 상태 변경 시 시그널 발행 확인"""
        # Given: qtbot으로 시그널 대기
        with qtbot.waitSignal(viewmodel.loading_changed, timeout=1000) as blocker:
            # When: 로딩 상태 변경
            viewmodel.is_loading = True

        # Then: 시그널 발행 확인
        assert blocker.args == [True]
        assert viewmodel.is_loading is True

    def test_loading_state_no_signal_when_same_value(self, viewmodel, qtbot):
        """동일한 값으로 설정 시 시그널 미발행 확인"""
        # Given: 초기 상태가 False
        # When/Then: 동일한 값(False)으로 설정 시 시그널 미발행
        # waitSignal이 타임아웃될 것으로 예상
        with pytest.raises(TimeoutError):
            with qtbot.waitSignal(viewmodel.loading_changed, timeout=100):
                viewmodel.is_loading = False

    def test_handle_error_emits_signal(self, viewmodel, qtbot):
        """오류 처리 시 시그널 발행 확인"""
        # Given: 테스트 오류 생성
        test_error = RuntimeError("Test error message")

        # When: 오류 처리
        with qtbot.waitSignal(viewmodel.error_occurred, timeout=1000) as blocker:
            viewmodel.handle_error(test_error)

        # Then: 시그널 발행 및 메시지 저장 확인
        assert blocker.args == ["Test error message"]
        assert viewmodel.error_message == "Test error message"

    def test_clear_error(self, viewmodel):
        """오류 메시지 초기화 확인"""
        # Given: 오류 메시지가 설정된 상태
        viewmodel.handle_error(RuntimeError("Some error"))
        assert viewmodel.error_message is not None

        # When: 오류 초기화
        viewmodel.clear_error()

        # Then: 오류 메시지가 None
        assert viewmodel.error_message is None

    def test_send_message_emits_signal(self, viewmodel, qtbot):
        """메시지 발송 시 시그널 발행 확인"""
        # When: 메시지 발송
        with qtbot.waitSignal(viewmodel.message_sent, timeout=1000) as blocker:
            viewmodel.send_message("Test Title", "Test Message")

        # Then: 시그널 발행 확인
        assert blocker.args == ["Test Title", "Test Message"]

    def test_multiple_loading_state_changes(self, viewmodel):
        """여러 번의 로딩 상태 변경 확인"""
        # When: 여러 번 상태 변경
        viewmodel.is_loading = True
        assert viewmodel.is_loading is True

        viewmodel.is_loading = False
        assert viewmodel.is_loading is False

        viewmodel.is_loading = True
        assert viewmodel.is_loading is True
