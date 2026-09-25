"""main.py의 종료 배선(감사 M-13).

- 트레이 '종료' → ShutdownCoordinator.request_shutdown (app.quit 직결 금지)
- 조정자가 끝나면 트레이를 정리하고 이벤트 루프를 끝낸다.
- 강제 종료(forced)면 아직 도는 QThread를 파괴하지 않도록 파이썬 종료 절차를 건너뛴다.
- flush 단계: 로거(DB 로그 큐) → 로컬 DB 순서.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QObject, Signal

from src import main as app_main
from src.core import worker_registry as registry_module
from src.core.worker_registry import ShutdownCoordinator, WorkerRegistry


class _FakeTray(QObject):
    quit_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.cleanup = MagicMock()
        self.show_shutting_down = MagicMock()


@pytest.fixture
def registry(monkeypatch):
    fresh = WorkerRegistry()
    monkeypatch.setattr(registry_module, "worker_registry", fresh)
    return fresh


@pytest.fixture
def no_flush(monkeypatch):
    monkeypatch.setattr(app_main, "default_flush_steps", lambda: [])


def test_tray_quit_goes_through_coordinator(qapp, registry, no_flush):
    app = MagicMock()
    window = MagicMock()
    tray = _FakeTray()

    coordinator = app_main.build_shutdown_coordinator(app, window, tray)
    assert isinstance(coordinator, ShutdownCoordinator)
    assert window.shutdown_coordinator is coordinator

    tray.quit_requested.emit()

    assert coordinator.is_finished
    tray.show_shutting_down.assert_called_once()
    tray.cleanup.assert_called_once()
    app.exit.assert_called_once_with(0)
    app.quit.assert_not_called()


def test_without_tray_window_still_gets_coordinator(qapp, registry, no_flush):
    app = MagicMock()
    window = MagicMock()

    coordinator = app_main.build_shutdown_coordinator(app, window, None)

    assert window.shutdown_coordinator is coordinator
    coordinator.request_shutdown()
    app.exit.assert_called_once_with(0)


def test_default_flush_steps_order():
    names = [name for name, _ in app_main.default_flush_steps()]
    assert names == ["logger", "local_db"]


def test_flush_logger_drains_queue_and_flushes_file_handlers():
    logger = MagicMock()
    handler = MagicMock()
    with (
        patch("src.utils.enhanced_logger.enhanced_logger", logger),
        patch("logging.getLogger") as get_logger,
    ):
        get_logger.return_value.handlers = [handler]
        app_main.flush_logger()

    logger.close.assert_called_once()
    handler.flush.assert_called_once()


def test_close_local_db_disposes_only_existing_instance(monkeypatch):
    from src.database import local_db

    monkeypatch.setattr(local_db, "_db_instance", None)
    app_main.close_local_db()  # 없으면 새로 만들지 않는다(APPDATA DB를 열지 않음)
    assert local_db._db_instance is None

    db = MagicMock()
    monkeypatch.setattr(local_db, "_db_instance", db)
    app_main.close_local_db()
    db.close.assert_called_once()


def test_finalize_exit_graceful_returns_code(registry, no_flush):
    coordinator = ShutdownCoordinator(registry, quit_app=MagicMock(), log=lambda *_: None)
    coordinator.request_shutdown()
    with patch("os._exit") as hard_exit:
        assert app_main.finalize_exit(coordinator, 0) == 0
    hard_exit.assert_not_called()


def test_finalize_exit_runs_blocking_shutdown_when_loop_ended_elsewhere(registry):
    """트레이를 거치지 않고 루프가 끝났으면(OS 종료 등) 여기서라도 정리한다."""
    coordinator = MagicMock()
    coordinator.is_finished = False
    coordinator.forced = False
    with patch("os._exit") as hard_exit:
        app_main.finalize_exit(coordinator, 0)
    coordinator.shutdown_blocking.assert_called_once()
    hard_exit.assert_not_called()


def test_finalize_exit_forced_skips_interpreter_teardown(registry):
    coordinator = MagicMock()
    coordinator.is_finished = True
    coordinator.forced = True
    with patch("os._exit") as hard_exit, patch("logging.shutdown") as log_shutdown:
        app_main.finalize_exit(coordinator, 0)
    log_shutdown.assert_called_once()
    hard_exit.assert_called_once_with(0)


class TestMainWindowCloseWithoutTray:
    """트레이가 없는 환경에서 창을 닫으면 조정자를 거쳐 끝낸다(예전엔 창만 닫히고 프로세스가 남았다)."""

    def _window(self, coordinator):
        from types import SimpleNamespace

        return SimpleNamespace(
            minimize_to_tray=True, tray_icon=None, shutdown_coordinator=coordinator
        )

    def test_close_requests_shutdown(self):
        from src.ui.main_window import MainWindow

        coordinator = MagicMock()
        coordinator.is_finished = False
        event = MagicMock()

        MainWindow.closeEvent(self._window(coordinator), event)

        event.ignore.assert_called_once()
        event.accept.assert_not_called()
        coordinator.request_shutdown.assert_called_once()

    def test_close_after_shutdown_finished_accepts(self):
        from src.ui.main_window import MainWindow

        coordinator = MagicMock()
        coordinator.is_finished = True
        event = MagicMock()

        MainWindow.closeEvent(self._window(coordinator), event)

        event.accept.assert_called_once()
        coordinator.request_shutdown.assert_not_called()


class TestEventLoopEndToEnd:
    """실제 이벤트 루프에서 트레이 '종료' → 워커 정지 → flush → 루프 종료까지."""

    def test_tray_quit_during_modal_dialog_stops_worker_and_ends_all_loops(
        self, qapp, registry, no_flush
    ):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QDialog

        from tests.core.test_worker_shutdown import _BlockingScan

        window = MagicMock()
        tray = _FakeTray()
        coordinator = app_main.build_shutdown_coordinator(qapp, window, tray)
        worker = _BlockingScan()
        dialog_returned: list[bool] = []

        def open_modal_and_quit() -> None:
            worker.start()
            assert worker.entered.wait(5)
            dialog = QDialog()
            # 마법사가 떠 있는 동안(중첩 루프) 트레이에서 종료를 누른 상황
            QTimer.singleShot(50, tray.quit_requested.emit)
            dialog.exec()
            dialog_returned.append(True)

        QTimer.singleShot(0, open_modal_and_quit)
        safety = QTimer()
        safety.setSingleShot(True)
        safety.timeout.connect(lambda: qapp.exit(99))
        safety.start(10_000)
        rc = qapp.exec()
        safety.stop()

        assert rc == 0, "종료 조정자가 아니라 안전 타이머가 루프를 끝냈습니다"
        assert dialog_returned == [True], "중첩(모달) 루프가 끝나지 않았습니다"
        assert worker.isFinished() and worker.outcome == "cancelled"
        assert coordinator.is_finished and not coordinator.forced
        tray.cleanup.assert_called_once()
