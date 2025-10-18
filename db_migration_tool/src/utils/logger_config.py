"""로거 설정 및 핸들러 팩토리

logging 핸들러 생성과 로거 설정을 중앙집중화합니다.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .app_paths import AppPaths


class LoggerConfig:
    """로거 설정 관리 클래스

    핸들러 팩토리 메서드와 로거 설정 유틸리티를 제공합니다.

    Examples:
        >>> from src.utils.logger_config import LoggerConfig
        >>> handler = LoggerConfig.create_file_handler()
        >>> logger = LoggerConfig.setup_logger('MyLogger', [handler])
    """

    DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    DEFAULT_LEVEL = logging.DEBUG

    @staticmethod
    def create_file_handler(
        log_dir: Optional[Path] = None,
        filename_pattern: str = "migration_{date}.log",
        level: int = logging.DEBUG,
        encoding: str = "utf-8",
    ) -> logging.FileHandler:
        """파일 핸들러 생성

        Args:
            log_dir: 로그 디렉토리. None이면 AppPaths에서 가져옴
            filename_pattern: 파일명 패턴. {date}는 YYYYMMDD로 치환됨
            level: 로그 레벨
            encoding: 파일 인코딩

        Returns:
            설정된 파일 핸들러

        Examples:
            >>> handler = LoggerConfig.create_file_handler()
            >>> handler.setLevel(logging.INFO)
        """
        if log_dir is None:
            log_dir = AppPaths.get_logs_dir()
        else:
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)

        filename = filename_pattern.format(date=datetime.now().strftime("%Y%m%d"))
        log_file = log_dir / filename

        handler = logging.FileHandler(log_file, encoding=encoding)
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(LoggerConfig.DEFAULT_FORMAT))
        return handler

    @staticmethod
    def create_console_handler(
        level: int = logging.INFO, format_string: Optional[str] = None
    ) -> logging.StreamHandler:
        """콘솔 핸들러 생성

        Args:
            level: 로그 레벨
            format_string: 포맷 문자열. None이면 DEFAULT_FORMAT 사용

        Returns:
            설정된 콘솔 핸들러

        Examples:
            >>> handler = LoggerConfig.create_console_handler(logging.WARNING)
        """
        handler = logging.StreamHandler()
        handler.setLevel(level)

        if format_string is None:
            format_string = LoggerConfig.DEFAULT_FORMAT

        handler.setFormatter(logging.Formatter(format_string))
        return handler

    @staticmethod
    def setup_logger(
        name: str,
        handlers: list[logging.Handler],
        level: int = logging.DEBUG,
        clear_existing: bool = True,
    ) -> logging.Logger:
        """로거 설정

        Args:
            name: 로거 이름
            handlers: 핸들러 리스트
            level: 로거 레벨
            clear_existing: 기존 핸들러 제거 여부 (중복 방지)

        Returns:
            설정된 로거

        Examples:
            >>> file_handler = LoggerConfig.create_file_handler()
            >>> console_handler = LoggerConfig.create_console_handler()
            >>> logger = LoggerConfig.setup_logger('MyApp', [file_handler, console_handler])
        """
        logger = logging.getLogger(name)
        logger.setLevel(level)

        # 기존 핸들러 제거 (중복 방지)
        if clear_existing:
            logger.handlers.clear()

        for handler in handlers:
            logger.addHandler(handler)

        # 부모 로거로 전파 방지 (중복 로그 방지)
        logger.propagate = False

        return logger

    @staticmethod
    def get_default_file_handler() -> logging.FileHandler:
        """기본 파일 핸들러 생성 (편의 메서드)

        Returns:
            기본 설정의 파일 핸들러
        """
        return LoggerConfig.create_file_handler()

    @staticmethod
    def get_default_console_handler() -> logging.StreamHandler:
        """기본 콘솔 핸들러 생성 (편의 메서드)

        Returns:
            기본 설정의 콘솔 핸들러
        """
        return LoggerConfig.create_console_handler()
