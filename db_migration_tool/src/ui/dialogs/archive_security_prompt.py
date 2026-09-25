"""파일 아카이브 실행 전 보안 확인(passphrase·legacy 허용).

신뢰 경계와 정책은 ``docs/base/archive-trust-boundary.md``. passphrase는 이 대화 상자에서
받아 워커에 넘길 뿐 저장·로그하지 않는다(재개할 때마다 다시 묻는다).

판단 로직(:func:`resolve_archive_security`)은 Qt 대화 상자와 분리해 두었다 — 묻고 확인하는
함수만 주입하면 되므로 창을 띄우지 않고 테스트할 수 있다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.core.archive_manifest import ArchiveManifestStore, ArchiveSecurityInfo, ManifestAuthError

MODE_EXPORT = "postgres_to_file"
MODE_IMPORT = "file_to_postgres"

# (title, label) -> (입력값, OK 여부)
AskPassphrase = Callable[[str, str], tuple[str, bool]]
# (title, message) -> 예/아니오
Confirm = Callable[[str, str], bool]
# (title, message) -> None
Alert = Callable[[str, str], None]


@dataclass(frozen=True)
class ArchiveSecurityChoice:
    passphrase: str | None
    allow_legacy_unverified: bool


def _missing_checksum_note(info: ArchiveSecurityInfo) -> str:
    missing = info.partitions_without_checksum
    if not missing:
        return ""
    head = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
    return (
        f"\n\nchecksum이 없는 파티션 {len(missing)}개는 파일 무결성도 검증할 수 없습니다"
        f"(크기·행 수만 확인): {head}"
    )


def resolve_archive_security(
    *,
    mode: str,
    info: ArchiveSecurityInfo,
    ask_passphrase: AskPassphrase,
    confirm: Confirm,
    alert: Alert,
) -> ArchiveSecurityChoice | None:
    """실행에 쓸 passphrase와 legacy 허용 여부를 정한다. 사용자가 취소하면 None."""
    if mode == MODE_EXPORT:
        return _resolve_export(info, ask_passphrase, confirm, alert)
    return _resolve_import(info, ask_passphrase, confirm, alert)


def _resolve_export(
    info: ArchiveSecurityInfo, ask: AskPassphrase, confirm: Confirm, alert: Alert
) -> ArchiveSecurityChoice | None:
    if info.signed:
        text, ok = ask(
            "아카이브 passphrase",
            "이 아카이브는 passphrase로 보호되어 있습니다.\nexport 때 쓴 passphrase를 입력하세요:",
        )
        if not ok or not text:
            return None
        return ArchiveSecurityChoice(text, False)

    text, ok = ask(
        "아카이브 passphrase (권장)",
        "manifest를 인증할 passphrase를 입력하세요.\n"
        "비워 두면 인증 없이 저장되며, 가져올 때 별도 확인이 필요합니다.\n"
        "passphrase는 저장되지 않습니다. 잊으면 인증된 가져오기를 할 수 없습니다.",
    )
    if not ok:
        return None
    if not text:
        return ArchiveSecurityChoice(None, False)

    again, ok = ask("passphrase 확인", "같은 passphrase를 한 번 더 입력하세요:")
    if not ok:
        return None
    if again != text:
        alert("passphrase 불일치", "두 번 입력한 passphrase가 다릅니다. 다시 시작하세요.")
        return None

    if info.partition_count:
        adopt = confirm(
            "인증 없는 기존 아카이브",
            f"이 폴더에는 인증 없이 저장된 파티션 {info.partition_count}개가 있습니다.\n"
            "passphrase를 지정하면 지금 폴더 내용 그대로 서명(채택)되어 이후에는 "
            "인증된 것으로 취급됩니다.\n\n새 폴더로 export 하는 것을 권장합니다. "
            "그래도 이 폴더의 기존 내용을 신뢰하고 계속할까요?" + _missing_checksum_note(info),
        )
        if not adopt:
            return None
        return ArchiveSecurityChoice(text, True)
    return ArchiveSecurityChoice(text, False)


def _resolve_import(
    info: ArchiveSecurityInfo, ask: AskPassphrase, confirm: Confirm, alert: Alert
) -> ArchiveSecurityChoice | None:
    if not info.exists:
        # manifest가 없으면 워커가 명확한 오류로 멈춘다. 여기서 막을 일은 없다.
        return ArchiveSecurityChoice(None, False)

    if info.signed:
        text, ok = ask(
            "아카이브 passphrase",
            "이 아카이브는 passphrase로 보호되어 있습니다.\nexport 때 쓴 passphrase를 입력하세요:",
        )
        if not ok or not text:
            return None
        allow = False
        if info.partitions_without_checksum:
            allow = confirm(
                "checksum 없는 파티션",
                "manifest는 인증되지만 일부 파티션에 checksum이 없습니다."
                + _missing_checksum_note(info)
                + "\n\n계속할까요?",
            )
            if not allow:
                return None
        return ArchiveSecurityChoice(text, allow)

    # 인증 정보가 없다. auth 블록을 지운 서명 아카이브(다운그레이드)와 진짜 legacy는 파일만으로
    # 구분할 수 없으므로, 사용자가 아는 사실(export 때 passphrase를 지정했는지)을 먼저 묻는다.
    text, ok = ask(
        "인증 정보 없는 아카이브",
        "이 아카이브의 manifest에는 인증 정보가 없습니다.\n"
        "export 때 passphrase를 지정했다면 입력하세요(인증 정보가 제거된 것이므로 거부됩니다).\n"
        "지정하지 않았다면(1.2.7 이하 export 포함) 비워 두고 확인을 누르세요.",
    )
    if not ok:
        return None
    if text:
        alert(
            "다운그레이드 의심",
            "passphrase로 export한 아카이브인데 manifest에 인증 정보가 없습니다.\n"
            "인증 정보가 제거되었거나 다른 아카이브로 바뀌었을 수 있어 가져오지 않습니다.\n"
            "원본 매체에서 아카이브를 다시 받으세요.",
        )
        return None

    accepted = confirm(
        "인증 없는 아카이브 가져오기",
        "이 아카이브의 manifest에는 인증 정보가 없습니다(1.2.7 이하에서 만들었거나 "
        "passphrase 없이 export).\nmanifest와 데이터 파일이 함께 바뀌어도 알아챌 수 없습니다.\n\n"
        "직접 만들었거나 신뢰할 수 있는 매체로 옮긴 아카이브일 때만 계속하세요.\n"
        "계속하면 이 확인이 작업 로그에 경고로 남습니다." + _missing_checksum_note(info),
    )
    if not accepted:
        return None
    return ArchiveSecurityChoice(None, True)


def prompt_archive_security(
    parent, *, mode: str, archive_path: str
) -> ArchiveSecurityChoice | None:
    """Qt 대화 상자로 묻는다. 취소·실패하면 None(호출자는 시작하지 않는다)."""
    from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox

    def ask(title: str, label: str) -> tuple[str, bool]:
        text, ok = QInputDialog.getText(parent, title, label, QLineEdit.EchoMode.Password)
        return text, bool(ok)

    def confirm(title: str, message: str) -> bool:
        answer = QMessageBox.warning(
            parent,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def alert(title: str, message: str) -> None:
        QMessageBox.critical(parent, title, message)

    try:
        info = ArchiveManifestStore(archive_path).inspect_security()
    except Exception as exc:
        alert("아카이브 확인 실패", f"manifest를 읽지 못했습니다: {exc}")
        return None

    choice = resolve_archive_security(
        mode=mode, info=info, ask_passphrase=ask, confirm=confirm, alert=alert
    )
    if choice is None or choice.passphrase is None or not info.signed:
        return choice

    # 서명된 아카이브는 여기서 바로 검증해 오타를 실행 전에 알린다.
    try:
        ArchiveManifestStore(archive_path, passphrase=choice.passphrase).load()
    except ManifestAuthError as exc:
        alert("passphrase 확인 실패", str(exc))
        return None
    return choice
