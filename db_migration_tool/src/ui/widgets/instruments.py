"""제어반 계기 위젯 — 램프, 스텝 레일, 계기 표시.

이 세 위젯이 앱 전체의 '상태를 읽는 언어'다.
연결 상태든 실행 상태든 램프는 항상 같은 4가지 색만 쓰고,
숫자는 항상 같은 고정폭 계기 자리에서 읽는다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from src.ui.theme import repolish

# 램프 상태 4가지. 이 밖의 값은 쓰지 않는다.
LAMP_IDLE = "idle"  # 아직 확인하지 않음
LAMP_BUSY = "busy"  # 확인 중 / 일시정지
LAMP_OK = "ok"  # 정상 / 실행 중 / 완료
LAMP_ERROR = "error"  # 오류 / 중단

_LAMP_STATES = (LAMP_IDLE, LAMP_BUSY, LAMP_OK, LAMP_ERROR)


class StatusLamp(QWidget):
    """램프(●) 하나와 그 옆의 상태 문구.

    연결 상태(소스/대상)와 실행 상태(대기/실행 중/완료)를 같은 모양으로 읽게 한다.
    """

    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(parent)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.title_label = QLabel(f"{title}:" if title else "")
        self.title_label.setProperty("role", "fieldTitle")
        self.title_label.setVisible(bool(title))
        layout.addWidget(self.title_label)

        self.lamp_label = QLabel("●")
        self.lamp_label.setProperty("role", "lamp")
        self.lamp_label.setProperty("state", LAMP_IDLE)
        layout.addWidget(self.lamp_label)

        self.text_label = QLabel("")
        self.text_label.setProperty("role", "lampText")
        self.text_label.setProperty("state", LAMP_IDLE)
        layout.addWidget(self.text_label)

        self._state = LAMP_IDLE

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str, message: str = "") -> None:
        """램프 색과 문구를 함께 바꾼다.

        Args:
            state: idle | busy | ok | error
            message: 램프 옆에 표시할 문구. 비우면 기존 문구를 유지한다.
        """
        if state not in _LAMP_STATES:
            state = LAMP_IDLE
        self._state = state

        for label in (self.lamp_label, self.text_label):
            label.setProperty("state", state)
            repolish(label)

        if message:
            self.text_label.setText(message)
            self.text_label.setToolTip(message)


class MetricReadout(QWidget):
    """숫자 하나를 읽는 계기 자리 — 값(고정폭, 크게) 위에 이름(작게)."""

    def __init__(self, label: str, placeholder: str = "-", parent: QWidget | None = None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)

        self.value_label = QLabel(placeholder)
        self.value_label.setProperty("role", "metricValue")
        layout.addWidget(self.value_label)

        self.name_label = QLabel(label)
        self.name_label.setProperty("role", "metricLabel")
        layout.addWidget(self.name_label)

        self._placeholder = placeholder

    def set_value(self, value: str) -> None:
        self.value_label.setText(value or self._placeholder)


class StepRail(QWidget):
    """마법사 진행 단계 레일 — [1] 연결 ── [2] 범위 ── [3] 실행.

    번호는 장식이 아니라 실제 순서를 뜻한다. 지나온 단계, 지금 단계,
    남은 단계가 한눈에 구분된다.
    """

    def __init__(self, steps: list[str], parent: QWidget | None = None):
        super().__init__(parent)

        self._chips: list[QLabel] = []
        self._names: list[QLabel] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        for index, name in enumerate(steps):
            chip = QLabel(str(index + 1))
            chip.setProperty("role", "stepChip")
            chip.setProperty("state", "todo")
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(chip)
            self._chips.append(chip)

            label = QLabel(name)
            label.setProperty("role", "stepName")
            label.setProperty("state", "todo")
            layout.addWidget(label)
            self._names.append(label)

            if index < len(steps) - 1:
                # 연결선은 고정 길이다. 늘어나게 두면 창 폭만큼 벌어져
                # 세 단계가 하나의 진행 표시가 아니라 따로 노는 항목처럼 보인다.
                line = QFrame()
                line.setProperty("role", "stepLine")
                line.setFrameShape(QFrame.Shape.HLine)
                line.setFixedWidth(48)
                line.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                layout.addWidget(line)

        layout.addStretch(1)
        self.set_current(0)

    def set_current(self, index: int) -> None:
        for i, (chip, name) in enumerate(zip(self._chips, self._names, strict=True)):
            if i < index:
                state = "done"
            elif i == index:
                state = "current"
            else:
                state = "todo"
            for widget in (chip, name):
                widget.setProperty("state", state)
                repolish(widget)
