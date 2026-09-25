"""재개 전 확인(H-08/H-09)을 두 마이그레이션 다이얼로그가 같은 방식으로 거치게 한다.

규칙 자체는 `HistoryManager.prepare_resume()`에 있다. 여기서는 그 판정을 사용자에게
알리고, legacy 이력의 명시적 확인과 차단된 이력의 명시적 폐기를 묻는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMessageBox, QWidget

from src.models.history import ResumeCheck, ResumeVerdict, endpoint_label

if TYPE_CHECKING:
    from src.models.history import HistoryManager
    from src.models.profile import ConnectionProfile

_BLOCKED_HINT = (
    "\n\n이어서 진행하려면 프로필의 연결 정보를 원래 작업 때와 같게 되돌리세요"
    "(비밀번호 변경은 허용됩니다).\n원래 작업을 더 이상 이어가지 않을 거라면 폐기할 수 있습니다."
)


def _offer_abandon(
    parent: QWidget | None, history_manager: HistoryManager, history_id: int
) -> None:
    reply = QMessageBox.question(
        parent,
        "미완료 작업 폐기",
        "이 미완료 작업을 폐기할까요?\n\n"
        "폐기한 작업은 다시 이어서 진행할 수 없습니다.\n"
        "대상에 이미 옮긴 데이터는 지워지지 않습니다.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if reply == QMessageBox.StandardButton.Yes:
        history_manager.abandon_history(history_id)


def resolve_resume(
    parent: QWidget | None,
    history_manager: HistoryManager,
    history_id: int,
    profile: ConnectionProfile,
) -> ResumeCheck | None:
    """재개해도 되면 검증 결과(`pending`, `supplemented`)를, 아니면 None을 돌려준다.

    None이면 안내는 이미 했다. 호출자는 재개 상태로 들어가지 않고, 폐기됐을 수 있으니
    미완료 작업 표시를 다시 읽는다.
    """
    try:
        check = history_manager.prepare_resume(history_id, profile)
    except Exception as exc:  # 판정을 못 했으면 재개하지 않는다(대상 분산 방지)
        QMessageBox.critical(parent, "재개 확인 실패", f"재개 전 검증에 실패했습니다.\n\n{exc}")
        return None

    if check.verdict is ResumeVerdict.LEGACY:
        reply = QMessageBox.question(
            parent,
            "이전 버전 작업 재개",
            f"{check.message}\n\n"
            f"미완료 파티션 {len(check.pending):,}개를 현재 프로필의 연결로 이어서 진행합니다.\n"
            f"현재 연결: {endpoint_label(profile.source_config)} → "
            f"{endpoint_label(profile.target_config)}\n\n"
            "원래 작업과 같은 원본·대상이 맞는지 확인하셨습니까?\n\n"
            "예를 누르면 현재 연결과 이 작업에 남아 있는 파티션 목록이 계획으로 고정되고,\n"
            "이후에는 연결이 바뀌면 재개가 거부됩니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return None
        try:
            check = history_manager.adopt_legacy_history(history_id, profile)
        except ValueError as exc:
            QMessageBox.warning(parent, "재개할 수 없습니다", str(exc))
            _offer_abandon(parent, history_manager, history_id)
            return None

    if check.allowed:
        return check

    QMessageBox.warning(parent, "재개할 수 없습니다", f"{check.message}{_BLOCKED_HINT}")
    if check.verdict is not ResumeVerdict.NOT_FOUND:
        _offer_abandon(parent, history_manager, history_id)
    return None
