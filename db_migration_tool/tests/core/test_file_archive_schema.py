"""아카이브 워커 조회·COPY 경로의 schema 한정(감사 H-04, 리뷰 후속).

- `information_schema.columns`는 search_path와 무관하게 모든 스키마의 컬럼을 보여 준다.
  `table_schema` 조건이 없으면 동명 부모 테이블이 있는 다른 스키마의 컬럼이 manifest에
  섞인다(연결의 search_path=public으로도 막히지 않는다).
- COUNT·COPY TO·COPY FROM·partition_table_info 조회는 `"public"."name"`으로 한정한다.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import src.core.file_archive_workers as archive_mod
from src.core.file_archive_workers import ArchiveMigrationWorkerBase
from src.core.table_types import TableType


class _Cursor:
    def __init__(self, conn: _Conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        self._conn.executed.append((text, params))
        self._rows = self._conn.responder(text, params)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, responder):
        self.responder = responder
        self.executed: list[tuple[str, object]] = []

    def cursor(self):
        return _Cursor(self)


# information_schema.columns가 돌려주는 행 흉내: 같은 이름의 부모가 public과 shadow에 있다.
_CATALOG = [
    ("public", "point_history", "path_id", "integer", None, "NO", None, 1),
    ("zz_shadow", "point_history", "x", "integer", None, "YES", None, 1),
    ("public", "point_history", "issued_date", "bigint", None, "NO", None, 2),
]


def _information_schema(text: str, params) -> list[tuple]:
    rows = [r for r in _CATALOG if r[1] == params[0]]
    if "table_schema = 'public'" in text:
        rows = [r for r in rows if r[0] == "public"]
    rows.sort(key=lambda r: r[7])
    return [r[2:7] for r in rows]


class TestParentColumns:
    def test_only_public_parent_columns_reach_the_manifest(self):
        conn = _Conn(_information_schema)

        cols = ArchiveMigrationWorkerBase._query_parent_columns(conn, "point_history")

        assert [c["name"] for c in cols] == ["path_id", "issued_date"]
        text, params = conn.executed[0]
        assert "table_schema = 'public'" in text
        assert params == ("point_history",)


class TestPartitionQueries:
    def test_row_count_reads_the_public_relation(self):
        conn = _Conn(lambda _t, _p: [(7,)])

        assert ArchiveMigrationWorkerBase._query_row_count(conn, "point_history_260504") == 7
        assert conn.executed[0][0] == 'SELECT COUNT(*) FROM "public"."point_history_260504"'

    def test_partition_meta_reads_public_partition_table_info(self):
        conn = _Conn(lambda _t, _p: [("PH", 1, 2)])

        meta = ArchiveMigrationWorkerBase._query_partition_meta(
            conn, "point_history_260504", TableType.POINT_HISTORY
        )

        assert meta == {"table_data": "PH", "from_date": 1, "to_date": 2}
        text, params = conn.executed[0]
        assert "FROM public.partition_table_info" in text
        assert params == ("point_history_260504",)


class TestCopyStatements:
    def test_export_copy_selects_from_the_public_relation(self):
        stmt = ArchiveMigrationWorkerBase._export_copy_sql(
            TableType.POINT_HISTORY, "point_history_260504"
        )

        assert stmt.startswith(
            'COPY (SELECT "path_id", "issued_date", "changed_value", "connection_status" '
            'FROM "public"."point_history_260504" ORDER BY '
        ), stmt
        assert stmt.endswith(") TO STDOUT WITH (FORMAT CSV, HEADER FALSE)")

    def test_import_copy_loads_into_the_public_relation(self):
        stmt = ArchiveMigrationWorkerBase._import_copy_sql(
            "point_history_260504", ["path_id", "issued_date"]
        )

        assert stmt == (
            'COPY "public"."point_history_260504" ("path_id", "issued_date") '
            "FROM STDIN WITH (FORMAT CSV, HEADER FALSE)"
        )

    def test_identifiers_are_quoted_not_interpolated(self):
        stmt = ArchiveMigrationWorkerBase._import_copy_sql('we"ird', ['c"ol'])

        assert stmt.startswith('COPY "public"."we""ird" ("c""ol")')


def test_module_has_no_single_part_relation_identifier():
    """`sql.Identifier(name)` 한 조각은 schema 없는 relation이다(컬럼도 quote_ident로 통일)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(archive_mod)))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Identifier"
        ):
            assert len(node.args) >= 2, ast.unparse(node)
