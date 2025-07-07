"""
향상된 로깅 유틸리티
- SUCCESS 레벨 추가
- 민감정보 마스킹
- DB 저장 지원
- 세션 ID 관리
"""
import logging
import re
import random
from datetime import datetime
from typing import Optional, Dict, Any
from queue import Queue
from threading import Thread
import time

from PySide6.QtCore import QObject, Signal

from ..database.local_db import get_db, LogEntry
from .logger import MigrationLogger


# SUCCESS 레벨 추가
SUCCESS_LEVEL = 25  # INFO(20)와 WARNING(30) 사이
logging.addLevelName(SUCCESS_LEVEL, 'SUCCESS')


class EnhancedLogger:
    """향상된 마이그레이션 로거"""
    
    def __init__(self):
        # 기본 로거 생성
        self.base_logger = MigrationLogger()
        self.logger = self.base_logger.logger
        
        # EnhancedLogger 전용 속성
        self.session_id = None
        self.db_queue = Queue()
        self.db_thread = None
        self.is_running = True
        
        # DB 저장 스레드 시작
        self._start_db_thread()
        
    def _start_db_thread(self):
        """DB 저장 스레드 시작"""
        self.db_thread = Thread(target=self._db_writer, daemon=True)
        self.db_thread.start()
        
    def _db_writer(self):
        """백그라운드에서 로그를 DB에 저장"""
        db = get_db()
        
        while self.is_running:
            try:
                # 배치 처리를 위해 잠시 대기
                time.sleep(0.1)
                
                # 큐에서 로그 가져오기
                logs_to_save = []
                while not self.db_queue.empty() and len(logs_to_save) < 100:
                    try:
                        log_data = self.db_queue.get_nowait()
                        logs_to_save.append(log_data)
                    except:
                        break
                
                # DB에 저장
                if logs_to_save:
                    session = db.get_session()
                    try:
                        for log_data in logs_to_save:
                            log_entry = LogEntry(
                                timestamp=log_data['timestamp'],
                                session_id=log_data['session_id'],
                                level=log_data['level'],
                                logger_name=log_data['logger_name'],
                                message=log_data['message']
                            )
                            session.add(log_entry)
                        session.commit()
                    except Exception as e:
                        session.rollback()
                        print(f"로그 DB 저장 오류: {e}")
                    finally:
                        session.close()
                        
            except Exception as e:
                print(f"로그 스레드 오류: {e}")
                
    def generate_session_id(self) -> str:
        """세션 ID 생성"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        random_suffix = ''.join(random.choices('0123456789ABCDEF', k=4))
        self.session_id = f"{timestamp}_{random_suffix}"
        return self.session_id
        
    def set_session_id(self, session_id: str):
        """세션 ID 설정"""
        self.session_id = session_id
        
    def _mask_sensitive_data(self, message: str) -> str:
        """민감한 데이터 마스킹"""
        # 비밀번호 패턴 (첫 3글자만 보이고 나머지는 마스킹)
        patterns = [
            # password=value 형태
            (r'(password|pwd|pass)=([^\s]{0,3})([^\s]*)', r'\1=\2***'),
            # Password=value 형태
            (r'(Password|Pwd|Pass)=([^\s]{0,3})([^\s]*)', r'\1=\2***'),
            # "password": "value" 형태 (JSON)
            (r'"(password|pwd|pass)":\s*"([^"]{0,3})([^"]*)"', r'"\1": "\2***"'),
            # PostgreSQL 연결 문자열
            (r'(postgresql://[^:]+:)([^@]{0,3})([^@]*)(@)', r'\1\2***\4'),
        ]
        
        masked_message = message
        for pattern, replacement in patterns:
            masked_message = re.sub(pattern, replacement, masked_message, flags=re.IGNORECASE)
        
        return masked_message
        
    def _log_to_db(self, level: str, message: str, logger_name: str = 'DBMigration'):
        """DB에 로그 저장 (비동기)"""
        if not self.session_id:
            self.generate_session_id()
            
        log_data = {
            'timestamp': datetime.now(),
            'session_id': self.session_id,
            'level': level,
            'logger_name': logger_name,
            'message': message
        }
        
        # 큐에 추가 (논블로킹)
        try:
            self.db_queue.put_nowait(log_data)
        except:
            pass  # 큐가 가득 찬 경우 무시
            
    def _format_and_log(self, level: str, message: str, exc_info: bool = False):
        """포맷팅 및 로깅"""
        # 민감정보 마스킹
        masked_message = self._mask_sensitive_data(message)
        
        # 파일 로깅 (기존 방식)
        if level == 'DEBUG':
            self.logger.debug(masked_message, exc_info=exc_info)
        elif level == 'INFO':
            self.logger.info(masked_message, exc_info=exc_info)
        elif level == 'WARNING':
            self.logger.warning(masked_message, exc_info=exc_info)
        elif level == 'ERROR':
            self.logger.error(masked_message, exc_info=exc_info)
        elif level == 'CRITICAL':
            self.logger.critical(masked_message, exc_info=exc_info)
        elif level == 'SUCCESS':
            self.logger.log(SUCCESS_LEVEL, masked_message, exc_info=exc_info)
        
        # DB 로깅
        self._log_to_db(level, masked_message)
        
    def close(self):
        """로거 종료"""
        self.is_running = False
        if self.db_thread:
            self.db_thread.join(timeout=2.0)
            
    # MigrationLogger의 메서드들을 위임
    def debug(self, message: str):
        """디버그 로그"""
        self._format_and_log('DEBUG', message)
        
    def info(self, message: str):
        """정보 로그"""
        self._format_and_log('INFO', message)
        
    def warning(self, message: str):
        """경고 로그"""
        self._format_and_log('WARNING', message)
        
    def error(self, message: str, exc_info: bool = False):
        """오류 로그"""
        self._format_and_log('ERROR', message, exc_info)
        
    def critical(self, message: str, exc_info: bool = False):
        """치명적 오류 로그"""
        self._format_and_log('CRITICAL', message, exc_info)
        
    def success(self, message: str):
        """성공 로그"""
        self._format_and_log('SUCCESS', message)
            

class LogSignalEmitter(QObject):
    """Qt 시그널을 위한 로그 이미터"""
    log_signal = Signal(str, str, str, str)  # timestamp, session_id, level, message
    
    def __init__(self, logger=None):
        super().__init__()
        self.logger = logger or EnhancedLogger()
        
    def emit_log(self, level: str, message: str):
        """로그 발생 및 시그널 전송"""
        # 로거에 기록
        if level == 'SUCCESS' and hasattr(self.logger, 'success'):
            self.logger.success(message)
        elif level == 'ERROR':
            self.logger.error(message)
        elif level == 'WARNING':
            self.logger.warning(message)
        elif level == 'DEBUG':
            self.logger.debug(message)
        else:
            self.logger.info(message)
            
        # UI로 시그널 전송
        timestamp = datetime.now().strftime('%y%m%d %H:%M:%S')
        session_id = getattr(self.logger, 'session_id', None) or 'NO_SESSION'
        masked_message = message
        if hasattr(self.logger, '_mask_sensitive_data'):
            masked_message = self.logger._mask_sensitive_data(message)
        
        self.log_signal.emit(timestamp, session_id, level, masked_message)


# 전역 로거 인스턴스
enhanced_logger = EnhancedLogger()
log_emitter = LogSignalEmitter(enhanced_logger)