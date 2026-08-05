"""마법사를 공용 조회 골격(scan_workers + ScanHostMixin)으로 이관한 결과 검증.

이관 전 마법사에 있던 결함들이다:
- 탐색이 한 번 실패하면 '완료여부 확인'이 **영구 비활성**으로 남았다
  (`_on_discovery_error`가 복구하지 않고, finished 람다는 discover_btn만 살렸다)
- 조회 워커가 도는 중 창이 닫혔고 정리도 없었다. main_window가 exec() 뒤
  `deleteLater()`를 걸므로, 실행 중인 QThread의 참조가 사라져 프로세스가 죽는다
- 늦게 도착한 결과를 걸러내지 않아 조건을 바꿔도 이전 결과가 화면을 덮었다
- 시그널을 람다로 연결해 수신자 파괴 시 자동 해제되지 않았다
"""

import ast
import inspect
import textwrap
import threading
from unittest.mock import MagicMock, patch

import pytest

import src.core.scan_workers as scan_mod
import src.ui.dialogs.migration_wizard_dialog as wizard_mod
from src.core.table_types import TableType


@pytest.fixture
def wizard(qapp):
    profile = MagicMock()
    profile.id = 1
    profile.name = "프로필"
    profile.source_kind = "postgres"
    profile.target_kind = "postgres"
    profile.source_config = {"host": "h"}
    profile.target_config = {"host": "h"}

    with (
        patch.object(wizard_mod, "HistoryManager") as history_manager,
        patch.object(wizard_mod, "CheckpointManager"),
        patch.object(wizard_mod.MigrationWizardDialog, "check_connections", lambda self: None),
    ):
        history_manager.return_value.get_incomplete_history.return_value = None
        dlg = wizard_mod.MigrationWizardDialog(None, profile)
    dlg.source_connected = True
    dlg.target_connected = True
    yield dlg
    dlg.deleteLater()


def _rows(count=2):
    return [
        {"table_name": f"t{i}", "row_count": 10, "table_type": TableType.POINT_HISTORY}
        for i in range(count)
    ]


class TestButtonDeadlockIsGone:
    """탐색 1회 실패로 '완료여부 확인'이 영구 비활성이 되던 문제."""

    def test_failed_discovery_restores_the_discover_button(self, wizard):
        wizard._on_discovery_error(wizard._scan_gen, "boom")
        wizard._on_discovery_finished()

        assert wizard.discover_btn.isEnabled(), "다시 시도할 수 없으면 창을 닫을 수밖에 없습니다"

    def test_check_button_follows_whether_partitions_exist(self, wizard):
        wizard._on_discovery_error(wizard._scan_gen, "boom")
        wizard._on_discovery_finished()
        assert not wizard.check_completed_btn.isEnabled(), "찾은 게 없으면 확인할 것도 없다"

        wizard._on_discovery_result(wizard._scan_gen, _rows())
        wizard._on_discovery_finished()
        assert wizard.check_completed_btn.isEnabled()

    def test_buttons_stay_locked_while_target_check_runs(self, wizard):
        wizard.discovered_partitions = [
            wizard_mod.PartitionSummary("t0", 1, TableType.POINT_HISTORY)
        ]
        wizard._mark_scan_started("target")

        wizard._on_discovery_finished()

        assert not wizard.discover_btn.isEnabled()
        assert not wizard.check_completed_btn.isEnabled()


class TestStaleResultsAreDiscarded:
    def test_stale_discovery_result_is_ignored(self, wizard):
        stale = wizard._scan_gen
        wizard._bump_generation()

        wizard._on_discovery_result(stale, _rows())

        assert wizard.discovered_partitions == []

    def test_stale_discovery_failure_is_ignored(self, wizard):
        """낡은 실패가 **현재 세대의** 목록을 지우면 안 된다."""
        stale = wizard._scan_gen
        wizard._bump_generation()
        wizard._on_discovery_result(wizard._scan_gen, _rows())

        wizard._on_discovery_error(stale, "낡은 실패")

        assert wizard.discovered_partitions, "낡은 실패가 현재 목록을 지웠습니다"

    def test_stale_target_check_does_not_overwrite_flags(self, wizard):
        wizard._bump_generation()
        wizard._on_discovery_result(wizard._scan_gen, _rows())
        wizard._on_target_check_result(wizard._scan_gen, {"t0": True})
        stale = wizard._scan_gen
        wizard._bump_generation()
        wizard._on_discovery_result(wizard._scan_gen, _rows())
        wizard._on_target_check_result(wizard._scan_gen, {"t0": True})

        wizard._on_target_check_result(stale, {"t0": False, "t1": False})

        assert wizard._target_has_data == {"t0": True}

    def test_failed_discovery_clears_the_previous_list(self, wizard):
        """실패했는데 이전 목록이 남으면 그걸 실행 대상으로 착각한다."""
        wizard._on_discovery_result(wizard._scan_gen, _rows())
        assert wizard.discovered_partitions

        wizard._on_discovery_error(wizard._scan_gen, "boom")

        assert wizard.discovered_partitions == []


class TestConditionChangesInvalidateResults:
    """조건이 바뀌면 지금 목록은 그 조건의 결과가 아니다.

    마법사는 `discover_partitions()` 안에서만 세대를 올렸다. 그래서 날짜나
    항목을 바꿔도 옛 조건으로 시작한 탐색 결과가 현재 세대로 통과했고,
    그 목록을 실행하면 이력에는 위젯의 **새** 날짜가, 실제로는 **옛** 범위의
    파티션이 기록됐다.
    """

    def test_changing_the_date_bumps_the_generation(self, wizard):
        before = wizard._scan_gen

        wizard.start_date_edit.setDate(wizard.start_date_edit.date().addDays(-3))

        assert wizard._scan_gen > before

    def test_preset_buttons_bump_the_generation(self, wizard):
        before = wizard._scan_gen

        wizard._set_preset_days(30)

        assert wizard._scan_gen > before

    def test_changing_the_table_type_bumps_the_generation(self, wizard):
        before = wizard._scan_gen
        # 마지막 하나는 끌 수 없으므로 켜는 쪽으로 바꾼다.
        target = next(cb for cb in wizard.table_type_checkboxes.values() if not cb.isChecked())

        target.setChecked(True)

        assert wizard._scan_gen > before

    def test_stale_result_is_discarded_after_a_date_change(self, wizard):
        stale = wizard._scan_gen
        wizard.start_date_edit.setDate(wizard.start_date_edit.date().addDays(-3))

        wizard._on_discovery_result(stale, _rows())

        assert wizard.discovered_partitions == []

    def test_the_drawn_list_is_cleared_too(self, wizard):
        """늦은 결과를 버리는 것만으로는 부족하다.

        이미 그려진 목록이 남아 있으면 사용자는 그게 새 조건의 결과라고
        믿고 실행한다.
        """
        wizard._on_discovery_result(wizard._scan_gen, _rows())
        assert wizard.partition_list.count() == 2

        wizard.start_date_edit.setDate(wizard.start_date_edit.date().addDays(-3))

        assert wizard.discovered_partitions == []
        assert wizard.partition_list.count() == 0
        assert wizard._target_has_data == {}

    def test_resume_mode_invalidates_a_running_discovery(self, wizard):
        """재개 모드는 날짜 위젯을 이전 이력의 값으로 덮어쓴다.

        그 전에 시작된 탐색은 다른 범위를 본 것이므로 살아남으면 안 된다.
        """
        history = MagicMock()
        history.id = 7
        history.start_date = "2020-01-01"
        history.end_date = "2020-01-03"
        wizard._incomplete_history = history
        wizard.checkpoint_manager.get_pending_checkpoints.return_value = []
        stale = wizard._scan_gen

        wizard._on_resume_clicked()
        wizard._on_discovery_result(stale, _rows())

        assert wizard.discovered_partitions == []

    def test_new_run_does_not_revive_buttons_while_a_scan_runs(self, wizard):
        """눌러도 반응 없는 죽은 버튼을 만들지 않는다."""
        wizard._mark_scan_started("discover")
        wizard.resume_mode = True
        wizard._lock_options_for_resume()

        wizard._on_new_run_clicked()

        assert not wizard.discover_btn.isEnabled()
        assert not wizard.check_completed_btn.isEnabled()

    def test_new_run_restores_buttons_when_idle(self, wizard):
        wizard._on_discovery_result(wizard._scan_gen, _rows())
        wizard.resume_mode = True
        wizard._lock_options_for_resume()

        wizard._on_new_run_clicked()

        assert wizard.discover_btn.isEnabled()
        assert wizard.check_completed_btn.isEnabled()


class TestWorkerLifetime:
    def test_scan_does_not_block_closing(self, wizard):
        worker = MagicMock()
        worker.isRunning.return_value = True
        wizard._scan_workers["discover"] = worker

        assert not wizard._block_close_while_running()

    def test_close_stops_the_scan_worker(self, wizard):
        """main_window가 exec() 뒤 deleteLater를 건다.

        정리하지 않으면 실행 중인 QThread의 참조가 사라져 프로세스가 죽는다.
        """
        worker = MagicMock()
        worker.isRunning.return_value = True
        worker.wait.return_value = True
        wizard._scan_workers["discover"] = worker

        wizard.close()

        worker.requestInterruption.assert_called_once()

    def test_reject_also_stops_the_scan_worker(self, wizard):
        worker = MagicMock()
        worker.isRunning.return_value = True
        worker.wait.return_value = True
        wizard._scan_workers["target"] = worker

        wizard.reject()

        worker.requestInterruption.assert_called_once()

    def test_connection_checker_is_cleaned_up_too(self, wizard):
        """연결 확인 워커도 정리 대상이다.

        생성자에서 바로 시작되므로 '창을 열자마자 Esc'가 현실적인 경로다.
        등록하지 않으면 실행 중인 QThread의 참조가 다이얼로그와 함께 사라진다.
        """
        checker = MagicMock()
        checker.isRunning.return_value = True
        checker.wait.return_value = True
        wizard._scan_workers["conn"] = checker

        wizard.close()

        checker.requestInterruption.assert_called_once()
        # BaseMigrationWorker 계열은 isInterruptionRequested()를 보지 않는다.
        checker.stop.assert_called_once()

    def test_scheduled_connection_check_does_not_start_after_close(self, wizard):
        wizard._closing = True

        with patch.object(wizard_mod, "CopyMigrationWorker") as worker_cls:
            wizard.check_connections()

        assert not worker_cls.called

    def test_migration_worker_still_blocks_close(self, wizard):
        """기존 정책 회귀 — 실행 중에는 여전히 닫을 수 없다."""
        wizard._set_run_state("running")

        with patch.object(wizard_mod.QMessageBox, "warning"):
            assert wizard._block_close_while_running()

    def test_real_worker_is_stopped_by_close(self, wizard, qtbot):
        gate = threading.Event()

        class Slow(scan_mod.PartitionScanWorker):
            def execute(self):
                gate.wait(10)
                return []

        worker = None
        try:
            with patch.object(wizard_mod, "PartitionScanWorker", Slow):
                wizard.discover_partitions()
                worker = wizard._scan_workers["discover"]
                qtbot.waitUntil(lambda: worker.isRunning(), timeout=5000)

                gate.set()
                wizard.close()
                qtbot.waitUntil(lambda: not worker.isRunning(), timeout=5000)
        finally:
            gate.set()
            if worker is not None:
                worker.wait(5000)

        assert wizard._closing


class TestNoLambdaConnections:
    """람다는 수신자 QObject가 없어 다이얼로그 파괴 시 자동 해제되지 않는다."""

    @pytest.mark.parametrize(
        "method",
        [
            wizard_mod.MigrationWizardDialog.discover_partitions,
            wizard_mod.MigrationWizardDialog.check_target_completed,
            wizard_mod.MigrationWizardDialog.run_rowcount_verification,
        ],
    )
    def test_worker_connections_use_bound_methods(self, method):
        source = textwrap.dedent(inspect.getsource(method))
        lambdas = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Lambda)]
        assert not lambdas, "워커 시그널 연결에 람다를 쓰지 마세요"


class TestSharedWorkersAreUsed:
    def test_wizard_no_longer_defines_its_own_scan_workers(self):
        """다이얼로그 안에 워커를 두면 재사용이 막히고 결함이 갈라진다."""
        for name in (
            "PartitionDiscoveryWorker",
            "TargetCompletedCheckWorker",
            "RowCountVerificationWorker",
        ):
            assert not hasattr(wizard_mod, name), f"{name}이 아직 다이얼로그에 남아 있습니다"

    def test_wizard_uses_the_shared_mixin(self):
        from src.ui.dialogs.scan_host import ScanHostMixin

        assert issubclass(wizard_mod.MigrationWizardDialog, ScanHostMixin)

    def test_verify_worker_counts_exactly(self):
        """검증은 '옮긴 게 맞는가'를 답한다. 추정치를 쓰면 의미가 없다."""
        source = textwrap.dedent(inspect.getsource(scan_mod.RowCountVerifyWorker.execute))
        assert "COUNT(*)" in source
        assert "reltuples" not in source


class TestVerifyWorkerCancellation:
    """검증은 소스와 대상에서 번갈아 COUNT(*)를 돈다.

    한쪽만 취소 대상으로 잡으면 검증 시간의 절반이 취소 불가 구간이 된다.
    그 구간에 창을 닫으면 정리에 실패해 워커 분리(abandon) 경로로 빠진다.
    """

    def _worker(self):
        return scan_mod.RowCountVerifyWorker(
            generation=0,
            source_config={"host": "s"},
            target_config={"host": "t"},
            table_names=["t0"],
        )

    def test_both_connections_are_cancelled(self):
        worker = self._worker()
        source, target = MagicMock(), MagicMock()

        with patch.object(scan_mod.psycopg, "connect", side_effect=[source, target]):
            worker.execute()

        # execute()가 끝나면 추적을 놓는다. 추적 시점의 동작을 보려면
        # 실행 중 상태를 흉내내야 한다.
        worker._track_connection(source)
        worker._track_connection(target)
        worker.cancel_query()

        source.cancel.assert_called_once()
        target.cancel.assert_called_once()

    def test_target_is_tracked_during_execution(self):
        worker = self._worker()
        source, target = MagicMock(), MagicMock()
        tracked: list[list] = []

        def spy(*_args, **_kwargs):
            tracked.append(list(worker._conns))
            return MagicMock(__enter__=MagicMock(), __exit__=MagicMock())

        source.cursor.side_effect = spy

        with patch.object(scan_mod.psycopg, "connect", side_effect=[source, target]):
            worker.execute()

        assert tracked, "cursor()가 불리지 않았습니다"
        assert target in tracked[0], "대상 커넥션이 취소 대상에서 빠졌습니다"

    def test_tracking_is_released_when_done(self):
        worker = self._worker()
        source, target = MagicMock(), MagicMock()

        with patch.object(scan_mod.psycopg, "connect", side_effect=[source, target]):
            worker.run()

        worker.cancel_query()
        source.cancel.assert_not_called()
        target.cancel.assert_not_called()

    def test_a_connection_is_tracked_only_once(self):
        worker = self._worker()
        conn = MagicMock()

        worker._track_connection(conn)
        worker._track_connection(conn)
        worker.cancel_query()

        conn.cancel.assert_called_once()
