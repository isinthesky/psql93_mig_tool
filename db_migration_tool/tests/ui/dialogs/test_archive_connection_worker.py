"""1단계 — 연결 확인 워커화 테스트

검증 층위를 둘로 나눈다.
- 워커 로직: `run()`을 직접 부르거나 슬롯을 직접 호출한다(결정적, 대다수)
- 스레드 통합: 실제로 `start()` 한다(배선이 살아 있는지 확인, 소수)
"""

import threading
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QThread

import src.core.scan_workers as scan_mod
import src.ui.dialogs.file_archive_migration_dialog as archive_mod
from src.core.scan_workers import (
    ConnectionCheckWorker,
    EndpointCheckResult,
    EndpointCheckSpec,
    ScanWorker,
)


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
    yield dlg
    dlg.deleteLater()


class TestWorkerContract:
    """규약을 어기면 스레드에서 무작위로 깨진다. 정적으로 못 박는다."""

    def test_scan_worker_does_not_redefine_finished(self):
        """finished를 가리면 '스레드 종료'라는 원래 뜻이 사라진다.

        정리 로직을 성공·실패 공통으로 걸 자리가 없어진다.
        """
        assert "finished" not in ScanWorker.__dict__

    def test_result_signals_carry_a_generation(self):
        import inspect

        source = inspect.getsource(ScanWorker)
        assert "result = Signal(int, object)" in source
        assert "failed = Signal(int, str)" in source

    def test_worker_takes_only_plain_values(self):
        """위젯/프로필/매니저를 넘기면 워커 스레드에서 UI나 ORM을 만지게 된다."""
        import inspect

        params = inspect.signature(ConnectionCheckWorker.__init__).parameters
        assert set(params) == {"self", "generation", "source", "target"}

    def test_endpoint_spec_is_immutable(self):
        spec = EndpointCheckSpec(kind="postgres", config={}, must_exist=True)
        with pytest.raises(AttributeError):
            spec.kind = "file"  # type: ignore[misc]


class TestConnectionCheckWorkerLogic:
    """run()을 직접 호출한다. 스레드 없음."""

    def _worker(self, source_kind="postgres", target_kind="file"):
        return ConnectionCheckWorker(
            7,
            EndpointCheckSpec(kind=source_kind, config={"host": "h"}, must_exist=True),
            EndpointCheckSpec(kind=target_kind, config={"archive_path": "D:/a"}, must_exist=False),
        )

    def test_checks_postgres_source_and_file_target(self):
        worker = self._worker()
        with (
            patch.object(
                scan_mod.PostgresOptimizer, "check_connection_quick", return_value=(True, "PG ok")
            ),
            patch.object(
                scan_mod.ConnectionValidator,
                "validate_file_archive_config",
                return_value=(True, ""),
            ),
        ):
            payload = worker.execute()

        assert payload["source"] == EndpointCheckResult(True, "PG ok")
        assert payload["target"].ok
        assert payload["target"].message == "출력 경로 사용 가능"

    def test_source_archive_message_says_path_verified(self):
        worker = self._worker(source_kind="file", target_kind="file")
        with patch.object(
            scan_mod.ConnectionValidator, "validate_file_archive_config", return_value=(True, "")
        ):
            payload = worker.execute()

        assert payload["source"].message == "경로 확인 완료"

    def test_failure_message_is_passed_through(self):
        worker = self._worker(source_kind="file", target_kind="file")
        with patch.object(
            scan_mod.ConnectionValidator,
            "validate_file_archive_config",
            return_value=(False, "경로가 없습니다"),
        ):
            payload = worker.execute()

        assert not payload["source"].ok
        assert payload["source"].message == "경로가 없습니다"

    def test_exception_becomes_failed_signal(self):
        worker = self._worker()
        received = []
        worker.failed.connect(lambda gen, msg: received.append((gen, msg)))
        worker.result.connect(lambda gen, payload: received.append(("result", payload)))

        with patch.object(
            scan_mod.PostgresOptimizer,
            "check_connection_quick",
            side_effect=RuntimeError("boom"),
        ):
            worker.run()

        assert received == [(7, "boom")], "예외는 failed로만 나가야 합니다"

    def test_generation_is_carried_on_success(self):
        worker = self._worker()
        received = []
        worker.result.connect(lambda gen, payload: received.append(gen))

        with (
            patch.object(
                scan_mod.PostgresOptimizer, "check_connection_quick", return_value=(True, "ok")
            ),
            patch.object(
                scan_mod.ConnectionValidator,
                "validate_file_archive_config",
                return_value=(True, ""),
            ),
        ):
            worker.run()

        assert received == [7]

    def test_interrupted_worker_emits_nothing(self):
        """취소된 결과를 UI에 밀어 넣으면 사용자가 안 누른 상태가 그려진다."""
        worker = self._worker()
        received = []
        worker.result.connect(lambda *a: received.append(a))
        worker.failed.connect(lambda *a: received.append(a))

        with (
            patch.object(
                scan_mod.PostgresOptimizer, "check_connection_quick", return_value=(True, "ok")
            ),
            patch.object(
                scan_mod.ConnectionValidator,
                "validate_file_archive_config",
                return_value=(True, ""),
            ),
            patch.object(ConnectionCheckWorker, "should_stop", return_value=True),
        ):
            worker.run()

        assert received == []

    def test_cancel_query_is_safe_without_a_connection(self):
        worker = self._worker()
        worker.cancel_query()  # 예외가 나면 안 된다


class TestDialogWiring:
    """슬롯을 직접 호출한다. 스레드 없음."""

    def test_result_updates_lamps_and_connected_flags(self, dialog):
        gen = dialog._scan_gen
        dialog._on_connection_check_result(
            gen,
            {
                "source": EndpointCheckResult(True, "연결됨"),
                "target": EndpointCheckResult(True, "출력 경로 사용 가능"),
            },
        )

        assert dialog.source_connected and dialog.target_connected
        assert dialog.source_lamp.state == "ok"
        assert dialog.source_status_message == "연결됨"

    def test_failure_sets_error_lamps(self, dialog):
        gen = dialog._scan_gen
        dialog._on_connection_check_failed(gen, "boom")

        assert dialog.source_lamp.state == "error"
        assert not dialog.source_connected
        assert "boom" in dialog.source_status_message

    def test_stale_result_is_discarded(self, dialog):
        """연결 상태 문구는 작업 이력에 기록된다. 낡은 결과가 덮으면 안 된다."""
        stale = dialog._scan_gen
        dialog._bump_generation()

        dialog._on_connection_check_result(
            stale,
            {
                "source": EndpointCheckResult(True, "낡은 결과"),
                "target": EndpointCheckResult(True, "낡은 결과"),
            },
        )

        assert not dialog.source_connected
        assert dialog.source_status_message != "낡은 결과"

    def test_stale_failure_is_discarded(self, dialog):
        dialog._on_connection_check_result(
            dialog._scan_gen,
            {
                "source": EndpointCheckResult(True, "연결됨"),
                "target": EndpointCheckResult(True, "연결됨"),
            },
        )
        stale = dialog._scan_gen
        dialog._bump_generation()

        dialog._on_connection_check_failed(stale, "낡은 실패")

        assert dialog.source_connected, "낡은 실패가 현재 상태를 덮었습니다"

    def test_finished_restores_the_recheck_button(self, dialog):
        dialog.recheck_btn.setEnabled(False)
        dialog._mark_scan_started("conn")

        dialog._on_connection_check_finished()

        assert dialog.recheck_btn.isEnabled()
        assert not dialog._is_scan_inflight("conn")

    def test_cancelled_check_does_not_leave_lamps_spinning(self, dialog):
        """취소된 워커는 결과도 실패도 보내지 않는다.

        그대로 두면 램프가 '확인 중...'에 영원히 멈춰 사용자가 상태를 알 수 없다.
        """
        dialog.source_lamp.set_state("busy", "확인 중...")
        dialog.target_lamp.set_state("busy", "확인 중...")

        dialog._on_connection_check_finished()

        assert dialog.source_lamp.state != "busy"
        assert not dialog.source_connected
        assert dialog.recheck_btn.isEnabled()

    def test_finished_does_not_overwrite_a_delivered_result(self, dialog):
        dialog._on_connection_check_result(
            dialog._scan_gen,
            {
                "source": EndpointCheckResult(True, "연결됨"),
                "target": EndpointCheckResult(True, "사용 가능"),
            },
        )

        dialog._on_connection_check_finished()

        assert dialog.source_lamp.state == "ok"
        assert dialog.source_status_message == "연결됨"

    def test_scheduled_check_does_not_start_after_close(self, dialog):
        """생성자가 QTimer로 예약하므로, 창이 뜨자마자 Esc를 누르면
        닫힌 뒤에 호출이 도착해 워커가 뜬다."""
        dialog._closing = True

        with patch.object(archive_mod, "ConnectionCheckWorker") as worker_cls:
            archive_mod.FileArchiveMigrationDialog.check_connections(dialog)

        assert not worker_cls.called, "닫히는 중에는 워커를 띄우면 안 됩니다"

    def test_stale_finished_does_not_overwrite_the_status_message(self, dialog):
        """연결 상태 문구는 작업 이력에 기록된다.

        낡은 워커가 끝나면서 '중단되었습니다'로 덮으면 그 값이 이력에 남는다.
        """
        dialog._on_connection_check_result(
            dialog._scan_gen,
            {
                "source": EndpointCheckResult(True, "연결됨"),
                "target": EndpointCheckResult(True, "사용 가능"),
            },
        )
        stale_gen = dialog._scan_gen
        dialog._bump_generation()
        dialog.source_lamp.set_state("busy", "확인 중...")

        stale_worker = MagicMock()
        stale_worker.generation = stale_gen
        with patch.object(
            archive_mod.FileArchiveMigrationDialog, "sender", return_value=stale_worker
        ):
            dialog._on_connection_check_finished()

        assert dialog.source_status_message == "연결됨", "낡은 finished가 문구를 덮었습니다"

    def test_second_request_is_ignored_while_inflight(self, dialog):
        dialog._mark_scan_started("conn")

        with patch.object(archive_mod, "ConnectionCheckWorker") as worker_cls:
            archive_mod.FileArchiveMigrationDialog.check_connections(dialog)

        assert not worker_cls.called, "확인 중에 또 시작하면 워커가 둘이 됩니다"

    def test_no_lambda_in_worker_connections(self):
        """람다는 수신자 QObject가 없어 자동 해제되지 않는다."""
        import ast
        import inspect
        import textwrap

        source = textwrap.dedent(
            inspect.getsource(archive_mod.FileArchiveMigrationDialog.check_connections)
        )
        assert not [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Lambda)], (
            "워커 시그널 연결에 람다를 쓰지 마세요"
        )


class TestThreadingIntegration:
    """실제로 start() 한다. 배선이 살아 있는지 확인하는 소수 케이스."""

    def test_check_runs_off_the_gui_thread_and_reaches_the_dialog(self, dialog, qtbot):
        """배선이 실제로 살아 있는지 확인하는 통합 케이스.

        결과 수신은 signal이 아니라 다이얼로그 상태로 기다린다. start() 뒤에
        waitSignal을 설치하면 이미 발행된 신호를 놓쳐 타임아웃할 수 있다.
        """
        gui_thread = QThread.currentThread()
        seen = {}

        def slow_check(_config):
            seen["thread"] = QThread.currentThread()
            return True, "연결됨"

        worker = None
        try:
            with (
                patch.object(
                    scan_mod.PostgresOptimizer, "check_connection_quick", side_effect=slow_check
                ),
                patch.object(
                    scan_mod.ConnectionValidator,
                    "validate_file_archive_config",
                    return_value=(True, ""),
                ),
            ):
                archive_mod.FileArchiveMigrationDialog.check_connections(dialog)
                worker = dialog._scan_workers["conn"]
                qtbot.waitUntil(lambda: dialog.source_connected, timeout=5000)
                qtbot.waitUntil(lambda: not worker.isRunning(), timeout=5000)
        finally:
            if worker is not None:
                worker.wait(5000)

        assert seen["thread"] != gui_thread, "GUI 스레드에서 돌면 창이 얼어붙습니다"
        assert dialog.source_lamp.state == "ok"

    def test_dialog_actually_closes_while_a_check_is_running(self, dialog, qtbot):
        """조회 중에도 창은 닫혀야 한다(조회는 파괴적이지 않다).

        닫기 전에 워커를 정리하지 않으면 실행 중인 QThread가 파괴돼 프로세스가 죽는다.
        """
        gate = threading.Event()

        def blocking_check(_config):
            gate.wait(10)
            return True, "ok"

        worker = None
        try:
            with (
                patch.object(
                    scan_mod.PostgresOptimizer, "check_connection_quick", side_effect=blocking_check
                ),
                patch.object(
                    scan_mod.ConnectionValidator,
                    "validate_file_archive_config",
                    return_value=(True, ""),
                ),
            ):
                archive_mod.FileArchiveMigrationDialog.check_connections(dialog)
                worker = dialog._scan_workers["conn"]
                qtbot.waitUntil(lambda: worker.isRunning(), timeout=5000)

                assert not dialog._block_close_while_running(), "조회는 닫기를 막지 않는다"

                gate.set()
                dialog.close()
                qtbot.waitUntil(lambda: not worker.isRunning(), timeout=5000)
        finally:
            gate.set()
            if worker is not None:
                worker.wait(5000)

        assert dialog._closing, "닫기 절차를 거쳐야 예약된 작업이 되살아나지 않습니다"
