"""이력 무결성 가드의 UI 경로 — H-08, H-09, M-12.

모델 계층 테스트(tests/models/test_history_*.py)가 규칙을 보고, 여기서는
다이얼로그와 메인 창이 그 규칙을 실제로 거치는지 본다.
"""

from __future__ import annotations

import copy
import os
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QMessageBox
from sqlalchemy import text

import src.database.local_db as local_db_module
import src.ui.dialogs.file_archive_migration_dialog as archive_mod
import src.ui.dialogs.migration_wizard_dialog as wizard_mod
import src.ui.dialogs.resume_guard as resume_guard
from src.database.local_db import Checkpoint, MigrationHistory
from src.models.history import CheckpointManager, HistoryManager
from src.models.profile import ConnectionProfile
from src.ui.main_window import MainWindow

PLAN = ["point_history_260101", "point_history_260102", "point_history_260103"]
SRC = {"host": "s.example", "port": 5446, "database": "bms93", "username": "u", "password": "a"}
DST = {"host": "d.example", "port": 5445, "database": "bms30", "username": "u", "password": "a"}
YES = QMessageBox.StandardButton.Yes
NO = QMessageBox.StandardButton.No


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "ui-history.db")
    instance = local_db_module.LocalDatabase()
    instance.db_path = path
    instance.initialize()
    monkeypatch.setattr(local_db_module, "_db_instance", instance, raising=False)
    yield instance
    if instance.engine:
        instance.engine.dispose()
    if os.path.exists(path):
        os.remove(path)


def _profile(src=None, dst=None) -> ConnectionProfile:
    return ConnectionProfile(
        id=1,
        name="p",
        source_config=copy.deepcopy(src or SRC),
        target_config=copy.deepcopy(dst or DST),
    )


def _wizard(qtbot, profile) -> wizard_mod.MigrationWizardDialog:
    with patch.object(wizard_mod.MigrationWizardDialog, "check_connections", lambda self: None):
        dlg = wizard_mod.MigrationWizardDialog(None, profile)
    qtbot.addWidget(dlg)
    return dlg


def _settle(dlg) -> None:
    """워커는 목(mock)이라 끝나지 않는다. 실행 상태를 내려야 정리 때 '진행 중' 경고 창이 안 뜬다."""
    dlg.worker = None
    dlg._set_run_state("idle")


def _counts(db) -> tuple[int, int]:
    with db.session_scope() as s:
        return s.query(MigrationHistory).count(), s.query(Checkpoint).count()


# ---------------------------------------------------------------- H-09


class TestWizardCreatesPlanAtomically:
    def test_checkpoint_failure_leaves_no_orphan_history(self, qtbot, db):
        with db.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TRIGGER inject_fail BEFORE INSERT ON checkpoints "
                    f"WHEN NEW.partition_name = '{PLAN[2]}' "
                    "BEGIN SELECT RAISE(ABORT, 'injected'); END"
                )
            )
        dlg = _wizard(qtbot, _profile())
        dlg.source_connected = dlg.target_connected = True
        dlg._frozen_selection = list(PLAN)

        with (
            patch.object(wizard_mod, "CopyMigrationWorker") as worker_cls,
            patch.object(wizard_mod.QMessageBox, "critical") as critical,
        ):
            try:
                dlg.start_migration()
            except Exception:  # 수정 전 코드는 예외가 새어 나간다 — 결과만 본다
                pass
        _settle(dlg)

        assert _counts(db) == (0, 0)
        worker_cls.assert_not_called()
        critical.assert_called_once()
        assert dlg.history_id is None

    def test_success_records_plan_and_all_checkpoints(self, qtbot, db):
        dlg = _wizard(qtbot, _profile())
        dlg.source_connected = dlg.target_connected = True
        dlg._frozen_selection = list(PLAN)

        with patch.object(wizard_mod, "CopyMigrationWorker"):
            dlg.start_migration()
        _settle(dlg)

        assert dlg.history_id is not None
        item = HistoryManager().get_history(dlg.history_id)
        assert item is not None and item.planned_count == len(PLAN)
        assert _counts(db) == (1, len(PLAN))


# ---------------------------------------------------------------- H-08


def _planned_history(profile=None) -> int:
    item = HistoryManager().create_planned_history(
        profile or _profile(), PLAN, "2026-01-01", "2026-01-03"
    )
    assert item.id is not None
    return item.id


class TestWizardResumeGate:
    def test_changed_target_is_refused(self, qtbot, db):
        _planned_history()
        moved = _profile(SRC, dict(DST, database="temp"))
        dlg = _wizard(qtbot, moved)

        with (
            patch.object(resume_guard.QMessageBox, "warning") as warning,
            patch.object(resume_guard.QMessageBox, "question", return_value=NO),
        ):
            dlg._on_resume_clicked()

        assert dlg.resume_mode is False
        assert dlg.history_id is None
        assert dlg._frozen_selection == []
        warning.assert_called_once()

    def test_password_rotation_resumes(self, qtbot, db):
        hid = _planned_history()
        dlg = _wizard(qtbot, _profile(dict(SRC, password="b"), dict(DST, password="c")))

        dlg._on_resume_clicked()

        assert dlg.resume_mode is True
        assert dlg.history_id == hid
        assert dlg._frozen_selection == PLAN

    def test_refused_history_can_be_explicitly_abandoned(self, qtbot, db):
        hid = _planned_history()
        dlg = _wizard(qtbot, _profile(SRC, dict(DST, database="temp")))

        with (
            patch.object(resume_guard.QMessageBox, "warning"),
            patch.object(resume_guard.QMessageBox, "question", return_value=YES),
        ):
            dlg._on_resume_clicked()

        assert dlg.resume_mode is False
        item = HistoryManager().get_history(hid)
        assert item is not None and item.status == "cancelled"
        assert HistoryManager().get_incomplete_history(1) is None

    def test_legacy_history_requires_confirmation(self, qtbot, db):
        hm, cm = HistoryManager(), CheckpointManager()
        legacy = hm.create_history(1, "2026-01-01", "2026-01-03")
        assert legacy.id is not None
        for name in PLAN:
            cm.create_checkpoint(legacy.id, name)
        dlg = _wizard(qtbot, _profile())

        with patch.object(resume_guard.QMessageBox, "question", return_value=NO) as question:
            dlg._on_resume_clicked()

        question.assert_called_once()
        assert dlg.resume_mode is False
        item = hm.get_history(legacy.id)
        assert item is not None and item.plan_version is None

    def test_legacy_history_confirmed_is_adopted(self, qtbot, db):
        hm, cm = HistoryManager(), CheckpointManager()
        legacy = hm.create_history(1, "2026-01-01", "2026-01-03")
        assert legacy.id is not None
        for name in PLAN:
            cm.create_checkpoint(legacy.id, name)
        dlg = _wizard(qtbot, _profile())

        with patch.object(resume_guard.QMessageBox, "question", return_value=YES):
            dlg._on_resume_clicked()

        assert dlg.resume_mode is True
        assert dlg._frozen_selection == PLAN
        item = hm.get_history(legacy.id)
        assert item is not None and item.plan_version == 1

    def test_restart_after_stop_revalidates_plan(self, qtbot, db):
        """같은 창에서 중단 후 '다시 시작'도 계획 기준 pending을 쓴다."""
        hid = _planned_history()
        with db.session_scope() as s:
            s.query(Checkpoint).filter(Checkpoint.partition_name == PLAN[1]).delete()
        dlg = _wizard(qtbot, _profile())
        dlg.source_connected = dlg.target_connected = True
        dlg.history_id = hid

        with patch.object(wizard_mod, "CopyMigrationWorker") as worker_cls:
            dlg.start_migration()
        _settle(dlg)

        assert dlg.resume_mode is True
        assert worker_cls.call_args.args[1] == PLAN


class TestArchiveResumeGate:
    def _dialog(self, qtbot, profile) -> archive_mod.FileArchiveMigrationDialog:
        with patch.object(
            archive_mod.FileArchiveMigrationDialog, "check_connections", lambda self: None
        ):
            dlg = archive_mod.FileArchiveMigrationDialog(None, profile)
        qtbot.addWidget(dlg)
        return dlg

    def test_changed_archive_path_is_refused(self, qtbot, db, tmp_path):
        original = _profile(SRC, {"kind": "file", "archive_path": str(tmp_path / "a")})
        _planned_history(original)
        moved = _profile(SRC, {"kind": "file", "archive_path": str(tmp_path / "b")})
        dlg = self._dialog(qtbot, moved)

        with (
            patch.object(resume_guard.QMessageBox, "warning") as warning,
            patch.object(resume_guard.QMessageBox, "question", return_value=NO),
        ):
            dlg._on_resume_clicked()

        assert dlg.resume_mode is False
        warning.assert_called_once()

    def test_same_archive_resumes(self, qtbot, db, tmp_path):
        original = _profile(SRC, {"kind": "file", "archive_path": str(tmp_path / "a")})
        hid = _planned_history(original)
        dlg = self._dialog(
            qtbot, _profile(SRC, {"kind": "file", "archive_path": str(tmp_path / "a")})
        )

        dlg._on_resume_clicked()

        assert dlg.resume_mode is True
        assert dlg.history_id == hid
        assert dlg._frozen_selection == PLAN


# ---------------------------------------------------------------- M-12


@pytest.fixture
def window():
    """MainWindow를 띄우지 않고 메서드만 떼어 검사한다(test_restricted_mode와 같은 방식).

    검사 대상이 부르는 보조 메서드는 실제 구현을 묶어 둔다 — 목으로 두면 판단이 사라진다.
    """
    win = MagicMock()
    win.license_state = None
    win.vm.current_profile = MagicMock(id=1)
    win.vm.current_profile.name = "p"
    for name in ("_delete_with_unfinished_work", "_confirm_identity_change"):
        setattr(win, name, getattr(MainWindow, name).__get__(win))
    return win


class TestProfileDeleteGuard:
    def test_profile_without_unfinished_work_is_deleted(self, window):
        window.vm.history_manager.count_incomplete_histories.return_value = 0

        with patch("src.ui.main_window.QMessageBox.question", return_value=YES):
            MainWindow.delete_connection(window)

        window.vm.delete_profile.assert_called_once_with(1)
        window.vm.history_manager.abandon_incomplete_histories.assert_not_called()

    def test_unfinished_work_blocks_plain_delete(self, window):
        window.vm.history_manager.count_incomplete_histories.return_value = 2

        with (
            patch("src.ui.main_window.QMessageBox.question", return_value=NO),
            patch("src.ui.main_window.QMessageBox.warning"),
        ):
            MainWindow.delete_connection(window)

        window.vm.delete_profile.assert_not_called()
        window.vm.history_manager.abandon_incomplete_histories.assert_not_called()

    def test_delete_with_unfinished_work_abandons_it_first(self, window):
        """삭제를 고르면 미완료 작업을 명시적으로 폐기한 뒤에만 지운다 — orphan 금지."""
        window.vm.history_manager.count_incomplete_histories.return_value = 2
        order: list[str] = []
        window.vm.history_manager.abandon_incomplete_histories.side_effect = lambda pid: (
            order.append("abandon") or 2
        )
        window.vm.delete_profile.side_effect = lambda pid: order.append("delete") or True

        with (
            patch("src.ui.main_window.QMessageBox.question", return_value=YES),
            patch("src.ui.main_window.QMessageBox.warning"),
        ):
            MainWindow.delete_connection(window)

        window.vm.history_manager.abandon_incomplete_histories.assert_called_once_with(1)
        assert order == ["abandon", "delete"]

    def test_lookup_failure_blocks_delete(self, window):
        """판정 실패 시 삭제는 막는다(재개 게이트와 반대 — 여기선 막는 쪽이 안전)."""
        window.vm.history_manager.count_incomplete_histories.side_effect = RuntimeError("db")

        with (
            patch("src.ui.main_window.QMessageBox.question", return_value=YES) as question,
            patch("src.ui.main_window.QMessageBox.critical"),
        ):
            MainWindow.delete_connection(window)

        window.vm.delete_profile.assert_not_called()
        question.assert_not_called()


class TestProfileEditIdentityWarning:
    def _edit(self, window, new_src, new_dst, answer):
        window.vm.current_profile = _profile()
        window.vm.history_manager.count_incomplete_histories.return_value = 1
        with (
            patch("src.ui.main_window.ConnectionDialog") as dialog,
            patch("src.ui.main_window.QMessageBox.question", return_value=answer) as question,
        ):
            dialog.return_value.exec.return_value = True
            dialog.return_value.get_profile_data.return_value = {
                "name": "p",
                "source_config": new_src,
                "target_config": new_dst,
            }
            MainWindow.edit_connection(window)
        return question

    def test_password_only_edit_saves_without_asking(self, window):
        question = self._edit(window, dict(SRC, password="x"), dict(DST, password="y"), NO)
        question.assert_not_called()
        window.vm.update_profile.assert_called_once()

    def test_identity_edit_with_unfinished_work_asks_and_can_cancel(self, window):
        question = self._edit(window, SRC, dict(DST, database="temp"), NO)
        question.assert_called_once()
        window.vm.update_profile.assert_not_called()

    def test_identity_edit_confirmed_saves(self, window):
        self._edit(window, SRC, dict(DST, database="temp"), YES)
        window.vm.update_profile.assert_called_once()
