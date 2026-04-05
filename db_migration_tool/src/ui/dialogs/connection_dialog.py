"""연결 설정 다이얼로그"""

from __future__ import annotations

from pathlib import Path

import psycopg
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.database.version_info import parse_version_string
from src.models.profile import (
    ENDPOINT_KIND_FILE,
    ENDPOINT_KIND_POSTGRES,
    ConnectionProfile,
)
from src.utils.validators import ConnectionValidator, VersionValidator

from .connection_mapper import (
    COMPAT_LABEL_TO_MODE,
    COMPAT_MODE_LABELS,
    ENDPOINT_KIND_LABELS,
    ConnectionMapper,
)


class ConnectionDialog(QDialog):
    """연결 설정 다이얼로그"""

    def __init__(self, parent=None, profile: ConnectionProfile | None = None):
        super().__init__(parent)
        self.profile = profile
        self.is_edit_mode = profile is not None
        self.endpoint_widgets: dict[str, dict] = {}

        self.setup_ui()
        if self.is_edit_mode:
            self.load_profile_data()

    def setup_ui(self):
        self.setWindowTitle("연결 편집" if self.is_edit_mode else "새 연결")
        self.setModal(True)
        self.resize(620, 470)

        layout = QVBoxLayout(self)

        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("프로필 이름:"))
        self.name_edit = QLineEdit()
        name_layout.addWidget(self.name_edit)
        layout.addLayout(name_layout)

        self.tab_widget = QTabWidget()
        self.source_tab = self.create_endpoint_tab("source", "소스")
        self.target_tab = self.create_endpoint_tab("target", "대상")
        self.tab_widget.addTab(self.source_tab, "소스")
        self.tab_widget.addTab(self.target_tab, "대상")
        layout.addWidget(self.tab_widget)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)

        self.test_source_btn = QPushButton("소스 테스트")
        self.test_source_btn.clicked.connect(lambda: self.test_connection("source"))
        button_box.addButton(self.test_source_btn, QDialogButtonBox.ActionRole)

        self.test_target_btn = QPushButton("대상 테스트")
        self.test_target_btn.clicked.connect(lambda: self.test_connection("target"))
        button_box.addButton(self.test_target_btn, QDialogButtonBox.ActionRole)

        button_box.setStyleSheet(
            """
            QPushButton {
                font-size: 16px;
                padding: 10px 18px;
                min-height: 40px;
                font-weight: bold;
            }
            """
        )
        layout.addWidget(button_box)

    def create_endpoint_tab(self, key: str, title: str) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        kind_row = QHBoxLayout()
        kind_row.addWidget(QLabel("엔드포인트 종류:"))
        kind_combo = QComboBox()
        kind_combo.addItems(list(ENDPOINT_KIND_LABELS.values()))
        kind_row.addWidget(kind_combo)
        kind_row.addStretch(1)
        layout.addLayout(kind_row)

        stacked = QStackedWidget()

        postgres_widget = QWidget()
        postgres_layout = QFormLayout(postgres_widget)

        host_edit = QLineEdit()
        host_edit.setPlaceholderText("localhost")
        postgres_layout.addRow("호스트:", host_edit)

        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setValue(5432)
        postgres_layout.addRow("포트:", port_spin)

        database_edit = QLineEdit()
        database_edit.setPlaceholderText("데이터베이스명")
        postgres_layout.addRow("데이터베이스:", database_edit)

        username_edit = QLineEdit()
        username_edit.setPlaceholderText("사용자명")
        postgres_layout.addRow("사용자명:", username_edit)

        password_edit = QLineEdit()
        password_edit.setEchoMode(QLineEdit.Password)
        password_edit.setPlaceholderText("비밀번호")
        postgres_layout.addRow("비밀번호:", password_edit)

        ssl_check = QCheckBox("SSL 연결 사용")
        postgres_layout.addRow("", ssl_check)

        compat_combo = QComboBox()
        compat_combo.addItems(list(COMPAT_MODE_LABELS.values()))
        compat_combo.setToolTip(
            "자동 감지: 연결 시 버전을 자동으로 감지합니다.\n"
            "PostgreSQL 9.3: 9.3 호환 모드를 강제합니다.\n"
            "PostgreSQL 16: 16 최적화 모드를 강제합니다."
        )
        postgres_layout.addRow("호환 모드:", compat_combo)

        file_widget = QWidget()
        file_layout = QFormLayout(file_widget)
        archive_row = QHBoxLayout()
        archive_path_edit = QLineEdit()
        archive_path_edit.setPlaceholderText("아카이브 폴더 경로")
        browse_btn = QPushButton("찾아보기")
        browse_btn.clicked.connect(lambda: self.browse_archive_path(key))
        archive_row.addWidget(archive_path_edit)
        archive_row.addWidget(browse_btn)
        file_layout.addRow("아카이브 경로:", archive_row)

        hint = QLabel(
            "- 소스 File Archive: manifest.json 이 있는 폴더\n"
            "- 대상 File Archive: 내보낼 새/기존 아카이브 폴더"
        )
        hint.setStyleSheet("color: #888888;")
        hint.setWordWrap(True)
        file_layout.addRow("", hint)

        stacked.addWidget(postgres_widget)
        stacked.addWidget(file_widget)
        layout.addWidget(stacked)
        layout.addStretch(1)

        kind_combo.currentIndexChanged.connect(
            lambda _idx, side=key: self.on_endpoint_kind_changed(side)
        )

        self.endpoint_widgets[key] = {
            "kind": kind_combo,
            "stack": stacked,
            "archive_path": archive_path_edit,
            "host": host_edit,
            "port": port_spin,
            "database": database_edit,
            "username": username_edit,
            "password": password_edit,
            "ssl": ssl_check,
            "compat_mode": compat_combo,
            "title": title,
        }
        self.on_endpoint_kind_changed(key)
        return widget

    def browse_archive_path(self, side: str):
        current = self.endpoint_widgets[side]["archive_path"].text().strip()
        start_dir = current or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "아카이브 폴더 선택", start_dir)
        if selected:
            self.endpoint_widgets[side]["archive_path"].setText(selected)

    def on_endpoint_kind_changed(self, side: str):
        widgets = self.endpoint_widgets[side]
        kind = ConnectionMapper.endpoint_kind_from_ui(widgets["kind"])
        widgets["stack"].setCurrentIndex(1 if kind == ENDPOINT_KIND_FILE else 0)

    def load_profile_data(self):
        if not self.profile:
            return

        self.name_edit.setText(self.profile.name)

        ConnectionMapper.set_endpoint_ui_from_config(
            config=self.profile.source_config,
            kind_combo=self.endpoint_widgets["source"]["kind"],
            archive_path=self.endpoint_widgets["source"]["archive_path"],
            host=self.endpoint_widgets["source"]["host"],
            port=self.endpoint_widgets["source"]["port"],
            database=self.endpoint_widgets["source"]["database"],
            username=self.endpoint_widgets["source"]["username"],
            password=self.endpoint_widgets["source"]["password"],
            ssl=self.endpoint_widgets["source"]["ssl"],
            compat_mode=self.endpoint_widgets["source"]["compat_mode"],
        )
        self.on_endpoint_kind_changed("source")

        ConnectionMapper.set_endpoint_ui_from_config(
            config=self.profile.target_config,
            kind_combo=self.endpoint_widgets["target"]["kind"],
            archive_path=self.endpoint_widgets["target"]["archive_path"],
            host=self.endpoint_widgets["target"]["host"],
            port=self.endpoint_widgets["target"]["port"],
            database=self.endpoint_widgets["target"]["database"],
            username=self.endpoint_widgets["target"]["username"],
            password=self.endpoint_widgets["target"]["password"],
            ssl=self.endpoint_widgets["target"]["ssl"],
            compat_mode=self.endpoint_widgets["target"]["compat_mode"],
        )
        self.on_endpoint_kind_changed("target")

    def _get_endpoint_profile_config(self, side: str) -> dict:
        widgets = self.endpoint_widgets[side]
        return ConnectionMapper.ui_to_endpoint_profile_config(
            kind_combo=widgets["kind"],
            archive_path=widgets["archive_path"],
            host=widgets["host"],
            port=widgets["port"],
            database=widgets["database"],
            username=widgets["username"],
            password=widgets["password"],
            ssl=widgets["ssl"],
            compat_mode=widgets["compat_mode"],
        )

    def _get_endpoint_validation_config(self, side: str) -> dict:
        widgets = self.endpoint_widgets[side]
        return ConnectionMapper.ui_to_endpoint_validation_config(
            kind_combo=widgets["kind"],
            archive_path=widgets["archive_path"],
            host=widgets["host"],
            port=widgets["port"],
            database=widgets["database"],
            username=widgets["username"],
        )

    def get_profile_data(self):
        return {
            "name": self.name_edit.text().strip(),
            "source_config": self._get_endpoint_profile_config("source"),
            "target_config": self._get_endpoint_profile_config("target"),
        }

    def test_connection(self, side: str):
        config = self._get_endpoint_profile_config(side)
        endpoint_title = self.endpoint_widgets[side]["title"]

        if config.get("kind") == ENDPOINT_KIND_FILE:
            must_exist = side == "source"
            valid, msg = ConnectionValidator.validate_file_archive_config(
                config,
                must_exist=must_exist,
            )
            if not valid:
                QMessageBox.critical(self, "검증 실패", msg)
                return

            path = Path(config["archive_path"]).expanduser()
            if must_exist:
                QMessageBox.information(
                    self,
                    "검증 성공",
                    f"{endpoint_title} File Archive 경로를 확인했습니다.\n\n{path}",
                )
            else:
                QMessageBox.information(
                    self,
                    "검증 성공",
                    f"{endpoint_title} File Archive 출력 경로를 사용할 수 있습니다.\n\n{path}",
                )
            return

        widgets = self.endpoint_widgets[side]
        psycopg_config = ConnectionMapper.ui_to_psycopg_config(
            widgets["host"],
            widgets["port"],
            widgets["database"],
            widgets["username"],
            widgets["password"],
            widgets["ssl"],
        )

        try:
            conn = psycopg.connect(**psycopg_config, connect_timeout=7)
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                version_str = cur.fetchone()[0]
                version_info = parse_version_string(version_str)
            conn.close()

            QMessageBox.information(
                self,
                "연결 성공",
                f"{endpoint_title} PostgreSQL 연결에 성공했습니다.\n\n"
                f"버전: {version_info}\n"
                f"원본: {version_str}",
            )
        except Exception as e:
            QMessageBox.critical(
                self,
                "연결 실패",
                f"{endpoint_title} 연결에 실패했습니다:\n\n{str(e)}",
            )

    def accept(self):
        name = self.name_edit.text().strip()
        valid, msg = ConnectionValidator.validate_profile_name(name)
        if not valid:
            QMessageBox.warning(self, "입력 오류", msg)
            return

        source_profile = self._get_endpoint_profile_config("source")
        target_profile = self._get_endpoint_profile_config("target")
        source_validation = self._get_endpoint_validation_config("source")
        target_validation = self._get_endpoint_validation_config("target")

        valid, msg = ConnectionValidator.validate_connection_config(source_validation)
        if not valid:
            QMessageBox.warning(self, "소스 오류", msg)
            return

        valid, msg = ConnectionValidator.validate_connection_config(target_validation)
        if not valid:
            QMessageBox.warning(self, "대상 오류", msg)
            return

        if source_profile.get("kind") == ENDPOINT_KIND_FILE:
            valid, msg = ConnectionValidator.validate_file_archive_config(
                source_profile,
                must_exist=True,
            )
            if not valid:
                QMessageBox.warning(self, "소스 아카이브 오류", msg)
                return
        else:
            source_compat = COMPAT_LABEL_TO_MODE.get(
                self.endpoint_widgets["source"]["compat_mode"].currentText(),
                "auto",
            )
            valid, msg = VersionValidator.validate_compat_mode(source_compat)
            if not valid:
                QMessageBox.warning(self, "소스 DB 호환 모드 오류", msg)
                return

        if target_profile.get("kind") == ENDPOINT_KIND_FILE:
            valid, msg = ConnectionValidator.validate_file_archive_config(
                target_profile,
                must_exist=False,
            )
            if not valid:
                QMessageBox.warning(self, "대상 아카이브 오류", msg)
                return
        else:
            target_compat = COMPAT_LABEL_TO_MODE.get(
                self.endpoint_widgets["target"]["compat_mode"].currentText(),
                "auto",
            )
            valid, msg = VersionValidator.validate_compat_mode(target_compat)
            if not valid:
                QMessageBox.warning(self, "대상 DB 호환 모드 오류", msg)
                return

        valid, msg = ConnectionValidator.validate_endpoint_pair(source_profile, target_profile)
        if not valid:
            QMessageBox.warning(self, "프로필 오류", msg)
            return

        super().accept()
