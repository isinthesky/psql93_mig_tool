"""
ViewModel 베이스 클래스

모든 ViewModel의 공통 기능을 제공합니다:
- 로딩 상태 관리
- 오류 처리 및 시그널
- 상태 변경 알림
"""

from PySide6.QtCore import QObject, Signal
from typing import Optional


class BaseViewModel(QObject):
    """ViewModel 베이스 클래스

    Qt 시그널을 통해 UI 업데이트를 알리고,
    비즈니스 로직은 Manager 계층에 위임합니다.
    """

    # 공통 시그널
    error_occurred = Signal(str)  # 오류 발생 (메시지)
    loading_changed = Signal(bool)  # 로딩 상태 변경
    message_sent = Signal(str, str)  # 메시지 발생 (제목, 내용)

    def __init__(self):
        super().__init__()
        self._is_loading = False
        self._error_message: Optional[str] = None

    @property
    def is_loading(self) -> bool:
        """로딩 상태"""
        return self._is_loading

    @is_loading.setter
    def is_loading(self, value: bool):
        """로딩 상태 설정 및 시그널 발행"""
        if self._is_loading != value:
            self._is_loading = value
            self.loading_changed.emit(value)

    @property
    def error_message(self) -> Optional[str]:
        """마지막 오류 메시지"""
        return self._error_message

    def handle_error(self, error: Exception):
        """오류 처리 및 시그널 발행

        Args:
            error: 발생한 예외
        """
        error_msg = str(error)
        self._error_message = error_msg
        self.error_occurred.emit(error_msg)

    def send_message(self, title: str, message: str):
        """메시지 발송

        Args:
            title: 메시지 제목
            message: 메시지 내용
        """
        self.message_sent.emit(title, message)

    def clear_error(self):
        """오류 메시지 초기화"""
        self._error_message = None
