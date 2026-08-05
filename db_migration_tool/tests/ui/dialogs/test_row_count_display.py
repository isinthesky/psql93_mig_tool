"""행 수 표기 — 추정치를 정확한 값처럼 보이게 하지 않는다.

탐색은 파티션마다 `SELECT COUNT(*)`를 돌렸다. 이 도구가 다루는 파티션은
수백만 행이고 한 번에 수십~수백 개를 훑으므로 전수 스캔이 탐색 시간을
지배했다. 그 숫자는 목록·합계 표시에만 쓰이므로(건너뛰기·완료 판정은
워커가 따로 확인한다) 플래너 통계로 바꿨다.

바꾼 대가로 화면의 숫자가 추정치가 됐다. 그렇다면 추정치라고 써야 한다.
특히 통계가 없어 0으로 온 값을 '0 rows'로 쓰면 사용자가 빈 파티션으로
읽고 체크를 푼다 — 실제로는 수백만 행일 수 있다.
"""

from unittest.mock import MagicMock, patch

import pytest

import src.ui.dialogs.file_archive_migration_dialog as archive_mod
import src.ui.dialogs.migration_wizard_dialog as wizard_mod
from src.core.table_types import TableType
from src.ui.dialogs.scan_host import format_row_count


class TestFormatRowCount:
    def test_exact_counts_are_plain(self):
        assert format_row_count(1234, False) == "1,234 rows"

    def test_estimates_say_so(self):
        assert format_row_count(1234, True) == "약 1,234 rows"

    def test_missing_statistics_are_not_reported_as_empty(self):
        """ANALYZE 전이면 통계가 없다. 모른다는 것을 모른다고 쓴다."""
        assert format_row_count(0, True) == "행 수 미상"

    def test_an_exact_zero_is_still_zero(self):
        assert format_row_count(0, False) == "0 rows"

    def test_negative_estimates_never_leak(self):
        """PG14+는 미분석 테이블에 -1을 준다."""
        assert format_row_count(-1, True) == "행 수 미상"


def _row(name="t0", count=900, estimated=None):
    item = {"table_name": name, "row_count": count, "table_type": TableType.POINT_HISTORY}
    if estimated is not None:
        item["row_count_estimated"] = estimated
    return item


@pytest.fixture
def wizard(qapp):
    profile = MagicMock()
    profile.id = 1
    profile.name = "프로필"
    profile.source_kind = "postgres"
    profile.target_kind = "postgres"
    profile.source_config = {"host": "h"}
    profile.target_config = {"host": "h"}

    with (
        patch.object(wizard_mod, "HistoryManager") as history_manager,
        patch.object(wizard_mod, "CheckpointManager"),
        patch.object(wizard_mod.MigrationWizardDialog, "check_connections", lambda self: None),
    ):
        history_manager.return_value.get_incomplete_history.return_value = None
        dlg = wizard_mod.MigrationWizardDialog(None, profile)
    yield dlg
    dlg.deleteLater()


def _archive(mode, source_kind, target_kind):
    profile = MagicMock()
    profile.id = 1
    profile.name = "프로필"
    profile.migration_mode = mode
    profile.source_kind = source_kind
    profile.target_kind = target_kind
    profile.source_config = {"host": "h"}
    profile.target_config = {"archive_path": "c:/a"}

    with (
        patch.object(archive_mod, "HistoryManager") as history_manager,
        patch.object(archive_mod, "CheckpointManager"),
        patch.object(
            archive_mod.FileArchiveMigrationDialog, "check_connections", lambda self: None
        ),
    ):
        history_manager.return_value.get_incomplete_history.return_value = None
        return archive_mod.FileArchiveMigrationDialog(None, profile)


class TestWizardLabels:
    def test_estimated_rows_are_marked_in_the_list(self, wizard):
        wizard._on_discovery_result(wizard._scan_gen, [_row(estimated=True)])

        assert "약 900 rows" in wizard.partition_list.item(0).text()

    def test_the_selection_total_inherits_the_estimate(self, wizard):
        """하나라도 추정이 섞이면 합계도 추정이다.

        정확한 값처럼 보이면 사용자가 그 숫자로 용량과 시간을 계산한다.
        """
        wizard._on_discovery_result(
            wizard._scan_gen,
            [_row("t0", 900, estimated=True), _row("t1", 100, estimated=False)],
        )

        assert wizard.partition_rows_label.text() == "선택 약 1,000 rows"

    def test_a_partition_without_statistics_is_not_called_empty(self, wizard):
        wizard._on_discovery_result(wizard._scan_gen, [_row(count=0, estimated=True)])

        assert "행 수 미상" in wizard.partition_list.item(0).text()

    def test_nothing_selected_reads_as_zero_not_unknown(self, wizard):
        wizard._on_discovery_result(wizard._scan_gen, [_row(estimated=True)])
        wizard.partition_list.item(0).setCheckState(
            wizard_mod.Qt.CheckState.Unchecked  # type: ignore[attr-defined]
        )

        assert wizard.partition_rows_label.text() == "선택 0 rows"


class TestArchiveDirections:
    """아카이브 다이얼로그는 방향에 따라 행 수의 출처가 다르다."""

    def test_export_from_postgres_shows_estimates(self, qapp):
        dlg = _archive("postgres_to_file", "postgres", "file")
        try:
            dlg._on_discovery_result(dlg._scan_gen, [_row(estimated=True)])
            assert "약 900 rows" in dlg.partition_list.item(0).text()
        finally:
            dlg.deleteLater()

    def test_import_from_archive_shows_exact_counts(self, qapp):
        """매니페스트의 행 수는 내보낼 때 실제로 센 값이다. 낮춰 부르지 않는다."""
        dlg = _archive("file_to_postgres", "file", "postgres")
        try:
            dlg._on_discovery_result(dlg._scan_gen, [_row()])
            text = dlg.partition_list.item(0).text()
            assert "900 rows" in text
            assert "약" not in text
        finally:
            dlg.deleteLater()


class TestDiscoveryMarksItsCounts:
    def test_discovery_no_longer_scans_every_partition(self):
        """`COUNT(*)`가 남아 있으면 탐색이 다시 분 단위로 늘어난다.

        문서 문자열이 아니라 실제로 실행되는 SQL을 본다 — 이 함수의
        설명에는 옛 `COUNT(*)` 이야기가 그대로 들어 있다.
        """
        import ast
        import inspect
        import textwrap

        from src.core.partition_discovery import PartitionDiscovery

        tree = ast.parse(textwrap.dedent(inspect.getsource(PartitionDiscovery._estimate_row_count)))
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        # 첫 문장이 문서 문자열이면 빼고 나머지 문자열 리터럴만 본다.
        body = func.body[1:] if ast.get_docstring(func) else func.body
        literals = [
            node.value
            for stmt in body
            for node in ast.walk(stmt)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        sql = "\n".join(literals)
        assert "reltuples" in sql
        assert "COUNT(*)" not in sql

    def test_estimates_are_clamped_at_zero(self):
        """PG14+의 미분석 테이블은 -1을 준다. 합계가 음수로 깎이면 안 된다."""
        from src.core.partition_discovery import PartitionDiscovery

        cursor = MagicMock()
        cursor.fetchone.return_value = (-1,)

        assert PartitionDiscovery({})._estimate_row_count(cursor, "t0") == 0

    def test_a_missing_table_reads_as_zero(self):
        from src.core.partition_discovery import PartitionDiscovery

        cursor = MagicMock()
        cursor.fetchone.return_value = None

        assert PartitionDiscovery({})._estimate_row_count(cursor, "t0") == 0
