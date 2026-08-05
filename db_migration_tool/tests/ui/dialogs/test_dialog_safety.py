"""리뷰에서 확인된 결함들에 대한 회귀 테스트

세 건 모두 '조용히 잘못되는' 종류라 눈으로는 안 잡힌다.
- 로그 뷰어: `<br>`로 줄을 넣으면 블록이 안 늘어 상한이 무력화된다.
- 연결 대화상자: 테스트 성공 뒤 입력을 고쳐도 결과가 남아 미검증 설정이 저장된다.
- 파일 아카이브: 동기 작업 중 큐에 있던 '다음'이 배달되면 실행 대상이 잘못 굳는다.
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest

import src.ui.dialogs.file_archive_migration_dialog as archive_mod
from src.ui.theme import LOG_VIEWER_MAX_BLOCKS


def mod_partition_summary(name, table_type):
    return archive_mod.PartitionSummary(table_name=name, row_count=100, table_type=table_type)


@pytest.fixture
def log_viewer(qapp, tmp_path):
    from src.ui.dialogs.log_viewer_dialog import LogViewerDialog

    dialog = LogViewerDialog(None)
    yield dialog
    dialog.close()


class TestLogViewerBlockLimit:
    def test_each_entry_becomes_its_own_block(self, log_viewer):
        """블록이 안 늘면 maximumBlockCount가 아무것도 막지 못한다."""
        log_viewer.log_text.clear()
        for i in range(5):
            log_viewer._append_line("260804 10:00:00", "S1", "INFO", f"msg {i}")

        assert log_viewer.log_text.document().blockCount() == 5

    def test_document_is_capped_and_drops_oldest(self, log_viewer):
        log_viewer.log_text.clear()
        for i in range(LOG_VIEWER_MAX_BLOCKS + 200):
            log_viewer._append_line("260804 10:00:00", "S1", "INFO", f"msg {i}")

        document = log_viewer.log_text.document()
        assert document.blockCount() == LOG_VIEWER_MAX_BLOCKS

        text = document.toPlainText()
        assert "msg 0" not in text, "상한을 넘은 오래된 줄은 잘려야 합니다"
        assert f"msg {LOG_VIEWER_MAX_BLOCKS + 199}" in text

    def test_status_reports_actual_block_count(self, log_viewer):
        """'N개 표시 중'은 실제 남은 줄 수여야 한다."""
        log_viewer.log_text.clear()
        for i in range(7):
            log_viewer._append_line("260804 10:00:00", "S1", "INFO", f"msg {i}")

        log_viewer._update_status_count()
        assert "7" in log_viewer.status_label.text()

    def test_status_announces_the_cap_when_reached(self, log_viewer):
        """상한까지 찼으면 '더 있는데 안 보인다'는 사실을 말해야 한다."""
        log_viewer.log_text.clear()
        for i in range(LOG_VIEWER_MAX_BLOCKS + 50):
            log_viewer._append_line("260804 10:00:00", "S1", "INFO", f"msg {i}")

        log_viewer._update_status_count()
        assert f"{LOG_VIEWER_MAX_BLOCKS:,}" in log_viewer.status_label.text()

    def test_message_markup_is_not_interpreted(self, log_viewer):
        log_viewer.log_text.clear()
        log_viewer._append_line("260804 10:00:00", "S1", "ERROR", "a < b & <b>c</b>")
        assert "<b>c</b>" in log_viewer.log_text.document().toPlainText()

    def test_live_signal_does_not_double_render(self, log_viewer):
        """emit_log는 DB에 쓴 뒤 신호를 보낸다.

        신호에서도 본문을 그리면 폴링이 같은 행을 다시 가져와 모든 로그가
        두 줄씩 쌓인다(신호에는 DB id가 없어 걸러낼 수도 없다).
        """
        log_viewer.log_text.clear()
        before = log_viewer.log_text.document().toPlainText()

        log_viewer.on_log_received("260804 10:00:00", "S1", "INFO", "중복되면 안 되는 줄")

        assert log_viewer.log_text.document().toPlainText() == before
        assert "마지막 업데이트" in log_viewer.last_update_label.text()

    def test_reopening_revives_polling(self, log_viewer):
        """메인 창은 뷰어를 싱글톤으로 재사용한다.

        닫으면서 멈춘 타이머를 되살리지 않으면 두 번째로 열었을 때
        화면이 영영 갱신되지 않는다(로그가 멈춘 것처럼 보인다).
        """
        log_viewer.show()
        log_viewer.close()
        assert not log_viewer.update_timer.isActive()

        log_viewer.show()
        assert log_viewer.update_timer.isActive()
        assert log_viewer.session_timer.isActive()

    def test_repeated_open_close_keeps_single_connection(self, log_viewer):
        for _ in range(3):
            log_viewer.show()
            log_viewer.close()
        log_viewer.show()
        assert log_viewer._log_signal_connected is True


class TestLogViewerPollingBounds:
    """폴링 제외 조건이 무한히 커지지 않아야 한다.

    본 적 있는 id를 전부 모아 `NOT IN (...)`으로 넘기면 파라미터가 계속 늘어
    결국 SQLite 한도(32,766)를 넘겨 폴링이 죽는다. 마지막 id 하나만 기억한다.
    """

    def test_does_not_accumulate_an_id_set(self, log_viewer):
        assert not hasattr(log_viewer, "displayed_log_ids"), (
            "id 집합을 다시 들고 있으면 폴링 쿼리 파라미터가 무한히 늘어납니다"
        )
        assert isinstance(log_viewer._last_log_id, int)

    def test_polling_twice_adds_nothing_new(self, log_viewer):
        log_viewer.show()
        log_viewer.check_new_logs()
        before = log_viewer.log_text.document().blockCount()

        log_viewer.check_new_logs()
        log_viewer.check_new_logs()

        assert log_viewer.log_text.document().blockCount() == before

    def test_clear_display_keeps_the_watermark(self, log_viewer):
        """'화면 지우기'는 지운 로그를 다시 끌어오지 않아야 한다."""
        log_viewer.show()
        mark = log_viewer._last_log_id

        log_viewer.clear_display()

        assert log_viewer._last_log_id == mark
        assert log_viewer.log_text.document().isEmpty()

    def test_refresh_resets_the_watermark(self, log_viewer):
        """'새로고침'은 조건에 맞는 로그를 다시 처음부터 불러온다."""
        log_viewer.show()
        log_viewer.clear_display()

        log_viewer.refresh_logs()

        assert log_viewer._last_log_id >= 0

    def _db_max_id(self, log_viewer, *conditions):
        from sqlalchemy import func

        from src.database.local_db import LogEntry

        session = log_viewer.db.get_session()
        try:
            return session.query(func.max(LogEntry.id)).filter(*conditions).scalar() or 0
        finally:
            session.close()

    def test_watermark_is_the_true_max_not_the_displayed_max(self, log_viewer):
        """기준점을 '표시된 것 중 최대 id'로 잡으면,

        표시 상한에 걸려 잘린 예전 로그가 나중에 폴링으로 딸려와 새 로그인 것처럼
        맨 아래 붙는다. 조회 시점에 존재하던 최대 id여야 한다.
        """
        log_viewer.apply_filters()

        expected = self._db_max_id(log_viewer, *log_viewer._current_filters())
        assert log_viewer._last_log_id == expected

    def test_watermark_follows_the_active_filter(self, log_viewer):
        from src.database.local_db import LogEntry

        log_viewer.level_filter.setCurrentText("ERROR")

        expected = self._db_max_id(
            log_viewer, LogEntry.level == "ERROR", *log_viewer._current_filters()
        )
        assert log_viewer._last_log_id == expected

    def test_watermark_comes_from_the_display_query_itself(self, log_viewer, monkeypatch):
        """기준점을 별도 MAX(id) 쿼리로 구하면 두 쿼리 사이에 커밋된 로그가

        '화면에 없는데 기준점에는 포함'되어 영영 표시되지 않는다.
        표시 대상과 기준점은 한 번의 조회에서 함께 나와야 한다.
        """
        import src.ui.dialogs.log_viewer_dialog as mod

        source = inspect.getsource(mod.LogViewerDialog.apply_filters)
        assert "func.max" not in source, (
            "표시 조회와 기준점 조회를 나누면 그 사이 커밋된 로그를 건너뜁니다"
        )
        assert "LogEntry.id.desc()" in source

    def test_display_order_is_insertion_order(self, log_viewer):
        """폴링은 id 오름차순으로 덧붙인다.

        최초 조회가 timestamp 순이면 두 경로의 정렬 기준이 어긋난다.
        """
        import src.ui.dialogs.log_viewer_dialog as mod

        source = inspect.getsource(mod.LogViewerDialog.apply_filters)
        assert "timestamp.desc()" not in source

    def test_filters_are_built_in_one_place(self, log_viewer):
        """조회와 증분 폴링이 다른 조건을 쓰면 누락/중복이 생긴다."""
        log_viewer.level_filter.setCurrentText("ERROR")
        assert len(log_viewer._current_filters()) >= 3  # 날짜 2개 + 레벨


class TestLogViewerRefreshCost:
    def test_session_list_refresh_does_not_redraw_the_log(self, log_viewer, monkeypatch):
        """세션 목록은 10초마다 갱신된다.

        갱신 중 콤보 신호가 튀면 그때마다 로그 문서 전체를 다시 그려
        10초마다 화면이 끊기고 스크롤 위치도 튄다.
        """
        log_viewer.show()

        calls = []
        monkeypatch.setattr(log_viewer, "apply_filters", lambda: calls.append(1))

        log_viewer.update_session_list()

        assert calls == [], "세션 목록 갱신이 로그 전체 재조회를 유발했습니다"

    def test_session_list_refresh_keeps_selection(self, log_viewer):
        log_viewer.show()
        before = log_viewer.session_filter.currentText()

        log_viewer.update_session_list()

        assert log_viewer.session_filter.currentText() == before

    def test_session_start_time_is_not_clipped_by_the_listing_window(self, log_viewer):
        """'어떤 세션을 보여줄지'와 '언제 시작했는지'는 다른 질문이다.

        한 번에 물으면 일주일 넘게 떠 있는 세션의 시작 시각이 실제 시작이 아니라
        '최근 7일 중 최초'로 잘린다.
        """
        import src.ui.dialogs.log_viewer_dialog as mod

        source = inspect.getsource(mod.LogViewerDialog.update_session_list)
        # 집계에 날짜 조건이 함께 걸려 있으면 시작 시각이 잘린다
        assert "recent_session_ids" in source
        assert "session_id.in_(recent_session_ids)" in source

    def test_first_show_does_not_requery(self, log_viewer, monkeypatch):
        """생성자에서 이미 불러왔으므로 첫 표시에서 또 읽을 이유가 없다."""
        calls = []
        monkeypatch.setattr(log_viewer, "apply_filters", lambda: calls.append(1))

        log_viewer.show()

        assert calls == []

    def test_reopen_catches_up(self, log_viewer, monkeypatch):
        log_viewer.show()
        log_viewer.close()

        calls = []
        monkeypatch.setattr(log_viewer, "apply_filters", lambda: calls.append(1))

        log_viewer.show()

        assert calls == [1], "닫혀 있는 동안 쌓인 로그를 따라잡아야 합니다"


@pytest.fixture
def connection_dialog(qapp):
    from src.ui.dialogs.connection_dialog import ConnectionDialog

    dialog = ConnectionDialog(None)
    yield dialog
    dialog.deleteLater()


class TestConnectionTestResultFreshness:
    def _mark_tested(self, dialog, side="source"):
        widgets = dialog.endpoint_widgets[side]
        widgets["result_lamp"].set_state("ok", "연결됨 · PostgreSQL 16.2")
        widgets["save_preset_btn"].setVisible(True)
        return widgets

    @pytest.mark.parametrize(
        "field,value",
        [
            ("host", "10.0.0.9"),
            ("database", "other_db"),
            ("username", "someone"),
            ("password", "changed"),
        ],
    )
    def test_editing_a_field_invalidates_the_result(self, connection_dialog, field, value):
        """저장은 현재 입력값을 읽으므로, 결과가 남으면 미검증 설정이 저장된다."""
        widgets = self._mark_tested(connection_dialog)
        widgets[field].setText(value)

        assert widgets["result_lamp"].state == "idle"
        assert not widgets["save_preset_btn"].isVisible()

    def test_changing_port_invalidates_the_result(self, connection_dialog):
        widgets = self._mark_tested(connection_dialog)
        widgets["port"].setValue(5433)

        assert widgets["result_lamp"].state == "idle"
        assert not widgets["save_preset_btn"].isVisible()

    def test_toggling_ssl_invalidates_the_result(self, connection_dialog):
        widgets = self._mark_tested(connection_dialog)
        widgets["ssl"].setChecked(True)

        assert widgets["result_lamp"].state == "idle"
        assert not widgets["save_preset_btn"].isVisible()

    def test_each_endpoint_keeps_its_own_result(self, connection_dialog):
        source = self._mark_tested(connection_dialog, "source")
        target = self._mark_tested(connection_dialog, "target")

        source["host"].setText("10.0.0.9")

        assert source["result_lamp"].state == "idle"
        assert target["result_lamp"].state == "ok", "다른 탭 결과까지 지우면 안 됩니다"


@pytest.fixture
def archive_dialog(qapp):
    profile = MagicMock()
    profile.id = 1
    profile.name = "아카이브"
    profile.migration_mode = "file_to_postgres"
    profile.source_kind = "file"
    profile.target_kind = "postgres"
    profile.source_config = {"archive_path": "D:/archive"}
    profile.target_config = {}

    with (
        patch.object(archive_mod, "HistoryManager") as history_manager,
        patch.object(archive_mod, "CheckpointManager"),
        patch.object(
            archive_mod.FileArchiveMigrationDialog, "check_connections", lambda self: None
        ),
    ):
        history_manager.return_value.get_incomplete_history.return_value = None
        dialog = archive_mod.FileArchiveMigrationDialog(None, profile)

    yield dialog
    dialog.deleteLater()


class TestArchiveBusyGuard:
    """`_busy` 컨텍스트매니저가 지키던 불변식을 비동기 경로로 이관한 것.

    옛 구현은 `processEvents()`로 큐를 비우면서 '다음'이 뒤늦게 배달돼
    대상 확인 전 상태(전 파티션 체크)로 실행 대상이 굳는 문제를 막으려고
    네비게이션을 잠갔다. 워커로 바뀐 뒤에는 `_selection_verified` 게이트가
    같은 역할을 하고, 이벤트 루프를 막지 않으므로 닫기는 오히려 허용된다.
    """

    def test_next_is_locked_until_the_target_check_completes(self, archive_dialog):
        from src.core.table_types import TableType

        archive_dialog.source_connected = True
        archive_dialog.target_connected = True
        archive_dialog.pages.setCurrentIndex(1)
        archive_dialog.discovered_partitions = [
            mod_partition_summary("tbl_0", TableType.POINT_HISTORY)
        ]
        archive_dialog._render_partition_list()
        archive_dialog._update_counts()
        archive_dialog._update_nav_state()

        assert archive_dialog.get_selected_partition_names()
        assert not archive_dialog.next_btn.isEnabled(), (
            "확인 전 상태로 넘어가면 완료 파티션까지 다시 적재됩니다"
        )

    def test_scan_does_not_lock_the_close_button(self, archive_dialog):
        """옛 구현은 이벤트 루프를 막아 닫기까지 잠갔다.

        워커로 옮긴 뒤에는 조회 중에도 창을 닫을 수 있어야 한다.
        """
        worker = MagicMock()
        worker.isRunning.return_value = True
        archive_dialog._scan_workers["discover"] = worker
        archive_dialog._update_nav_state()

        assert archive_dialog.close_btn.isEnabled()
        assert not archive_dialog._block_close_while_running()

    def test_navigation_returns_to_rule_after_a_scan(self, archive_dialog):
        """작업이 끝나면 무조건 켜는 게 아니라 규칙대로 되돌아와야 한다."""
        archive_dialog.source_connected = False
        archive_dialog.target_connected = False
        archive_dialog._mark_scan_started("discover")

        archive_dialog._on_discovery_finished()

        assert archive_dialog.close_btn.isEnabled()
        # 연결이 안 됐으므로 '다음'은 여전히 잠겨 있어야 한다
        assert not archive_dialog.next_btn.isEnabled()

    def test_dialog_no_longer_pumps_the_event_loop(self):
        """`processEvents()`가 큐에 밀린 클릭을 뒤늦게 배달하던 원인이었다."""
        import src.ui.dialogs.file_archive_migration_dialog as mod

        source = inspect.getsource(mod)
        assert "processEvents" not in source

    def test_connection_check_failure_does_not_lock_the_dialog(self, archive_dialog):
        """연결 확인 실패가 창을 잠그면 안 된다.

        복구를 성공 경로에만 두면, 실패했을 때 '닫기'까지 영구 비활성이 되어
        Esc 말고는 창을 빠져나갈 수 없다. 그래서 복구는 성공·실패 공통인
        QThread.finished 쪽에 있어야 한다.
        """
        gen = archive_dialog._scan_gen
        archive_dialog.recheck_btn.setEnabled(False)

        archive_dialog._on_connection_check_failed(gen, "boom")
        archive_dialog._on_connection_check_finished()

        assert archive_dialog.close_btn.isEnabled()
        assert archive_dialog.recheck_btn.isEnabled()
        assert archive_dialog.source_lamp.state == "error", "실패가 램프에 드러나야 합니다"
        assert not archive_dialog.source_connected
