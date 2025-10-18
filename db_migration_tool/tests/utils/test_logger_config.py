"""logger_config.py 단위 테스트"""

import logging
import tempfile
from pathlib import Path

import pytest

from src.utils.app_paths import AppPaths
from src.utils.logger_config import LoggerConfig


class TestLoggerConfig:
    """LoggerConfig 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def reset_app_paths(self):
        """각 테스트 전후로 AppPaths 초기화"""
        AppPaths.set_custom_root(None)
        yield
        AppPaths.set_custom_root(None)

    def test_create_file_handler(self):
        """파일 핸들러 생성 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            AppPaths.set_custom_root(Path(tmpdir))
            handler = LoggerConfig.create_file_handler()

            assert isinstance(handler, logging.FileHandler)
            assert handler.level == logging.DEBUG
            assert handler.formatter is not None

    def test_create_file_handler_with_custom_dir(self):
        """커스텀 디렉토리로 파일 핸들러 생성 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_dir = Path(tmpdir) / "custom_logs"
            handler = LoggerConfig.create_file_handler(log_dir=custom_dir)

            assert isinstance(handler, logging.FileHandler)
            assert custom_dir.exists()

    def test_create_file_handler_with_custom_pattern(self):
        """커스텀 파일명 패턴으로 핸들러 생성 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            AppPaths.set_custom_root(Path(tmpdir))
            handler = LoggerConfig.create_file_handler(filename_pattern="custom_{date}.log")

            assert isinstance(handler, logging.FileHandler)
            # 핸들러가 파일을 생성했는지 확인
            logs_dir = AppPaths.get_logs_dir()
            list(logs_dir.glob("custom_*.log"))
            # baseFilename 속성으로 파일명 확인
            assert "custom_" in handler.baseFilename

    def test_create_console_handler(self):
        """콘솔 핸들러 생성 테스트"""
        handler = LoggerConfig.create_console_handler()

        assert isinstance(handler, logging.StreamHandler)
        assert handler.level == logging.INFO
        assert handler.formatter is not None

    def test_create_console_handler_with_custom_level(self):
        """커스텀 레벨로 콘솔 핸들러 생성 테스트"""
        handler = LoggerConfig.create_console_handler(level=logging.WARNING)

        assert handler.level == logging.WARNING

    def test_create_console_handler_with_custom_format(self):
        """커스텀 포맷으로 콘솔 핸들러 생성 테스트"""
        custom_format = "%(levelname)s - %(message)s"
        handler = LoggerConfig.create_console_handler(format_string=custom_format)

        assert handler.formatter._fmt == custom_format

    def test_setup_logger(self):
        """로거 설정 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            AppPaths.set_custom_root(Path(tmpdir))

            file_handler = LoggerConfig.create_file_handler()
            console_handler = LoggerConfig.create_console_handler()

            logger = LoggerConfig.setup_logger("TestLogger", [file_handler, console_handler])

            assert logger.name == "TestLogger"
            assert logger.level == logging.DEBUG
            assert len(logger.handlers) == 2
            assert file_handler in logger.handlers
            assert console_handler in logger.handlers

    def test_setup_logger_clears_existing_handlers(self):
        """로거 설정 시 기존 핸들러 제거 테스트"""
        # 첫 번째 설정
        handler1 = LoggerConfig.create_console_handler()
        logger = LoggerConfig.setup_logger("TestLogger2", [handler1])
        assert len(logger.handlers) == 1

        # 두 번째 설정 (기존 핸들러 제거됨)
        handler2 = LoggerConfig.create_console_handler()
        logger = LoggerConfig.setup_logger("TestLogger2", [handler2])
        assert len(logger.handlers) == 1
        assert handler2 in logger.handlers
        assert handler1 not in logger.handlers

    def test_setup_logger_without_clearing(self):
        """기존 핸들러 유지하면서 로거 설정 테스트"""
        handler1 = LoggerConfig.create_console_handler()
        logger = LoggerConfig.setup_logger("TestLogger3", [handler1])

        handler2 = LoggerConfig.create_console_handler()
        logger = LoggerConfig.setup_logger("TestLogger3", [handler2], clear_existing=False)

        # 두 핸들러 모두 존재해야 함
        assert len(logger.handlers) == 2

    def test_logger_propagate_false(self):
        """로거 전파 방지 확인"""
        handler = LoggerConfig.create_console_handler()
        logger = LoggerConfig.setup_logger("TestLogger4", [handler])

        assert logger.propagate is False

    def test_get_default_file_handler(self):
        """기본 파일 핸들러 생성 편의 메서드 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            AppPaths.set_custom_root(Path(tmpdir))
            handler = LoggerConfig.get_default_file_handler()

            assert isinstance(handler, logging.FileHandler)
            assert handler.level == logging.DEBUG

    def test_get_default_console_handler(self):
        """기본 콘솔 핸들러 생성 편의 메서드 테스트"""
        handler = LoggerConfig.get_default_console_handler()

        assert isinstance(handler, logging.StreamHandler)
        assert handler.level == logging.INFO

    def test_logger_writes_to_file(self):
        """로거가 실제로 파일에 쓰는지 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            AppPaths.set_custom_root(Path(tmpdir))
            handler = LoggerConfig.create_file_handler()
            logger = LoggerConfig.setup_logger("FileWriteTest", [handler])

            # 로그 작성
            test_message = "Test log message"
            logger.info(test_message)

            # 핸들러 플러시
            handler.flush()

            # 파일 내용 확인
            log_file = Path(handler.baseFilename)
            assert log_file.exists()
            content = log_file.read_text()
            assert test_message in content
            assert "INFO" in content

    def test_multiple_handlers_same_logger(self):
        """하나의 로거에 여러 핸들러 추가 테스트"""
        with tempfile.TemporaryDirectory() as tmpdir:
            AppPaths.set_custom_root(Path(tmpdir))

            file_handler1 = LoggerConfig.create_file_handler(filename_pattern="log1_{date}.log")
            file_handler2 = LoggerConfig.create_file_handler(filename_pattern="log2_{date}.log")
            console_handler = LoggerConfig.create_console_handler()

            logger = LoggerConfig.setup_logger(
                "MultiHandlerTest", [file_handler1, file_handler2, console_handler]
            )

            assert len(logger.handlers) == 3

            # 로그 작성
            logger.info("Multi handler test")

            # 모든 핸들러 플러시
            for handler in logger.handlers:
                handler.flush()

            # 두 파일 모두에 로그가 기록되었는지 확인
            if isinstance(file_handler1, logging.FileHandler):
                assert Path(file_handler1.baseFilename).exists()
            if isinstance(file_handler2, logging.FileHandler):
                assert Path(file_handler2.baseFilename).exists()
