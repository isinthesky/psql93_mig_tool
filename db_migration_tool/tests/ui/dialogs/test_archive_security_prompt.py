"""실행 전 아카이브 보안 확인 로직(창을 띄우지 않는 판단부)."""

from __future__ import annotations

import pytest

from src.core.archive_manifest import ArchiveSecurityInfo
from src.ui.dialogs.archive_security_prompt import (
    MODE_EXPORT,
    MODE_IMPORT,
    ArchiveSecurityChoice,
    resolve_archive_security,
)

NEW = ArchiveSecurityInfo(False, False, 0, ())
UNSIGNED = ArchiveSecurityInfo(True, False, 48, ())
UNSIGNED_NO_SUM = ArchiveSecurityInfo(True, False, 2, ("point_history_240101",))
SIGNED = ArchiveSecurityInfo(True, True, 3, ())


class Script:
    """ask/confirm/alert 응답을 순서대로 돌려주고 호출을 기록한다."""

    def __init__(self, answers=(), confirms=()):
        self.answers = list(answers)
        self.confirms = list(confirms)
        self.asked: list[str] = []
        self.confirmed: list[str] = []
        self.alerts: list[str] = []

    def ask(self, title, label):
        self.asked.append(title)
        return self.answers.pop(0)

    def confirm(self, title, message):
        self.confirmed.append(message)
        return self.confirms.pop(0)

    def alert(self, title, message):
        self.alerts.append(title)

    def run(self, mode, info):
        return resolve_archive_security(
            mode=mode,
            info=info,
            ask_passphrase=self.ask,
            confirm=self.confirm,
            alert=self.alert,
        )


NO_PASSPHRASE = ("", True)


def test_import_legacy_archive_needs_explicit_confirmation():
    script = Script(answers=[NO_PASSPHRASE], confirms=[True])
    assert script.run(MODE_IMPORT, UNSIGNED) == ArchiveSecurityChoice(None, True)
    assert script.confirmed and "인증 정보가 없습니다" in script.confirmed[0]


def test_import_legacy_archive_declined_does_not_start():
    assert Script(answers=[NO_PASSPHRASE], confirms=[False]).run(MODE_IMPORT, UNSIGNED) is None


def test_import_unsigned_archive_asks_for_export_passphrase_before_legacy_confirm():
    """리뷰 지적: 인증 없는 경로에서 passphrase를 묻지 않으면 auth를 지운 아카이브가
    legacy 확인 한 번으로 통과한다. 먼저 'export 때 passphrase를 지정했나'를 묻는다."""
    script = Script(answers=[NO_PASSPHRASE], confirms=[True])
    script.run(MODE_IMPORT, UNSIGNED)
    assert len(script.asked) == 1


def test_import_unsigned_archive_with_passphrase_is_refused_as_downgrade():
    script = Script(answers=[("pw", True)], confirms=[True])
    assert script.run(MODE_IMPORT, UNSIGNED) is None
    assert script.alerts == ["다운그레이드 의심"]
    assert not script.confirmed  # legacy 확인 창으로 넘어가지 않는다


def test_import_unsigned_archive_passphrase_prompt_cancelled_does_not_start():
    script = Script(answers=[("", False)], confirms=[True])
    assert script.run(MODE_IMPORT, UNSIGNED) is None
    assert not script.confirmed


def test_import_legacy_warning_lists_missing_checksums():
    script = Script(answers=[NO_PASSPHRASE], confirms=[True])
    script.run(MODE_IMPORT, UNSIGNED_NO_SUM)
    assert "point_history_240101" in script.confirmed[0]


def test_import_signed_archive_requires_passphrase_and_no_legacy_flag():
    script = Script(answers=[("pw", True)])
    assert script.run(MODE_IMPORT, SIGNED) == ArchiveSecurityChoice("pw", False)
    assert not script.confirmed


@pytest.mark.parametrize("answer", [("", True), ("pw", False)])
def test_import_signed_archive_cancelled_or_blank_does_not_start(answer):
    assert Script(answers=[answer]).run(MODE_IMPORT, SIGNED) is None


def test_export_new_archive_with_passphrase_confirms_twice():
    script = Script(answers=[("pw", True), ("pw", True)])
    assert script.run(MODE_EXPORT, NEW) == ArchiveSecurityChoice("pw", False)


def test_export_mismatched_confirmation_aborts():
    script = Script(answers=[("pw", True), ("px", True)])
    assert script.run(MODE_EXPORT, NEW) is None
    assert script.alerts


def test_export_blank_passphrase_means_unauthenticated():
    assert Script(answers=[("", True)]).run(MODE_EXPORT, NEW) == ArchiveSecurityChoice(None, False)


def test_export_signing_existing_unsigned_archive_requires_adoption_confirmation():
    script = Script(answers=[("pw", True), ("pw", True)], confirms=[True])
    assert script.run(MODE_EXPORT, UNSIGNED) == ArchiveSecurityChoice("pw", True)
    declined = Script(answers=[("pw", True), ("pw", True)], confirms=[False])
    assert declined.run(MODE_EXPORT, UNSIGNED) is None


def test_export_into_signed_archive_requires_its_passphrase():
    assert Script(answers=[("", True)]).run(MODE_EXPORT, SIGNED) is None
    assert Script(answers=[("pw", True)]).run(MODE_EXPORT, SIGNED) == ArchiveSecurityChoice(
        "pw", False
    )


# ── Qt 래퍼: 실제 아카이브 + 대화 상자 대체 ────────────────────────────────


def _signed_archive(tmp_path, passphrase="pw"):
    from src.core.archive_manifest import ArchiveManifestStore

    store = ArchiveManifestStore(tmp_path / "a", passphrase=passphrase, kdf_iterations=1000)
    store.load_or_create(source={"kind": "postgres"}, target={"kind": "file"})
    return str(tmp_path / "a")


def _patch_dialogs(monkeypatch, *, texts, yes: bool):
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from src.ui.dialogs import archive_security_prompt as mod

    answers = list(texts)
    alerts: list[str] = []
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: (answers.pop(0), True))
    button = QMessageBox.StandardButton.Yes if yes else QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: button)
    monkeypatch.setattr(QMessageBox, "critical", lambda _p, title, _m: alerts.append(title))
    return mod, alerts


def test_prompt_rejects_wrong_passphrase_before_start(tmp_path, monkeypatch):
    path = _signed_archive(tmp_path)
    mod, alerts = _patch_dialogs(monkeypatch, texts=["wrong"], yes=False)
    assert mod.prompt_archive_security(None, mode=MODE_IMPORT, archive_path=path) is None
    assert alerts == ["passphrase 확인 실패"]


def test_prompt_accepts_correct_passphrase(tmp_path, monkeypatch):
    path = _signed_archive(tmp_path)
    mod, alerts = _patch_dialogs(monkeypatch, texts=["pw"], yes=False)
    choice = mod.prompt_archive_security(None, mode=MODE_IMPORT, archive_path=path)
    assert choice == ArchiveSecurityChoice("pw", False)
    assert not alerts


def test_prompt_legacy_archive_confirmed(tmp_path, monkeypatch):
    from src.core.archive_manifest import ArchiveManifestStore

    ArchiveManifestStore(tmp_path / "a").load_or_create()
    mod, _ = _patch_dialogs(monkeypatch, texts=[""], yes=True)
    choice = mod.prompt_archive_security(None, mode=MODE_IMPORT, archive_path=str(tmp_path / "a"))
    assert choice == ArchiveSecurityChoice(None, True)


def test_prompt_stripped_auth_archive_refused_when_passphrase_entered(tmp_path, monkeypatch):
    """서명된 아카이브에서 auth를 지운 경우 — 사용자가 passphrase를 넣으면 시작하지 않는다."""
    import json

    from src.core.archive_manifest import ArchiveManifestStore

    path = _signed_archive(tmp_path)
    manifest_path = ArchiveManifestStore(path).manifest_path
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data.pop("auth")
    data["version"] = 2
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    mod, alerts = _patch_dialogs(monkeypatch, texts=["pw"], yes=True)
    assert mod.prompt_archive_security(None, mode=MODE_IMPORT, archive_path=path) is None
    assert alerts == ["다운그레이드 의심"]
