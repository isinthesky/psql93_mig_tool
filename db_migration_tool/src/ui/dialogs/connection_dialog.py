"""연결 설정 다이얼로그"""

from __future__ import annotations

from pathlib import Path

import psycopg
from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication,
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
    ConnectionProfile,
)
from src.models.saved_connection import SavedConnectionManager
from src.ui.widgets import StatusLamp
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
        self.saved_conn_manager = SavedConnectionManager()
        self._preset_items: dict[str, list[dict]] = {"source": [], "target": []}

        self.setup_ui()
        if self.is_edit_mode:
            self.load_profile_data()

    def setup_ui(self):
        self.setWindowTitle("연결 편집" if self.is_edit_mode else "새 연결")
        self.setModal(True)
        self.resize(660, 540)

        layout = QVBoxLayout(self)

        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("프로필 이름:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("예: 운영DB → 검증DB")
        self.name_edit.setToolTip("목록에서 구분하기 쉬운 연결 프로필 이름을 입력하세요.")
        name_layout.addWidget(self.name_edit)
        layout.addLayout(name_layout)

        self.tab_widget = QTabWidget()
        self.source_tab = self.create_endpoint_tab("source", "소스")
        self.target_tab = self.create_endpoint_tab("target", "대상")
        self.tab_widget.addTab(self.source_tab, "소스")
        self.tab_widget.addTab(self.target_tab, "대상")
        layout.addWidget(self.tab_widget)

        # 저장/취소만 남긴다. 연결 테스트는 테스트하는 탭 안으로 옮겼다.
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)

        ok_btn = button_box.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("저장")
        ok_btn.setObjectName("primaryAction")
        ok_btn.setToolTip("입력한 연결 정보를 확인한 뒤 프로필로 저장합니다.")
        ok_btn.setDefault(True)
        button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")

        layout.addWidget(button_box)

    def create_endpoint_tab(self, key: str, title: str) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        kind_row = QHBoxLayout()
        kind_row.addWidget(QLabel("엔드포인트 종류:"))
        kind_combo = QComboBox()
        kind_combo.addItems(list(ENDPOINT_KIND_LABELS.values()))
        kind_combo.setToolTip(f"{title} 엔드포인트가 PostgreSQL인지 File Archive인지 선택합니다.")
        kind_row.addWidget(kind_combo)
        kind_row.addStretch(1)
        layout.addLayout(kind_row)

        stacked = QStackedWidget()

        postgres_widget = QWidget()
        postgres_layout = QFormLayout(postgres_widget)

        preset_row = QHBoxLayout()
        preset_combo = QComboBox()
        preset_combo.setMinimumWidth(300)
        preset_combo.setToolTip("저장된 PostgreSQL 연결 정보를 불러와 입력값을 채웁니다.")
        preset_combo.addItem("저장된 연결 선택...")
        preset_combo.currentIndexChanged.connect(
            lambda idx, side=key: self._on_preset_selected(side, idx)
        )
        delete_preset_btn = QPushButton("삭제")
        delete_preset_btn.setToolTip(
            "선택한 저장 연결만 삭제합니다. 현재 프로필이나 작업 이력은 삭제하지 않습니다."
        )
        delete_preset_btn.setProperty("variant", "chip")
        delete_preset_btn.setAutoDefault(False)
        delete_preset_btn.clicked.connect(
            lambda _=False, side=key: self._delete_selected_preset(side)
        )
        # 콤보가 남는 폭을 가져간다. 안 그러면 '삭제'가 행 절반을 차지한다.
        preset_row.addWidget(preset_combo, 1)
        preset_row.addWidget(delete_preset_btn)
        postgres_layout.addRow("저장된 연결:", preset_row)

        host_edit = QLineEdit()
        host_edit.setPlaceholderText("예: localhost 또는 192.168.0.10")
        host_edit.setToolTip("PostgreSQL 서버 주소를 입력하세요.")
        postgres_layout.addRow("호스트:", host_edit)

        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setValue(5432)
        postgres_layout.addRow("포트:", port_spin)

        database_edit = QLineEdit()
        database_edit.setPlaceholderText("예: facreport")
        database_edit.setToolTip("접속할 PostgreSQL 데이터베이스 이름을 입력하세요.")
        postgres_layout.addRow("데이터베이스:", database_edit)

        username_edit = QLineEdit()
        username_edit.setPlaceholderText("예: postgres")
        username_edit.setToolTip("PostgreSQL 접속 사용자명을 입력하세요.")
        postgres_layout.addRow("사용자명:", username_edit)

        password_edit = QLineEdit()
        password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        password_edit.setPlaceholderText("비밀번호 입력")
        password_edit.setToolTip(
            "PostgreSQL 접속 비밀번호를 입력하세요. 저장 정책은 기존 설정을 따릅니다."
        )
        postgres_layout.addRow("비밀번호:", password_edit)

        ssl_check = QCheckBox("SSL 연결 사용")
        ssl_check.setToolTip("서버가 SSL 접속을 요구할 때 선택하세요.")
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
        archive_path_edit.setPlaceholderText("예: D:/backup/psql93_archive")
        archive_path_edit.setToolTip("File Archive로 읽거나 쓸 폴더 경로를 지정하세요.")
        browse_btn = QPushButton("찾아보기")
        browse_btn.setToolTip("아카이브 폴더를 파일 선택 창에서 지정합니다.")
        browse_btn.clicked.connect(lambda: self.browse_archive_path(key))
        archive_row.addWidget(archive_path_edit)
        archive_row.addWidget(browse_btn)
        file_layout.addRow("아카이브 경로:", archive_row)

        hint = QLabel(
            "- 소스 File Archive: manifest.json 이 있는 기존 아카이브 폴더\n"
            "- 대상 File Archive: 내보낸 파일을 저장할 새/기존 폴더(필요 시 생성)"
        )
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        file_layout.addRow("", hint)

        stacked.addWidget(postgres_widget)
        stacked.addWidget(file_widget)
        layout.addWidget(stacked)
        layout.addStretch(1)

        # 테스트는 테스트할 대상 옆에서 하고, 결과도 그 자리에서 읽는다.
        test_row = QHBoxLayout()
        test_btn = QPushButton("연결 테스트")
        test_btn.setToolTip(f"현재 입력한 {title} 연결 정보로 접속 가능 여부를 확인합니다.")
        test_btn.setAutoDefault(False)
        test_btn.clicked.connect(lambda _=False, side=key: self.test_connection(side))
        test_row.addWidget(test_btn)

        save_preset_btn = QPushButton("이 연결 저장")
        save_preset_btn.setToolTip("확인된 연결 정보를 저장된 연결 목록에 추가합니다.")
        save_preset_btn.setProperty("variant", "chip")
        save_preset_btn.setAutoDefault(False)
        save_preset_btn.setVisible(False)
        save_preset_btn.clicked.connect(lambda _=False, side=key: self._save_preset_clicked(side))
        test_row.addWidget(save_preset_btn)

        result_lamp = StatusLamp()
        result_lamp.set_state("idle", "아직 확인하지 않음")
        test_row.addWidget(result_lamp)
        test_row.addStretch(1)
        layout.addLayout(test_row)

        kind_combo.currentIndexChanged.connect(
            lambda _idx, side=key: self.on_endpoint_kind_changed(side)
        )

        # 입력을 고치면 직전 테스트 결과는 더 이상 이 설정에 대한 결과가 아니다.
        # (저장은 '현재 위젯 값'을 읽고, 저장된 연결은 host/port/db/user/ssl 기준
        #  upsert라 비밀번호만 고쳐 저장하면 검증된 프리셋을 덮어쓴다.)
        for edit in (host_edit, database_edit, username_edit, password_edit, archive_path_edit):
            edit.textChanged.connect(lambda _text, side=key: self._reset_test_result(side))
        port_spin.valueChanged.connect(lambda _v, side=key: self._reset_test_result(side))
        ssl_check.toggled.connect(lambda _c, side=key: self._reset_test_result(side))

        self.endpoint_widgets[key] = {
            "kind": kind_combo,
            "stack": stacked,
            "preset_combo": preset_combo,
            "delete_preset_btn": delete_preset_btn,
            "archive_path": archive_path_edit,
            "host": host_edit,
            "port": port_spin,
            "database": database_edit,
            "username": username_edit,
            "password": password_edit,
            "ssl": ssl_check,
            "compat_mode": compat_combo,
            "test_btn": test_btn,
            "save_preset_btn": save_preset_btn,
            "result_lamp": result_lamp,
            "title": title,
        }
        self._refresh_presets(key)
        self.on_endpoint_kind_changed(key)
        return widget

    def browse_archive_path(self, side: str):
        current = self.endpoint_widgets[side]["archive_path"].text().strip()
        start_dir = current or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "아카이브 폴더 선택", start_dir)
        if selected:
            self.endpoint_widgets[side]["archive_path"].setText(selected)

    def _refresh_presets(self, side: str):
        combo = self.endpoint_widgets[side]["preset_combo"]
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("저장된 연결 선택...")
        presets = self.saved_conn_manager.get_all()
        self._preset_items[side] = presets
        for p in presets:
            combo.addItem(p["label"])
        combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def _on_preset_selected(self, side: str, index: int):
        if index <= 0:
            return
        presets = self._preset_items.get(side, [])
        if index - 1 >= len(presets):
            return
        preset = presets[index - 1]
        w = self.endpoint_widgets[side]
        w["host"].setText(preset["host"])
        w["port"].setValue(preset["port"])
        w["database"].setText(preset["database"])
        w["username"].setText(preset["username"])
        w["password"].setText(preset["password"])
        w["ssl"].setChecked(preset["ssl"])
        mode_label = COMPAT_MODE_LABELS.get(preset["compat_mode"], COMPAT_MODE_LABELS["auto"])
        idx = w["compat_mode"].findText(mode_label)
        if idx >= 0:
            w["compat_mode"].setCurrentIndex(idx)
        # 입력값이 바뀌었으니 이전 테스트 결과는 무효다.
        self._reset_test_result(side)

    def _delete_selected_preset(self, side: str):
        combo = self.endpoint_widgets[side]["preset_combo"]
        index = combo.currentIndex()
        if index <= 0:
            return
        presets = self._preset_items.get(side, [])
        if index - 1 >= len(presets):
            return
        preset = presets[index - 1]
        reply = QMessageBox.question(
            self,
            "프리셋 삭제",
            f"'{preset['label']}'을(를) 삭제하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.saved_conn_manager.delete(preset["id"])
            self._refresh_presets(side)
            for other_side in ("source", "target"):
                if other_side != side:
                    self._refresh_presets(other_side)

    def _save_preset_clicked(self, side: str):
        config = self._get_endpoint_profile_config(side)
        if config.get("kind") == ENDPOINT_KIND_FILE:
            return

        widgets = self.endpoint_widgets[side]
        try:
            self.saved_conn_manager.save_connection(config)
        except Exception as e:
            widgets["result_lamp"].set_state("error", f"저장 실패: {e}")
            return

        self._refresh_presets("source")
        self._refresh_presets("target")
        widgets["save_preset_btn"].setVisible(False)
        widgets["result_lamp"].set_state("ok", "저장된 연결에 추가했습니다")

    def on_endpoint_kind_changed(self, side: str):
        widgets = self.endpoint_widgets[side]
        kind = ConnectionMapper.endpoint_kind_from_ui(widgets["kind"])
        widgets["stack"].setCurrentIndex(1 if kind == ENDPOINT_KIND_FILE else 0)
        # 종류를 바꾸면 이전 테스트 결과는 더 이상 이 입력에 대한 결과가 아니다.
        self._reset_test_result(side)

    def _reset_test_result(self, side: str):
        widgets = self.endpoint_widgets.get(side)
        if not widgets or "result_lamp" not in widgets:
            return
        widgets["result_lamp"].set_state("idle", "아직 확인하지 않음")
        widgets["save_preset_btn"].setVisible(False)

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
        """연결을 확인하고 결과를 그 탭 안에서 보여준다.

        확인 결과는 창을 가로막는 대화상자가 아니라 입력값 옆에 남는다.
        고쳐야 할 값과 결과를 한 화면에서 같이 볼 수 있다.
        """
        widgets = self.endpoint_widgets[side]
        lamp = widgets["result_lamp"]
        config = self._get_endpoint_profile_config(side)

        lamp.set_state("busy", "확인 중...")
        widgets["save_preset_btn"].setVisible(False)

        if config.get("kind") == ENDPOINT_KIND_FILE:
            must_exist = side == "source"
            valid, msg = ConnectionValidator.validate_file_archive_config(
                config,
                must_exist=must_exist,
            )
            if not valid:
                lamp.set_state("error", msg)
                return

            path = Path(config["archive_path"]).expanduser()
            lamp.set_state(
                "ok",
                f"{'경로 확인됨' if must_exist else '출력 경로 사용 가능'} · {path}",
            )
            return

        psycopg_config = ConnectionMapper.ui_to_psycopg_config(
            widgets["host"],
            widgets["port"],
            widgets["database"],
            widgets["username"],
            widgets["password"],
            widgets["ssl"],
        )

        widgets["test_btn"].setEnabled(False)
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        QApplication.processEvents()  # '확인 중...'이 실제로 보이게 한 번 그린다
        try:
            with psycopg.connect(**psycopg_config, connect_timeout=7) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version()")
                    version_row = cur.fetchone()
                    if not version_row:
                        lamp.set_state("error", "연결 실패 · 서버 버전을 읽지 못했습니다")
                        return
                    version_str = version_row[0]
                    version_info = parse_version_string(version_str)
        except Exception as e:
            lamp.set_state("error", f"연결 실패 · {e}")
            return
        finally:
            QApplication.restoreOverrideCursor()
            widgets["test_btn"].setEnabled(True)

        lamp.set_state("ok", f"연결됨 · {version_info}")
        lamp.text_label.setToolTip(version_str)
        widgets["save_preset_btn"].setVisible(True)

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
