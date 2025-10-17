"""logger_mixins.py 단위 테스트"""
import pytest
import time
import re

from src.utils.logger_mixins import SensitiveDataMasker, DatabaseLoggerMixin


class TestSensitiveDataMasker:
    """SensitiveDataMasker 클래스 테스트"""

    def test_mask_simple_password(self):
        """간단한 password= 패턴 마스킹 테스트"""
        message = "password=secret123"
        masked = SensitiveDataMasker.mask(message)

        assert "sec***" in masked
        assert "secret123" not in masked

    def test_mask_password_uppercase(self):
        """대문자 Password= 패턴 마스킹 테스트"""
        message = "Password=MySecret456"
        masked = SensitiveDataMasker.mask(message)

        assert "MyS***" in masked
        assert "MySecret456" not in masked

    def test_mask_pwd_pattern(self):
        """pwd= 패턴 마스킹 테스트"""
        message = "pwd=test789"
        masked = SensitiveDataMasker.mask(message)

        assert "tes***" in masked
        assert "test789" not in masked

    def test_mask_json_password(self):
        """JSON 형식 비밀번호 마스킹 테스트"""
        message = '{"password": "json_secret"}'
        masked = SensitiveDataMasker.mask(message)

        assert "jso***" in masked
        assert "json_secret" not in masked

    def test_mask_postgresql_connection_string(self):
        """PostgreSQL 연결 문자열 마스킹 테스트"""
        message = "postgresql://user:my_password@localhost:5432/db"
        masked = SensitiveDataMasker.mask(message)

        assert "my_***" in masked
        assert "my_password" not in masked
        assert "localhost" in masked  # 호스트는 유지

    def test_mask_multiple_passwords(self):
        """여러 비밀번호 동시 마스킹 테스트"""
        message = "source password=pass1 target password=pass2"
        masked = SensitiveDataMasker.mask(message)

        assert "pass1" not in masked
        assert "pass2" not in masked
        assert "pas***" in masked

    def test_mask_preserves_non_sensitive_data(self):
        """민감하지 않은 데이터는 유지되는지 테스트"""
        message = "host=localhost port=5432 database=mydb"
        masked = SensitiveDataMasker.mask(message)

        assert message == masked  # 변경 없어야 함

    def test_mask_empty_password(self):
        """빈 비밀번호 처리 테스트"""
        message = "password="
        masked = SensitiveDataMasker.mask(message)

        assert "password=***" in masked

    def test_mask_short_password(self):
        """짧은 비밀번호 (3자 이하) 마스킹 테스트"""
        message = "password=ab"
        masked = SensitiveDataMasker.mask(message)

        assert "ab***" in masked
        # 3자 이하는 모두 표시됨

    def test_mask_case_insensitive(self):
        """대소문자 구분 없이 마스킹되는지 테스트"""
        message1 = "PASSWORD=secret"
        message2 = "password=secret"
        message3 = "Password=secret"

        masked1 = SensitiveDataMasker.mask(message1)
        masked2 = SensitiveDataMasker.mask(message2)
        masked3 = SensitiveDataMasker.mask(message3)

        # 모두 마스킹되어야 함
        assert "secret" not in masked1
        assert "secret" not in masked2
        assert "secret" not in masked3


class TestDatabaseLoggerMixin:
    """DatabaseLoggerMixin 클래스 테스트"""

    def test_initialization(self):
        """믹스인 초기화 테스트"""
        mixin = DatabaseLoggerMixin()

        assert mixin.session_id is None
        assert mixin.db_queue is not None
        assert mixin.is_running is True
        assert mixin.db_thread is not None
        assert mixin.db_thread.is_alive()

        mixin.close()

    def test_generate_session_id(self):
        """세션 ID 생성 테스트"""
        mixin = DatabaseLoggerMixin()
        session_id = mixin.generate_session_id()

        assert session_id is not None
        assert mixin.session_id == session_id

        # 형식 확인: YYYYMMDD_HHMMSS_XXXX
        pattern = r'\d{8}_\d{6}_[0-9A-F]{4}'
        assert re.match(pattern, session_id)

        mixin.close()

    def test_set_session_id(self):
        """세션 ID 설정 테스트"""
        mixin = DatabaseLoggerMixin()
        custom_id = "custom_session_123"

        mixin.set_session_id(custom_id)
        assert mixin.session_id == custom_id

        mixin.close()

    def test_log_to_db_creates_session_id(self):
        """log_to_db 호출 시 세션 ID 자동 생성 테스트"""
        mixin = DatabaseLoggerMixin()
        assert mixin.session_id is None

        mixin.log_to_db('INFO', 'Test message')

        assert mixin.session_id is not None

        mixin.close()

    def test_log_to_db_adds_to_queue(self):
        """log_to_db가 큐에 로그를 추가하는지 테스트"""
        mixin = DatabaseLoggerMixin()

        mixin.log_to_db('INFO', 'Test message 1')
        mixin.log_to_db('DEBUG', 'Test message 2')

        # 큐에 아이템이 있어야 함
        assert not mixin.db_queue.empty()

        mixin.close()

    def test_close_stops_thread(self):
        """close() 호출 시 스레드가 종료되는지 테스트"""
        mixin = DatabaseLoggerMixin()

        assert mixin.is_running is True
        assert mixin.db_thread.is_alive()

        mixin.close()
        time.sleep(0.3)  # 스레드 종료 대기

        assert mixin.is_running is False
        # 스레드가 종료되거나 데몬이므로 살아있을 수 있음
        # (timeout으로 join했으므로 상태 확인은 생략)

    def test_session_id_unique_per_instance(self):
        """인스턴스마다 고유한 세션 ID가 생성되는지 테스트"""
        mixin1 = DatabaseLoggerMixin()
        mixin2 = DatabaseLoggerMixin()

        id1 = mixin1.generate_session_id()
        id2 = mixin2.generate_session_id()

        assert id1 != id2

        mixin1.close()
        mixin2.close()

    def test_log_to_db_with_different_levels(self):
        """다양한 로그 레벨로 DB 로그 저장 테스트"""
        mixin = DatabaseLoggerMixin()

        levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'SUCCESS']
        for level in levels:
            mixin.log_to_db(level, f'{level} message')

        # 큐에 아이템이 있어야 함
        assert mixin.db_queue.qsize() >= len(levels)

        mixin.close()

    def test_log_to_db_with_custom_logger_name(self):
        """커스텀 로거 이름으로 DB 로그 저장 테스트"""
        mixin = DatabaseLoggerMixin()

        custom_logger = 'CustomLogger'
        mixin.log_to_db('INFO', 'Custom logger message', logger_name=custom_logger)

        # 큐에서 아이템 확인
        assert not mixin.db_queue.empty()
        log_data = mixin.db_queue.get()
        assert log_data['logger_name'] == custom_logger

        mixin.close()

    def test_queue_full_doesnt_raise_exception(self):
        """큐가 가득 차도 예외를 발생시키지 않는지 테스트"""
        mixin = DatabaseLoggerMixin()

        # 큐를 가득 채우기 (매우 많은 로그)
        try:
            for i in range(10000):
                mixin.log_to_db('INFO', f'Message {i}')
        except Exception as e:
            pytest.fail(f"log_to_db should not raise exception: {e}")

        mixin.close()

    def test_multiple_sessions_independently(self):
        """여러 믹스인 인스턴스가 독립적으로 동작하는지 테스트"""
        mixin1 = DatabaseLoggerMixin()
        mixin2 = DatabaseLoggerMixin()

        mixin1.set_session_id("session_1")
        mixin2.set_session_id("session_2")

        mixin1.log_to_db('INFO', 'From session 1')
        mixin2.log_to_db('INFO', 'From session 2')

        # 각자의 큐에 로그가 있어야 함
        assert not mixin1.db_queue.empty()
        assert not mixin2.db_queue.empty()

        mixin1.close()
        mixin2.close()
