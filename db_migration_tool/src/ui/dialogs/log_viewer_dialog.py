"""
로그 뷰어 다이얼로그 (Non-modal)
"""

import html
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
from sqlalchemy import func

from src.database.local_db import LogEntry, get_db
from src.ui.theme import LOG_VIEWER_MAX_BLOCKS, TEXT_MUTED, log_color
from src.utils.enhanced_logger import log_emitter


class LogViewerDialog(QDialog):
    """로그 뷰어 다이얼로그"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.db = get_db()
        self.auto_scroll = True
        self.current_session_filter = None
        self.current_level_filter = None
        # 이미 가져온 로그를 id 집합으로 들고 NOT IN 하면, 집합이 무한히 커져
        # 1초마다 수만 개의 바인드 파라미터를 넘기게 되고 결국 SQLite 파라미터
        # 한도(32,766)를 넘겨 폴링이 죽는다. 마지막으로 본 id 하나만 기억한다.
        self._last_log_id = 0
        self._log_signal_connected = False

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
        self.log_text.setObjectName("logViewer")
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("조건에 맞는 로그가 없습니다.")
        # 오래 켜 둬도 메모리가 계속 늘지 않도록 표시 줄 수를 제한한다.
        # 이 제한은 _append_line이 항목마다 블록을 만들 때만 동작한다.
        self.log_text.document().setMaximumBlockCount(LOG_VIEWER_MAX_BLOCKS)
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
        """시그널 연결

        표시 내용의 출처는 DB 폴링 하나뿐이다. 실시간 신호는 갱신 시각 표시용이다.
        """
        self._connect_log_signal()

    def _connect_log_signal(self):
        """중복 연결 없이 한 번만 잇는다."""
        if self._log_signal_connected:
            return
        log_emitter.log_signal.connect(self.on_log_received)
        self._log_signal_connected = True

    def _disconnect_log_signal(self):
        """연결돼 있을 때만 끊는다(안 그러면 PySide6가 경고를 뿜는다)."""
        if not self._log_signal_connected:
            return
        try:
            log_emitter.log_signal.disconnect(self.on_log_received)
        except (RuntimeError, TypeError):
            pass
        self._log_signal_connected = False

    def load_initial_logs(self):
        """초기 로그 로드"""
        self.refresh_logs()
        self.update_session_list()

    def refresh_logs(self):
        """로그 새로고침"""
        self.log_text.clear()
        self._last_log_id = 0
        self.apply_filters()

    def clear_display(self):
        """화면 지우기

        기준점(_last_log_id)은 그대로 둔다. 지우고 나면 새로 들어오는 로그만
        보이는 게 '화면 지우기'가 뜻하는 바다(다시 보려면 '새로고침').
        """
        self.log_text.clear()
        self.status_label.setText("로그 0개 표시 중")

    def toggle_auto_scroll(self, checked):
        """자동 스크롤 토글"""
        self.auto_scroll = checked

    def _current_filters(self):
        """화면의 필터 조건을 SQLAlchemy 조건 목록으로 만든다.

        조회(apply_filters)와 증분 폴링(check_new_logs)이 반드시 같은 조건을 써야
        하므로 한 곳에서만 만든다.
        """
        start_date = self.start_date.date().toPython()
        end_date = self.end_date.date().toPython() + timedelta(days=1)
        conditions = [LogEntry.timestamp >= start_date, LogEntry.timestamp < end_date]

        level_text = self.level_filter.currentText()
        if level_text != "전체":
            conditions.append(LogEntry.level == level_text)

        session_text = self.session_filter.currentText()
        if session_text != "전체 세션":
            conditions.append(LogEntry.session_id == session_text.split(" - ")[0])

        search_text = self.search_input.text().strip()
        if search_text:
            conditions.append(LogEntry.message.contains(search_text))

        return conditions

    def apply_filters(self):
        """필터 적용"""
        session = self.db.get_session()
        try:
            conditions = self._current_filters()

            # 화면에는 최근 것부터 상한만큼만 보여준다(더 예전 로그는 필터를 좁혀서 본다).
            #
            # id 내림차순으로 자른다. timestamp로 자르고 기준점을 따로 MAX(id)
            # 쿼리로 구하면, 두 쿼리 사이에 커밋된 로그가 '화면에는 없는데 기준점에는
            # 포함'되어 영영 표시되지 않는다(로그는 백그라운드에서 계속 쌓인다).
            # 한 번의 쿼리에서 표시 대상과 기준점을 함께 얻어 그 틈을 없앤다.
            # id는 삽입 순서이므로 로그 정렬 기준으로도 timestamp보다 정확하다.
            logs = (
                session.query(LogEntry)
                .filter(*conditions)
                .order_by(LogEntry.id.desc())
                .limit(LOG_VIEWER_MAX_BLOCKS)
                .all()
            )

            # 오래된 순서로 표시
            logs.reverse()

            # 로그 표시
            self.log_text.clear()

            for log in logs:
                self.append_log_entry(log)

            # 방금 그린 것 중 가장 큰 id = 이 조회 시점의 최대 id.
            # (id는 자동증가이고 로그를 지우는 코드가 없으므로, 이후 기록되는 로그는
            #  반드시 더 큰 id를 받는다 = 새 로그는 누락되지 않는다.)
            self._last_log_id = logs[-1].id if logs else 0

            self._update_status_count()
            self.last_update_label.setText(
                f"마지막 업데이트: {datetime.now().strftime('%H:%M:%S')}"
            )

        finally:
            session.close()

    def _update_status_count(self):
        """화면에 실제로 남아 있는 줄 수를 그대로 보고한다."""
        document = self.log_text.document()
        shown = 0 if document.isEmpty() else document.blockCount()
        if shown >= LOG_VIEWER_MAX_BLOCKS:
            self.status_label.setText(
                f"로그 {shown:,}개 표시 중 (최근 {LOG_VIEWER_MAX_BLOCKS:,}개까지만 표시합니다. "
                "더 예전 로그는 날짜/레벨 필터를 좁혀서 보세요.)"
            )
        else:
            self.status_label.setText(f"로그 {shown:,}개 표시 중")

    def check_new_logs(self):
        """새 로그 확인"""
        if not self.isVisible():
            return

        session = self.db.get_session()
        try:
            # 현재 필터 조건으로, 마지막으로 본 id 이후만.
            # (기본키 인덱스를 타고 바인드 파라미터도 1개다)
            new_logs = (
                session.query(LogEntry)
                .filter(LogEntry.id > self._last_log_id, *self._current_filters())
                .order_by(LogEntry.id)
                .limit(100)
                .all()
            )

            if new_logs:
                for log in new_logs:
                    self.append_log_entry(log)

                self._last_log_id = max(log.id for log in new_logs)

                # 문서는 상한만큼만 남기므로 앞쪽은 잘려나간다.
                # 표시 개수는 실제 남은 줄 수로 보고한다.
                self._update_status_count()
                self.last_update_label.setText(
                    f"마지막 업데이트: {datetime.now().strftime('%H:%M:%S')}"
                )

        finally:
            session.close()

    def update_session_list(self):
        """세션 목록 업데이트"""
        session = self.db.get_session()
        try:
            # 최근 7일 안에 로그가 있는 세션들을 고른다.
            #
            # 세션 시작 시각은 그 세션 로그의 최소 timestamp다. 원래는 상관
            # 서브쿼리 조건이 `LogEntry.session_id == LogEntry.session_id`(같은
            # 컬럼끼리 비교)여서 항상 참이 되는 바람에 세션과 무관한 전체 최소
            # 시각이 나오고 있었다.
            #
            # '어떤 세션을 보여줄지'와 '그 세션이 언제 시작했는지'는 다른 질문이라
            # 두 단계로 나눈다. 한 번에 하면 일주일 넘게 떠 있는 세션의 시작 시각이
            # 실제 시작이 아니라 '최근 7일 중 최초'로 잘린다.
            recent_date = datetime.now() - timedelta(days=7)
            recent_session_ids = [
                row[0]
                for row in session.query(LogEntry.session_id)
                .filter(LogEntry.timestamp >= recent_date, LogEntry.session_id.isnot(None))
                .distinct()
                .all()
            ]

            start_time = func.min(LogEntry.timestamp).label("start_time")
            sessions = (
                session.query(LogEntry.session_id, start_time)
                .filter(LogEntry.session_id.in_(recent_session_ids))
                .group_by(LogEntry.session_id)
                .order_by(start_time)
                .all()
                if recent_session_ids
                else []
            )

            # 현재 선택 저장
            current_text = self.session_filter.currentText()

            # 목록을 다시 채우는 동안 currentTextChanged가 튀면 apply_filters가
            # 호출되어 10초마다 로그 문서 전체를 다시 그린다(스크롤 위치도 튄다).
            # 선택이 실제로 바뀐 경우에만 아래에서 한 번 다시 적용한다.
            self.session_filter.blockSignals(True)
            try:
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
                self.session_filter.blockSignals(False)

            # 고르고 있던 세션이 목록에서 사라졌으면 필터가 실제로 바뀐 것이다.
            if self.session_filter.currentText() != current_text:
                self.apply_filters()

        finally:
            session.close()

    def _append_line(self, timestamp: str, session_id: str, level: str, message: str):
        """로그 한 줄을 색과 함께 붙인다(메시지는 그대로 보이도록 이스케이프).

        줄바꿈을 `<br>`로 넣으면 블록이 늘지 않아 모든 로그가 하나의 거대한 블록에
        쌓이고, maximumBlockCount 제한이 전혀 걸리지 않는다(뷰어를 오래 켜 두면
        레이아웃 비용이 계속 커진다). 항목마다 블록을 새로 연다.
        """
        safe = html.escape(message).replace("\n", " ")
        line = (
            f'<span style="color: {TEXT_MUTED}">[{timestamp}] [{session_id}]</span> '
            f'<span style="color: {log_color(level)}">[{level}] {safe}</span>'
        )

        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        if not self.log_text.document().isEmpty():
            cursor.insertBlock()
        cursor.insertHtml(line)

        # 자동 스크롤
        if self.auto_scroll:
            self.log_text.moveCursor(QTextCursor.End)

    def append_log_entry(self, log_entry):
        """로그 엔트리 추가"""
        self._append_line(
            log_entry.timestamp.strftime("%y%m%d %H:%M:%S"),
            log_entry.session_id or "NO_SESSION",
            log_entry.level,
            log_entry.message,
        )

    @Slot(str, str, str, str)
    def on_log_received(self, timestamp, session_id, level, message):
        """실시간 로그 신호 수신.

        여기서 본문을 그리지 않는다. `log_emitter.emit_log()`는 DB에 먼저 기록한 뒤
        신호를 보내므로(`enhanced_logger.py`), 여기서 그리면 1초 뒤 폴링이 같은 행을
        다시 가져와 **모든 로그가 두 줄씩** 쌓인다. 신호에는 DB id가 없어서
        `_last_log_id` 기준으로 걸러낼 수도 없다.

        따라서 화면 내용은 DB 폴링(`check_new_logs`) 하나만 책임진다.
        이 슬롯은 '방금 뭔가 기록됐다'는 사실만 알린다(최대 1초 뒤 본문이 붙는다).
        """
        self.last_update_label.setText(f"마지막 업데이트: {datetime.now().strftime('%H:%M:%S')}")

    def showEvent(self, event):
        """다시 열릴 때 갱신을 되살린다.

        메인 창은 이 다이얼로그를 싱글톤으로 재사용하므로, 닫으면서 멈춘 타이머와
        끊은 시그널을 여기서 복구하지 않으면 두 번째로 열었을 때 화면이 영영
        갱신되지 않는다(로그가 멈춘 것처럼 보인다).
        """
        super().showEvent(event)

        # 타이머가 멈춰 있었다 = 닫혔다가 다시 열렸다.
        # 처음 열 때는 __init__의 load_initial_logs()가 이미 채웠으므로 다시 읽지 않는다.
        was_closed = not self.update_timer.isActive()

        if was_closed:
            self.update_timer.start(1000)
        if not self.session_timer.isActive():
            self.session_timer.start(10000)

        self._connect_log_signal()

        if was_closed:
            # 닫혀 있는 동안 쌓인 로그를 한 번에 따라잡는다.
            self.refresh_logs()

    def closeEvent(self, event):
        """다이얼로그 닫기 이벤트"""
        # 타이머 정지
        self.update_timer.stop()
        self.session_timer.stop()

        # 시그널 연결 해제
        self._disconnect_log_signal()

        event.accept()
