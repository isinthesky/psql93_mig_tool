"""연결 설정 UI ↔ Dict 매핑 유틸리티"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QSpinBox

from src.models.profile import ENDPOINT_KIND_FILE, ENDPOINT_KIND_POSTGRES

COMPAT_MODE_AUTO = "auto"
COMPAT_MODE_9_3 = "9.3"
COMPAT_MODE_16 = "16"

COMPAT_MODE_LABELS = {
    COMPAT_MODE_AUTO: "자동 감지",
    COMPAT_MODE_9_3: "PostgreSQL 9.3",
    COMPAT_MODE_16: "PostgreSQL 16",
}
COMPAT_LABEL_TO_MODE = {v: k for k, v in COMPAT_MODE_LABELS.items()}

ENDPOINT_KIND_LABELS = {
    ENDPOINT_KIND_POSTGRES: "PostgreSQL",
    ENDPOINT_KIND_FILE: "File Archive",
}
ENDPOINT_LABEL_TO_KIND = {v: k for k, v in ENDPOINT_KIND_LABELS.items()}


class ConnectionMapper:
    """DB/파일 아카이브 연결 설정 UI 헬퍼"""

    @staticmethod
    def ui_to_profile_config(
        host: QLineEdit,
        port: QSpinBox,
        database: QLineEdit,
        username: QLineEdit,
        password: QLineEdit,
        ssl: QCheckBox,
        compat_mode: QComboBox | None = None,
    ) -> dict[str, Any]:
        config = {
            "kind": ENDPOINT_KIND_POSTGRES,
            "host": host.text().strip() or "localhost",
            "port": port.value(),
            "database": database.text().strip(),
            "username": username.text().strip(),
            "password": password.text(),
            "ssl": ssl.isChecked(),
        }

        if compat_mode is not None:
            label = compat_mode.currentText()
            config["compat_mode"] = COMPAT_LABEL_TO_MODE.get(label, COMPAT_MODE_AUTO)
        else:
            config["compat_mode"] = COMPAT_MODE_AUTO

        return config

    @staticmethod
    def ui_to_psycopg_config(
        host: QLineEdit,
        port: QSpinBox,
        database: QLineEdit,
        username: QLineEdit,
        password: QLineEdit,
        ssl: QCheckBox,
    ) -> dict[str, Any]:
        config = {
            "host": host.text().strip() or "localhost",
            "port": port.value(),
            "dbname": database.text().strip(),
            "user": username.text().strip(),
            "password": password.text(),
        }

        if ssl.isChecked():
            config["sslmode"] = "require"

        return config

    @staticmethod
    def ui_to_validation_config(
        host: QLineEdit,
        port: QSpinBox,
        database: QLineEdit,
        username: QLineEdit,
    ) -> dict[str, Any]:
        return {
            "kind": ENDPOINT_KIND_POSTGRES,
            "host": host.text().strip() or "localhost",
            "port": port.value(),
            "database": database.text().strip(),
            "username": username.text().strip(),
        }

    @staticmethod
    def set_ui_from_config(
        config: dict[str, Any],
        host: QLineEdit,
        port: QSpinBox,
        database: QLineEdit,
        username: QLineEdit,
        password: QLineEdit,
        ssl: QCheckBox,
        compat_mode: QComboBox | None = None,
    ):
        host.setText(config.get("host", "localhost"))
        port.setValue(config.get("port", 5432))
        database.setText(config.get("database", ""))
        username.setText(config.get("username", ""))
        password.setText(config.get("password", ""))
        ssl.setChecked(config.get("ssl", False))

        if compat_mode is not None:
            mode = config.get("compat_mode", COMPAT_MODE_AUTO)
            label = COMPAT_MODE_LABELS.get(mode, COMPAT_MODE_LABELS[COMPAT_MODE_AUTO])
            index = compat_mode.findText(label)
            if index >= 0:
                compat_mode.setCurrentIndex(index)

    @staticmethod
    def endpoint_kind_from_ui(kind_combo: QComboBox | None) -> str:
        if kind_combo is None:
            return ENDPOINT_KIND_POSTGRES
        return ENDPOINT_LABEL_TO_KIND.get(kind_combo.currentText(), ENDPOINT_KIND_POSTGRES)

    @staticmethod
    def ui_to_endpoint_profile_config(
        *,
        kind_combo: QComboBox,
        archive_path: QLineEdit,
        host: QLineEdit,
        port: QSpinBox,
        database: QLineEdit,
        username: QLineEdit,
        password: QLineEdit,
        ssl: QCheckBox,
        compat_mode: QComboBox | None = None,
    ) -> dict[str, Any]:
        kind = ConnectionMapper.endpoint_kind_from_ui(kind_combo)
        if kind == ENDPOINT_KIND_FILE:
            return {
                "kind": ENDPOINT_KIND_FILE,
                "archive_path": archive_path.text().strip(),
            }
        return ConnectionMapper.ui_to_profile_config(
            host,
            port,
            database,
            username,
            password,
            ssl,
            compat_mode,
        )

    @staticmethod
    def ui_to_endpoint_validation_config(
        *,
        kind_combo: QComboBox,
        archive_path: QLineEdit,
        host: QLineEdit,
        port: QSpinBox,
        database: QLineEdit,
        username: QLineEdit,
    ) -> dict[str, Any]:
        kind = ConnectionMapper.endpoint_kind_from_ui(kind_combo)
        if kind == ENDPOINT_KIND_FILE:
            return {
                "kind": ENDPOINT_KIND_FILE,
                "archive_path": archive_path.text().strip(),
            }
        return ConnectionMapper.ui_to_validation_config(host, port, database, username)

    @staticmethod
    def set_endpoint_ui_from_config(
        *,
        config: dict[str, Any],
        kind_combo: QComboBox,
        archive_path: QLineEdit,
        host: QLineEdit,
        port: QSpinBox,
        database: QLineEdit,
        username: QLineEdit,
        password: QLineEdit,
        ssl: QCheckBox,
        compat_mode: QComboBox | None = None,
    ):
        kind = config.get("kind", ENDPOINT_KIND_POSTGRES)
        label = ENDPOINT_KIND_LABELS.get(kind, ENDPOINT_KIND_LABELS[ENDPOINT_KIND_POSTGRES])
        index = kind_combo.findText(label)
        if index >= 0:
            kind_combo.setCurrentIndex(index)

        archive_path.setText(config.get("archive_path", ""))
        ConnectionMapper.set_ui_from_config(
            config,
            host,
            port,
            database,
            username,
            password,
            ssl,
            compat_mode,
        )
