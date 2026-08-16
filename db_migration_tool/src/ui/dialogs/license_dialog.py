"""라이선스 등록·상태 확인 창.

만료·미인증이어도 이 창은 '앱을 못 쓰게 하는 벽'이 아니다. 닫으면 제한 모드로
앱이 열리고, 중단된 마이그레이션은 계속 재개할 수 있다(설계 §2.1).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from src.licensing import LicenseState, LicenseStatus, register_key
from src.licensing.machine import format_for_display as format_machine_id
from src.ui.widgets import StatusLamp

_LAMP = {
    LicenseStatus.VALID: ("ok", "정상"),
    LicenseStatus.EXPIRING: ("busy", "만료 임박"),
    LicenseStatus.EXPIRED: ("error", "만료됨"),
    LicenseStatus.MISSING: ("idle", "미등록"),
    LicenseStatus.INVALID: ("error", "키 오류"),
    LicenseStatus.WRONG_MACHINE: ("error", "다른 PC"),
}


class LicenseDialog(QDialog):
    """라이선스 키를 등록하고 현재 상태를 보여준다."""

    def __init__(self, parent, state: LicenseState):
        super().__init__(parent)
        self.state = state

        self.setWindowTitle("라이선스")
        self.setModal(True)
        self.resize(560, 300)

        root = QVBoxLayout(self)

        self.lamp = StatusLamp("상태")
        root.addWidget(self.lamp)

        self.detail_label = QLabel()
        self.detail_label.setWordWrap(True)
        self.detail_label.setProperty("role", "hint")
        root.addWidget(self.detail_label)

        # 등록 코드 — 공급사에 전달해 키를 받을 때 쓴다.
        code_row = QHBoxLayout()
        code_title = QLabel("이 PC의 등록 코드:")
        code_title.setProperty("role", "fieldTitle")
        code_row.addWidget(code_title)

        self.machine_field = QLineEdit(format_machine_id(state.machine_id))
        self.machine_field.setReadOnly(True)
        self.machine_field.setCursorPosition(0)
        code_row.addWidget(self.machine_field, 1)
        root.addLayout(code_row)

        key_title = QLabel("라이선스 키")
        key_title.setProperty("role", "fieldTitle")
        root.addWidget(key_title)

        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText(
            "DBMT1-XXXXX-XXXXX-… (하이픈·대소문자는 신경 쓰지 않아도 됩니다)"
        )
        self.key_edit.returnPressed.connect(self.apply_key)
        root.addWidget(self.key_edit)

        root.addStretch(1)

        buttons = QHBoxLayout()
        self.register_btn = QPushButton("등록")
        self.register_btn.setObjectName("primaryAction")
        self.register_btn.clicked.connect(self.apply_key)

        self.close_btn = QPushButton("닫기")
        self.close_btn.clicked.connect(self.reject)

        for btn in (self.register_btn, self.close_btn):
            btn.setAutoDefault(False)

        buttons.addWidget(self.close_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.register_btn)
        root.addLayout(buttons)

        self._render(state)

    def _render(self, state: LicenseState) -> None:
        lamp_state, lamp_text = _LAMP.get(state.status, ("idle", "확인 중"))
        self.lamp.set_state(lamp_state, lamp_text)

        lines = []
        if state.payload:
            lines.append(f"고객사: {state.payload.customer}")
            lines.append(f"만료일: {state.payload.expires:%Y-%m-%d} (이 날까지 유효)")
        if state.message:
            lines.append(state.message)
        if state.is_restricted:
            lines.append(
                "제한 모드입니다 — 새 마이그레이션은 시작할 수 없지만, "
                "중단된 작업의 재개와 검증·조회는 계속 사용할 수 있습니다."
            )
        self.detail_label.setText("\n".join(lines))

    def apply_key(self) -> None:
        text = self.key_edit.text().strip()
        if not text:
            QMessageBox.information(self, "라이선스", "키를 입력하세요.")
            return

        new_state = register_key(text)
        self.state = new_state
        self._render(new_state)

        if new_state.status is LicenseStatus.INVALID:
            QMessageBox.warning(self, "라이선스", new_state.message)
            return

        QMessageBox.information(self, "라이선스", "등록되었습니다.")
        self.key_edit.clear()
        if not new_state.is_restricted:
            self.accept()

    def keyPressEvent(self, event):  # noqa: N802 (Qt 시그니처)
        # Esc 로 닫아도 앱은 제한 모드로 계속 쓸 수 있어야 한다.
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)
