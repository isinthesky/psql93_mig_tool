"""디자인 토큰과 계기 위젯 테스트"""

import pytest

from src.ui import theme


class TestTheme:
    def test_stylesheet_has_no_unresolved_tokens(self):
        qss = theme.build_stylesheet()
        assert "$" not in qss, "치환되지 않은 Template 토큰이 남아 있습니다"
        assert qss.strip()

    def test_stylesheet_defines_lamp_states(self):
        qss = theme.build_stylesheet()
        for state in ("busy", "ok", "error"):
            assert f'QLabel[role="lamp"][state="{state}"]' in qss

    def test_status_rail_uses_id_selector(self):
        """`QWidget {...}`로 쓰면 자식 QLabel마다 테두리가 그려진다."""
        qss = theme.build_stylesheet()
        assert "QFrame#statusRail" in qss

    def test_emphasised_buttons_define_a_disabled_look(self):
        """id 선택자는 QPushButton:disabled를 이긴다.

        비활성 규칙을 따로 안 적으면 못 누르는 버튼이 밝게 그려진다.
        """
        qss = theme.build_stylesheet()
        for name in ("newProfileButton", "primaryAction", "startButton", "migrationButton"):
            assert f"QPushButton#{name}:disabled" in qss, f"{name}의 비활성 스타일이 없습니다"

    def test_log_color_falls_back_to_body_text(self):
        assert theme.log_color("ERROR") == theme.LOG_LEVEL_COLORS["ERROR"]
        assert theme.log_color("error") == theme.LOG_LEVEL_COLORS["ERROR"]
        assert theme.log_color("알수없는레벨") == theme.TEXT


@pytest.mark.usefixtures("qapp")
class TestInstruments:
    def test_lamp_state_and_message(self):
        from src.ui.widgets import StatusLamp

        lamp = StatusLamp("소스 DB")
        assert lamp.state == "idle"

        lamp.set_state("ok", "연결됨")
        assert lamp.state == "ok"
        assert lamp.lamp_label.property("state") == "ok"
        assert lamp.text_label.text() == "연결됨"

    def test_lamp_rejects_unknown_state(self):
        from src.ui.widgets import StatusLamp

        lamp = StatusLamp()
        lamp.set_state("무슨상태", "x")
        assert lamp.state == "idle"

    def test_lamp_keeps_message_when_omitted(self):
        from src.ui.widgets import StatusLamp

        lamp = StatusLamp()
        lamp.set_state("ok", "연결됨")
        lamp.set_state("error")
        assert lamp.text_label.text() == "연결됨"

    def test_metric_placeholder_and_set_value(self):
        from src.ui.widgets import MetricReadout

        metric = MetricReadout("처리 속도")
        assert metric.value_label.text() == "-"

        metric.set_value("1.2M rows/s")
        assert metric.value_label.text() == "1.2M rows/s"

        # 빈 값은 자리표시자로 되돌린다 — 계기 자리가 비어 보이면 안 된다.
        metric.set_value("")
        assert metric.value_label.text() == "-"

    def test_step_rail_marks_done_current_todo(self):
        from src.ui.widgets import StepRail

        rail = StepRail(["연결", "범위", "실행"])
        rail.set_current(1)

        states = [chip.property("state") for chip in rail._chips]
        assert states == ["done", "current", "todo"]
