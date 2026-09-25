"""TableCreator의 DDL 트랜잭션 경계(감사 M-14)와 schema 한정(감사 H-04).

재현하는 결함
- 인덱스·CLUSTER 오류를 SAVEPOINT 없이 잡아 PostgreSQL 트랜잭션이 중단(aborted)된 채로 이어 썼다.
  이어지는 필수 DDL이 `in_failed_sql_transaction`으로 실패하거나, 중단된 트랜잭션에 COMMIT을
  보내 DDL이 **조용히 ROLLBACK** 된 뒤 metadata(`partition_table_info`)만 따로 커밋됐다.
- psycopg3 예외 클래스(`psycopg.errors.*`)로만 분기해 psycopg2 연결(COPY 워커)에서는 분기가 안 탔다.
- 부모·파티션·metadata를 각각 커밋해 중간 실패 시 일부만 남았다.
- relation을 schema 없이 써서 `search_path` 선행 스키마의 동명 객체를 건드릴 수 있었다.

`FakePgConnection`은 실제 PostgreSQL의 중단 트랜잭션 규칙을 지킨다(tests/database/fake_pg.py).
"""

from __future__ import annotations

import psycopg
import psycopg2
import pytest

from src.core.table_creator import TableCreator
from tests.database.fake_pg import INERROR, FakePgConnection

FLAVORS = ["psycopg", "psycopg2"]
PARTITION = "point_history_260504"
PARENT = "point_history"
FROM_MS = 1777820400000
TO_MS = 1777906799999

SOURCE_COLUMNS = [
    ("path_id", "integer", None, "NO", None),
    ("issued_date", "bigint", None, "NO", None),
    ("changed_value", "character varying", 100, "YES", None),
    ("connection_status", "boolean", None, "YES", "true"),
]


def _source(flavor: str, table_data: str = "PH", columns=SOURCE_COLUMNS) -> FakePgConnection:
    def responder(sql_text, params):
        if "partition_table_info" in sql_text:
            return [(table_data, FROM_MS, TO_MS)]
        if "information_schema.columns" in sql_text:
            return list(columns)
        return []

    return FakePgConnection(responder, flavor=flavor)


def _target(
    flavor: str,
    *,
    fail: dict[str, str] | None = None,
    parent_exists: bool = False,
    partition_exists: bool = False,
    partition_rows: int = 0,
) -> FakePgConnection:
    """`fail`: {SQL 부분 문자열: 예외 클래스 이름} — 그 문장을 실패시킨다."""
    fail = fail or {}

    def responder(sql_text, params):
        for needle, exc_name in fail.items():
            if needle in sql_text:
                raise conn.error(exc_name, f"injected failure: {needle}")
        if "information_schema.tables" in sql_text:
            if "'partition_table_info'" in sql_text:
                return [(True,)]
            name = params[0] if params else None
            if name == PARENT:
                return [(parent_exists,)]
            if name == PARTITION:
                return [(partition_exists,)]
            return [(False,)]
        if sql_text.startswith("SELECT 1 FROM") and "partition_table_info" in sql_text:
            return []
        if sql_text.startswith("SELECT COUNT(*)"):
            return [(partition_rows,)]
        return []

    conn = FakePgConnection(responder, flavor=flavor)
    return conn


def _ddl(conn: FakePgConnection, prefix: str) -> list[str]:
    return [s for s in conn.committed if s.upper().startswith(prefix.upper())]


# ── M-14: 무시 가능한 DDL은 SAVEPOINT로 격리 ─────────────────────────────


@pytest.mark.parametrize("flavor", FLAVORS)
def test_duplicate_index_does_not_abort_parent_creation(flavor):
    """중복 인덱스(42P07)는 건너뛰되, 트리거·파티션·metadata는 모두 커밋돼야 한다."""
    source = _source(flavor)
    target = _target(flavor, fail={"_path_id_date": "DuplicateTable"})

    created, rows = TableCreator(source, target).ensure_partition_ready(PARTITION)

    assert (created, rows) == (True, 0)
    assert target.silent_rollbacks == 0, (
        "중단된 트랜잭션에 COMMIT을 보냈습니다(DDL이 조용히 사라짐)"
    )
    assert _ddl(target, "CREATE OR REPLACE FUNCTION"), "트리거 함수가 커밋되지 않았습니다"
    assert _ddl(target, "CREATE TRIGGER")
    assert any(PARTITION in s for s in _ddl(target, "CREATE TABLE"))
    assert _ddl(target, "INSERT INTO public.partition_table_info")
    # 실패한 인덱스만 빠지고, 다른 인덱스는 남는다.
    assert any("_path_id_idx" in s for s in _ddl(target, "CREATE INDEX"))
    assert not any("_path_id_date" in s for s in _ddl(target, "CREATE INDEX"))


@pytest.mark.parametrize("flavor", FLAVORS)
@pytest.mark.parametrize("exc_name", ["InsufficientPrivilege", "UndefinedObject"])
def test_cluster_failure_keeps_ddl_and_metadata_together(flavor, exc_name):
    """CLUSTER 실패는 무시 가능하지만 트랜잭션을 중단시키면 안 된다.

    예전 코드: psycopg3 연결에서는 예외를 잡고 중단된 트랜잭션에 COMMIT → 파티션 DDL이
    조용히 ROLLBACK → 이어서 metadata만 새 트랜잭션으로 커밋(테이블 없는 metadata).
    """
    source = _source(flavor)
    target = _target(flavor, parent_exists=True, fail={"CLUSTER": exc_name})

    TableCreator(source, target).ensure_partition_ready(PARTITION)

    assert target.silent_rollbacks == 0
    assert any(PARTITION in s for s in _ddl(target, "CREATE TABLE"))
    assert _ddl(target, "INSERT INTO public.partition_table_info")
    assert not _ddl(target, "CLUSTER")


# ── M-14: 필수 DDL은 즉시 실패, DDL과 metadata는 함께 ──────────────────────


@pytest.mark.parametrize("flavor", FLAVORS)
def test_required_ddl_failure_fails_immediately_and_rolls_back(flavor):
    source = _source(flavor)
    target = _target(flavor, fail={"CREATE OR REPLACE FUNCTION": "InsufficientPrivilege"})

    with pytest.raises(Exception, match="테이블 생성 오류"):
        TableCreator(source, target).ensure_partition_ready(PARTITION)

    assert target.committed == [], "필수 DDL 실패 뒤에 일부가 커밋됐습니다"
    assert target.status != INERROR, (
        "실패 뒤 연결을 중단 상태로 남기면 호출자의 다음 쿼리가 실패합니다"
    )
    assert target.rollbacks >= 1
    # 실패 직후 멈춘다 — 트리거·파티션 DDL을 이어서 시도하지 않는다.
    failed_at = next(i for i, s in enumerate(target.statements) if "CREATE OR REPLACE" in s)
    later = target.statements[failed_at + 1 :]
    assert not any(s.startswith(("CREATE TRIGGER", "CREATE TABLE", "INSERT")) for s in later)


@pytest.mark.parametrize("flavor", FLAVORS)
def test_metadata_failure_rolls_back_the_ddl(flavor):
    """metadata 기록이 실패하면 방금 만든 테이블도 남지 않아야 한다(함께 성공/함께 실패)."""
    source = _source(flavor)
    target = _target(
        flavor, fail={"INSERT INTO public.partition_table_info": "InsufficientPrivilege"}
    )

    with pytest.raises(Exception, match="테이블 생성 오류"):
        TableCreator(source, target).ensure_partition_ready(PARTITION)

    assert not _ddl(target, "CREATE TABLE"), "metadata 없이 테이블만 커밋됐습니다"
    assert target.committed == []
    assert target.status != INERROR


@pytest.mark.parametrize("flavor", FLAVORS)
def test_partition_ddl_failure_does_not_leave_parent_half_built(flavor):
    source = _source(flavor)
    target = _target(flavor, fail={f'"public"."{PARTITION}"': "DuplicateObject"})

    with pytest.raises(Exception, match="테이블 생성 오류"):
        TableCreator(source, target).ensure_partition_ready(PARTITION)

    assert target.committed == []


@pytest.mark.parametrize("flavor", FLAVORS)
def test_unexpected_error_in_optional_index_is_not_swallowed(flavor):
    """취소(57014)·연결 오류 등은 '무시 가능'이 아니다. 삼키면 취소가 먹지 않는다."""
    source = _source(flavor)
    target = _target(flavor, fail={"_path_id_idx": "QueryCanceled"})

    with pytest.raises(Exception, match="테이블 생성 오류"):
        TableCreator(source, target).ensure_partition_ready(PARTITION)

    assert target.committed == []


@pytest.mark.parametrize("flavor", FLAVORS)
def test_rule_table_ddl_and_metadata_commit_once(flavor):
    """RULE 기반 타입(TH)도 한 트랜잭션: 커밋은 생성 끝에 한 번."""
    source = _source(flavor, table_data="TH")
    target = FakePgConnection(
        lambda s, p: (
            [(True,)]
            if "'partition_table_info'" in s
            else ([(p[0] == "trend_history",)] if "information_schema.tables" in s else [])
        ),
        flavor=flavor,
    )

    TableCreator(source, target).ensure_partition_ready("trend_history_260504")

    assert target.commits == 1
    assert _ddl(target, "CREATE RULE")
    assert _ddl(target, "INSERT INTO public.partition_table_info")


# ── H-04: 모든 relation을 schema 한정 + quote ────────────────────────────


def _created_everything(flavor: str = "psycopg") -> FakePgConnection:
    source = _source(flavor)
    target = _target(flavor)
    TableCreator(source, target).ensure_partition_ready(PARTITION)
    return target


def test_parent_and_partition_ddl_are_schema_qualified():
    target = _created_everything()
    creates = _ddl(target, "CREATE TABLE")
    assert any(s.startswith(f'CREATE TABLE IF NOT EXISTS "public"."{PARENT}" (') for s in creates)
    partition_ddl = next(s for s in creates if PARTITION in s)
    assert partition_ddl.startswith(f'CREATE TABLE IF NOT EXISTS "public"."{PARTITION}"')
    assert f'INHERITS ("public"."{PARENT}")' in partition_ddl
    assert f'CONSTRAINT "{PARTITION}_pkey" PRIMARY KEY("path_id", "issued_date")' in partition_ddl


def test_indexes_trigger_and_cluster_are_schema_qualified():
    target = _created_everything()
    for stmt in _ddl(target, "CREATE INDEX"):
        assert f' ON "public"."{PARENT}" ' in stmt, stmt

    func = _ddl(target, "CREATE OR REPLACE FUNCTION")[0]
    assert func.startswith(f'CREATE OR REPLACE FUNCTION "public"."{PARENT}_partition_insert"()')
    # 트리거 본문의 동적 INSERT도 public 파티션만 가리켜야 한다.
    assert "format('INSERT INTO %I.%I VALUES ($1.*)', 'public'," in func

    trigger_stmts = [s for s in target.committed if "TRIGGER" in s and "FUNCTION" not in s]
    assert any(
        f'DROP TRIGGER IF EXISTS "insert_{PARENT}_trigger" ON "public"."{PARENT}"' in s
        for s in trigger_stmts
    )
    assert any(
        f'BEFORE INSERT ON "public"."{PARENT}"' in s
        and f'EXECUTE PROCEDURE "public"."{PARENT}_partition_insert"()' in s
        for s in trigger_stmts
    )

    cluster = _ddl(target, "CLUSTER")
    assert cluster == [f'CLUSTER "public"."{PARTITION}" USING "{PARTITION}_pkey"']


def test_rule_is_schema_qualified():
    source = _source("psycopg", table_data="TH")
    target = FakePgConnection(
        lambda s, p: (
            [(True,)]
            if "'partition_table_info'" in s
            else ([(p[0] == "trend_history",)] if "information_schema.tables" in s else [])
        )
    )
    TableCreator(source, target).ensure_partition_ready("trend_history_260504")

    drop = next(s for s in target.committed if s.startswith("DROP RULE"))
    assert drop == 'DROP RULE IF EXISTS "rule_trend_history_260504" ON "public"."trend_history"'
    rule = _ddl(target, "CREATE RULE")[0]
    assert 'ON INSERT TO "public"."trend_history"' in rule
    assert 'DO INSTEAD INSERT INTO "public"."trend_history_260504" (' in rule


def test_partition_table_info_is_schema_qualified_on_both_sides():
    source = _source("psycopg")
    target = _target("psycopg")
    TableCreator(source, target).ensure_partition_ready(PARTITION)

    for conn in (source, target):
        for stmt in conn.statements:
            if "partition_table_info" in stmt and "information_schema" not in stmt:
                assert "public.partition_table_info" in stmt, stmt


def test_source_columns_are_read_from_public_only():
    """information_schema.columns를 이름만으로 고르면 shadow 스키마의 동명 테이블 컬럼이 섞인다."""
    source = _source("psycopg")
    TableCreator(source, _target("psycopg")).ensure_partition_ready(PARTITION)

    column_query = next(s for s in source.statements if "information_schema.columns" in s)
    assert "table_schema = 'public'" in column_query


def test_existing_partition_count_and_truncate_are_schema_qualified():
    source = _source("psycopg")
    target = _target("psycopg", partition_exists=True, partition_rows=5)

    created, rows = TableCreator(source, target).ensure_partition_ready(
        PARTITION, truncate_mode="auto"
    )

    assert (created, rows) == (False, 5)
    assert f'SELECT COUNT(*) FROM "public"."{PARTITION}"' in target.statements
    assert f'TRUNCATE TABLE "public"."{PARTITION}" RESTART IDENTITY' in target.statements
    # TRUNCATE는 호출자의 COPY와 같은 트랜잭션이어야 하므로 여기서 커밋하지 않는다.
    assert f'TRUNCATE TABLE "public"."{PARTITION}" RESTART IDENTITY' not in target.committed


def test_column_names_are_quoted():
    target = _created_everything()
    parent_ddl = next(s for s in _ddl(target, "CREATE TABLE") if f'"{PARENT}" (' in s)
    assert '"path_id" integer NOT NULL' in parent_ddl
    assert '"changed_value" character varying(100)' in parent_ddl


def test_driver_exceptions_are_real_classes():
    """테스트가 실제 드라이버 예외 계층을 쓰는지(가짜 예외로 통과하지 않게)."""
    assert isinstance(_target("psycopg").error("DuplicateTable"), psycopg.errors.DuplicateTable)
    assert isinstance(_target("psycopg2").error("DuplicateTable"), psycopg2.Error)
