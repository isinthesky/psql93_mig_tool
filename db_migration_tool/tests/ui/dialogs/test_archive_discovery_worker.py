"""3단계 — 파티션 탐색 워커화 + 체인 테스트

탐색은 이 다이얼로그에서 상한이 없는 유일한 작업이다(파티션마다 COUNT(*) 전수 스캔).
그래서 취소 훅이 여기서 가장 중요하다.

체인의 핵심: 탐색 결과가 대상 확인으로 이어질 때 **같은 generation을 물려준다.**
그래야 낡은 탐색 결과가 새로운 대상 확인을 트리거하지 못한다.
"""

import threading
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QThread

import src.core.scan_workers as scan_mod
import src.ui.dialogs.file_archive_migration_dialog as archive_mod
from src.core.archive_manifest import ScanCancelled
from src.core.scan_workers import PartitionScanWorker
from src.core.table_types import TableType


@pytest.fixture
def dialog(qapp):
    profile = MagicMock()
    profile.id = 1
    profile.name = "프로필"
    profile.migration_mode = "postgres_to_file"
    profile.source_kind = "postgres"
    profile.target_kind = "file"
    profile.source_config = {"host": "h", "port": 5432}
    profile.target_config = {"archive_path": "D:/archive"}

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


def _rows(count=3):
    return [
        {
            "table_name": f"tbl_{i}",
            "row_count": (i + 1) * 100,
            "table_type": TableType.POINT_HISTORY,
        }
        for i in range(count)
    ]


class TestWorkerLogic:
    def _worker(self, kind="postgres"):
        return PartitionScanWorker(
            5,
            kind,
            {"host": "h", "archive_path": "D:/a"},
            date(2026, 8, 1),
            date(2026, 8, 5),
            [TableType.POINT_HISTORY],
        )

    def test_postgres_source_passes_cancellation_hooks(self):
        """훅이 안 넘어가면 COUNT(*) 루프를 멈출 방법이 없다."""
        worker = self._worker()
        discovery = MagicMock()
        discovery.discover_partitions.return_value = _rows()

        with patch.object(scan_mod, "PartitionDiscovery", return_value=discovery):
            assert worker.execute() == _rows()

        kwargs = discovery.discover_partitions.call_args.kwargs
        assert callable(kwargs["should_stop"])
        assert callable(kwargs["on_progress"])
        # COUNT(*) 전수 스캔은 플래그로 못 멈춘다. 커넥션을 받아야 cancel()이 걸린다.
        assert kwargs["on_connection"] == worker._track_connection

    def test_cancel_query_can_actually_kill_a_running_scan(self):
        """1단계의 ConnectionCheckWorker와 달리 여기서는 실제로 끊을 수 있어야 한다."""
        worker = self._worker()
        conn = MagicMock()
        discovery = MagicMock()

        def capture(*_args, **kwargs):
            kwargs["on_connection"](conn)
            return []

        discovery.discover_partitions.side_effect = capture
        with patch.object(scan_mod, "PartitionDiscovery", return_value=discovery):
            worker.execute()

        worker.cancel_query()
        conn.cancel.assert_called_once()

    def test_archive_source_uses_manifest_store(self):
        worker = self._worker(kind="file")
        store = MagicMock()
        store.filter_partitions.return_value = _rows(2)

        with patch.object(scan_mod, "ArchiveManifestStore", return_value=store):
            assert len(worker.execute()) == 2

    def test_cancellation_emits_nothing(self):
        worker = self._worker()
        received = []
        worker.result.connect(lambda *a: received.append(a))
        worker.failed.connect(lambda *a: received.append(a))

        discovery = MagicMock()
        discovery.discover_partitions.side_effect = ScanCancelled("취소")
        with patch.object(scan_mod, "PartitionDiscovery", return_value=discovery):
            worker.run()

        assert received == []

    def test_failure_is_reported(self):
        worker = self._worker()
        received = []
        worker.failed.connect(lambda gen, msg: received.append((gen, msg)))

        discovery = MagicMock()
        discovery.discover_partitions.side_effect = RuntimeError("망가짐")
        with patch.object(scan_mod, "PartitionDiscovery", return_value=discovery):
            worker.run()

        assert received == [(5, "망가짐")]

    def test_worker_takes_only_plain_values(self):
        import inspect

        params = inspect.signature(PartitionScanWorker.__init__).parameters
        assert set(params) == {
            "self",
            "generation",
            "source_kind",
            "source_config",
            "start_date",
            "end_date",
            "table_types",
        }


class TestDiscoveryCancellationInCore:
    """`PartitionDiscovery`가 취소를 실제로 존중하는가."""

    def test_should_stop_raises_scan_cancelled(self):
        from src.core.partition_discovery import PartitionDiscovery

        discovery = PartitionDiscovery({"host": "h"})
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [("tbl_0", "point_history", 0, 10**13, True)]

        with (
            patch.object(discovery, "_create_connection", return_value=conn),
            pytest.raises(ScanCancelled),
        ):
            discovery.discover_partitions(
                date(2026, 8, 1),
                date(2026, 8, 5),
                [TableType.POINT_HISTORY],
                should_stop=lambda: True,
            )

    def test_connect_timeout_is_set(self):
        """없으면 방화벽이 패킷을 버릴 때 취소 플래그조차 못 읽는다."""
        from src.core import partition_discovery as pd

        discovery = pd.PartitionDiscovery({"host": "h"})
        with patch.object(pd.psycopg, "connect") as connect:
            discovery._create_connection()

        assert connect.call_args.kwargs["connect_timeout"] == pd.CONNECT_TIMEOUT_SECONDS

    def test_progress_never_goes_backwards(self):
        """주 조회와 fallback이 각각 1부터 세면 화면 숫자가 되돌아간다.

        사용자는 작업이 멈췄거나 다시 시작한 줄 안다.
        """
        from src.core.partition_discovery import PartitionDiscovery

        discovery = PartitionDiscovery({"host": "h"})
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        calls = {"n": 0}

        def fetchall():
            calls["n"] += 1
            if calls["n"] == 1:
                return [
                    ("tbl_a", "point_history", 0, 10**13, True),
                    ("tbl_b", "point_history", 0, 10**13, True),
                ]
            return [("phys_1",), ("phys_2",), ("phys_3",)]

        cur.fetchall.side_effect = fetchall
        cur.fetchone.return_value = (0,)

        seen: list[tuple[int, int]] = []
        with (
            patch.object(discovery, "_create_connection", return_value=conn),
            patch.object(discovery, "_check_table_exists", return_value=False),
        ):
            discovery.discover_partitions(
                date(2026, 8, 1),
                date(2026, 8, 5),
                [TableType.POINT_HISTORY],
                on_progress=lambda done, total: seen.append((done, total)),
            )

        done_values = [done for done, _ in seen]
        assert done_values == sorted(done_values), f"진행률이 되돌아갔습니다: {seen}"
        assert len(set(done_values)) == len(done_values), "같은 값을 두 번 보냈습니다"

    def test_hooks_are_optional(self):
        """기존 호출부(워커 밖)는 훅 없이 그대로 동작해야 한다."""
        import inspect

        from src.core.partition_discovery import PartitionDiscovery

        params = inspect.signature(PartitionDiscovery.discover_partitions).parameters
        assert params["should_stop"].default is None
        assert params["on_progress"].default is None


class TestDialogWiring:
    def test_result_populates_the_list(self, dialog):
        with patch.object(archive_mod, "TargetCompletedScanWorker"):
            dialog._on_discovery_result(dialog._scan_gen, _rows())

        assert len(dialog.discovered_partitions) == 3
        assert dialog.partition_list.count() == 3

    def test_result_chains_into_the_target_check(self, dialog):
        with patch.object(archive_mod, "TargetCompletedScanWorker") as worker_cls:
            dialog._on_discovery_result(dialog._scan_gen, _rows())

        assert worker_cls.called, "탐색 후 대상 확인이 이어져야 합니다"

    def test_chain_passes_the_same_generation(self, dialog):
        """다른 세대를 쓰면 낡은 탐색이 새 확인을 트리거한다."""
        gen = dialog._scan_gen
        with patch.object(archive_mod, "TargetCompletedScanWorker") as worker_cls:
            dialog._on_discovery_result(gen, _rows())

        assert worker_cls.call_args.args[0] == gen

    def test_empty_result_does_not_chain(self, dialog):
        with patch.object(archive_mod, "TargetCompletedScanWorker") as worker_cls:
            dialog._on_discovery_result(dialog._scan_gen, [])

        assert not worker_cls.called
        assert "없습니다" in dialog.discover_status.text()

    def test_stale_result_is_discarded(self, dialog):
        stale = dialog._scan_gen
        dialog._bump_generation()

        with patch.object(archive_mod, "TargetCompletedScanWorker") as worker_cls:
            dialog._on_discovery_result(stale, _rows())

        assert dialog.discovered_partitions == []
        assert not worker_cls.called, "낡은 탐색이 대상 확인을 트리거했습니다"

    def test_failure_clears_the_previous_list(self, dialog):
        """실패했는데 이전 목록이 남으면 그걸 실행 대상으로 착각한다."""
        with patch.object(archive_mod, "TargetCompletedScanWorker"):
            dialog._on_discovery_result(dialog._scan_gen, _rows())
        assert dialog.discovered_partitions

        dialog._on_discovery_failed(dialog._scan_gen, "boom")

        assert dialog.discovered_partitions == []
        assert dialog._target_has_data == {}
        assert "실패" in dialog.discover_status.text()

    def test_starting_a_scan_clears_the_old_list_immediately(self, dialog):
        """결과를 기다리는 동안 이전 목록이 체크된 채 남아 있으면 안 된다."""
        with patch.object(archive_mod, "TargetCompletedScanWorker"):
            dialog._on_discovery_result(dialog._scan_gen, _rows())
        assert dialog.discovered_partitions

        with patch.object(archive_mod, "PartitionScanWorker"):
            dialog.discover_partitions()

        assert dialog.discovered_partitions == []
        assert dialog.partition_list.count() == 0

    def test_progress_is_shown(self, dialog):
        dialog._on_discovery_progress(dialog._scan_gen, 4, 12)
        assert "4/12" in dialog.discover_status.text()

    def test_stale_progress_is_ignored(self, dialog):
        stale = dialog._scan_gen
        dialog._bump_generation()
        dialog.discover_status.setText("현재")

        dialog._on_discovery_progress(stale, 4, 12)

        assert dialog.discover_status.text() == "현재"

    def test_second_request_is_ignored_while_inflight(self, dialog):
        dialog._mark_scan_started("discover")

        with patch.object(archive_mod, "PartitionScanWorker") as worker_cls:
            dialog.discover_partitions()

        assert not worker_cls.called

    def test_scan_does_not_start_while_closing(self, dialog):
        dialog._closing = True

        with patch.object(archive_mod, "PartitionScanWorker") as worker_cls:
            dialog.discover_partitions()

        assert not worker_cls.called

    def test_requires_both_endpoints(self, dialog):
        dialog.source_connected = False

        with (
            patch.object(archive_mod.QMessageBox, "warning"),
            patch.object(archive_mod, "PartitionScanWorker") as worker_cls,
        ):
            dialog.discover_partitions()

        assert not worker_cls.called

    def test_rejects_inverted_date_range(self, dialog):
        dialog.start_date_edit.setDate(dialog.end_date_edit.date().addDays(3))

        with (
            patch.object(archive_mod.QMessageBox, "warning") as warned,
            patch.object(archive_mod, "PartitionScanWorker") as worker_cls,
        ):
            dialog.discover_partitions()

        assert warned.called
        assert not worker_cls.called

    def test_cancelled_scan_does_not_leave_status_spinning(self, dialog):
        dialog.discover_status.setText("파티션 탐색 중... (2/9)")

        dialog._on_discovery_finished()

        assert "탐색 중" not in dialog.discover_status.text()

    def test_no_lambda_in_worker_connections(self):
        import ast
        import inspect
        import textwrap

        source = textwrap.dedent(
            inspect.getsource(archive_mod.FileArchiveMigrationDialog.discover_partitions)
        )
        assert not [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Lambda)]


class TestScanButtonState:
    """버튼 활성화를 한 곳에서 정하지 않으면 순서에 따라 어긋난다.

    실제로 탐색 완료 핸들러가 (그 뒤에 시작된) 대상 확인 도중에
    버튼을 되살리는 문제가 있었다.
    """

    def test_buttons_stay_locked_while_the_target_check_runs(self, dialog):
        dialog.discovered_partitions = [
            archive_mod.PartitionSummary("tbl_0", 1, TableType.POINT_HISTORY)
        ]
        dialog._mark_scan_started("target")

        dialog._on_discovery_finished()

        assert not dialog.discover_btn.isEnabled(), "대상 확인 중에 탐색이 열렸습니다"
        assert not dialog.check_completed_btn.isEnabled()

    def test_check_button_starts_disabled(self, dialog):
        """생성 직후에는 찾은 파티션이 없다. 규칙은 처음부터 적용돼야 한다."""
        assert not dialog.discovered_partitions
        assert not dialog.check_completed_btn.isEnabled()

    def test_buttons_return_when_everything_is_done(self, dialog):
        dialog.discovered_partitions = [
            archive_mod.PartitionSummary("tbl_0", 1, TableType.POINT_HISTORY)
        ]

        dialog._sync_scan_buttons()

        assert dialog.discover_btn.isEnabled()
        assert dialog.check_completed_btn.isEnabled()


class TestThreadingIntegration:
    def test_discovery_runs_off_the_gui_thread(self, dialog, qtbot):
        gui_thread = QThread.currentThread()
        seen = {}

        def fake_discover(worker_self):
            seen["thread"] = QThread.currentThread()
            return _rows(2)

        worker = None
        try:
            with (
                patch.object(scan_mod.PartitionScanWorker, "_discover_postgres", fake_discover),
                patch.object(archive_mod, "TargetCompletedScanWorker"),
            ):
                dialog.discover_partitions()
                worker = dialog._scan_workers["discover"]
                qtbot.waitUntil(lambda: bool(dialog.discovered_partitions), timeout=5000)
                qtbot.waitUntil(lambda: not worker.isRunning(), timeout=5000)
        finally:
            if worker is not None:
                worker.wait(5000)

        assert seen["thread"] != gui_thread

    def test_dialog_closes_while_discovery_runs(self, dialog, qtbot):
        gate = threading.Event()

        def blocking_discover(worker_self):
            gate.wait(10)
            return []

        worker = None
        try:
            with patch.object(
                scan_mod.PartitionScanWorker, "_discover_postgres", blocking_discover
            ):
                dialog.discover_partitions()
                worker = dialog._scan_workers["discover"]
                qtbot.waitUntil(lambda: worker.isRunning(), timeout=5000)

                assert not dialog._block_close_while_running()
                gate.set()
                dialog.close()
                qtbot.waitUntil(lambda: not worker.isRunning(), timeout=5000)
        finally:
            gate.set()
            if worker is not None:
                worker.wait(5000)
