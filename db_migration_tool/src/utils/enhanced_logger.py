"""
향상된 로깅 유틸리티
- SUCCESS 레벨 추가
- 민감정보 마스킹 (SensitiveDataMasker 활용)
- DB 저장 지원 (DatabaseLoggerMixin 활용)
- 세션 ID 관리
"""

import logging
from datetime import datetime

from PySide6.QtCore import QObject, Signal

from .logger import MigrationLogger
from .logger_mixins import DatabaseLoggerMixin, SensitiveDataMasker

# SUCCESS 레벨 추가
SUCCESS_LEVEL = 25  # INFO(20)와 WARNING(30) 사이
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")


class EnhancedLogger(DatabaseLoggerMixin):
    """향상된 마이그레이션 로거

    DatabaseLoggerMixin을 상속받아 DB 큐/스레드 로직 재사용
    """

    def __init__(self):
        # DatabaseLoggerMixin 초기화 (DB 큐/스레드 시작)
        super().__init__()

        # 기본 로거 생성
        self.base_logger = MigrationLogger()
        self.logger = self.base_logger.logger

    def _format_and_log(self, level: str, message: str, exc_info: bool = False):
        """포맷팅 및 로깅"""
        # 민감정보 마스킹 (SensitiveDataMasker 활용)
        masked_message = SensitiveDataMasker.mask(message)

        # 파일 로깅 (기존 방식)
        if level == "DEBUG":
            self.logger.debug(masked_message, exc_info=exc_info)
        elif level == "INFO":
            self.logger.info(masked_message, exc_info=exc_info)
        elif level == "WARNING":
            self.logger.warning(masked_message, exc_info=exc_info)
        elif level == "ERROR":
            self.logger.error(masked_message, exc_info=exc_info)
        elif level == "CRITICAL":
            self.logger.critical(masked_message, exc_info=exc_info)
        elif level == "SUCCESS":
            self.logger.log(SUCCESS_LEVEL, masked_message, exc_info=exc_info)

        # DB 로깅 (DatabaseLoggerMixin의 log_to_db 활용)
        self.log_to_db(level, masked_message)

    # MigrationLogger의 메서드들을 위임
    def debug(self, message: str):
        """디버그 로그"""
        self._format_and_log("DEBUG", message)

    def info(self, message: str):
        """정보 로그"""
        self._format_and_log("INFO", message)

    def warning(self, message: str):
        """경고 로그"""
        self._format_and_log("WARNING", message)

    def error(self, message: str, exc_info: bool = False):
        """오류 로그"""
        self._format_and_log("ERROR", message, exc_info)

    def critical(self, message: str, exc_info: bool = False):
        """치명적 오류 로그"""
        self._format_and_log("CRITICAL", message, exc_info)

    def success(self, message: str):
        """성공 로그"""
        self._format_and_log("SUCCESS", message)


class LogSignalEmitter(QObject):
    """Qt 시그널을 위한 로그 이미터"""

    log_signal = Signal(str, str, str, str)  # timestamp, session_id, level, message

    def __init__(self, logger=None):
        super().__init__()
        self.logger = logger or EnhancedLogger()

    def emit_log(self, level: str, message: str):
        """로그 발생 및 시그널 전송"""
        # 로거에 기록
        if level == "SUCCESS" and hasattr(self.logger, "success"):
            self.logger.success(message)
        elif level == "ERROR":
            self.logger.error(message)
        elif level == "WARNING":
            self.logger.warning(message)
        elif level == "DEBUG":
            self.logger.debug(message)
        else:
            self.logger.info(message)

        # UI로 시그널 전송 (민감정보 마스킹)
        timestamp = datetime.now().strftime("%y%m%d %H:%M:%S")
        session_id = getattr(self.logger, "session_id", None) or "NO_SESSION"
        masked_message = SensitiveDataMasker.mask(message)

        self.log_signal.emit(timestamp, session_id, level, masked_message)


# 전역 로거 인스턴스
enhanced_logger = EnhancedLogger()
log_emitter = LogSignalEmitter(enhanced_logger)
