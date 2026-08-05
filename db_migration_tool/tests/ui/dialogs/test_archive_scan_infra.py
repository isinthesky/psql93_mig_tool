"""조회(scan) 작업 골격과 안전 게이트 테스트

이 파일의 핵심은 **D3 불변식** 하나다:

    대상 확인이 현재 목록에 대해 끝나지 않았으면
    '다음'은 꺼져 있고 go_next()는 _frozen_selection을 굳히지 않는다.

이게 깨지면 확인 전 상태(= 전 파티션이 체크된 것처럼 보이는 상태)가 실행 대상으로
굳고, file→postgres import가 truncate_mode="auto"로 **이미 완료된 파티션까지
TRUNCATE 후 재적재**한다.

동기 → 비동기 전환 내내 이 파일이 초록이어야 한다.
"""

from unittest.mock import MagicMock, patch

import pytest

import src.ui.dialogs.file_archive_migration_dialog as archive_mod
import src.ui.dialogs.scan_host as scan_host
from src.core.table_types import TableType


def _make_dialog(qapp, *, migration_mode="file_to_postgres"):
    profile = MagicMock()
    profile.id = 1
    profile.name = "아카이브 프로필"
    profile.migration_mode = migration_mode
    profile.source_kind = "file" if migration_mode == "file_to_postgres" else "postgres"
    profile.target_kind = "postgres" if migration_mode == "file_to_postgres" else "file"
    profile.source_config = {"archive_path": "D:/archive"}
    profile.target_config = {"archive_path": "D:/archive"}

    with (
        patch.object(archive_mod, "HistoryManager") as history_manager,
        patch.object(archive_mod, "CheckpointManager"),
        patch.object(
            archive_mod.FileArchiveMigrationDialog, "check_connections", lambda self: None
        ),
    ):
        history_manager.return_value.get_incomplete_history.return_value = None
        return archive_mod.FileArchiveMigrationDialog(None, profile)


@pytest.fixture
def dialog(qapp):
    dlg = _make_dialog(qapp)
    yield dlg
    dlg.deleteLater()


def _fill_partitions(dlg, count=3):
    dlg.discovered_partitions = [
        archive_mod.PartitionSummary(f"tbl_{i}", (i + 1) * 100, TableType.POINT_HISTORY)
        for i in range(count)
    ]
    dlg._render_partition_list()
    dlg._update_counts()


class TestSelectionGate:
    """D3 불변식 — 이 클래스가 이 작업 전체의 안전망이다."""

    def test_next_is_locked_before_target_check(self, dialog):
        dialog.pages.setCurrentIndex(1)
        _fill_partitions(dialog)
        dialog._update_nav_state()

        assert dialog.get_selected_partition_names(), "확인 전에는 전 항목이 체크된 상태다"
        assert not dialog._selection_verified
        assert not dialog.next_btn.isEnabled(), "대상 확인 전에는 '다음'이 잠겨야 합니다"

    def test_next_unlocks_after_target_check(self, dialog):
        dialog.pages.setCurrentIndex(1)
        _fill_partitions(dialog)

        dialog._selection_verified = True
        dialog._update_nav_state()

        assert dialog.next_btn.isEnabled()

    def test_go_next_refuses_to_freeze_unverified_selection(self, dialog):
        """버튼만 막으면 안 된다. 큐에 남은 클릭이 배달될 수 있다."""
        dialog.pages.setCurrentIndex(1)
        _fill_partitions(dialog)

        with patch.object(archive_mod.QMessageBox, "warning") as warned:
            dialog.go_next()

        assert dialog._frozen_selection == [], "확인 전 선택이 실행 대상으로 굳었습니다"
        assert dialog.pages.currentIndex() == 1, "확인 전에는 실행 페이지로 넘어가면 안 됩니다"
        assert warned.called, "왜 못 넘어가는지 알려줘야 합니다"

    def test_go_next_freezes_after_verification(self, dialog):
        dialog.pages.setCurrentIndex(1)
        _fill_partitions(dialog)
        dialog._selection_verified = True

        dialog.go_next()

        assert dialog._frozen_selection, "확인 후에는 정상적으로 굳어야 합니다"
        assert dialog.pages.currentIndex() == 2

    def test_resume_mode_bypasses_the_gate(self, dialog):
        """재개 대상은 미완료 체크포인트라 목록 확인과 무관하다."""
        dialog.resume_mode = True
        dialog._frozen_selection = ["tbl_a"]
        dialog.pages.setCurrentIndex(1)
        dialog._update_nav_state()

        assert dialog._can_freeze_selection()
        assert dialog.next_btn.isEnabled()

    def test_gate_explains_itself(self, dialog):
        dialog.pages.setCurrentIndex(1)
        _fill_partitions(dialog)
        dialog._update_nav_state()

        assert "확인" in dialog.next_btn.toolTip()


class TestVerificationSurvivesFailure:
    """확인 실패가 게이트를 확실히 닫는가.

    '성공 → 재확인 실패' 순서에서 게이트가 열린 채 남으면, 확인되지 않은
    목록(= 전 파티션이 체크된 상태)이 그대로 실행 대상이 되어 이미 완료된
    파티션까지 TRUNCATE 후 재적재된다.
    """

    def _prepare(self, dlg):
        dlg.pages.setCurrentIndex(1)
        _fill_partitions(dlg)

    def test_failure_after_success_closes_the_gate(self, dialog):
        self._prepare(dialog)
        gen = dialog._scan_gen

        dialog._on_target_check_result(gen, {"tbl_0": True, "tbl_1": False, "tbl_2": False})
        assert dialog._selection_verified

        # 수동 재확인이 시작되면 게이트가 내려가고, 실패하면 그대로 닫혀 있어야 한다.
        dialog.check_target_completed()
        dialog._on_target_check_failed(dialog._scan_gen, "boom")

        assert not dialog._selection_verified, "재확인이 실패하면 게이트가 닫혀야 합니다"
        assert not dialog.next_btn.isEnabled()

    def test_starting_a_check_immediately_closes_the_gate(self, dialog):
        """확인이 끝나기 전에는 이전 결과를 믿으면 안 된다."""
        self._prepare(dialog)
        dialog._on_target_check_result(dialog._scan_gen, {"tbl_0": True})
        assert dialog._selection_verified

        dialog.check_target_completed()

        assert not dialog._selection_verified

    def test_failure_does_not_keep_stale_completion_flags(self, dialog):
        """실패 후 남은 완료 플래그를 믿으면 안 된다."""
        self._prepare(dialog)

        dialog._on_target_check_failed(dialog._scan_gen, "boom")

        assert dialog._target_has_data == {}

    def test_failure_is_visible_to_the_user(self, dialog):
        self._prepare(dialog)

        dialog._on_target_check_failed(dialog._scan_gen, "boom")

        assert "실패" in dialog.discover_status.text(), "실패가 화면에 드러나야 합니다"


class TestGenerationInvalidation:
    def test_new_scan_invalidates_verification(self, dialog):
        dialog._selection_verified = True
        before = dialog._scan_gen

        dialog._bump_generation()

        assert dialog._scan_gen == before + 1
        assert not dialog._selection_verified

    def test_changing_dates_invalidates_verification(self, dialog):
        dialog._selection_verified = True

        dialog.start_date_edit.setDate(dialog.start_date_edit.date().addDays(-3))

        assert not dialog._selection_verified, "날짜를 바꾸면 이전 확인은 무효입니다"

    def test_changing_table_types_invalidates_verification(self, dialog):
        boxes = list(dialog.table_type_checkboxes.values())
        unchecked = [cb for cb in boxes if not cb.isChecked()]
        if not unchecked:
            pytest.skip("테이블 타입이 하나뿐입니다")

        dialog._selection_verified = True
        unchecked[0].setChecked(True)

        assert not dialog._selection_verified

    def test_name_filter_does_not_invalidate(self, dialog):
        """이름 필터는 보기만 거른다. 여기서 무효화하면 필터마다 재탐색을 강요한다."""
        _fill_partitions(dialog)
        dialog._selection_verified = True

        dialog.partition_filter.setText("tbl_1")

        assert dialog._selection_verified

    def test_stale_generation_is_detectable(self, dialog):
        stale = dialog._scan_gen
        dialog._bump_generation()

        assert not dialog._is_current_generation(stale)
        assert dialog._is_current_generation(dialog._scan_gen)


class TestScanLifecycle:
    def test_no_active_scan_initially(self, dialog):
        assert not dialog._has_active_scan()

    def test_active_scan_is_detected(self, dialog):
        worker = MagicMock()
        worker.isRunning.return_value = True
        dialog._scan_workers["discover"] = worker

        assert dialog._has_active_scan()

    def test_scan_does_not_count_as_migration_running(self, dialog):
        """조회는 닫기를 막지 않는다. 막으면 수 분간 닫을 수 없는 창이 된다."""
        worker = MagicMock()
        worker.isRunning.return_value = True
        dialog._scan_workers["discover"] = worker

        assert not dialog._is_running()
        assert not dialog._block_close_while_running()

    def test_inflight_guard_covers_the_gap_after_run_returns(self, dialog):
        """isRunning()만 보면 run()이 끝나고 슬롯이 아직 안 돈 구간을 놓친다."""
        worker = MagicMock()
        worker.isRunning.return_value = False
        dialog._scan_workers["discover"] = worker

        dialog._mark_scan_started("discover")
        assert dialog._is_scan_inflight("discover")

        dialog._mark_scan_finished("discover")
        assert not dialog._is_scan_inflight("discover")

    def test_shutdown_requests_interruption_and_waits(self, dialog):
        worker = MagicMock()
        worker.isRunning.return_value = True
        worker.wait.return_value = True
        dialog._scan_workers["discover"] = worker

        assert dialog._shutdown_scans(1000) is True
        worker.requestInterruption.assert_called_once()
        worker.wait.assert_called_once_with(1000)

    def test_shutdown_cancels_the_running_query(self, dialog):
        worker = MagicMock()
        worker.isRunning.return_value = True
        worker.wait.return_value = True
        dialog._scan_workers["target"] = worker

        dialog._shutdown_scans(1000)

        worker.cancel_query.assert_called_once()

    def test_shutdown_reports_failure_when_worker_will_not_stop(self, dialog):
        worker = MagicMock()
        worker.isRunning.return_value = True
        worker.wait.return_value = False
        dialog._scan_workers["discover"] = worker

        assert dialog._shutdown_scans(10) is False

    def test_shutdown_never_terminates(self, dialog):
        """terminate()는 psycopg 커넥션과 아카이브 파일 락을 남긴다.

        주석이 아니라 실제 호출을 본다.
        """
        import ast
        import inspect
        import textwrap

        source = textwrap.dedent(
            inspect.getsource(archive_mod.FileArchiveMigrationDialog._shutdown_scans)
        )
        called = {
            node.func.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "terminate" not in called

    def test_close_stops_scans_before_accepting(self, dialog):
        worker = MagicMock()
        worker.isRunning.return_value = True
        worker.wait.return_value = True
        dialog._scan_workers["discover"] = worker

        dialog.close()

        worker.requestInterruption.assert_called_once()

    def test_close_is_deferred_while_a_scan_refuses_to_stop(self, dialog):
        worker = MagicMock()
        worker.isRunning.return_value = True
        worker.wait.return_value = False
        dialog._scan_workers["discover"] = worker

        with patch.object(archive_mod.QTimer, "singleShot") as retry:
            dialog.close()

        assert retry.called, "멈추지 않으면 나중에 다시 시도해야 합니다"

    def test_close_eventually_gives_up_instead_of_hanging(self, dialog):
        """창을 인질로 잡지 않는다.

        무한 재시도하면 멈추지 않는 워커 하나 때문에 창을 영영 닫을 수 없다.
        """
        worker = MagicMock()
        worker.isRunning.return_value = True
        worker.wait.return_value = False
        dialog._scan_workers["discover"] = worker

        allowed = False
        for _ in range(archive_mod.FileArchiveMigrationDialog.SCAN_SHUTDOWN_MAX_ATTEMPTS):
            allowed = dialog._prepare_close()
            if allowed:
                break

        assert allowed, "상한을 넘으면 닫기를 허용해야 합니다"

    def test_abandoned_worker_is_detached_and_parked(self, dialog):
        """떼어낸 워커는 시그널을 끊고 참조를 유지해야 한다.

        실행 중인 QThread의 마지막 참조가 사라지면 프로세스가 죽는다.
        """
        worker = MagicMock()
        worker.isRunning.return_value = True
        dialog._scan_workers["discover"] = worker

        try:
            dialog._abandon_scans()

            assert worker.disconnect.called, "사라질 위젯을 건드리지 못하게 끊어야 합니다"
            assert worker in scan_host._ORPHANED_SCAN_WORKERS, "참조가 유지돼야 합니다"
            assert dialog._scan_workers == {}
        finally:
            scan_host._ORPHANED_SCAN_WORKERS.discard(worker)

    def test_stopped_worker_is_not_parked(self, dialog):
        worker = MagicMock()
        worker.isRunning.return_value = False
        dialog._scan_workers["discover"] = worker

        dialog._abandon_scans()

        assert worker not in scan_host._ORPHANED_SCAN_WORKERS

    def test_reject_also_stops_scans(self, dialog):
        """Esc는 closeEvent를 거치지 않을 수 있다."""
        worker = MagicMock()
        worker.isRunning.return_value = True
        worker.wait.return_value = True
        dialog._scan_workers["target"] = worker

        dialog.reject()

        worker.requestInterruption.assert_called_once()

    def test_migration_worker_still_blocks_close(self, dialog):
        """기존 정책 회귀 — 실행 중에는 여전히 닫을 수 없다."""
        dialog._set_run_state("running")

        with patch.object(archive_mod.QMessageBox, "warning"):
            assert dialog._block_close_while_running()
