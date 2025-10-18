"""
로깅 유틸리티
"""

import logging

from .app_paths import AppPaths
from .logger_config import LoggerConfig


class MigrationLogger:
    """마이그레이션 로거"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        # 로그 디렉토리 확보 (AppPaths가 자동 생성)
        self.log_dir = AppPaths.get_logs_dir()

        # 로거 설정 (LoggerConfig 활용)
        self.logger = LoggerConfig.setup_logger(
            name="DBMigration",
            level=logging.DEBUG,
            handlers=[LoggerConfig.create_file_handler(level=logging.DEBUG)],
            clear_existing=True,
        )

        self._initialized = True

    def debug(self, message: str):
        """디버그 로그"""
        self.logger.debug(message)

    def info(self, message: str):
        """정보 로그"""
        self.logger.info(message)

    def warning(self, message: str):
        """경고 로그"""
        self.logger.warning(message)

    def error(self, message: str, exc_info: bool = False):
        """오류 로그"""
        self.logger.error(message, exc_info=exc_info)

    def critical(self, message: str, exc_info: bool = False):
        """치명적 오류 로그"""
        self.logger.critical(message, exc_info=exc_info)


# 전역 로거 인스턴스
logger = MigrationLogger()
