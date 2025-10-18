"""
입력 검증 유틸리티
"""

import re
from typing import Any


class ConnectionValidator:
    """연결 정보 검증"""

    @staticmethod
    def validate_connection_config(config: dict[str, Any]) -> tuple[bool, str]:
        """연결 설정 검증"""
        # 필수 필드 확인
        required_fields = ["host", "port", "database", "username"]
        for field in required_fields:
            if not config.get(field):
                return False, f"{field}는 필수 입력 항목입니다."

        # 호스트 검증
        host = config["host"]
        if not host or len(host) > 255:
            return False, "올바른 호스트 주소를 입력하세요."

        # 포트 검증
        port = config["port"]
        if not isinstance(port, int) or port < 1 or port > 65535:
            return False, "포트는 1-65535 사이의 숫자여야 합니다."

        # 데이터베이스명 검증
        database = config["database"]
        if not re.match(r"^[a-zA-Z0-9_]+$", database):
            return False, "데이터베이스명은 영문자, 숫자, 언더스코어만 사용 가능합니다."

        # 사용자명 검증
        username = config["username"]
        if not re.match(r"^[a-zA-Z0-9_]+$", username):
            return False, "사용자명은 영문자, 숫자, 언더스코어만 사용 가능합니다."

        return True, ""

    @staticmethod
    def validate_profile_name(name: str) -> tuple[bool, str]:
        """프로필 이름 검증"""
        if not name or not name.strip():
            return False, "프로필 이름을 입력하세요."

        if len(name) > 100:
            return False, "프로필 이름은 100자 이하여야 합니다."

        if not re.match(r"^[a-zA-Z0-9가-힣\s\-_]+$", name):
            return False, "프로필 이름에 특수문자를 사용할 수 없습니다."

        return True, ""


class DateValidator:
    """날짜 검증"""

    @staticmethod
    def validate_date_range(start_date, end_date) -> tuple[bool, str]:
        """날짜 범위 검증"""
        if not start_date or not end_date:
            return False, "시작 날짜와 종료 날짜를 선택하세요."

        if start_date > end_date:
            return False, "시작 날짜가 종료 날짜보다 늦습니다."

        # 날짜 차이 확인 (최대 1년)
        delta = end_date - start_date
        if delta.days > 365:
            return False, "날짜 범위는 최대 1년까지 선택 가능합니다."

        return True, ""
