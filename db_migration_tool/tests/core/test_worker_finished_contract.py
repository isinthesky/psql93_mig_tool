"""마이그레이션 워커의 finished 규약.

QThread는 run()이 반환할 때 finished를 스스로 발행한다. 하위 클래스가 같은
이름으로 시그널을 다시 선언하고 직접 emit하면 슬롯이 두 번 불리고, 완료
핸들러가 두 번 돌면서 "완료" 뒤에 "중단"이 이어 찍힌다.
"""

from PySide6.QtCore import QThread

from src.core.base_migration_worker import BaseMigrationWorker
from src.core.copy_migration_worker import CopyMigrationWorker
from src.core.file_archive_workers import (
    FileToPostgresArchiveWorker,
    PostgresToFileArchiveWorker,
)
from src.core.migration_worker import MigrationWorker

WORKER_CLASSES = [
    BaseMigrationWorker,
    CopyMigrationWorker,
    MigrationWorker,
    PostgresToFileArchiveWorker,
    FileToPostgresArchiveWorker,
]


class TestFinishedContract:
    def test_no_worker_redefines_finished(self):
        """finished를 가리면 '스레드 종료'라는 원래 뜻이 사라진다."""
        offenders = [cls.__name__ for cls in WORKER_CLASSES if "finished" in cls.__dict__]
        assert not offenders, f"finished를 재선언한 워커: {offenders}"

    def test_run_does_not_emit_finished_itself(self):
        """run()이 직접 emit하면 QThread의 발행과 합쳐져 2회가 된다."""
        import inspect

        source = inspect.getsource(BaseMigrationWorker.run)
        assert "self.finished.emit()" not in source


class _CountingWorker(QThread):
    """BaseMigrationWorker와 같은 구조로 finished 발행 횟수를 센다."""

    def run(self):
        pass


def test_finished_fires_exactly_once(qtbot):
    """계약이 지켜지면 슬롯은 정확히 한 번 불린다."""
    calls = []
    worker = _CountingWorker()
    worker.finished.connect(lambda: calls.append(1))

    with qtbot.waitSignal(worker.finished, timeout=5000):
        worker.start()
    worker.wait(5000)
    qtbot.wait(50)

    assert len(calls) == 1


def test_base_worker_finished_fires_exactly_once(qtbot, monkeypatch):
    """실제 BaseMigrationWorker 하위 클래스로도 1회인지 확인한다."""

    class _Worker(BaseMigrationWorker):
        def _execute_migration(self):
            pass

    monkeypatch.setattr(
        "src.core.base_migration_worker.enhanced_logger.generate_session_id",
        lambda: "test-session",
    )

    worker = _Worker(profile=None, partitions=[], history_id=1)
    calls = []
    worker.finished.connect(lambda: calls.append(1))

    with qtbot.waitSignal(worker.finished, timeout=5000):
        worker.start()
    worker.wait(5000)
    qtbot.wait(50)

    assert len(calls) == 1, f"finished가 {len(calls)}회 발행됐습니다"
