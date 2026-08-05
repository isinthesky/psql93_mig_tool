"""connection_mapper.py 단위 테스트"""

import sys

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLineEdit, QSpinBox

from src.ui.dialogs.connection_mapper import ENDPOINT_KIND_LABELS, ConnectionMapper


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture
def mock_widgets():
    host = QLineEdit()
    port = QSpinBox()
    port.setRange(1, 65535)
    database = QLineEdit()
    username = QLineEdit()
    password = QLineEdit()
    ssl = QCheckBox()

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


@pytest.fixture
def endpoint_widgets(mock_widgets):
    kind = QComboBox()
    kind.addItems(list(ENDPOINT_KIND_LABELS.values()))
    archive_path = QLineEdit()
    return {
        **mock_widgets,
        "kind": kind,
        "archive_path": archive_path,
    }


class TestConnectionMapper:
    def test_ui_to_profile_config(self, mock_widgets):
        config = ConnectionMapper.ui_to_profile_config(
            mock_widgets["host"],
            mock_widgets["port"],
            mock_widgets["database"],
            mock_widgets["username"],
            mock_widgets["password"],
            mock_widgets["ssl"],
        )

        assert config["kind"] == "postgres"
        assert config["host"] == "localhost"
        assert config["port"] == 5432
        assert config["database"] == "testdb"
        assert config["username"] == "testuser"
        assert config["password"] == "testpass"
        assert config["ssl"] is False

    def test_ui_to_profile_config_with_empty_host(self, mock_widgets):
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
        config = ConnectionMapper.ui_to_psycopg_config(
            mock_widgets["host"],
            mock_widgets["port"],
            mock_widgets["database"],
            mock_widgets["username"],
            mock_widgets["password"],
            mock_widgets["ssl"],
        )

        assert "dbname" in config
        assert "user" in config
        assert config["dbname"] == "testdb"
        assert config["user"] == "testuser"
        assert config["host"] == "localhost"
        assert config["port"] == 5432
        assert config["password"] == "testpass"
        assert "sslmode" not in config

    def test_ui_to_psycopg_config_with_ssl(self, mock_widgets):
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
        config = ConnectionMapper.ui_to_validation_config(
            mock_widgets["host"],
            mock_widgets["port"],
            mock_widgets["database"],
            mock_widgets["username"],
        )

        assert config["kind"] == "postgres"
        assert config["host"] == "localhost"
        assert config["port"] == 5432
        assert config["database"] == "testdb"
        assert config["username"] == "testuser"
        assert "password" not in config

    def test_set_ui_from_config(self, mock_widgets):
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

    def test_ui_to_endpoint_profile_config_file(self, endpoint_widgets):
        endpoint_widgets["kind"].setCurrentText("File Archive")
        endpoint_widgets["archive_path"].setText("/tmp/archive")

        config = ConnectionMapper.ui_to_endpoint_profile_config(
            kind_combo=endpoint_widgets["kind"],
            archive_path=endpoint_widgets["archive_path"],
            host=endpoint_widgets["host"],
            port=endpoint_widgets["port"],
            database=endpoint_widgets["database"],
            username=endpoint_widgets["username"],
            password=endpoint_widgets["password"],
            ssl=endpoint_widgets["ssl"],
        )

        assert config == {"kind": "file", "archive_path": "/tmp/archive"}

    def test_set_endpoint_ui_from_config_file(self, endpoint_widgets):
        config = {"kind": "file", "archive_path": "/tmp/archive2"}
        ConnectionMapper.set_endpoint_ui_from_config(
            config=config,
            kind_combo=endpoint_widgets["kind"],
            archive_path=endpoint_widgets["archive_path"],
            host=endpoint_widgets["host"],
            port=endpoint_widgets["port"],
            database=endpoint_widgets["database"],
            username=endpoint_widgets["username"],
            password=endpoint_widgets["password"],
            ssl=endpoint_widgets["ssl"],
        )
        assert endpoint_widgets["kind"].currentText() == "File Archive"
        assert endpoint_widgets["archive_path"].text() == "/tmp/archive2"
