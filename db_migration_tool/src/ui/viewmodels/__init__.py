"""
UI ViewModel 패키지

ViewModel은 UI와 비즈니스 로직 사이의 중간 계층으로,
Qt 위젯에 직접 의존하지 않고 테스트 가능한 상태 관리를 제공합니다.
"""

from .base_viewmodel import BaseViewModel

__all__ = ["BaseViewModel"]
