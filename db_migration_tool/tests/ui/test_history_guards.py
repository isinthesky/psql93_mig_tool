"""이력 무결성 가드의 UI 경로 — H-08, H-09, M-12.

모델 계층 테스트(tests/models/test_history_*.py)가 규칙을 보고, 여기서는
다이얼로그와 메인 창이 그 규칙을 실제로 거치는지 본다.
"""

from __future__ import annotations

import copy
import os
from datetime import datetime
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


def _choose(types):
    """legacy 채택 때 '원래 작업의 항목'을 고르는 창의 답(None = 취소)."""
    return patch.object(
        resume_guard,
        "choose_original_types",
        return_value=None if types is None else list(types),
    )


def _legacy_history(names=PLAN, start="2026-01-01", end="2026-01-03") -> int:
    """구버전 경로(create_history + checkpoint 개별 생성)로 만든 legacy 이력."""
    hm, cm = HistoryManager(), CheckpointManager()
    legacy = hm.create_history(1, start, end)
    assert legacy.id is not None
    for name in names:
        cm.create_checkpoint(legacy.id, name)
    return legacy.id


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

        with (
            _choose(["point_history"]),
            patch.object(resume_guard.QMessageBox, "question", return_value=NO) as question,
        ):
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

        with (
            _choose(["point_history"]),
            patch.object(resume_guard.QMessageBox, "question", return_value=YES),
        ):
            dlg._on_resume_clicked()

        assert dlg.resume_mode is True
        assert dlg._frozen_selection == PLAN
        item = hm.get_history(legacy.id)
        assert item is not None and item.plan_version == 1

    def _truncated_legacy(self) -> int:
        """구버전 H-09로 3일 범위에 checkpoint가 2개만 남은 legacy 이력."""
        hm, cm = HistoryManager(), CheckpointManager()
        legacy = hm.create_history(1, "2026-01-01", "2026-01-03")
        assert legacy.id is not None
        for name in PLAN[:2]:
            cm.create_checkpoint(legacy.id, name)
        return legacy.id

    def test_legacy_gap_is_shown_and_supplemented_on_confirm(self, qtbot, db):
        hid = self._truncated_legacy()
        dlg = _wizard(qtbot, _profile())

        with (
            _choose(["point_history"]),
            patch.object(resume_guard.QMessageBox, "question", return_value=YES) as question,
        ):
            dlg._on_resume_clicked()

        question.assert_called_once()
        shown = question.call_args.args[2]
        # 범위 대비 누락을 수치와 이름으로 보여 주고, 보충된다고 알린다.
        assert "누락 1개" in shown
        assert PLAN[2] in shown
        assert "보충" in shown
        # 남은 2개만이 아니라 범위 전체(3개)가 계획·재개 대상이 된다.
        assert dlg.resume_mode is True
        assert dlg._frozen_selection == PLAN
        item = HistoryManager().get_history(hid)
        assert item is not None and item.planned_count == 3

    def test_legacy_gap_declined_writes_nothing(self, qtbot, db):
        hid = self._truncated_legacy()
        dlg = _wizard(qtbot, _profile())

        with (
            _choose(["point_history"]),
            patch.object(resume_guard.QMessageBox, "question", return_value=NO),
        ):
            dlg._on_resume_clicked()

        assert dlg.resume_mode is False
        item = HistoryManager().get_history(hid)
        assert item is not None and item.plan_version is None
        assert _counts(db) == (1, 2)

    def test_legacy_with_unreadable_range_is_not_adopted(self, qtbot, db):
        """범위를 모르면 누락을 확인할 수 없으므로 채택 확인 대신 거부·폐기 안내로 간다."""
        hm, cm = HistoryManager(), CheckpointManager()
        legacy = hm.create_history(1, "", "")
        assert legacy.id is not None
        cm.create_checkpoint(legacy.id, PLAN[0])
        dlg = _wizard(qtbot, _profile())

        with (
            patch.object(resume_guard.QMessageBox, "warning") as warning,
            patch.object(resume_guard.QMessageBox, "question", return_value=NO) as question,
        ):
            dlg._on_resume_clicked()

        warning.assert_called_once()
        assert "범위" in warning.call_args.args[2]
        # 묻는 것은 폐기 여부 하나뿐이다(채택 확인 없음).
        question.assert_called_once()
        assert question.call_args.args[1] == "미완료 작업 폐기"
        assert dlg.resume_mode is False
        item = hm.get_history(legacy.id)
        assert item is not None and item.plan_version is None

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


PS = ["point_sec_history_260101", "point_sec_history_260102", "point_sec_history_260103"]
MONTHLY_TRAILING = ["running_time_history_2601", "trend_history_2601"]


class TestLegacyMultiTypeAdoption:
    """리뷰 major(H-08): 다중 유형 legacy 이력이 유형 경계에서 끊기면 뒤 유형이 통째로 없다.

    PH checkpoint만 남은 이력은 'PH만 고른 작업'과 'PH 뒤에서 끊긴 PH+PS(+RT/TH) 작업'을
    로컬 데이터로 구분할 수 없다. 뒤 유형(PS·RT·TH)의 포함 여부를 사용자가 유형마다 명시적으로
    고른다 — **기본값이 없다**(리뷰 라운드 1: 기본 포함은 가장 흔한 PH 단일 유형 이력을 뒤 유형
    보충, import에서는 대상 TRUNCATE로 바꾼다). ED는 생성 순서상 PH보다 앞이라 원래 선택하지
    않은 것이 확실하다. 이력이 시작된 날에 없던 유형도 묻지 않는다.
    """

    def test_trailing_types_chosen_to_include_are_supplemented(self, qtbot, db):
        hid = _legacy_history()
        dlg = _wizard(qtbot, _profile())

        with (
            _choose(
                ["point_history", "point_sec_history", "running_time_history", "trend_history"]
            ),
            patch.object(resume_guard.QMessageBox, "question", return_value=YES) as question,
        ):
            dlg._on_resume_clicked()

        shown = question.call_args.args[2]
        assert "point_sec_history" in shown
        assert "energy_display" not in shown
        # 끊겨서 빠졌을 수 있는 뒤 유형까지 계획·재개 대상이 된다(subset 완료 방지).
        expected = sorted([*PLAN, *PS, *MONTHLY_TRAILING])
        assert dlg.resume_mode is True
        assert dlg._frozen_selection == expected
        item = HistoryManager().get_history(hid)
        assert item is not None and item.planned_count == len(expected)

    def test_accepting_the_chooser_without_deciding_writes_nothing(self, qtbot, db):
        """'확인'만 눌러서는 어떤 유형도 포함·제외되지 않는다(기본값 없음, fail-closed)."""
        hid = _legacy_history()
        dlg = _wizard(qtbot, _profile())

        with (
            patch.object(
                resume_guard.LegacyTypeChooser,
                "exec",
                return_value=resume_guard.QDialog.DialogCode.Accepted,
            ),
            patch.object(resume_guard.QMessageBox, "question", return_value=YES) as question,
        ):
            dlg._on_resume_clicked()

        question.assert_not_called()
        assert dlg.resume_mode is False
        item = HistoryManager().get_history(hid)
        assert item is not None and item.plan_version is None
        assert _counts(db) == (1, 3)

    def test_unselected_types_are_not_in_the_warning(self, qtbot, db):
        _legacy_history()
        dlg = _wizard(qtbot, _profile())

        with (
            _choose(["point_history", "point_sec_history"]),
            patch.object(resume_guard.QMessageBox, "question", return_value=YES) as question,
        ):
            dlg._on_resume_clicked()

        shown = question.call_args.args[2]
        assert "누락 3개" in shown
        assert "point_sec_history" in shown
        for unselected in ("running_time_history", "trend_history", "energy_display"):
            assert unselected not in shown
        assert dlg._frozen_selection == sorted([*PLAN, *PS])

    def test_cancelled_type_choice_writes_nothing(self, qtbot, db):
        hid = _legacy_history()
        dlg = _wizard(qtbot, _profile())

        with (
            _choose(None),
            patch.object(resume_guard.QMessageBox, "question", return_value=YES) as question,
        ):
            dlg._on_resume_clicked()

        question.assert_not_called()
        assert dlg.resume_mode is False
        item = HistoryManager().get_history(hid)
        assert item is not None and item.plan_version is None
        assert _counts(db) == (1, 3)

    def test_chooser_has_no_default_and_requires_every_decision(self, qtbot, db):
        hid = _legacy_history()
        coverage = HistoryManager().prepare_resume(hid, _profile()).legacy
        assert coverage is not None

        chooser = resume_guard.LegacyTypeChooser(None, coverage, "postgres_to_postgres")
        qtbot.addWidget(chooser)

        # checkpoint가 있는 유형은 고를 대상이 아니다(항상 포함). 앞 유형(ED)은 보이지 않는다.
        assert sorted(chooser.choices) == [
            "point_sec_history",
            "running_time_history",
            "trend_history",
        ]
        assert not any(
            include.isChecked() or exclude.isChecked()
            for include, exclude in chooser.choices.values()
        )
        assert not chooser.ok_button.isEnabled()
        assert chooser.chosen_types() is None

        chooser.choices["point_sec_history"][0].setChecked(True)
        chooser.choices["running_time_history"][1].setChecked(True)
        assert not chooser.ok_button.isEnabled()
        assert chooser.chosen_types() is None

        chooser.choices["trend_history"][1].setChecked(True)
        assert chooser.ok_button.isEnabled()
        assert chooser.chosen_types() == ["point_history", "point_sec_history"]

    def test_import_chooser_says_including_empties_target_partitions(self, qtbot, db):
        hid = _legacy_history()
        coverage = HistoryManager().prepare_resume(hid, _profile()).legacy
        assert coverage is not None

        chooser = resume_guard.LegacyTypeChooser(None, coverage, "file_to_postgres")
        qtbot.addWidget(chooser)

        assert "비운 뒤" in chooser.intro.text()

    def test_types_that_did_not_exist_yet_are_not_asked(self, qtbot, db):
        """다중 유형 지원(2025-11-19) 전에 시작한 이력은 PH 단일 유형이 확실하다."""
        hid = _legacy_history()
        with db.session_scope() as s:
            s.query(MigrationHistory).filter(MigrationHistory.id == hid).update(
                {"started_at": datetime(2025, 11, 1, 10, 0)}
            )
        dlg = _wizard(qtbot, _profile())

        with (
            patch.object(resume_guard, "choose_original_types") as chooser,
            patch.object(resume_guard.QMessageBox, "question", return_value=YES),
        ):
            dlg._on_resume_clicked()

        chooser.assert_not_called()
        assert dlg.resume_mode is True
        assert dlg._frozen_selection == PLAN


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

    def _start(self, dlg):
        security = MagicMock(passphrase=None, allow_legacy_unverified=False)
        with (
            patch.object(archive_mod, "prompt_archive_security", return_value=security),
            patch.object(archive_mod, "PostgresToFileArchiveWorker") as worker_cls,
        ):
            dlg.start_migration()
        _settle(dlg)
        return worker_cls

    def test_adopted_legacy_archive_run_tolerates_only_supplemented_partitions(
        self, qtbot, db, tmp_path
    ):
        """H-09 리뷰: 보충한 '있을 수 있는 이름'만 원본에 없어도 0건 완료로 닫는다.

        리뷰 라운드 1: 원래 checkpoint(PLAN[:2])는 구버전이 실제로 고른 파티션이라, 없어졌다면
        잘못된 원본·아카이브를 가리킨다 — 워커에 '없어도 되는 이름'으로 넘기지 않는다.
        """
        archive = {"kind": "file", "archive_path": str(tmp_path / "a")}
        _legacy_history(PLAN[:2])
        dlg = self._dialog(qtbot, _profile(SRC, archive))

        with (
            _choose(["point_history"]),
            patch.object(resume_guard.QMessageBox, "question", return_value=YES),
        ):
            dlg._on_resume_clicked()
        assert dlg.resume_mode is True
        worker_cls = self._start(dlg)

        worker_cls.assert_called_once()
        assert worker_cls.call_args.args[1] == PLAN
        assert worker_cls.return_value.absent_ok_partitions == frozenset({PLAN[2]})

    def test_adopted_legacy_without_gaps_tolerates_nothing(self, qtbot, db, tmp_path):
        archive = {"kind": "file", "archive_path": str(tmp_path / "a")}
        _legacy_history()
        dlg = self._dialog(qtbot, _profile(SRC, archive))

        with (
            _choose(["point_history"]),
            patch.object(resume_guard.QMessageBox, "question", return_value=YES),
        ):
            dlg._on_resume_clicked()
        worker_cls = self._start(dlg)

        assert worker_cls.return_value.absent_ok_partitions == frozenset()

    def test_planned_archive_run_keeps_absent_partitions_failing(self, qtbot, db, tmp_path):
        archive = {"kind": "file", "archive_path": str(tmp_path / "a")}
        _planned_history(_profile(SRC, archive))
        dlg = self._dialog(qtbot, _profile(SRC, archive))

        dlg._on_resume_clicked()
        worker_cls = self._start(dlg)

        worker_cls.assert_called_once()
        assert worker_cls.return_value.absent_ok_partitions == frozenset()


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
    for name in (
        "_delete_with_unfinished_work",
        "_confirm_identity_change",
        "_recorded_endpoint_fingerprints",
    ):
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


class TestLockedProfileIdentityWarning:
    """잠긴 프로필(w1-secrets)은 설정이 기본값이라 profile 설정과 비교할 수 없다.

    복호화하지 못한 프로필의 설정은 localhost:5432 같은 기본값이므로, 예전처럼 profile
    설정과 비교하면 같은 endpoint를 다시 입력해도 '변경됨'으로 판정돼 재개 불가 경고가
    잘못 뜬다. 잠긴 프로필은 미완료 이력에 기록된 endpoint 지문과 비교한다.
    """

    def _edit_locked(self, window, history, new_src, new_dst, answer=NO):
        db_row = MagicMock(id=1, created_at=None, updated_at=None)
        db_row.name = "p"
        locked = ConnectionProfile.locked_from_db_model(db_row, "키 없음")
        window.vm.current_profile = locked
        window.vm.history_manager.count_incomplete_histories.return_value = 1
        window.vm.history_manager.get_incomplete_history.return_value = history
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

    @staticmethod
    def _history(src, dst):
        from src.models.history import MigrationHistoryItem, endpoint_fingerprint

        return MigrationHistoryItem(
            id=7,
            profile_id=1,
            status="failed",
            plan_version=1,
            source_fingerprint=endpoint_fingerprint(src),
            target_fingerprint=endpoint_fingerprint(dst),
        )

    def test_reentering_recorded_endpoint_saves_without_warning(self, window):
        question = self._edit_locked(window, self._history(SRC, DST), SRC, DST)
        question.assert_not_called()
        window.vm.update_profile.assert_called_once()

    def test_different_endpoint_than_recorded_still_warns(self, window):
        question = self._edit_locked(
            window, self._history(SRC, DST), SRC, dict(DST, database="temp")
        )
        question.assert_called_once()
        window.vm.update_profile.assert_not_called()

    def test_legacy_history_without_fingerprint_warns(self, window):
        from src.models.history import MigrationHistoryItem

        legacy = MigrationHistoryItem(id=7, profile_id=1, status="failed")
        question = self._edit_locked(window, legacy, SRC, DST)
        question.assert_called_once()
