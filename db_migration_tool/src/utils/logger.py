"""
로깅 유틸리티
"""
import logging
import os
from pathlib import Path
from datetime import datetime
from PySide6.QtCore import QStandardPaths


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
            
        # 로그 디렉토리 생성
        self.log_dir = self._get_log_dir()
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)
        
        # 로거 설정
        self.logger = logging.getLogger('DBMigration')
        self.logger.setLevel(logging.DEBUG)
        
        # 파일 핸들러
        log_file = os.path.join(
            self.log_dir, 
            f"migration_{datetime.now().strftime('%Y%m%d')}.log"
        )
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        
        # 포맷터
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        file_handler.setFormatter(formatter)
        
        # 핸들러 추가
        self.logger.addHandler(file_handler)
        
        self._initialized = True
        
    def _get_log_dir(self):
        """로그 디렉토리 경로 가져오기"""
        app_data_dir = QStandardPaths.writableLocation(
            QStandardPaths.AppDataLocation
        )
        return os.path.join(app_data_dir, "logs")
        
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