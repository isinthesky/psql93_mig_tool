"""작업 이력 다이얼로그"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.models.history import HistoryManager
from src.models.profile import ProfileManager


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
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("새로고침")
        refresh_btn.setStyleSheet("""
            QPushButton {
                font-size: 14px;
                padding: 8px 16px;
                font-weight: bold;
            }
        """)
        refresh_btn.clicked.connect(self.refresh)
        btn_row.addStretch(1)
        btn_row.addWidget(refresh_btn)
        layout.addLayout(btn_row)

    def _resolve_profile_name(self, profile_id: int) -> str:
        if profile_id not in self._profile_name_cache:
            profile = self.profile_manager.get_profile(profile_id)
            self._profile_name_cache[profile_id] = profile.name if profile else "알 수 없음"
        return self._profile_name_cache[profile_id]

    def refresh(self):
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
            status_text = {
                "completed": "완료",
                "failed": "실패",
                "cancelled": "취소",
                "running": "진행중",
            }.get(history.status, history.status)
            self.table.setItem(row, 5, QTableWidgetItem(status_text))

    def showEvent(self, event):
        super().showEvent(event)
        self._profile_name_cache.clear()
        self.refresh()
