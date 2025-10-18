"""connection_mapper.py 단위 테스트"""

import sys

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox, QLineEdit, QSpinBox

from src.ui.dialogs.connection_mapper import ConnectionMapper, ConnectionWidgetSet


# QApplication 필요
@pytest.fixture(scope="module", autouse=True)
def qapp():
    """Qt Application 픽스처"""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture
def mock_widgets():
    """목 위젯 생성"""
    host = QLineEdit()
    port = QSpinBox()
    port.setRange(1, 65535)  # 포트 범위 설정
    database = QLineEdit()
    username = QLineEdit()
    password = QLineEdit()
    ssl = QCheckBox()

    # 기본값 설정
    host.setText("localhost")
    port.setValue(5432)
    database.setText("testdb")
    username.setText("testuser")
    password.setText("testpass")
    ssl.setChecked(False)

    return {
        "host": host,
        "port": port,
        "database": database,
        "username": username,
        "password": password,
        "ssl": ssl,
    }


class TestConnectionMapper:
    """ConnectionMapper 클래스 테스트"""

    def test_ui_to_profile_config(self, mock_widgets):
        """UI → 프로필 Dict 변환 테스트"""
        config = ConnectionMapper.ui_to_profile_config(
            mock_widgets["host"],
            mock_widgets["port"],
            mock_widgets["database"],
            mock_widgets["username"],
            mock_widgets["password"],
            mock_widgets["ssl"],
        )

        assert config["host"] == "localhost"
        assert config["port"] == 5432
        assert config["database"] == "testdb"
        assert config["username"] == "testuser"
        assert config["password"] == "testpass"
        assert config["ssl"] is False

    def test_ui_to_profile_config_with_empty_host(self, mock_widgets):
        """빈 호스트는 localhost로 대체"""
        mock_widgets["host"].setText("")
        config = ConnectionMapper.ui_to_profile_config(
            mock_widgets["host"],
            mock_widgets["port"],
            mock_widgets["database"],
            mock_widgets["username"],
            mock_widgets["password"],
            mock_widgets["ssl"],
        )

        assert config["host"] == "localhost"

    def test_ui_to_psycopg_config(self, mock_widgets):
        """UI → psycopg Dict 변환 테스트"""
        config = ConnectionMapper.ui_to_psycopg_config(
            mock_widgets["host"],
            mock_widgets["port"],
            mock_widgets["database"],
            mock_widgets["username"],
            mock_widgets["password"],
            mock_widgets["ssl"],
        )

        # psycopg 키 이름 확인
        assert "dbname" in config
        assert "user" in config
        assert config["dbname"] == "testdb"
        assert config["user"] == "testuser"
        assert config["host"] == "localhost"
        assert config["port"] == 5432
        assert config["password"] == "testpass"
        assert "sslmode" not in config  # SSL이 False일 때

    def test_ui_to_psycopg_config_with_ssl(self, mock_widgets):
        """SSL 활성화 시 sslmode 추가"""
        mock_widgets["ssl"].setChecked(True)
        config = ConnectionMapper.ui_to_psycopg_config(
            mock_widgets["host"],
            mock_widgets["port"],
            mock_widgets["database"],
            mock_widgets["username"],
            mock_widgets["password"],
            mock_widgets["ssl"],
        )

        assert config["sslmode"] == "require"

    def test_ui_to_validation_config(self, mock_widgets):
        """UI → 검증용 Dict 변환 테스트"""
        config = ConnectionMapper.ui_to_validation_config(
            mock_widgets["host"],
            mock_widgets["port"],
            mock_widgets["database"],
            mock_widgets["username"],
        )

        assert config["host"] == "localhost"
        assert config["port"] == 5432
        assert config["database"] == "testdb"
        assert config["username"] == "testuser"
        # 비밀번호는 검증용에 포함 안 됨
        assert "password" not in config

    def test_profile_config_to_ui(self):
        """프로필 Dict → UI 값 튜플 변환 테스트"""
        config = {
            "host": "192.168.1.1",
            "port": 5433,
            "database": "mydb",
            "username": "admin",
            "password": "secret",
            "ssl": True,
        }

        host, port, db, user, pwd, ssl = ConnectionMapper.profile_config_to_ui(config)

        assert host == "192.168.1.1"
        assert port == 5433
        assert db == "mydb"
        assert user == "admin"
        assert pwd == "secret"
        assert ssl is True

    def test_profile_config_to_ui_with_defaults(self):
        """기본값 적용 테스트"""
        config = {}  # 빈 설정

        host, port, db, user, pwd, ssl = ConnectionMapper.profile_config_to_ui(config)

        assert host == "localhost"
        assert port == 5432
        assert db == ""
        assert user == ""
        assert pwd == ""
        assert ssl is False

    def test_set_ui_from_config(self, mock_widgets):
        """프로필 Dict → UI 위젯 설정 테스트"""
        config = {
            "host": "10.0.0.1",
            "port": 5433,
            "database": "proddb",
            "username": "produser",
            "password": "prodpass",
            "ssl": True,
        }

        ConnectionMapper.set_ui_from_config(
            config,
            mock_widgets["host"],
            mock_widgets["port"],
            mock_widgets["database"],
            mock_widgets["username"],
            mock_widgets["password"],
            mock_widgets["ssl"],
        )

        assert mock_widgets["host"].text() == "10.0.0.1"
        assert mock_widgets["port"].value() == 5433
        assert mock_widgets["database"].text() == "proddb"
        assert mock_widgets["username"].text() == "produser"
        assert mock_widgets["password"].text() == "prodpass"
        assert mock_widgets["ssl"].isChecked() is True


class TestConnectionWidgetSet:
    """ConnectionWidgetSet 클래스 테스트"""

    def test_initialization(self, mock_widgets):
        """위젯 세트 초기화 테스트"""
        widget_set = ConnectionWidgetSet(
            mock_widgets["host"],
            mock_widgets["port"],
            mock_widgets["database"],
            mock_widgets["username"],
            mock_widgets["password"],
            mock_widgets["ssl"],
        )

        assert widget_set.host is mock_widgets["host"]
        assert widget_set.port is mock_widgets["port"]
        assert widget_set.database is mock_widgets["database"]
        assert widget_set.username is mock_widgets["username"]
        assert widget_set.password is mock_widgets["password"]
        assert widget_set.ssl is mock_widgets["ssl"]

    def test_to_profile_config(self, mock_widgets):
        """위젯 세트 → 프로필 Dict 변환 테스트"""
        widget_set = ConnectionWidgetSet(**mock_widgets)
        config = widget_set.to_profile_config()

        assert config["host"] == "localhost"
        assert config["port"] == 5432
        assert config["database"] == "testdb"

    def test_to_psycopg_config(self, mock_widgets):
        """위젯 세트 → psycopg Dict 변환 테스트"""
        widget_set = ConnectionWidgetSet(**mock_widgets)
        config = widget_set.to_psycopg_config()

        assert "dbname" in config
        assert "user" in config
        assert config["dbname"] == "testdb"

    def test_to_validation_config(self, mock_widgets):
        """위젯 세트 → 검증용 Dict 변환 테스트"""
        widget_set = ConnectionWidgetSet(**mock_widgets)
        config = widget_set.to_validation_config()

        assert "password" not in config
        assert "host" in config
        assert "database" in config

    def test_load_from_config(self, mock_widgets):
        """프로필 Dict → 위젯 세트 로드 테스트"""
        widget_set = ConnectionWidgetSet(**mock_widgets)

        new_config = {
            "host": "newhost",
            "port": 9999,
            "database": "newdb",
            "username": "newuser",
            "password": "newpass",
            "ssl": True,
        }

        widget_set.load_from_config(new_config)

        assert mock_widgets["host"].text() == "newhost"
        assert mock_widgets["port"].value() == 9999
        assert mock_widgets["database"].text() == "newdb"
        assert mock_widgets["username"].text() == "newuser"
        assert mock_widgets["password"].text() == "newpass"
        assert mock_widgets["ssl"].isChecked() is True

    def test_round_trip_conversion(self, mock_widgets):
        """왕복 변환 테스트 (저장 → 로드 → 저장)"""
        widget_set = ConnectionWidgetSet(**mock_widgets)

        # 1. UI → Dict
        config1 = widget_set.to_profile_config()

        # 2. Dict → UI
        mock_widgets["host"].setText("")  # 값 변경
        widget_set.load_from_config(config1)

        # 3. UI → Dict (다시)
        config2 = widget_set.to_profile_config()

        # 동일해야 함
        assert config1 == config2
