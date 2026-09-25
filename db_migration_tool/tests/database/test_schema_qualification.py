"""조회 경로의 schema 한정(감사 H-04).

카탈로그에서 `public`을 골라 놓고 실제 조회는 이름만으로 하면, `search_path` 선행 스키마
(`$user` 등)의 동명 객체를 읽는다. 존재 확인과 실제 조회가 서로 다른 테이블을 보게 된다.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from unittest.mock import MagicMock, patch

from psycopg import sql

import src.core.scan_workers as scan_mod
from src.database.postgres_utils import PostgresOptimizer
from src.database.version_info import PgVersionFamily, PgVersionInfo
from src.database.version_sql import SQL_TEMPLATES
from tests.database.fake_pg import FakePgConnection

PG93 = PgVersionInfo(major=9, minor=3, full_version="9.3.25", family=PgVersionFamily.PG_9_3)
PG16 = PgVersionInfo(major=16, minor=0, full_version="16.4", family=PgVersionFamily.PG_16)


def _rendered(call) -> str:
    query = call.args[0]
    if isinstance(query, sql.Composable):
        return query.as_string()
    return " ".join(str(query).split())


class TestEstimateTableSize:
    def _run(self, version):
        seen: list[tuple[str, object]] = []

        def responder(sql_text, params):
            seen.append((sql_text, params))
            if "information_schema.tables" in sql_text:
                return [(True,)]
            return [(10, 8192)]

        conn = FakePgConnection(responder, flavor="psycopg2")
        info = PostgresOptimizer.estimate_table_size(conn, "point_history_260504", version)
        return info, seen

    def test_size_is_taken_from_the_public_oid_not_a_name_lookup(self):
        """`pg_table_size('name')`은 regclass 변환이 search_path를 따른다."""
        for version in (PG93, PG16):
            info, seen = self._run(version)
            estimate_sql, params = seen[-1]
            assert "(%s)" not in estimate_sql.replace("relname = %s", ""), estimate_sql
            assert "c.oid" in estimate_sql
            assert params == ("point_history_260504",)
            assert info["row_count"] == 10
            assert info["total_size_bytes"] == 8192

    def test_every_template_resolves_size_by_oid(self):
        for version, queries in SQL_TEMPLATES.items():
            estimate = queries["estimate_size"]
            assert "_size(c.oid)" in estimate, version
            assert "nspname = 'public'" in estimate, version

    def test_no_template_interpolates_an_unqualified_table(self):
        """`{table}` 자리표시자는 호출자가 schema 없이 이름을 끼워 넣게 만든다."""
        for version, queries in SQL_TEMPLATES.items():
            for name, template in queries.items():
                assert "{table}" not in template, f"{version}.{name}"

    def test_missing_catalog_row_is_a_lookup_failure_not_absence(self):
        def responder(sql_text, _params):
            if "information_schema.tables" in sql_text:
                return [(True,)]
            return []

        conn = FakePgConnection(responder, flavor="psycopg2")
        info = PostgresOptimizer.estimate_table_size(conn, "t", PG16)
        assert info["exists"] is True
        assert info["lookup_failed"] is True


class TestCopyPrivilegeProbe:
    def test_probe_table_lives_in_pg_temp(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        ok, _ = PostgresOptimizer._probe_copy_privilege(conn, check_write=True)

        assert ok
        statements = [" ".join(str(c.args[0]).split()) for c in cursor.execute.call_args_list]
        statements += [" ".join(str(c.args[0]).split()) for c in cursor.copy_expert.call_args_list]
        for stmt in statements:
            assert "pg_temp.dbmig_copy_probe" in stmt, stmt


class TestScanWorkers:
    def test_target_completed_scan_reads_public_relation(self):
        worker = scan_mod.TargetCompletedScanWorker(1, "postgres", {"host": "h"}, ["p1"])
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [(True,), (1,)]

        with patch.object(scan_mod.psycopg, "connect", return_value=conn):
            assert worker.execute() == {"p1": True}

        rendered = [_rendered(c) for c in cur.execute.call_args_list]
        assert 'SELECT 1 FROM "public"."p1" LIMIT 1' in rendered

    def test_row_count_verify_counts_public_relations_on_both_sides(self):
        worker = scan_mod.RowCountVerifyWorker(0, {"host": "s"}, {"host": "t"}, ["p1"])
        source, target = MagicMock(), MagicMock()
        for conn in (source, target):
            conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (3,)

        with patch.object(scan_mod.psycopg, "connect", side_effect=[source, target]):
            result = worker.execute()

        assert result[0]["ok"] is True
        for conn in (source, target):
            cur = conn.cursor.return_value.__enter__.return_value
            rendered = [_rendered(c) for c in cur.execute.call_args_list]
            assert rendered == ['SELECT COUNT(*) FROM "public"."p1"']

    def test_scan_worker_source_has_no_single_part_identifier(self):
        """`sql.Identifier(name)` 한 조각은 schema 없는 relation이다."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(scan_mod)))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Identifier"
            ):
                assert len(node.args) >= 2, ast.unparse(node)
