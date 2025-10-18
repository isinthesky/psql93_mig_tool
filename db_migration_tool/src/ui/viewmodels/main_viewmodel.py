"""
MainWindow를 위한 ViewModel

프로필 관리와 이력 조회 로직을 담당합니다.
"""

from PySide6.QtCore import Signal
from typing import List, Optional

from src.models.profile import ProfileManager, ConnectionProfile
from src.models.history import HistoryManager, MigrationHistoryItem
from .base_viewmodel import BaseViewModel


class MainViewModel(BaseViewModel):
    """메인 윈도우 ViewModel

    프로필 CRUD와 이력 조회 로직을 MainWindow에서 분리합니다.
    """

    # 프로필 관련 시그널
    profiles_changed = Signal(list)  # 프로필 목록 변경
    current_profile_changed = Signal(object)  # 현재 선택 프로필 변경 (ConnectionProfile 또는 None)

    # 이력 관련 시그널
    histories_changed = Signal(list)  # 이력 목록 변경

    def __init__(self, profile_manager: Optional[ProfileManager] = None,
                 history_manager: Optional[HistoryManager] = None):
        super().__init__()

        # 매니저 인스턴스 (테스트 시 목 주입 가능)
        self.profile_manager = profile_manager or ProfileManager()
        self.history_manager = history_manager or HistoryManager()

        # 내부 상태
        self._profiles: List[ConnectionProfile] = []
        self._current_profile: Optional[ConnectionProfile] = None
        self._histories: List[MigrationHistoryItem] = []

    # --- 프로필 관련 메서드 ---

    def load_profiles(self):
        """프로필 목록 로드"""
        try:
            self.is_loading = True
            self._profiles = self.profile_manager.get_all_profiles()
            self.profiles_changed.emit(self._profiles)
        except Exception as e:
            self.handle_error(e)
        finally:
            self.is_loading = False

    def select_profile(self, profile_id: Optional[int]):
        """프로필 선택

        Args:
            profile_id: 선택할 프로필 ID (None이면 선택 해제)
        """
        try:
            if profile_id is None:
                # 선택 해제
                self._current_profile = None
                self.current_profile_changed.emit(None)
            else:
                # 프로필 선택
                profile = self.profile_manager.get_profile(profile_id)
                if profile:
                    self._current_profile = profile
                    self.current_profile_changed.emit(profile)
        except Exception as e:
            self.handle_error(e)

    def create_profile(self, profile_data: dict) -> bool:
        """새 프로필 생성

        Args:
            profile_data: 프로필 정보 딕셔너리

        Returns:
            bool: 생성 성공 여부
        """
        try:
            self.profile_manager.create_profile(profile_data)
            self.load_profiles()  # 목록 자동 갱신
            self.send_message("성공", "새 연결이 생성되었습니다.")
            return True
        except Exception as e:
            self.handle_error(e)
            return False

    def update_profile(self, profile_id: int, profile_data: dict) -> bool:
        """프로필 업데이트

        Args:
            profile_id: 업데이트할 프로필 ID
            profile_data: 새로운 프로필 정보

        Returns:
            bool: 업데이트 성공 여부
        """
        try:
            self.profile_manager.update_profile(profile_id, profile_data)
            self.load_profiles()  # 목록 자동 갱신
            self.send_message("성공", "연결이 수정되었습니다.")
            return True
        except Exception as e:
            self.handle_error(e)
            return False

    def delete_profile(self, profile_id: int) -> bool:
        """프로필 삭제

        Args:
            profile_id: 삭제할 프로필 ID

        Returns:
            bool: 삭제 성공 여부
        """
        try:
            self.profile_manager.delete_profile(profile_id)
            self.load_profiles()  # 목록 자동 갱신

            # 현재 선택된 프로필이 삭제된 경우 초기화
            if self._current_profile and self._current_profile.id == profile_id:
                self._current_profile = None
                self.current_profile_changed.emit(None)

            self.send_message("성공", "연결이 삭제되었습니다.")
            return True
        except Exception as e:
            self.handle_error(e)
            return False

    @property
    def current_profile(self) -> Optional[ConnectionProfile]:
        """현재 선택된 프로필"""
        return self._current_profile

    @property
    def profiles(self) -> List[ConnectionProfile]:
        """프로필 목록"""
        return self._profiles

    # --- 이력 관련 메서드 ---

    def load_histories(self):
        """작업 이력 목록 로드"""
        try:
            self.is_loading = True
            self._histories = self.history_manager.get_all_history()
            self.histories_changed.emit(self._histories)
        except Exception as e:
            self.handle_error(e)
        finally:
            self.is_loading = False

    def refresh_histories(self):
        """이력 새로고침 (load_histories의 별칭)"""
        self.load_histories()

    @property
    def histories(self) -> List[MigrationHistoryItem]:
        """이력 목록"""
        return self._histories

    # --- 초기화 메서드 ---

    def initialize(self):
        """초기 데이터 로드

        MainWindow가 열릴 때 호출되어 프로필과 이력을 로드합니다.
        """
        self.load_profiles()
        self.load_histories()
