"""
로그 뷰어 다이얼로그 (Non-modal)
"""

from datetime import datetime, timedelta

from PySide6.QtCore import QDate, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
)

from src.database.local_db import LogEntry, get_db
from src.utils.enhanced_logger import log_emitter


class LogViewerDialog(QDialog):
    """로그 뷰어 다이얼로그"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.db = get_db()
        self.auto_scroll = True
        self.current_session_filter = None
        self.current_level_filter = None
        self.displayed_log_ids = set()

        self.setup_ui()
        self.setup_timers()
        self.load_initial_logs()
        self.connect_signals()

    def setup_ui(self):
        """UI 초기화"""
        self.setWindowTitle("로그 뷰어")
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint
        )
        self.resize(900, 600)

        layout = QVBoxLayout(self)

        # 툴바
        toolbar = self.create_toolbar()
        layout.addWidget(toolbar)

        # 필터 영역
        filter_layout = self.create_filter_layout()
        layout.addLayout(filter_layout)

        # 로그 표시 영역
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #ffffff;
                font-family: Consolas, Monaco, monospace;
                font-size: 12px;
            }
        """)
        layout.addWidget(self.log_text)

        # 상태바
        status_layout = self.create_status_layout()
        layout.addLayout(status_layout)

    def create_toolbar(self):
        """툴바 생성"""
        toolbar = QToolBar()

        # 새로고침
        refresh_action = QAction("새로고침", self)
        refresh_action.triggered.connect(self.refresh_logs)
        toolbar.addAction(refresh_action)

        # 모두 지우기
        clear_action = QAction("화면 지우기", self)
        clear_action.triggered.connect(self.clear_display)
        toolbar.addAction(clear_action)

        toolbar.addSeparator()

        # 자동 스크롤
        self.auto_scroll_check = QCheckBox("자동 스크롤")
        self.auto_scroll_check.setChecked(True)
        self.auto_scroll_check.toggled.connect(self.toggle_auto_scroll)
        toolbar.addWidget(self.auto_scroll_check)

        return toolbar

    def create_filter_layout(self):
        """필터 레이아웃 생성"""
        layout = QHBoxLayout()

        # 검색
        layout.addWidget(QLabel("검색:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("로그 메시지 검색...")
        self.search_input.returnPressed.connect(self.apply_filters)
        layout.addWidget(self.search_input)

        # 레벨 필터
        layout.addWidget(QLabel("레벨:"))
        self.level_filter = QComboBox()
        self.level_filter.addItems(
            ["전체", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"]
        )
        self.level_filter.currentTextChanged.connect(self.apply_filters)
        layout.addWidget(self.level_filter)

        # 세션 필터
        layout.addWidget(QLabel("세션:"))
        self.session_filter = QComboBox()
        self.session_filter.setMinimumWidth(200)
        self.session_filter.addItem("전체 세션")
        self.session_filter.currentTextChanged.connect(self.apply_filters)
        layout.addWidget(self.session_filter)

        # 날짜 범위
        layout.addWidget(QLabel("시작:"))
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDate(QDate.currentDate().addDays(-7))
        self.start_date.dateChanged.connect(self.apply_filters)
        layout.addWidget(self.start_date)

        layout.addWidget(QLabel("종료:"))
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDate(QDate.currentDate())
        self.end_date.dateChanged.connect(self.apply_filters)
        layout.addWidget(self.end_date)

        layout.addStretch()

        return layout

    def create_status_layout(self):
        """상태바 레이아웃 생성"""
        layout = QHBoxLayout()

        self.status_label = QLabel("로그 0개 표시중")
        layout.addWidget(self.status_label)

        layout.addStretch()

        self.last_update_label = QLabel("마지막 업데이트: -")
        layout.addWidget(self.last_update_label)

        return layout

    def setup_timers(self):
        """타이머 설정"""
        # 실시간 업데이트 타이머 (1초마다)
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.check_new_logs)
        self.update_timer.start(1000)

        # 세션 목록 업데이트 타이머 (10초마다)
        self.session_timer = QTimer()
        self.session_timer.timeout.connect(self.update_session_list)
        self.session_timer.start(10000)

    def connect_signals(self):
        """시그널 연결"""
        # 실시간 로그 수신
        log_emitter.log_signal.connect(self.on_log_received)

    def load_initial_logs(self):
        """초기 로그 로드"""
        self.refresh_logs()
        self.update_session_list()

    def refresh_logs(self):
        """로그 새로고침"""
        self.log_text.clear()
        self.displayed_log_ids.clear()
        self.apply_filters()

    def clear_display(self):
        """화면 지우기"""
        self.log_text.clear()
        self.displayed_log_ids.clear()
        self.status_label.setText("로그 0개 표시중")

    def toggle_auto_scroll(self, checked):
        """자동 스크롤 토글"""
        self.auto_scroll = checked

    def apply_filters(self):
        """필터 적용"""
        session = self.db.get_session()
        try:
            query = session.query(LogEntry)

            # 날짜 필터
            start_date = self.start_date.date().toPython()
            end_date = self.end_date.date().toPython() + timedelta(days=1)
            query = query.filter(LogEntry.timestamp >= start_date, LogEntry.timestamp < end_date)

            # 레벨 필터
            level_text = self.level_filter.currentText()
            if level_text != "전체":
                query = query.filter(LogEntry.level == level_text)

            # 세션 필터
            session_text = self.session_filter.currentText()
            if session_text != "전체 세션":
                session_id = session_text.split(" - ")[0]
                query = query.filter(LogEntry.session_id == session_id)

            # 검색 필터
            search_text = self.search_input.text().strip()
            if search_text:
                query = query.filter(LogEntry.message.contains(search_text))

            # 최근 10,000개만
            logs = query.order_by(LogEntry.timestamp.desc()).limit(10000).all()

            # 오래된 순서로 표시
            logs.reverse()

            # 로그 표시
            self.log_text.clear()
            self.displayed_log_ids.clear()

            for log in logs:
                self.append_log_entry(log)
                self.displayed_log_ids.add(log.id)

            self.status_label.setText(f"로그 {len(logs)}개 표시중")
            self.last_update_label.setText(
                f"마지막 업데이트: {datetime.now().strftime('%H:%M:%S')}"
            )

        finally:
            session.close()

    def check_new_logs(self):
        """새 로그 확인"""
        if not self.isVisible():
            return

        session = self.db.get_session()
        try:
            # 현재 필터 조건으로 새 로그만 조회
            query = session.query(LogEntry)

            # 표시된 ID 제외
            if self.displayed_log_ids:
                query = query.filter(~LogEntry.id.in_(self.displayed_log_ids))

            # 날짜 필터
            start_date = self.start_date.date().toPython()
            end_date = self.end_date.date().toPython() + timedelta(days=1)
            query = query.filter(LogEntry.timestamp >= start_date, LogEntry.timestamp < end_date)

            # 레벨 필터
            level_text = self.level_filter.currentText()
            if level_text != "전체":
                query = query.filter(LogEntry.level == level_text)

            # 세션 필터
            session_text = self.session_filter.currentText()
            if session_text != "전체 세션":
                session_id = session_text.split(" - ")[0]
                query = query.filter(LogEntry.session_id == session_id)

            # 검색 필터
            search_text = self.search_input.text().strip()
            if search_text:
                query = query.filter(LogEntry.message.contains(search_text))

            # 새 로그 가져오기
            new_logs = query.order_by(LogEntry.timestamp).limit(100).all()

            if new_logs:
                for log in new_logs:
                    self.append_log_entry(log)
                    self.displayed_log_ids.add(log.id)

                # 표시 개수 제한 (최근 10,000개)
                if len(self.displayed_log_ids) > 10000:
                    # 오래된 로그 제거 로직 (필요시 구현)
                    pass

                self.status_label.setText(f"로그 {len(self.displayed_log_ids)}개 표시중")
                self.last_update_label.setText(
                    f"마지막 업데이트: {datetime.now().strftime('%H:%M:%S')}"
                )

        finally:
            session.close()

    def update_session_list(self):
        """세션 목록 업데이트"""
        session = self.db.get_session()
        try:
            # 최근 7일간의 세션 조회
            recent_date = datetime.now() - timedelta(days=7)
            sessions = (
                session.query(
                    LogEntry.session_id,
                    session.query(LogEntry.timestamp)
                    .filter(LogEntry.session_id == LogEntry.session_id)
                    .order_by(LogEntry.timestamp)
                    .limit(1)
                    .scalar_subquery()
                    .label("start_time"),
                )
                .filter(LogEntry.timestamp >= recent_date, LogEntry.session_id.isnot(None))
                .group_by(LogEntry.session_id)
                .order_by("start_time")
                .all()
            )

            # 현재 선택 저장
            current_text = self.session_filter.currentText()

            # 콤보박스 업데이트
            self.session_filter.clear()
            self.session_filter.addItem("전체 세션")

            for session_id, start_time in sessions:
                if session_id:
                    display_text = f"{session_id} - {start_time.strftime('%m/%d %H:%M')}"
                    self.session_filter.addItem(display_text)

            # 이전 선택 복원
            index = self.session_filter.findText(current_text)
            if index >= 0:
                self.session_filter.setCurrentIndex(index)

        finally:
            session.close()

    def append_log_entry(self, log_entry):
        """로그 엔트리 추가"""
        # 타임스탬프 형식
        timestamp = log_entry.timestamp.strftime("%y%m%d %H:%M:%S")
        session_id = log_entry.session_id or "NO_SESSION"
        level = log_entry.level
        message = log_entry.message

        # 색상 매핑
        color_map = {
            "DEBUG": "#808080",  # Gray
            "INFO": "#FFFFFF",  # White
            "SUCCESS": "#0080FF",  # Blue
            "WARNING": "#FFA500",  # Orange
            "ERROR": "#FF0000",  # Red
            "CRITICAL": "#FF00FF",  # Magenta
        }

        color = color_map.get(level, "#FFFFFF")

        # HTML 형식으로 추가
        html = (
            f'<span style="color: {color}">[{timestamp}] [{session_id}] [{level}] {message}</span>'
        )

        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertHtml(html + "<br>")

        # 자동 스크롤
        if self.auto_scroll:
            self.log_text.moveCursor(QTextCursor.End)

    @Slot(str, str, str, str)
    def on_log_received(self, timestamp, session_id, level, message):
        """실시간 로그 수신"""
        # 필터 확인
        level_text = self.level_filter.currentText()
        if level_text != "전체" and level != level_text:
            return

        session_text = self.session_filter.currentText()
        if session_text != "전체 세션":
            filter_session_id = session_text.split(" - ")[0]
            if session_id != filter_session_id:
                return

        search_text = self.search_input.text().strip()
        if search_text and search_text.lower() not in message.lower():
            return

        # 색상 매핑
        color_map = {
            "DEBUG": "#808080",  # Gray
            "INFO": "#FFFFFF",  # White
            "SUCCESS": "#0080FF",  # Blue
            "WARNING": "#FFA500",  # Orange
            "ERROR": "#FF0000",  # Red
            "CRITICAL": "#FF00FF",  # Magenta
        }

        color = color_map.get(level, "#FFFFFF")

        # HTML 형식으로 추가
        html = (
            f'<span style="color: {color}">[{timestamp}] [{session_id}] [{level}] {message}</span>'
        )

        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertHtml(html + "<br>")

        # 자동 스크롤
        if self.auto_scroll:
            self.log_text.moveCursor(QTextCursor.End)

        # 상태 업데이트
        self.last_update_label.setText(f"마지막 업데이트: {datetime.now().strftime('%H:%M:%S')}")

    def closeEvent(self, event):
        """다이얼로그 닫기 이벤트"""
        # 타이머 정지
        self.update_timer.stop()
        self.session_timer.stop()

        # 시그널 연결 해제
        try:
            log_emitter.log_signal.disconnect(self.on_log_received)
        except (RuntimeError, TypeError):
            pass

        event.accept()
