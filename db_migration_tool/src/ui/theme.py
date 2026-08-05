"""UI 디자인 토큰 및 전역 스타일시트.

디자인 방향: **제어반(Control Panel)**
현장 운영자가 이미 하루 종일 읽고 있는 설비 제어반의 언어를 그대로 쓴다.
- 상태는 색이 고정된 **램프**로 읽는다(대기/확인 중/정상/오류 4가지뿐).
- 숫자(행 수, 속도, 시간)와 테이블명은 **고정폭 서체**로 읽는다.
  자릿수가 흔들리지 않아 실행 중 계기판이 떨리지 않는다.
- 한 화면에 **채워진 버튼은 하나**뿐이다. 그게 지금 눌러야 할 버튼이다.

색상/서체/간격은 전부 이 파일의 토큰에서만 정의한다.
개별 위젯에서 setStyleSheet으로 색을 직접 박지 말고,
objectName 또는 role/state 동적 프로퍼티를 지정해 여기에서 스타일을 받는다.

    label.setProperty("role", "hint")      # QLabel[role="hint"]
    lamp.setProperty("state", "ok")        # QLabel[role="lamp"][state="ok"]

동적 프로퍼티를 런타임에 바꿀 때는 반드시 repolish()를 호출해야 반영된다.
"""

from __future__ import annotations

from string import Template

from PySide6.QtWidgets import QWidget

# ── 색상 토큰 ────────────────────────────────────────────────────────────
# 계기반 표면 (어두운 순서: well < surface < rail)
SURFACE = "#111827"  # 창 배경
SURFACE_RAIL = "#172136"  # 상태 레일·요약 패널 등 살짝 떠 있는 면
SURFACE_WELL = "#0f172a"  # 입력/로그처럼 파인 면
BORDER = "#334155"
BORDER_STRONG = "#475569"
FOCUS = "#93c5fd"

# 글자
TEXT = "#f3f6fb"
TEXT_STRONG = "#f8fafc"
TEXT_MUTED = "#94a3b8"  # 모든 보조 설명문은 이 값 하나만 쓴다
TEXT_DANGER = "#fca5a5"  # 어두운 바탕에서 읽히는 경고 글자색

# 램프 (의미 고정 — 다른 뜻으로 재사용 금지)
LAMP_IDLE = "#64748b"  # 대기: 아직 확인하지 않음
LAMP_BUSY = "#f59e0b"  # 확인 중 / 일시정지
LAMP_OK = "#22c55e"  # 정상 / 실행 중 / 완료
LAMP_ERROR = "#dc2626"  # 오류 / 중단

# 액션
ACCENT = "#2563eb"
ACCENT_HOVER = "#1d4ed8"
ACCENT_BORDER = "#3b82f6"
GO = "#15803d"  # 마이그레이션 실행 계열
GO_HOVER = "#166534"
GO_BORDER = "#22c55e"
DANGER = "#b91c1c"
DANGER_HOVER = "#991b1b"
DANGER_BORDER = "#ef4444"

# ── 서체 토큰 ────────────────────────────────────────────────────────────
# UI: 한글이 기본으로 붙는 Windows 서체. 데이터: 자릿수가 고정되는 고정폭.
FONT_UI = '"Malgun Gothic", "Segoe UI", sans-serif'
FONT_MONO = '"Cascadia Mono", Consolas, "D2Coding", monospace'

SIZE_XS = 11
SIZE_SM = 12
SIZE_MD = 13
SIZE_LG = 15
SIZE_XL = 20

# 로그 위젯이 무한히 커지지 않도록 하는 상한(줄 수)
# 두 마법사의 실행 로그용. QTextEdit.append()는 호출마다 블록을 만들므로 그대로 동작한다.
LOG_MAX_BLOCKS = 5000
# 로그 뷰어용. 조회 limit과 같은 값이어야 "N개 표시 중"이 거짓말이 되지 않는다.
LOG_VIEWER_MAX_BLOCKS = 10000

# 로그 레벨 색상 (실행 로그 / 로그 뷰어 공용)
LOG_LEVEL_COLORS = {
    "DEBUG": TEXT_MUTED,
    "INFO": TEXT,
    "SUCCESS": LAMP_OK,
    "WARNING": LAMP_BUSY,
    "ERROR": "#f87171",
    "CRITICAL": "#f472b6",
}


def log_color(level: str) -> str:
    """로그 레벨에 대응하는 색을 돌려준다."""
    return LOG_LEVEL_COLORS.get(level.upper(), TEXT)


def repolish(widget: QWidget) -> None:
    """동적 프로퍼티(role/state)를 바꾼 뒤 스타일을 다시 계산시킨다."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


_TOKENS = {
    "surface": SURFACE,
    "rail": SURFACE_RAIL,
    "well": SURFACE_WELL,
    "border": BORDER,
    "border_strong": BORDER_STRONG,
    "focus": FOCUS,
    "text": TEXT,
    "text_strong": TEXT_STRONG,
    "muted": TEXT_MUTED,
    "lamp_idle": LAMP_IDLE,
    "lamp_busy": LAMP_BUSY,
    "lamp_ok": LAMP_OK,
    "lamp_error": LAMP_ERROR,
    "accent": ACCENT,
    "accent_hover": ACCENT_HOVER,
    "accent_border": ACCENT_BORDER,
    "go": GO,
    "go_hover": GO_HOVER,
    "go_border": GO_BORDER,
    "danger": DANGER,
    "danger_hover": DANGER_HOVER,
    "danger_border": DANGER_BORDER,
    "font_ui": FONT_UI,
    "font_mono": FONT_MONO,
    "xs": SIZE_XS,
    "sm": SIZE_SM,
    "md": SIZE_MD,
    "lg": SIZE_LG,
    "xl": SIZE_XL,
}

_QSS = Template(
    """
/* ── 기본 ─────────────────────────────────────────────── */
QWidget {
    color: $text;
    font-family: $font_ui;
    font-size: ${md}px;
}
QMainWindow, QDialog {
    background-color: $surface;
}
QToolBar {
    background-color: #0b1220;
    border-bottom: 1px solid $border;
    spacing: 6px;
    padding: 4px;
}
QStatusBar {
    background-color: #0b1220;
    color: #d7e3f4;
    border-top: 1px solid $border;
}
QGroupBox {
    border: 1px solid $border;
    border-radius: 6px;
    margin-top: 12px;
    padding: 12px 10px 10px 10px;
    font-weight: 600;
    color: $text_strong;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: #dbeafe;
}
QLabel {
    color: #e5edf7;
}

/* ── 입력 ─────────────────────────────────────────────── */
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDateEdit,
QListWidget, QTableWidget {
    background-color: $well;
    color: $text_strong;
    border: 1px solid $border_strong;
    border-radius: 4px;
    selection-background-color: $accent;
    selection-color: #ffffff;
}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDateEdit {
    padding: 6px;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDateEdit:focus, QListWidget:focus, QTableWidget:focus {
    border: 1px solid #60a5fa;
    background-color: #111c34;
}
QListWidget::item {
    padding: 8px;
    border-bottom: 1px solid #223047;
}
QListWidget::item:selected {
    background-color: $accent;
    color: #ffffff;
}
QTableWidget {
    alternate-background-color: #142033;
    gridline-color: $border;
}
QHeaderView::section {
    background-color: #1e293b;
    color: $text_strong;
    border: 1px solid $border;
    padding: 6px;
    font-weight: 600;
}
QTabWidget::pane {
    border: 1px solid $border;
    border-radius: 4px;
}
QTabBar::tab {
    background-color: #1f2937;
    color: #dbeafe;
    border: 1px solid $border;
    padding: 8px 14px;
}
QTabBar::tab:selected {
    background-color: $accent;
    color: #ffffff;
}

/* ── 버튼 ─────────────────────────────────────────────── */
/* 기본은 조용한 회색. 화면마다 '채워진' 버튼은 하나만 둔다. */
QPushButton {
    background-color: #1f2937;
    color: $text_strong;
    border: 1px solid #64748b;
    border-radius: 5px;
    padding: 7px 12px;
    font-weight: 600;
    /* 다이얼로그 안에서는 padding이 버튼의 sizeHint에 반영되지 않아
       '이전'·'검증 실행' 같은 문구가 잘려 보였다(단독 위젯일 때는 정상).
       min-width만은 확실히 반영되므로 여기서 바닥을 깔아 준다.
       텍스트가 더 길면 sizeHint가 그만큼 커지므로 넓은 버튼은 그대로 넓어진다. */
    min-width: 96px;
}
QPushButton:hover {
    background-color: $border;
    border-color: $focus;
}
QPushButton:pressed {
    background-color: $accent_hover;
}
QPushButton:focus {
    border: 1px solid $focus;
}
QPushButton:disabled {
    background-color: #243244;
    color: $muted;
    border-color: $border;
}
QDialogButtonBox QPushButton {
    min-width: 96px;
}

/* 보조 액션(날짜 프리셋, 전체 선택 등)은 한 단계 낮춘다 */
QPushButton[variant="chip"] {
    background-color: transparent;
    color: $muted;
    border: 1px solid $border;
    border-radius: 4px;
    padding: 4px 10px;
    font-weight: 500;
    /* 보조 액션은 좁아야 한다. 기본 버튼의 min-width를 물려받으면
       날짜 프리셋 같은 짧은 칩이 본 액션만큼 커진다. */
    min-width: 0;
}
QPushButton[variant="chip"]:hover {
    color: $text_strong;
    border-color: $focus;
}

QPushButton#newProfileButton, QPushButton#primaryAction, QPushButton#startButton {
    background-color: $accent;
    border-color: $accent_border;
    color: #ffffff;
}
QPushButton#newProfileButton:hover, QPushButton#primaryAction:hover,
QPushButton#startButton:hover {
    background-color: $accent_hover;
}
QPushButton#migrationButton {
    background-color: $go;
    border-color: $go_border;
    color: #ffffff;
}
QPushButton#migrationButton:hover {
    background-color: $go_hover;
}
/* id 선택자가 QPushButton:disabled 를 이기므로 비활성 상태를 따로 적어준다.
   (없으면 못 누르는 버튼이 눌리는 버튼처럼 밝게 보인다) */
QPushButton#newProfileButton:disabled, QPushButton#primaryAction:disabled,
QPushButton#startButton:disabled, QPushButton#migrationButton:disabled {
    background-color: #243244;
    color: $muted;
    border-color: $border;
}
/* 파괴적 액션은 평소엔 윤곽선만, 눌릴 수 있는 순간에만 채운다 */
QPushButton#dangerAction {
    background-color: transparent;
    border-color: $danger_border;
    color: #fca5a5;
}
QPushButton#dangerAction:hover {
    background-color: $danger;
    color: #ffffff;
}
QPushButton#dangerAction:disabled {
    background-color: #243244;
    color: $muted;
    border-color: $border;
}
/* 메인 창의 큰 액션 버튼 */
QPushButton[variant="large"] {
    font-size: ${lg}px;
    padding: 12px 20px;
    min-height: 45px;
}

/* ── 진행률 ───────────────────────────────────────────── */
QProgressBar {
    background-color: $well;
    color: $text_strong;
    border: 1px solid $border_strong;
    border-radius: 4px;
    text-align: center;
    font-family: $font_mono;
}
QProgressBar::chunk {
    background-color: $lamp_ok;
    border-radius: 3px;
}

QToolTip {
    background-color: $text_strong;
    color: $surface;
    border: 1px solid #64748b;
    padding: 4px;
}

/* ── 텍스트 역할 ───────────────────────────────────────── */
QLabel[role="stepTitle"] {
    font-size: ${xl}px;
    font-weight: 700;
    color: $text_strong;
}
QLabel[role="hint"] {
    color: $muted;
    font-size: ${sm}px;
}
QLabel[role="muted"] {
    color: $muted;
}
QLabel[role="fieldTitle"] {
    font-weight: 700;
    color: $text_strong;
}

/* ── 계기(숫자 읽는 자리) ───────────────────────────────── */
QLabel[role="metricValue"] {
    font-family: $font_mono;
    font-size: ${lg}px;
    font-weight: 600;
    color: $text_strong;
}
QLabel[role="metricLabel"] {
    color: $muted;
    font-size: ${xs}px;
}

/* ── 램프 ─────────────────────────────────────────────── */
QLabel[role="lamp"] {
    font-size: ${lg}px;
    color: $lamp_idle;
}
QLabel[role="lamp"][state="busy"] { color: $lamp_busy; }
QLabel[role="lamp"][state="ok"]    { color: $lamp_ok; }
QLabel[role="lamp"][state="error"] { color: $lamp_error; }

QLabel[role="lampText"] { color: $muted; }
QLabel[role="lampText"][state="busy"]  { color: #fcd34d; }
QLabel[role="lampText"][state="ok"]    { color: #bbf7d0; }
QLabel[role="lampText"][state="error"] { color: #fca5a5; }

/* 상태 레일: 자식 QLabel까지 테두리가 번지지 않도록 id 선택자를 쓴다 */
QFrame#statusRail {
    background-color: $rail;
    border: 1px solid $border;
    border-radius: 6px;
}

/* ── 스텝 레일 ─────────────────────────────────────────── */
QLabel[role="stepChip"] {
    min-width: 22px;
    max-width: 22px;
    min-height: 22px;
    max-height: 22px;
    border-radius: 11px;
    border: 1px solid $border_strong;
    color: $muted;
    font-weight: 700;
    font-size: ${sm}px;
}
QLabel[role="stepChip"][state="current"] {
    background-color: $accent;
    border-color: $accent_border;
    color: #ffffff;
}
QLabel[role="stepChip"][state="done"] {
    background-color: #1e3a8a;
    border-color: $accent_border;
    color: #dbeafe;
}
QLabel[role="stepName"] {
    color: $muted;
    font-size: ${sm}px;
}
QLabel[role="stepName"][state="current"] {
    color: $text_strong;
    font-weight: 700;
}
QLabel[role="stepName"][state="done"] {
    color: #cbd5f5;
}
QFrame[role="stepLine"] {
    background-color: $border;
    max-height: 1px;
    min-height: 1px;
    border: none;
}

/* ── 고정폭으로 읽는 위젯 ──────────────────────────────── */
QTextEdit#runLog, QTextEdit#logViewer, QListWidget#partitionList {
    font-family: $font_mono;
    font-size: ${sm}px;
}
QTextEdit#logViewer {
    background-color: #101827;
}
QLabel[role="summary"] {
    font-family: $font_mono;
    color: #dbeafe;
}
"""
)


def build_stylesheet() -> str:
    """앱 전역 스타일시트를 만든다 (qdarkstyle 위에 덧씌워 사용)."""
    return _QSS.substitute(_TOKENS)
