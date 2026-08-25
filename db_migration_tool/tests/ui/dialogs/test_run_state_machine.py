"""실행 페이지 상태 머신 테스트

과거에 버튼 활성화를 여러 핸들러에서 따로 건드리다가
'취소하고 나면 시작도 취소도 못 누르는' 막다른 상태가 생겼다.
상태 하나가 버튼 전체를 정하는지 여기서 확인한다.
"""

from unittest.mock import MagicMock, patch

import pytest

import src.ui.dialogs.file_archive_migration_dialog as archive_mod
import src.ui.dialogs.migration_wizard_dialog as wizard_mod


@pytest.fixture
def wizard(qapp):
    profile = MagicMock()
    profile.id = 1
    profile.name = "테스트 프로필"
    profile.source_config = {}
    profile.target_config = {}

    with (
        patch.object(wizard_mod, "HistoryManager") as history_manager,
        patch.object(wizard_mod, "CheckpointManager"),
        patch.object(wizard_mod.MigrationWizardDialog, "check_connections", lambda self: None),
    ):
        history_manager.return_value.get_incomplete_history.return_value = None
        dialog = wizard_mod.MigrationWizardDialog(None, profile)

    yield dialog
    dialog.deleteLater()


@pytest.fixture
def archive_dialog(qapp):
    profile = MagicMock()
    profile.id = 1
    profile.name = "아카이브 프로필"
    profile.migration_mode = "postgres_to_file"
    profile.source_kind = "postgres"
    profile.target_kind = "file"
    profile.source_config = {}
    profile.target_config = {"archive_path": "D:/archive"}

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


class TestWizardRunState:
    def test_idle_offers_start_only(self, wizard):
        assert wizard.run_state == "idle"
        assert wizard.start_btn.isEnabled()
        assert not wizard.pause_btn.isEnabled()
        assert not wizard.cancel_btn.isEnabled()
        assert wizard.close_btn.isEnabled()

    def test_running_locks_close_and_offers_cancel(self, wizard):
        wizard._set_run_state("running")
        assert not wizard.start_btn.isEnabled()
        assert wizard.pause_btn.isEnabled()
        assert wizard.cancel_btn.isEnabled()
        assert not wizard.close_btn.isEnabled()

    def test_paused_switches_pause_label(self, wizard):
        wizard._set_run_state("paused")
        assert wizard.pause_btn.text() == "재개"
        assert wizard.cancel_btn.isEnabled()

    def test_stopped_can_be_resumed_and_closed(self, wizard):
        """취소 직후 아무 버튼도 못 누르던 막다른 상태 회귀 테스트."""
        wizard._set_run_state("stopped")
        assert wizard.start_btn.isEnabled()
        assert wizard.start_btn.text() == "이어서 시작"
        assert wizard.close_btn.isEnabled()

    def test_done_blocks_restart(self, wizard):
        wizard._set_run_state("done")
        assert not wizard.start_btn.isEnabled()
        assert not wizard.cancel_btn.isEnabled()
        assert wizard.close_btn.isEnabled()

    def test_verify_needs_history(self, wizard):
        wizard._set_run_state("done")
        assert not wizard.verify_btn.isEnabled()

        wizard.history_id = 7
        wizard._set_run_state("done")
        assert wizard.verify_btn.isEnabled()

    def test_restart_after_stop_resumes_instead_of_new_history(self, wizard):
        """중단 후 재시작이 이력을 새로 만들면 이미 옮긴 파티션을 다시 복사한다."""
        wizard.history_id = 42
        wizard.resume_mode = False
        wizard.source_connected = True
        wizard.target_connected = True
        wizard._frozen_selection = []

        pending = [MagicMock(partition_name="tbl_a"), MagicMock(partition_name="tbl_b")]
        wizard.checkpoint_manager.get_pending_checkpoints.return_value = pending

        with patch.object(wizard_mod, "CopyMigrationWorker") as worker_cls:
            worker_cls.return_value.isRunning.return_value = False
            wizard.start_migration()

        assert wizard.resume_mode is True
        assert wizard._frozen_selection == ["tbl_a", "tbl_b"]
        wizard.history_manager.create_history.assert_not_called()
        # 재개는 server-side COPY로 할 수 없다
        assert wizard.copy_mode == "python"


class TestWizardPartitionList:
    def _fill(self, wizard, count=4):
        from src.core.table_types import TableType

        wizard.discovered_partitions = [
            wizard_mod.PartitionSummary(f"tbl_{i}", i * 1000, TableType.POINT_HISTORY)
            for i in range(count)
        ]
        wizard._render_partition_list()
        wizard._update_counts()

    def test_completed_partitions_start_unchecked(self, wizard):
        wizard._completed_from_history = {"tbl_1"}
        self._fill(wizard)
        assert "tbl_1" not in wizard.get_selected_partition_names()

    def test_row_count_label_counts_selection_only(self, wizard):
        wizard._completed_from_history = {"tbl_3"}
        self._fill(wizard)
        # 0 + 1000 + 2000 = 3000 (tbl_3의 3000은 제외)
        assert "3,000" in wizard.partition_rows_label.text()

    def test_filter_hides_without_changing_selection(self, wizard):
        self._fill(wizard)
        before = wizard.get_selected_partition_names()

        wizard.partition_filter.setText("tbl_2")
        assert sum(1 for _ in wizard._visible_partition_items()) == 1
        assert wizard.get_selected_partition_names() == before

    def test_bulk_actions_only_touch_visible_items(self, wizard):
        self._fill(wizard)
        wizard.partition_filter.setText("tbl_2")
        wizard._bulk_check(False)

        remaining = wizard.get_selected_partition_names()
        assert "tbl_2" not in remaining
        assert "tbl_0" in remaining

    def test_selection_outside_filter_is_announced(self, wizard):
        """필터 밖에 체크된 항목이 남아 있으면 조용히 실행되면 안 된다."""
        self._fill(wizard)
        wizard.partition_filter.setText("tbl_2")

        label = wizard.partition_selected_label.text()
        assert "필터 밖" in label
        assert wizard.partition_selected_label.toolTip()

    def test_no_filter_notice_when_nothing_hidden(self, wizard):
        self._fill(wizard)
        assert "필터 밖" not in wizard.partition_selected_label.text()


class TestWizardNavigation:
    def test_default_dialog_size_and_compact_date_row(self, wizard):
        assert wizard.size().toTuple() == (1000, 1200)

        date_group = wizard.start_date_edit.parentWidget()
        assert date_group.layout().count() == 1
        date_row = date_group.layout().itemAt(0).layout()
        assert date_row.indexOf(wizard.start_date_edit) >= 0
        assert date_row.indexOf(wizard.preset_today_btn) >= 0
        assert date_row.indexOf(wizard.preset_30d_btn) >= 0

    def test_run_summary_uses_two_columns(self, wizard):
        wizard._refresh_summary()

        summary_layout = wizard.summary_labels[0].parentWidget().layout()
        assert len(wizard.summary_labels) == 6
        assert summary_layout.getItemPosition(summary_layout.indexOf(wizard.summary_labels[0]))[
            :2
        ] == (0, 0)
        assert summary_layout.getItemPosition(summary_layout.indexOf(wizard.summary_labels[1]))[
            :2
        ] == (0, 1)
        assert summary_layout.getItemPosition(summary_layout.indexOf(wizard.summary_labels[5]))[
            :2
        ] == (2, 1)
        assert all(not label.isHidden() for label in wizard.summary_labels)

    def test_back_is_locked_after_a_run_started(self, wizard):
        """이미 실행한 뒤 범위를 다시 고르면 선택과 작업 이력이 어긋난다."""
        wizard.pages.setCurrentIndex(2)
        wizard._set_run_state("done")
        assert not wizard.back_btn.isEnabled()

    def test_back_is_available_before_running(self, wizard):
        wizard.pages.setCurrentIndex(2)
        wizard._set_run_state("idle")
        assert wizard.back_btn.isEnabled()

    def test_resume_survives_going_back_and_forward(self, wizard):
        """재개 대상은 목록 체크가 아니라 미완료 체크포인트다.

        여기서 목록 선택을 요구하면 재개 → 이전 → 다음에서 실행 페이지로
        영영 돌아갈 수 없다(목록은 탐색한 적이 없어 비어 있다).
        """
        wizard.source_connected = True
        wizard.target_connected = True
        wizard.resume_mode = True
        wizard.history_id = 99
        wizard._frozen_selection = ["tbl_a", "tbl_b"]
        wizard.pages.setCurrentIndex(2)
        wizard._set_run_state("idle")

        wizard.go_back()
        assert wizard.pages.currentIndex() == 1
        assert wizard.next_btn.isEnabled(), "재개 모드인데 '다음'이 잠겨 빠져나갈 수 없습니다"

        wizard.go_next()
        assert wizard.pages.currentIndex() == 2
        assert wizard._frozen_selection == ["tbl_a", "tbl_b"]


class TestWizardProgress:
    def test_start_is_rightmost_with_cancel_immediately_before_it(self, wizard):
        controls = wizard.start_btn.parentWidget().layout()

        assert controls.itemAt(controls.count() - 1).widget() is wizard.start_btn
        assert controls.itemAt(controls.count() - 2).widget() is wizard.cancel_btn

    def test_server_copy_uses_busy_indicator_until_percent_is_known(self, wizard):
        wizard.on_progress(
            {
                "total_progress": 0,
                "total_partitions": 2,
                "completed_partitions": 0,
                "current_progress": 0,
                "current_partition": "tbl_a",
                "current_rows": 0,
                "current_indeterminate": True,
                "speed": 0,
            }
        )

        assert wizard.current_progress.minimum() == 0
        assert wizard.current_progress.maximum() == 0
        assert "Server-side COPY 진행 중" in wizard.current_label.text()

        wizard.on_progress(
            {
                "current_progress": 37,
                "current_partition": "tbl_a",
                "current_rows": 370,
                "current_indeterminate": False,
            }
        )

        assert wizard.current_progress.minimum() == 0
        assert wizard.current_progress.maximum() == 100
        assert wizard.current_progress.value() == 37
        assert wizard.current_label.text() == "tbl_a (370 rows)"

    def test_terminal_transition_restores_percent_range(self, wizard):
        wizard._set_current_progress_indeterminate(True)

        wizard._set_run_state("failed")

        assert wizard.current_progress.minimum() == 0
        assert wizard.current_progress.maximum() == 100


class TestArchiveRunState:
    def test_partial_offers_retry_of_failures(self, archive_dialog):
        archive_dialog._set_run_state("partial")
        assert archive_dialog.start_btn.isEnabled()
        assert archive_dialog.start_btn.text() == "실패분 다시 실행"
        assert archive_dialog.close_btn.isEnabled()

    def test_running_locks_close(self, archive_dialog):
        archive_dialog._set_run_state("running")
        assert not archive_dialog.close_btn.isEnabled()
        assert archive_dialog.cancel_btn.isEnabled()


class TestTrayRunNotification:
    """실행 상태가 트레이까지 전달되는지 확인한다.

    과거에 `set_migration_running`을 아무도 부르지 않아 플래그가 늘 False였다.
    그 결과 마이그레이션 중에도 트레이 '종료'가 되묻지 않고 앱을 껐다.
    """

    @staticmethod
    def _capture(dialog):
        seen: list[bool] = []
        dialog.migration_running_changed.connect(seen.append)
        return seen

    def test_wizard_reports_running(self, wizard):
        seen = self._capture(wizard)
        wizard._set_run_state("running")
        assert seen == [True]

    def test_wizard_reports_paused_as_still_running(self, wizard):
        """일시정지는 아직 끝난 게 아니다. 여기서 False를 보내면 종료 확인이 사라진다."""
        seen = self._capture(wizard)
        wizard._set_run_state("paused")
        assert seen == [True]

    @pytest.mark.parametrize("state", ["idle", "done", "stopped", "failed"])
    def test_wizard_reports_not_running_on_terminal_states(self, wizard, state):
        seen = self._capture(wizard)
        wizard._set_run_state(state)
        assert seen == [False]

    def test_archive_reports_running(self, archive_dialog):
        seen = self._capture(archive_dialog)
        archive_dialog._set_run_state("running")
        assert seen == [True]

    @pytest.mark.parametrize("state", ["idle", "done", "partial", "stopped", "failed"])
    def test_archive_reports_not_running_on_terminal_states(self, archive_dialog, state):
        seen = self._capture(archive_dialog)
        archive_dialog._set_run_state(state)
        assert seen == [False]

    def test_main_window_forwards_to_tray(self):
        from src.ui.main_window import MainWindow

        window = MagicMock()
        MainWindow.set_migration_running(window, True)
        window.tray_icon.set_migration_running.assert_called_once_with(True)

    def test_main_window_without_tray_is_harmless(self):
        """트레이 설정에 실패한 환경에서도 실행 자체는 막히면 안 된다."""
        from src.ui.main_window import MainWindow

        window = MagicMock()
        window.tray_icon = None
        MainWindow.set_migration_running(window, True)


class TestTableTypeGuard:
    def test_last_checked_type_cannot_be_unchecked(self, wizard):
        checked = [cb for cb in wizard.table_type_checkboxes.values() if cb.isChecked()]
        assert len(checked) == 1
        assert not checked[0].isEnabled(), "마지막 항목은 해제할 수 없어야 합니다"

    def test_second_selection_unlocks_the_first(self, wizard):
        boxes = list(wizard.table_type_checkboxes.values())
        others = [cb for cb in boxes if not cb.isChecked()]
        if not others:
            pytest.skip("테이블 타입이 하나뿐입니다")

        others[0].setChecked(True)
        assert all(cb.isEnabled() for cb in boxes)
