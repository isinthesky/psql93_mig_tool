"""2단계 — 대상 완료 여부 확인 워커화 테스트

이 확인 결과가 틀리면 데이터가 상한다. `_target_has_data`가 비어 있으면
전 파티션이 미완료로 보이고, file→postgres import가 `truncate_mode="auto"`로
이미 옮긴 파티션까지 TRUNCATE 후 재적재한다.

따라서 "확인이 끝났는가"와 "확인이 성공했는가"를 구분하는 것이 핵심이다.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QThread

import src.core.scan_workers as scan_mod
import src.ui.dialogs.file_archive_migration_dialog as archive_mod
from src.core.archive_manifest import ScanCancelled
from src.core.scan_workers import TargetCompletedScanWorker
from src.core.table_types import TableType


@pytest.fixture
def dialog(qapp):
    profile = MagicMock()
    profile.id = 1
    profile.name = "프로필"
    profile.migration_mode = "file_to_postgres"
    profile.source_kind = "file"
    profile.target_kind = "postgres"
    profile.source_config = {"archive_path": "D:/archive"}
    profile.target_config = {"host": "h", "port": 5432, "database": "d", "username": "u"}

    with (
        patch.object(archive_mod, "HistoryManager") as history_manager,
        patch.object(archive_mod, "CheckpointManager"),
        patch.object(
            archive_mod.FileArchiveMigrationDialog, "check_connections", lambda self: None
        ),
    ):
        history_manager.return_value.get_incomplete_history.return_value = None
        dlg = archive_mod.FileArchiveMigrationDialog(None, profile)
    dlg.source_connected = True
    dlg.target_connected = True
    dlg.pages.setCurrentIndex(1)
    yield dlg
    dlg.deleteLater()


def _fill(dlg, count=3):
    dlg.discovered_partitions = [
        archive_mod.PartitionSummary(f"tbl_{i}", 100, TableType.POINT_HISTORY) for i in range(count)
    ]
    dlg._render_partition_list()
    dlg._update_counts()


class TestWorkerLogic:
    """run()/execute()를 직접 부른다. 스레드 없음."""

    def _worker(self, kind="postgres", names=None):
        return TargetCompletedScanWorker(
            3,
            kind,
            {"host": "h", "archive_path": "D:/a"},
            names or ["tbl_0", "tbl_1"],
        )

    def test_archive_path_uses_manifest_store(self):
        worker = self._worker(kind="file")
        store = MagicMock()
        store.get_completed_status.return_value = {"tbl_0": True, "tbl_1": False}

        with patch.object(scan_mod, "ArchiveManifestStore", return_value=store):
            assert worker.execute() == {"tbl_0": True, "tbl_1": False}

        # 취소·진행률 훅이 실제로 전달돼야 수 GB 체크섬을 멈출 수 있다
        kwargs = store.get_completed_status.call_args.kwargs
        assert callable(kwargs["should_stop"])
        assert callable(kwargs["on_progress"])

    def test_missing_manifest_means_nothing_completed(self):
        worker = self._worker(kind="file")
        store = MagicMock()
        store.get_completed_status.side_effect = FileNotFoundError()

        with patch.object(scan_mod, "ArchiveManifestStore", return_value=store):
            assert worker.execute() == {"tbl_0": False, "tbl_1": False}

    def test_postgres_query_avoids_count_star(self):
        """COUNT(*) 전수 스캔은 대용량 파티션에서 확인을 수 분으로 만든다."""
        import ast
        import inspect
        import textwrap

        source = textwrap.dedent(inspect.getsource(TargetCompletedScanWorker._check_postgres))
        literals = [
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        sql_text = " ".join(literals)
        assert "information_schema.tables" in sql_text
        assert "LIMIT 1" in sql_text
        assert "COUNT(*)" not in sql_text

    def test_postgres_sets_connect_timeout(self):
        """없으면 방화벽이 패킷을 버릴 때 취소 플래그조차 못 읽는다."""
        worker = self._worker()
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)

        with patch.object(scan_mod.psycopg, "connect", return_value=conn) as connect:
            worker.execute()

        assert connect.call_args.kwargs["connect_timeout"] == scan_mod.CONNECT_TIMEOUT_SECONDS

    def test_postgres_falls_back_to_defaults_for_missing_keys(self):
        """키가 빠지면 None이 넘어가 libpq 기본값으로 엉뚱한 DB에 붙을 수 있다.

        (프로필 정규화가 채워 주지만 워커는 임의 dict를 받는다)
        """
        worker = TargetCompletedScanWorker(1, "postgres", {}, ["tbl_0"])
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)

        with patch.object(scan_mod.psycopg, "connect", return_value=conn) as connect:
            worker.execute()

        params = connect.call_args.kwargs
        assert params["host"] == "localhost"
        assert params["port"] == 5432
        assert params["dbname"] == ""
        assert params["user"] == ""

    def test_postgres_uses_autocommit(self):
        """루프 전체가 한 트랜잭션이면 대상 DB에 idle-in-transaction이 남는다."""
        worker = self._worker()
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)

        with patch.object(scan_mod.psycopg, "connect", return_value=conn):
            worker.execute()

        assert conn.autocommit is True

    def test_postgres_closes_the_connection(self):
        worker = self._worker()
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)

        with patch.object(scan_mod.psycopg, "connect", return_value=conn):
            worker.execute()

        conn.close.assert_called_once()

    def test_postgres_closes_the_connection_on_error(self):
        worker = self._worker()
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("boom")

        with (
            patch.object(scan_mod.psycopg, "connect", return_value=conn),
            pytest.raises(RuntimeError),
        ):
            worker.execute()

        conn.close.assert_called_once()

    def test_cancellation_emits_nothing(self):
        """취소는 실패가 아니다. '오류'로 알리면 사용자가 잘못 판단한다."""
        worker = self._worker(kind="file")
        received = []
        worker.result.connect(lambda *a: received.append(("result", a)))
        worker.failed.connect(lambda *a: received.append(("failed", a)))

        store = MagicMock()
        store.get_completed_status.side_effect = ScanCancelled("취소")
        with patch.object(scan_mod, "ArchiveManifestStore", return_value=store):
            worker.run()

        assert received == []

    def test_real_failure_is_reported(self):
        worker = self._worker(kind="file")
        received = []
        worker.failed.connect(lambda gen, msg: received.append((gen, msg)))

        store = MagicMock()
        store.get_completed_status.side_effect = RuntimeError("망가짐")
        with patch.object(scan_mod, "ArchiveManifestStore", return_value=store):
            worker.run()

        assert received == [(3, "망가짐")]

    def test_worker_takes_only_plain_values(self):
        import inspect

        params = inspect.signature(TargetCompletedScanWorker.__init__).parameters
        assert set(params) == {
            "self",
            "generation",
            "target_kind",
            "target_config",
            "partition_names",
        }


class TestDialogWiring:
    """슬롯 직접 호출. 스레드 없음."""

    def test_result_opens_the_gate_and_excludes_completed(self, dialog):
        _fill(dialog)

        dialog._on_target_check_result(
            dialog._scan_gen, {"tbl_0": True, "tbl_1": False, "tbl_2": False}
        )

        assert dialog._selection_verified
        assert dialog.next_btn.isEnabled()
        assert "tbl_0" not in dialog.get_selected_partition_names()

    def test_failure_keeps_the_gate_closed(self, dialog):
        _fill(dialog)

        dialog._on_target_check_failed(dialog._scan_gen, "boom")

        assert not dialog._selection_verified
        assert not dialog.next_btn.isEnabled()
        assert dialog._target_has_data == {}

    def test_stale_result_does_not_open_the_gate(self, dialog):
        """확인 도중 조건이 바뀌면 그 결과는 지금 목록의 것이 아니다."""
        _fill(dialog)
        stale = dialog._scan_gen
        dialog._bump_generation()

        dialog._on_target_check_result(stale, {"tbl_0": True})

        assert not dialog._selection_verified, "낡은 결과로 게이트가 열렸습니다"
        assert dialog._target_has_data == {}

    def test_stale_failure_does_not_clobber_current_state(self, dialog):
        _fill(dialog)
        dialog._on_target_check_result(dialog._scan_gen, {"tbl_0": True})
        stale = dialog._scan_gen
        dialog._bump_generation()
        dialog._on_target_check_result(dialog._scan_gen, {"tbl_0": True, "tbl_1": True})

        dialog._on_target_check_failed(stale, "낡은 실패")

        assert dialog._selection_verified
        assert dialog._target_has_data == {"tbl_0": True, "tbl_1": True}

    def test_progress_is_shown(self, dialog):
        dialog._on_target_check_progress(dialog._scan_gen, 3, 47)
        assert "3/47" in dialog.discover_status.text()

    def test_stale_progress_is_ignored(self, dialog):
        stale = dialog._scan_gen
        dialog._bump_generation()
        dialog.discover_status.setText("현재 상태")

        dialog._on_target_check_progress(stale, 3, 47)

        assert dialog.discover_status.text() == "현재 상태"

    def test_finished_restores_buttons(self, dialog):
        _fill(dialog)
        dialog.discover_btn.setEnabled(False)
        dialog.check_completed_btn.setEnabled(False)
        dialog._mark_scan_started("target")

        dialog._on_target_check_finished()

        assert dialog.discover_btn.isEnabled()
        assert dialog.check_completed_btn.isEnabled()
        assert not dialog._is_scan_inflight("target")

    def test_check_button_stays_off_without_partitions(self, dialog):
        """찾은 파티션이 없으면 '완료여부 확인'은 할 일이 없다."""
        dialog.discovered_partitions = []
        dialog._mark_scan_started("target")

        dialog._on_target_check_finished()

        assert not dialog.check_completed_btn.isEnabled()

    def test_cancelled_check_does_not_leave_status_spinning(self, dialog):
        dialog.discover_status.setText("대상 확인 중... (2/47)")

        dialog._on_target_check_finished()

        assert "확인 중" not in dialog.discover_status.text()

    def test_second_request_is_ignored_while_inflight(self, dialog):
        _fill(dialog)
        dialog._mark_scan_started("target")

        with patch.object(archive_mod, "TargetCompletedScanWorker") as worker_cls:
            dialog.check_target_completed()

        assert not worker_cls.called

    def test_check_does_not_start_while_closing(self, dialog):
        _fill(dialog)
        dialog._closing = True

        with patch.object(archive_mod, "TargetCompletedScanWorker") as worker_cls:
            dialog.check_target_completed()

        assert not worker_cls.called

    def test_starting_a_check_closes_the_gate_immediately(self, dialog):
        _fill(dialog)
        dialog._on_target_check_result(dialog._scan_gen, {"tbl_0": True})
        assert dialog._selection_verified

        with patch.object(archive_mod, "TargetCompletedScanWorker"):
            dialog.check_target_completed()

        assert not dialog._selection_verified

    def test_no_lambda_in_worker_connections(self):
        import ast
        import inspect
        import textwrap

        source = textwrap.dedent(
            inspect.getsource(archive_mod.FileArchiveMigrationDialog.check_target_completed)
        )
        assert not [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Lambda)]


class TestStaleScanIsStopped:
    """조건이 바뀌면 돌던 조회는 결과가 버려진다. 계속 돌 이유가 없다.

    아카이브 체크섬 확인은 수 분씩 걸리므로, 날짜만 바꿔도 쓸모없어진 작업이
    계속 돌면 버튼이 그동안 잠긴 채로 남는다.
    """

    def test_bumping_generation_interrupts_running_scans(self, dialog):
        worker = MagicMock()
        worker.isRunning.return_value = True
        dialog._scan_workers["target"] = worker

        dialog._bump_generation()

        worker.requestInterruption.assert_called_once()
        worker.cancel_query.assert_called_once()

    def test_finished_scans_are_not_interrupted(self, dialog):
        worker = MagicMock()
        worker.isRunning.return_value = False
        dialog._scan_workers["target"] = worker

        dialog._bump_generation()

        worker.requestInterruption.assert_not_called()

    def test_changing_dates_interrupts_a_running_scan(self, dialog):
        worker = MagicMock()
        worker.isRunning.return_value = True
        dialog._scan_workers["target"] = worker

        dialog.start_date_edit.setDate(dialog.start_date_edit.date().addDays(-1))

        worker.requestInterruption.assert_called()


class TestDiscoveryChain:
    def test_discovery_hands_the_status_line_to_the_target_check(self, dialog):
        """탐색이 '완료: N개'로 덮으면 '확인 중'이 사라진다."""
        _fill(dialog)

        with patch.object(archive_mod, "TargetCompletedScanWorker"):
            dialog.check_target_completed()

        assert "확인 중" in dialog.discover_status.text()

    def test_discovery_does_not_open_the_gate_by_itself(self, dialog):
        """탐색만으로 넘어가면 확인 전 상태(전 파티션 체크)가 실행 대상이 된다."""
        with patch.object(archive_mod, "TargetCompletedScanWorker"):
            dialog._on_discovery_result(
                dialog._scan_gen,
                [{"table_name": "tbl_0", "row_count": 1, "table_type": TableType.POINT_HISTORY}],
            )

        assert dialog.discovered_partitions, "탐색 결과는 반영돼야 합니다"
        assert not dialog._selection_verified
        assert not dialog.next_btn.isEnabled()


class TestThreadingIntegration:
    def test_check_runs_off_the_gui_thread(self, dialog, qtbot):
        gui_thread = QThread.currentThread()
        seen = {}
        _fill(dialog)

        def fake_pg(worker_self):
            seen["thread"] = QThread.currentThread()
            return {"tbl_0": True, "tbl_1": False, "tbl_2": False}

        worker = None
        try:
            with patch.object(scan_mod.TargetCompletedScanWorker, "_check_postgres", fake_pg):
                dialog.check_target_completed()
                worker = dialog._scan_workers["target"]
                qtbot.waitUntil(lambda: dialog._selection_verified, timeout=5000)
                qtbot.waitUntil(lambda: not worker.isRunning(), timeout=5000)
        finally:
            if worker is not None:
                worker.wait(5000)

        assert seen["thread"] != gui_thread
        assert "tbl_0" not in dialog.get_selected_partition_names()

    def test_dialog_closes_while_a_target_check_runs(self, dialog, qtbot):
        gate = threading.Event()
        _fill(dialog)

        def blocking_pg(worker_self):
            gate.wait(10)
            return {}

        worker = None
        try:
            with patch.object(scan_mod.TargetCompletedScanWorker, "_check_postgres", blocking_pg):
                dialog.check_target_completed()
                worker = dialog._scan_workers["target"]
                qtbot.waitUntil(lambda: worker.isRunning(), timeout=5000)

                assert not dialog._block_close_while_running()
                gate.set()
                dialog.close()
                qtbot.waitUntil(lambda: not worker.isRunning(), timeout=5000)
        finally:
            gate.set()
            if worker is not None:
                worker.wait(5000)
