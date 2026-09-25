"""재개 전 확인(H-08/H-09)을 두 마이그레이션 다이얼로그가 같은 방식으로 거치게 한다.

규칙 자체는 `HistoryManager.prepare_resume()`에 있다. 여기서는 그 판정을 사용자에게
알리고, legacy 이력의 명시적 확인과 차단된 이력의 명시적 폐기를 묻는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QMessageBox,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from src.core.table_types import TABLE_TYPE_CONFIG, get_table_type
from src.models.history import (
    LegacyCoverage,
    ResumeCheck,
    ResumeVerdict,
    endpoint_label,
    legacy_creation_order,
    legacy_supplement_note,
)

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


def _type_label(table_name: str) -> str:
    table_type = get_table_type(table_name)
    return f"{TABLE_TYPE_CONFIG[table_type].display_name} ({table_type.value}, {table_name})"


class LegacyTypeChooser(QDialog):
    """legacy 이력의 원래 작업에 어떤 항목(테이블 유형)이 있었는지 고른다(H-08 리뷰).

    구버전은 checkpoint를 유형 코드 순서로 만들었으므로, checkpoint가 없는 **뒤 유형**은
    원래 선택하지 않았을 수도, 끊겨서 통째로 빠졌을 수도 있다. 로컬 기록으로는 구분할 수
    없어 사용자가 유형마다 '있었음(보충)'/'없었음(제외)'을 고른다.

    **기본값이 없다**(리뷰 라운드 1). 기본 포함은 가장 흔한 PH 단일 유형 이력을 뒤 유형 범위
    전체의 보충으로 바꾸고, import 경로에서는 대상 파티션을 묻지 않고 비운다. 기본 제외는
    끊긴 다중 유형 작업을 subset 완료로 되돌린다. 그래서 모든 유형을 정하기 전에는 '확인'이
    꺼져 있고, 정하지 않은 채 닫히면 아무것도 기록하지 않는다.
    checkpoint가 있는 유형은 항상 포함한다. 순서상 앞의 빈 유형과 이력이 시작된 날에 없던
    유형은 원래 작업에 있을 수 없어 묻지 않는다.
    """

    def __init__(
        self, parent: QWidget | None, coverage: LegacyCoverage, migration_mode: str | None = None
    ):
        super().__init__(parent)
        self.setWindowTitle("원래 작업의 항목 확인")
        self._represented = list(coverage.represented_types)
        order = "→".join(get_table_type(t).value for t in legacy_creation_order())
        present = ", ".join(_type_label(t) for t in self._represented) or "(없음)"

        layout = QVBoxLayout(self)
        lines = [
            "이 작업은 이전 버전에서 만들어져 어떤 항목을 골랐는지 기록이 없습니다.",
            f"체크포인트가 남은 항목(항상 포함): {present}",
        ]
        if coverage.unavailable_types:
            lines.append(
                "이 작업을 시작할 때 도구에 없던 항목(제외): "
                + ", ".join(_type_label(t) for t in coverage.unavailable_types)
            )
        lines += [
            "",
            f"이전 버전은 항목 순서({order})로 체크포인트를 만들었기 때문에, 중간에 끊겼다면 "
            "아래 항목이 통째로 빠졌을 수 있습니다. 항목마다 원래 작업에 있었는지 고르세요. "
            "기본값은 없습니다. 모르면 취소하고 원래 작업의 기록을 확인하세요.",
            "",
            "'있었음'을 고른 항목은 기록된 날짜 범위 전체를 계획에 보충합니다. "
            + legacy_supplement_note(migration_mode),
        ]
        if migration_mode == "file_to_postgres":
            lines.append(
                "원래 작업에 없던 항목을 '있었음'으로 고르면 그 항목의 대상 데이터(아카이브 "
                "이후에 쌓인 행 포함)가 아카이브 내용으로 덮어써질 수 있습니다."
            )
        self.intro = QLabel("\n".join(lines))
        self.intro.setWordWrap(True)
        layout.addWidget(self.intro)

        # 유형마다 (있었음, 없었음) 라디오 한 쌍. 둘 다 꺼진 상태로 시작한다.
        self.choices: dict[str, tuple[QRadioButton, QRadioButton]] = {}
        grid = QGridLayout()
        for row, (table_name, names) in enumerate(coverage.trailing_types.items()):
            include = QRadioButton("있었음(보충)")
            exclude = QRadioButton("없었음(제외)")
            group = QButtonGroup(self)
            group.addButton(include)
            group.addButton(exclude)
            include.toggled.connect(self._refresh)
            exclude.toggled.connect(self._refresh)
            grid.addWidget(
                QLabel(f"{_type_label(table_name)} — 범위 후보 {len(names):,}개"), row, 0
            )
            grid.addWidget(include, row, 1)
            grid.addWidget(exclude, row, 2)
            self.choices[table_name] = (include, exclude)
        layout.addLayout(grid)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert ok_button is not None
        self.ok_button = ok_button
        self._refresh()

    def decided(self) -> bool:
        """모든 뒤 유형을 '있었음'이나 '없었음'으로 정했는가."""
        return all(inc.isChecked() or exc.isChecked() for inc, exc in self.choices.values())

    def _refresh(self, *_args: object) -> None:
        self.ok_button.setEnabled(self.decided())

    def chosen_types(self) -> list[str] | None:
        """원래 작업에 포함된 유형(checkpoint가 있는 유형 + '있었음' 유형). 덜 정했으면 None."""
        if not self.decided():
            return None
        included = [t for t, (inc, _exc) in self.choices.items() if inc.isChecked()]
        return sorted({*self._represented, *included})


def choose_original_types(
    parent: QWidget | None, coverage: LegacyCoverage, migration_mode: str | None = None
) -> list[str] | None:
    """원래 작업의 유형을 고르게 한다. 취소했거나 다 정하지 않았으면 None(아무것도 쓰지 않음)."""
    chooser = LegacyTypeChooser(parent, coverage, migration_mode)
    if chooser.exec() != QDialog.DialogCode.Accepted:
        return None
    return chooser.chosen_types()


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
        coverage = check.legacy
        if coverage is None or not coverage.range_ok:
            # 범위를 모르면 누락 여부를 확인할 수 없다 — 남은 일부로 계획을 고정하지 않는다.
            QMessageBox.warning(parent, "재개할 수 없습니다", check.message)
            _offer_abandon(parent, history_manager, history_id)
            return None
        original_types: list[str] | None = None
        if coverage.undecided_types:
            # 유형 경계에서 끊긴 다중 유형 작업은 누락이 gaps로 드러나지 않는다(H-08 리뷰).
            # 뒤 유형을 원래 작업에 넣을지 먼저 정하고, 그 결정으로 누락을 다시 계산한다.
            original_types = choose_original_types(parent, coverage, profile.migration_mode)
            if original_types is None:
                return None
            try:
                check = history_manager.prepare_resume(
                    history_id, profile, original_types=original_types
                )
            except Exception as exc:
                QMessageBox.critical(
                    parent, "재개 확인 실패", f"재개 전 검증에 실패했습니다.\n\n{exc}"
                )
                return None
            coverage = check.legacy
            if check.verdict is not ResumeVerdict.LEGACY or coverage is None:
                # 확인하는 사이 다른 창이 채택했거나 이력이 사라졌다 — 일반 판정으로 넘긴다.
                return _finish(parent, history_manager, history_id, check)
        gaps = list(coverage.gaps)
        total = len(check.pending) + len(gaps)
        supplement_note = f"(누락 보충 {len(gaps):,}개 포함)" if gaps else ""
        reply = QMessageBox.question(
            parent,
            "이전 버전 작업 재개",
            f"{check.message}\n\n"
            f"미완료 파티션 {total:,}개{supplement_note}를 현재 프로필의 연결로 이어서 진행합니다.\n"
            f"현재 연결: {endpoint_label(profile.source_config)} → "
            f"{endpoint_label(profile.target_config)}\n\n"
            "원래 작업과 같은 원본·대상이 맞는지 확인하셨습니까?\n\n"
            "예를 누르면 현재 연결과 이 작업의 파티션 목록(남은 체크포인트와 범위 대비 누락 "
            "보충분)이 계획으로 고정되고,\n이후에는 연결이 바뀌면 재개가 거부됩니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return None
        try:
            # 누락(checkpoint가 있는 유형 + 포함하기로 한 뒤 유형)은 반드시 보충한다
            # (H-09 subset 완료 방지).
            check = history_manager.adopt_legacy_history(
                history_id, profile, supplement=gaps, original_types=original_types
            )
        except ValueError as exc:
            QMessageBox.warning(parent, "재개할 수 없습니다", str(exc))
            _offer_abandon(parent, history_manager, history_id)
            return None

    return _finish(parent, history_manager, history_id, check)


def _finish(
    parent: QWidget | None, history_manager: HistoryManager, history_id: int, check: ResumeCheck
) -> ResumeCheck | None:
    if check.allowed:
        return check

    QMessageBox.warning(parent, "재개할 수 없습니다", f"{check.message}{_BLOCKED_HINT}")
    if check.verdict is not ResumeVerdict.NOT_FOUND:
        _offer_abandon(parent, history_manager, history_id)
    return None
