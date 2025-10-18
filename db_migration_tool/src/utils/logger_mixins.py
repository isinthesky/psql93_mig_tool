"""로거 확장 기능 믹스인

민감정보 마스킹과 DB 로그 저장 기능을 제공합니다.
"""

import random
import re
import time
from datetime import datetime
from queue import Queue
from threading import Thread
from typing import Optional


class SensitiveDataMasker:
    """민감정보 마스킹 유틸리티

    로그 메시지에서 비밀번호, 연결 문자열 등의 민감정보를 마스킹합니다.

    Examples:
        >>> masker = SensitiveDataMasker()
        >>> masked = masker.mask("password=secret123")
        >>> print(masked)  # "password=sec***"
    """

    # 마스킹 패턴 (패턴, 치환 문자열) 쌍
    PATTERNS = [
        # password=value 형태
        (r"(password|pwd|pass)=([^\s]{0,3})([^\s]*)", r"\1=\2***"),
        # Password=value 형태 (대문자)
        (r"(Password|Pwd|Pass)=([^\s]{0,3})([^\s]*)", r"\1=\2***"),
        # "password": "value" 형태 (JSON)
        (r'"(password|pwd|pass)":\s*"([^"]{0,3})([^"]*)"', r'"\1": "\2***"'),
        # PostgreSQL 연결 문자열
        (r"(postgresql://[^:]+:)([^@]{0,3})([^@]*)(@)", r"\1\2***\4"),
    ]

    @classmethod
    def mask(cls, message: str) -> str:
        """민감한 데이터 마스킹

        Args:
            message: 원본 메시지

        Returns:
            마스킹된 메시지

        Examples:
            >>> message = "Connecting with password=mysecret123"
            >>> SensitiveDataMasker.mask(message)
            'Connecting with password=mys***'
        """
        masked = message
        for pattern, replacement in cls.PATTERNS:
            masked = re.sub(pattern, replacement, masked, flags=re.IGNORECASE)
        return masked


class DatabaseLoggerMixin:
    """DB 로그 저장 믹스인

    로그를 비동기로 DB에 저장하는 기능을 제공합니다.
    별도 스레드에서 큐를 통해 배치로 처리합니다.

    Attributes:
        session_id: 현재 세션 ID
        db_queue: 로그 데이터 큐
        is_running: 스레드 실행 상태
        db_thread: DB 저장 스레드

    Examples:
        >>> class MyLogger(DatabaseLoggerMixin):
        ...     def __init__(self):
        ...         DatabaseLoggerMixin.__init__(self)
        ...
        >>> logger = MyLogger()
        >>> logger.log_to_db('INFO', 'Test message')
        >>> logger.close()
    """

    def __init__(self):
        """DB 로거 믹스인 초기화"""
        self.session_id: Optional[str] = None
        self.db_queue = Queue()
        self.is_running = True
        self.db_thread: Optional[Thread] = None
        self._start_db_thread()

    def _start_db_thread(self):
        """DB 저장 스레드 시작"""
        self.db_thread = Thread(target=self._db_writer, daemon=True)
        self.db_thread.start()

    def _db_writer(self):
        """백그라운드 DB 저장 워커

        큐에서 로그를 가져와 배치로 DB에 저장합니다.
        """
        # 지연 import (순환 의존성 방지)
        try:
            from ..database.local_db import LogEntry, get_db
        except ImportError:
            # DB 모듈이 없으면 종료
            return

        db = get_db()

        while self.is_running:
            try:
                # 배치 처리를 위해 잠시 대기
                time.sleep(0.1)

                # 큐에서 로그 가져오기 (최대 100개)
                logs_to_save = []
                while not self.db_queue.empty() and len(logs_to_save) < 100:
                    try:
                        log_data = self.db_queue.get_nowait()
                        logs_to_save.append(log_data)
                    except Exception:
                        break

                # DB에 저장
                if logs_to_save:
                    session = db.get_session()
                    try:
                        for log_data in logs_to_save:
                            log_entry = LogEntry(
                                timestamp=log_data["timestamp"],
                                session_id=log_data["session_id"],
                                level=log_data["level"],
                                logger_name=log_data["logger_name"],
                                message=log_data["message"],
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

    def log_to_db(self, level: str, message: str, logger_name: str = "DBMigration"):
        """DB에 로그 저장 (비동기)

        Args:
            level: 로그 레벨 (DEBUG, INFO, WARNING, ERROR, CRITICAL, SUCCESS)
            message: 로그 메시지
            logger_name: 로거 이름

        Examples:
            >>> mixin = DatabaseLoggerMixin()
            >>> mixin.log_to_db('INFO', 'Application started')
        """
        if not self.session_id:
            self.generate_session_id()

        log_data = {
            "timestamp": datetime.now(),
            "session_id": self.session_id,
            "level": level,
            "logger_name": logger_name,
            "message": message,
        }

        # 큐에 추가 (논블로킹)
        try:
            self.db_queue.put_nowait(log_data)
        except Exception:
            pass  # 큐가 가득 찬 경우 무시

    def generate_session_id(self) -> str:
        """세션 ID 생성

        Returns:
            생성된 세션 ID (형식: YYYYMMDD_HHMMSS_XXXX)

        Examples:
            >>> mixin = DatabaseLoggerMixin()
            >>> session_id = mixin.generate_session_id()
            >>> print(session_id)  # '20250118_143022_A3F5'
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        random_suffix = "".join(random.choices("0123456789ABCDEF", k=4))
        self.session_id = f"{timestamp}_{random_suffix}"
        return self.session_id

    def set_session_id(self, session_id: str):
        """세션 ID 설정

        Args:
            session_id: 세션 ID

        Examples:
            >>> mixin = DatabaseLoggerMixin()
            >>> mixin.set_session_id('custom_session_123')
        """
        self.session_id = session_id

    def close(self):
        """로거 종료 및 스레드 정리

        DB 큐를 비우고 스레드를 안전하게 종료합니다.

        Examples:
            >>> mixin = DatabaseLoggerMixin()
            >>> # ... 로깅 작업
            >>> mixin.close()
        """
        self.is_running = False
        if self.db_thread:
            self.db_thread.join(timeout=2.0)
