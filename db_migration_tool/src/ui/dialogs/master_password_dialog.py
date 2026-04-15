"""마스터 비밀번호 설정/로그인 다이얼로그"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from src.utils.master_password import MasterPasswordService


class MasterPasswordDialog(QDialog):
    """최초 설정 및 로그인에 공용으로 사용하는 다이얼로그"""

    def __init__(self, mode: str = "unlock", parent=None):
        super().__init__(parent)
        self.mode = mode
        self._password = ""
        self.setup_ui()

    def setup_ui(self):
        is_setup_mode = self.mode == "setup"

        self.setModal(True)
        self.resize(420, 220 if is_setup_mode else 180)
        self.setWindowTitle("마스터 비밀번호 설정" if is_setup_mode else "로그인")

        layout = QVBoxLayout(self)

        description = QLabel(
            "이 앱을 사용하려면 마스터 비밀번호가 필요합니다.\n"
            "입력한 비밀번호로 저장된 연결 정보 암호화 키를 생성합니다."
            if is_setup_mode
            else "계속하려면 마스터 비밀번호를 입력해 주세요."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("마스터 비밀번호")
        self.password_edit.returnPressed.connect(self.accept)
        layout.addWidget(self.password_edit)

        self.confirm_edit: QLineEdit | None = None
        if is_setup_mode:
            hint = QLabel("4자 이상 입력해 주세요. 변경 기능은 이번 작업 범위에서 제외됩니다.")
            hint.setWordWrap(True)
            layout.addWidget(hint)

            self.confirm_edit = QLineEdit()
            self.confirm_edit.setEchoMode(QLineEdit.Password)
            self.confirm_edit.setPlaceholderText("마스터 비밀번호 확인")
            self.confirm_edit.returnPressed.connect(self.accept)
            layout.addWidget(self.confirm_edit)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def get_password(self) -> str:
        return self._password

    def accept(self):
        password = self.password_edit.text()

        try:
            MasterPasswordService.validate_password(password)
        except ValueError as exc:
            QMessageBox.warning(self, "입력 오류", str(exc))
            return

        if self.mode == "setup":
            confirm_password = self.confirm_edit.text() if self.confirm_edit else ""
            if password != confirm_password:
                QMessageBox.warning(self, "입력 오류", "비밀번호 확인이 일치하지 않습니다.")
                return

        self._password = password
        super().accept()
