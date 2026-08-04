"""작업 이력 다이얼로그"""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.models.history import HistoryManager
from src.models.profile import ProfileManager
from src.ui.theme import LAMP_BUSY, LAMP_ERROR, LAMP_OK, TEXT_MUTED

# 상태 코드 → (표시 문구, 색). 램프와 같은 의미 체계를 쓴다.
STATUS_DISPLAY = {
    "completed": ("완료", LAMP_OK),
    "failed": ("실패", LAMP_ERROR),
    "cancelled": ("취소", TEXT_MUTED),
    "running": ("진행 중", LAMP_BUSY),
}


class HistoryDialog(QDialog):
    """작업 이력 조회 다이얼로그 (modeless)"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history_manager = HistoryManager()
        self.profile_manager = ProfileManager()
        self._profile_name_cache: dict[int, str] = {}

        self.setWindowTitle("작업 이력")
        self.resize(900, 500)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["프로필", "시작 날짜", "종료 날짜", "시작 시간", "완료 시간", "상태"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        self.empty_label = QLabel("")
        self.empty_label.setProperty("role", "hint")
        btn_row.addWidget(self.empty_label)
        btn_row.addStretch(1)

        self.refresh_btn = QPushButton("새로고침")
        self.refresh_btn.setObjectName("primaryAction")
        self.refresh_btn.setToolTip("작업 이력 목록을 최신 상태로 다시 불러옵니다.")
        self.refresh_btn.clicked.connect(self.refresh)
        btn_row.addWidget(self.refresh_btn)
        layout.addLayout(btn_row)

    def _resolve_profile_name(self, profile_id: int) -> str:
        if profile_id not in self._profile_name_cache:
            profile = self.profile_manager.get_profile(profile_id)
            self._profile_name_cache[profile_id] = profile.name if profile else "알 수 없음"
        return self._profile_name_cache[profile_id]

    def refresh(self):
        self._profile_name_cache.clear()
        histories = self.history_manager.get_all_history()
        self.table.setRowCount(0)

        for history in histories:
            row = self.table.rowCount()
            self.table.insertRow(row)

            self.table.setItem(row, 0, QTableWidgetItem(self._resolve_profile_name(history.profile_id)))
            self.table.setItem(row, 1, QTableWidgetItem(str(history.start_date)))
            self.table.setItem(row, 2, QTableWidgetItem(str(history.end_date)))
            self.table.setItem(
                row, 3,
                QTableWidgetItem(
                    history.started_at.strftime("%Y-%m-%d %H:%M:%S") if history.started_at else ""
                ),
            )
            self.table.setItem(
                row, 4,
                QTableWidgetItem(
                    history.completed_at.strftime("%Y-%m-%d %H:%M:%S") if history.completed_at else ""
                ),
            )
            status_text, status_color = STATUS_DISPLAY.get(
                history.status, (history.status, TEXT_MUTED)
            )
            status_item = QTableWidgetItem(status_text)
            status_item.setForeground(QColor(status_color))
            status_item.setToolTip(f"원본 상태 코드: {history.status}")
            self.table.setItem(row, 5, status_item)

        self.empty_label.setText(
            "아직 실행한 작업이 없습니다. 메인 창에서 프로필을 고르고 마이그레이션을 시작하세요."
            if not histories
            else f"작업 {len(histories)}건"
        )

    def showEvent(self, event):
        super().showEvent(event)
        self._profile_name_cache.clear()
        self.refresh()
