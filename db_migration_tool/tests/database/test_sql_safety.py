"""SQL 경계 공용 헬퍼(감사 H-04 / M-01 / M-14).

- 식별자 quote와 schema 한정: `search_path` 선행 스키마의 동명 객체를 잡지 않게 한다.
- SQLSTATE 판별: psycopg(3)와 psycopg2 예외를 같은 규칙으로 분류한다.
- SAVEPOINT 격리: 실패해도 되는 문장이 트랜잭션 전체를 중단(aborted)시키지 않게 한다.
- 커밋 가드: 중단된 트랜잭션에 COMMIT을 보내 '조용한 ROLLBACK'을 성공으로 오인하지 않게 한다.
"""

from __future__ import annotations

import psycopg
import psycopg2
import psycopg2.errors
import pytest

from src.database.postgres_utils import (
    PUBLIC_SCHEMA,
    commit_or_raise,
    is_db_error,
    isolated_statement,
    qualified_name,
    quote_ident,
    run_optional_statement,
    sqlstate_of,
)
from tests.database.fake_pg import INERROR, FakePgConnection

FLAVORS = ["psycopg", "psycopg2"]


class TestIdentifierQuoting:
    def test_plain_name_is_double_quoted(self):
        assert quote_ident("point_history_260504") == '"point_history_260504"'

    def test_embedded_quote_is_doubled(self):
        assert quote_ident('we"ird') == '"we""ird"'

    def test_case_is_preserved(self):
        # quote하면 대소문자를 접지 않는다. 카탈로그 이름 그대로 가리킨다.
        assert quote_ident("Mixed") == '"Mixed"'

    @pytest.mark.parametrize("bad", ["", "a\x00b", None, 123])
    def test_invalid_names_are_rejected(self, bad):
        with pytest.raises(ValueError):
            quote_ident(bad)

    def test_qualified_name_defaults_to_public(self):
        assert PUBLIC_SCHEMA == "public"
        assert qualified_name("point_history") == '"public"."point_history"'

    def test_qualified_name_with_schema(self):
        assert qualified_name("t", schema="s x") == '"s x"."t"'


class TestSqlstate:
    def test_psycopg3_class_sqlstate(self):
        assert sqlstate_of(psycopg.errors.DuplicateTable("x")) == "42P07"

    def test_psycopg2_class_without_server_pgcode(self):
        # 드라이버 밖에서 만든 psycopg2 예외는 pgcode가 None이다. 클래스로 역조회한다.
        exc = psycopg2.errors.DuplicateTable("x")
        assert exc.pgcode is None
        assert sqlstate_of(exc) == "42P07"

    def test_psycopg2_query_canceled(self):
        assert sqlstate_of(psycopg2.errors.QueryCanceled("x")) == "57014"

    def test_non_db_error_has_no_sqlstate(self):
        assert sqlstate_of(RuntimeError("x")) is None
        assert not is_db_error(RuntimeError("x"))

    @pytest.mark.parametrize("flavor", FLAVORS)
    def test_is_db_error_both_drivers(self, flavor):
        conn = FakePgConnection(flavor=flavor)
        assert is_db_error(conn.error("UndefinedTable"))


class TestIsolatedStatement:
    @pytest.mark.parametrize("flavor", FLAVORS)
    def test_failure_inside_savepoint_leaves_transaction_usable(self, flavor):
        def responder(sql_text, _params):
            if sql_text.startswith("BAD"):
                raise conn.error("DuplicateTable")
            return [(1,)]

        conn = FakePgConnection(responder, flavor=flavor)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            with pytest.raises((psycopg.Error, psycopg2.Error)):
                with isolated_statement(conn, cur):
                    cur.execute("BAD statement")
            # 트랜잭션이 복구되어 다음 문장이 돈다.
            cur.execute("SELECT 2")
        assert conn.status != INERROR
        conn.commit()
        assert conn.silent_rollbacks == 0
        assert conn.committed == ["SELECT 1", "SELECT 2"]

    def test_without_savepoint_the_transaction_is_aborted(self):
        """대조군: 격리 없이 실패를 삼키면 이후 문장이 전부 거부된다(M-01/M-14의 원인)."""
        conn = FakePgConnection(
            lambda s, _p: (_ for _ in ()).throw(conn.error("DuplicateTable")) if "BAD" in s else []
        )
        with conn.cursor() as cur:
            try:
                cur.execute("BAD")
            except psycopg.Error:
                pass
            with pytest.raises(psycopg.errors.InFailedSqlTransaction):
                cur.execute("SELECT 2")

    def test_autocommit_skips_savepoint(self):
        conn = FakePgConnection(autocommit=True)
        with conn.cursor() as cur, isolated_statement(conn, cur):
            cur.execute("SELECT 1")
        assert not any(s.startswith("SAVEPOINT") for s in conn.statements)

    def test_success_releases_savepoint(self):
        conn = FakePgConnection()
        with conn.cursor() as cur, isolated_statement(conn, cur, name="sp_x"):
            cur.execute("SELECT 1")
        assert conn.statements == ['SAVEPOINT "sp_x"', "SELECT 1", 'RELEASE SAVEPOINT "sp_x"']

    def test_broken_rollback_reraises_original_error(self):
        """ROLLBACK TO까지 실패하면(연결 끊김 등) 원래 오류를 숨기지 않는다."""

        def responder(sql_text, _params):
            if sql_text == "BAD":
                raise conn.error("AdminShutdown")
            return []

        conn = FakePgConnection(responder)
        conn.fail_rollback_to = True
        with conn.cursor() as cur, pytest.raises(psycopg.errors.AdminShutdown):
            with isolated_statement(conn, cur):
                cur.execute("BAD")


class TestRunOptionalStatement:
    @pytest.mark.parametrize("flavor", FLAVORS)
    def test_ignorable_error_is_isolated_and_reported(self, flavor):
        def responder(sql_text, _params):
            if sql_text.startswith("CREATE INDEX"):
                raise conn.error("DuplicateTable", 'relation "idx" already exists')
            return []

        conn = FakePgConnection(responder, flavor=flavor)
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE t ()")
            ran = run_optional_statement(
                conn, cur, "CREATE INDEX idx ON t (a)", ignorable={"42P07"}, label="idx"
            )
            cur.execute("CREATE TRIGGER trg")
        assert ran is False
        commit_or_raise(conn)
        assert conn.committed == ["CREATE TABLE t ()", "CREATE TRIGGER trg"]

    @pytest.mark.parametrize("flavor", FLAVORS)
    def test_non_ignorable_error_propagates(self, flavor):
        def responder(sql_text, _params):
            if sql_text.startswith("CREATE INDEX"):
                raise conn.error("QueryCanceled", "canceling statement due to user request")
            return []

        conn = FakePgConnection(responder, flavor=flavor)
        with conn.cursor() as cur, pytest.raises((psycopg.Error, psycopg2.Error)):
            run_optional_statement(
                conn, cur, "CREATE INDEX idx ON t (a)", ignorable={"42P07"}, label="idx"
            )

    def test_success_returns_true(self):
        conn = FakePgConnection()
        with conn.cursor() as cur:
            assert run_optional_statement(conn, cur, "CLUSTER t", ignorable={"42501"}, label="c")


class TestCommitGuard:
    def test_refuses_to_commit_aborted_transaction(self):
        """중단된 트랜잭션의 COMMIT은 조용한 ROLLBACK이다. 성공으로 보고하면 안 된다."""

        def responder(sql_text, _params):
            if sql_text == "BAD":
                raise conn.error("UndefinedTable")
            return []

        conn = FakePgConnection(responder)
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE t ()")
            with pytest.raises(psycopg.Error):
                cur.execute("BAD")
        with pytest.raises(RuntimeError, match="중단"):
            commit_or_raise(conn)
        assert conn.commits == 0

    def test_commits_healthy_transaction(self):
        conn = FakePgConnection()
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE t ()")
        commit_or_raise(conn)
        assert conn.committed == ["CREATE TABLE t ()"]


class TestApplyParamsIsolation:
    """세션 파라미터 하나가 실패해도 앞서 적용한 파라미터가 되돌아가면 안 된다.

    예전 코드는 실패 시 트랜잭션 전체를 rollback해서 그 전의 `SET`까지 취소했다
    (트랜잭션 안의 SET은 ROLLBACK으로 되돌아간다). 9.3에서 `checkpoint_segments`가 매번
    실패하므로 그 앞의 성능 파라미터가 늘 사라졌다.
    """

    @pytest.mark.parametrize("flavor", FLAVORS)
    def test_failed_parameter_does_not_undo_earlier_ones(self, flavor):
        from src.database.postgres_utils import PostgresOptimizer

        def responder(sql_text, _params):
            if sql_text.startswith("SET bad_param"):
                raise conn.error("CantChangeRuntimeParam", "cannot be changed now")
            return []

        conn = FakePgConnection(responder, flavor=flavor)
        PostgresOptimizer.apply_params(conn, {"work_mem": "64MB", "bad_param": "1", "x": "2"})

        assert "SET work_mem = %s" in conn.committed
        assert "SET x = %s" in conn.committed
        assert conn.silent_rollbacks == 0
