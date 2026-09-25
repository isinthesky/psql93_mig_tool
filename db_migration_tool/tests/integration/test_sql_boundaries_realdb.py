"""실DB: search_path shadow(H-04), 추정 실패 복구(M-01), DDL SAVEPOINT·커밋 경계(M-14).

기본은 건너뛴다. 실행 조건(환경변수):
    DBMIG_REALDB_SQL_TESTS=1
    DBMIG_E2E_DST_HOST / DBMIG_E2E_DST_PORT / DBMIG_E2E_DST_PW  (대상 — DB 이름은 반드시 temp)
    DBMIG_E2E_SRC_HOST / DBMIG_E2E_SRC_PORT / DBMIG_E2E_SRC_PW  (원본 PG 9.3 — 읽기 전용 세션)
    [선택] DBMIG_E2E_DST_DB(기본 temp), DBMIG_E2E_SRC_DB(기본 bms93), *_USER(기본 postgres),
           DBMIG_W2SQL_PARTITION(기본 point_history_260504)

안전장치
- 쓰기는 대상 `temp` DB에만 한다. 원본은 `readonly` 세션으로 읽기만 한다.
- shadow 스키마 `zz_shadow_w2sql`을 만들고 끝나면 CASCADE로 지운다.
- 대상에 배정 파티션이 이미 있으면 건너뛴다(덮어쓰지 않음). 테스트가 만든 파티션과
  `partition_table_info` 행만 지운다.
- 트리거·인덱스 DDL 검증은 트랜잭션 안에서만 하고 ROLLBACK한다(기존 함수·트리거 불변).
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import patch

import psycopg
import psycopg2
import pytest

import src.core.scan_workers as scan_mod
from src.core.file_archive_workers import ArchiveMigrationWorkerBase
from src.core.partition_discovery import PartitionDiscovery
from src.core.table_creator import IGNORABLE_CLUSTER_SQLSTATES, TableCreator
from src.core.table_types import TableType
from src.database.postgres_utils import (
    PostgresOptimizer,
    commit_or_raise,
    run_optional_statement,
    transaction_is_aborted,
)
from src.database.version_info import PgVersionFamily, PgVersionInfo

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("DBMIG_REALDB_SQL_TESTS") != "1", reason="DBMIG_REALDB_SQL_TESTS=1 필요"
    ),
]

SHADOW = "zz_shadow_w2sql"
PARTITION = os.environ.get("DBMIG_W2SQL_PARTITION", "point_history_260504")
PARENT = "point_history"
SHADOW_ROWS = 3


def _cfg(side: str) -> dict[str, Any]:
    prefix = f"DBMIG_E2E_{side}_"
    host = os.environ.get(prefix + "HOST")
    pw = os.environ.get(prefix + "PW")
    if not host or not pw:
        pytest.skip(f"{prefix}HOST/{prefix}PW 필요")
    return {
        "host": host,
        "port": int(os.environ.get(prefix + "PORT", "5432")),
        "dbname": os.environ.get(prefix + "DB", "temp" if side == "DST" else "bms93"),
        "user": os.environ.get(prefix + "USER", "postgres"),
        "password": pw,
        "connect_timeout": 10,
    }


def _dst(search_path: str | None = None, *, driver: str = "psycopg2") -> Any:
    cfg = _cfg("DST")
    assert cfg["dbname"] == "temp", "쓰기 테스트는 temp DB에서만 허용"
    if search_path:
        cfg["options"] = f"-c search_path={search_path}"
    if driver == "psycopg2":
        return psycopg2.connect(**cfg)
    return psycopg.connect(**cfg)


def _src() -> Any:
    conn = psycopg2.connect(**_cfg("SRC"))
    conn.set_session(readonly=True)
    return conn


def _all(conn: Any, query: str, params: tuple = ()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def _one(conn: Any, query: str, params: tuple = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    return row[0] if row else None


@pytest.fixture(scope="module")
def shadow() -> Iterator[dict[str, Any]]:
    admin = _dst()
    admin.autocommit = True
    if _one(admin, "SELECT to_regclass(%s)", (f"public.{PARTITION}",)) is not None:
        admin.close()
        pytest.skip(f"public.{PARTITION}이 이미 있어 건드리지 않습니다")
    had_info = _one(
        admin,
        "SELECT count(*) FROM public.partition_table_info WHERE table_name = %s",
        (PARTITION,),
    )
    pub_from, pub_to = None, None
    src = _src()
    try:
        with src.cursor() as cur:
            cur.execute(
                "SELECT from_date, to_date FROM public.partition_table_info WHERE table_name = %s",
                (PARTITION,),
            )
            pub_from, pub_to = cur.fetchone()
        src.rollback()
    finally:
        src.close()

    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SHADOW} CASCADE")
        cur.execute(f"CREATE SCHEMA {SHADOW}")
        cur.execute(
            f"CREATE TABLE {SHADOW}.partition_table_info (LIKE public.partition_table_info)"
        )
        # 날짜 범위 안이지만 값이 다른 행 — 탐색이 shadow를 읽으면 from_timestamp가 달라진다.
        cur.execute(
            f"INSERT INTO {SHADOW}.partition_table_info "
            "(table_name, table_data, from_date, to_date, use_flag, save_date, cluster_index) "
            "VALUES (%s, 'PH', %s, %s, true, now(), false)",
            (PARTITION, pub_from + 1, pub_to),
        )
        cur.execute(f"CREATE TABLE {SHADOW}.{PARENT} (x int)")
        cur.execute(
            f"CREATE TABLE {SHADOW}.{PARTITION} (path_id int, issued_date bigint, "
            "changed_value varchar(100), connection_status boolean)"
        )
        cur.execute(
            f"INSERT INTO {SHADOW}.{PARTITION} "
            "SELECT g, %s, 'shadow', true FROM generate_series(1, %s) g",
            (pub_from, SHADOW_ROWS),
        )
        cur.execute(f"ANALYZE {SHADOW}.{PARTITION}")
    try:
        yield {"admin": admin, "from": pub_from, "to": pub_to}
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SHADOW} CASCADE")
            cur.execute(f"DROP TABLE IF EXISTS public.{PARTITION}")
            if not had_info:
                cur.execute(
                    "DELETE FROM public.partition_table_info WHERE table_name = %s", (PARTITION,)
                )
        admin.close()


def _shadow_state(admin: Any) -> tuple[int, int, int]:
    return (
        _one(admin, f"SELECT count(*) FROM {SHADOW}.{PARTITION}"),
        _one(admin, f"SELECT count(*) FROM {SHADOW}.partition_table_info"),
        _one(admin, f"SELECT count(*) FROM {SHADOW}.{PARENT}"),
    )


def test_h04_table_creator_touches_only_public_under_shadow_search_path(shadow):
    admin = shadow["admin"]
    before = _shadow_state(admin)
    source = _src()
    target = _dst(f"{SHADOW},public")
    try:
        assert _one(target, "SHOW search_path").startswith(SHADOW)
        target.rollback()

        # 1) 생성 경로: public에 파티션·metadata가 한 트랜잭션으로 생긴다.
        created, rows = TableCreator(source, target).ensure_partition_ready(PARTITION)
        assert (created, rows) == (True, 0)
        assert _one(admin, "SELECT to_regclass(%s)::text", (f"public.{PARTITION}",)) == PARTITION
        parent = _one(
            admin,
            "SELECT inhparent::regclass::text FROM pg_inherits WHERE inhrelid = %s::regclass",
            (f"public.{PARTITION}",),
        )
        assert parent == PARENT  # public.point_history (search_path=public 기준 표기)
        assert (
            _one(
                admin,
                "SELECT table_data FROM public.partition_table_info WHERE table_name = %s",
                (PARTITION,),
            )
            == "PH"
        )
        assert _shadow_state(admin) == before, "shadow 스키마 객체가 바뀌었습니다"

        # 2) 기존 테이블 경로: COUNT·TRUNCATE가 public을 본다(shadow는 3행).
        with admin.cursor() as cur:
            cur.execute(
                f"INSERT INTO public.{PARTITION} (path_id, issued_date, changed_value, "
                "connection_status) VALUES (1, %s, 'a', true), (2, %s, 'b', true)",
                (shadow["from"], shadow["from"]),
            )
        created, rows = TableCreator(source, target).ensure_partition_ready(
            PARTITION, truncate_mode="auto"
        )
        assert (created, rows) == (False, 2)
        target.commit()
        assert _one(admin, f"SELECT count(*) FROM public.{PARTITION}") == 0
        assert _shadow_state(admin) == before
    finally:
        source.close()
        target.close()


def test_h04_discovery_estimate_and_scan_workers_read_public_under_shadow(shadow):
    admin = shadow["admin"]
    with admin.cursor() as cur:
        cur.execute(
            f"INSERT INTO public.{PARTITION} (path_id, issued_date, changed_value, "
            "connection_status) VALUES (10, %s, 'a', true)",
            (shadow["from"],),
        )
        cur.execute(f"ANALYZE public.{PARTITION}")

    # 탐색: temp를 소스처럼 쓰되 search_path를 shadow 우선으로 둔다.
    discovery_conn = _dst(f"{SHADOW},public", driver="psycopg")
    with patch.object(PartitionDiscovery, "_create_connection", return_value=discovery_conn):
        found = PartitionDiscovery({}).discover_partitions(
            date(2026, 5, 4), date(2026, 5, 4), [TableType.POINT_HISTORY]
        )
    mine = [p for p in found if p["table_name"] == PARTITION]
    assert len(mine) == 1
    assert mine[0]["from_timestamp"] == shadow["from"], "shadow partition_table_info를 읽었습니다"
    assert mine[0]["row_count"] == 1, "shadow 테이블의 reltuples를 읽었습니다"

    # 크기 추정: public oid 기준.
    conn = _dst(f"{SHADOW},public")
    try:
        pg16 = PgVersionInfo(16, 0, "16", PgVersionFamily.PG_16)
        info = PostgresOptimizer.estimate_table_size(conn, PARTITION, pg16)
        assert info["exists"] is True and not info.get("lookup_failed")
        assert info["row_count"] == 1
        public_size = _one(
            admin, "SELECT pg_total_relation_size(%s::regclass)", (f"public.{PARTITION}",)
        )
        assert info["total_size_bytes"] == public_size
    finally:
        conn.close()

    # 조회 워커: 연결 빌더가 넣는 search_path=public을 shadow 우선으로 덮어 시험한다.
    def shadow_connect(_config, **_kw):
        return _dst(f"{SHADOW},public", driver="psycopg")

    with patch.object(scan_mod, "connect_psycopg", side_effect=shadow_connect):
        verify = scan_mod.RowCountVerifyWorker(0, {}, {}, [PARTITION]).execute()
        done = scan_mod.TargetCompletedScanWorker(0, "postgres", {}, [PARTITION]).execute()
    assert verify[0]["source_count"] == verify[0]["target_count"] == 1
    assert done == {PARTITION: True}


class _FailingEstimateCursor:
    """실제 psycopg 커서를 감싸 특정 테이블의 추정 조회만 서버 오류(0 나누기)로 바꾼다."""

    def __init__(self, real: Any, fail_for: str):
        self._real = real
        self._fail_for = fail_for
        self.connection = real.connection

    def __enter__(self) -> _FailingEstimateCursor:
        self._real.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.__exit__(*exc)

    def execute(self, query: Any, params: Any = None) -> Any:
        if "reltuples" in str(query) and params and params[0] == self._fail_for:
            return self._real.execute("SELECT 1/0")
        return self._real.execute(query, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _Wrapper:
    def __init__(self, real: Any, fail_for: str):
        self._real = real
        self._fail_for = fail_for

    def cursor(self) -> _FailingEstimateCursor:
        return _FailingEstimateCursor(self._real.cursor(), self._fail_for)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_m01_real_estimate_failure_does_not_abort_discovery(shadow):
    """실제 서버 오류로 트랜잭션을 중단시킨 뒤에도 탐색이 이어지는지(savepoint 복구)."""
    real = _dst(driver="psycopg")
    wrapped = _Wrapper(real, PARTITION)
    with patch.object(PartitionDiscovery, "_create_connection", return_value=wrapped):
        found = PartitionDiscovery({}).discover_partitions(
            date(2026, 5, 4), date(2026, 5, 4), [TableType.POINT_HISTORY]
        )
    mine = [p for p in found if p["table_name"] == PARTITION]
    assert len(mine) == 1 and mine[0]["row_count"] == 0


@pytest.mark.parametrize("driver", ["psycopg2", "psycopg"])
def test_m14_real_duplicate_index_and_cluster_failure_keep_transaction_alive(shadow, driver):
    conn = _dst(driver=driver)
    try:
        creator = TableCreator(None, conn)
        with conn.cursor() as cur:
            # 대조군: 격리 없이 실패하면 실제로 트랜잭션이 중단된다.
            with pytest.raises((psycopg.Error, psycopg2.Error)):
                cur.execute(f"CREATE INDEX point_history_path_id_idx ON public.{PARENT} (path_id)")
            assert transaction_is_aborted(conn)
            with pytest.raises(RuntimeError):
                commit_or_raise(conn)
            conn.rollback()

            # 중복 인덱스(실제 42P07)를 SAVEPOINT로 격리 → 이어지는 문장이 돈다.
            creator._create_parent_indexes(PARENT, TableType.POINT_HISTORY, cur)
            assert not transaction_is_aborted(conn)
            cur.execute("SELECT 1")

            # CLUSTER 실패(실제 42704)도 격리.
            cur.execute("CREATE TEMP TABLE zz_w2sql_probe (id int) ON COMMIT DROP")
            ran = run_optional_statement(
                conn,
                cur,
                "CLUSTER pg_temp.zz_w2sql_probe USING zz_no_such_index",
                ignorable=IGNORABLE_CLUSTER_SQLSTATES,
                label="probe",
            )
            assert ran is False
            cur.execute("SELECT count(*) FROM pg_temp.zz_w2sql_probe")
            assert cur.fetchone()[0] == 0
    finally:
        conn.rollback()
        conn.close()


def test_m14_h04_real_trigger_ddl_routes_to_public_partition(shadow):
    """트리거 DDL을 트랜잭션 안에서 실제로 만들고 shadow search_path에서 부모에 INSERT →
    public 파티션으로만 간다. 끝에 ROLLBACK(기존 함수·트리거 불변)."""
    admin = shadow["admin"]
    before = _shadow_state(admin)
    old_src = _one(
        admin,
        "SELECT md5(prosrc) FROM pg_proc WHERE oid = 'public.point_history_partition_insert'::regproc",
    )
    conn = _dst(f"{SHADOW},public")
    try:
        creator = TableCreator(None, conn)
        with conn.cursor() as cur:
            creator._create_trigger_based_partitioning(PARENT, TableType.POINT_HISTORY, cur)
            public_before = _one(conn, f"SELECT count(*) FROM public.{PARTITION}")
            cur.execute(
                "INSERT INTO public.point_history (path_id, issued_date, changed_value, "
                "connection_status) VALUES (999999, %s, 'trg', true)",
                (shadow["from"] + 1000,),
            )
            assert _one(conn, f"SELECT count(*) FROM public.{PARTITION}") == public_before + 1
            assert _one(conn, f"SELECT count(*) FROM {SHADOW}.{PARTITION}") == SHADOW_ROWS
    finally:
        conn.rollback()
        conn.close()
    assert _shadow_state(admin) == before
    assert (
        _one(
            admin,
            "SELECT md5(prosrc) FROM pg_proc "
            "WHERE oid = 'public.point_history_partition_insert'::regproc",
        )
        == old_src
    ), "ROLLBACK 후에도 기존 트리거 함수가 바뀌었습니다"


def test_h04_archive_queries_and_copy_use_public_under_shadow(shadow):
    """아카이브 워커: 부모 컬럼(information_schema)·COUNT·metadata·COPY TO/FROM이 shadow
    search_path에서도 public만 본다. COPY FROM은 트랜잭션 안에서만 하고 ROLLBACK."""
    admin = shadow["admin"]
    if _one(admin, "SELECT to_regclass(%s)", (f"public.{PARTITION}",)) is None:
        source, target = _src(), _dst()
        try:
            TableCreator(source, target).ensure_partition_ready(PARTITION)
        finally:
            source.close()
            target.close()
    before = _shadow_state(admin)
    public_cols = [
        r[0]
        for r in _all(
            admin,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            (PARENT,),
        )
    ]
    public_rows = _one(admin, f"SELECT count(*) FROM public.{PARTITION}")

    conn = _dst(f"{SHADOW},public")
    try:
        assert _one(conn, "SHOW search_path").startswith(SHADOW)
        base = ArchiveMigrationWorkerBase
        cols = [c["name"] for c in base._query_parent_columns(conn, PARENT)]
        assert cols == public_cols and "x" not in cols, cols
        assert base._query_row_count(conn, PARTITION) == public_rows
        meta = base._query_partition_meta(conn, PARTITION, TableType.POINT_HISTORY)
        assert meta["from_date"] == shadow["from"], "shadow partition_table_info를 읽었습니다"

        buf = io.StringIO()
        with conn.cursor() as cur:
            cur.copy_expert(base._export_copy_sql(TableType.POINT_HISTORY, PARTITION), buf)
        lines = buf.getvalue().splitlines()
        assert len(lines) == public_rows
        assert not any("shadow" in line for line in lines)

        line = f"888888,{shadow['from']},imp,true\n"
        with conn.cursor() as cur:
            cur.copy_expert(
                base._import_copy_sql(
                    PARTITION, ["path_id", "issued_date", "changed_value", "connection_status"]
                ),
                io.StringIO(line),
            )
        assert _one(conn, f"SELECT count(*) FROM public.{PARTITION}") == public_rows + 1
        assert _one(conn, f"SELECT count(*) FROM {SHADOW}.{PARTITION}") == SHADOW_ROWS
    finally:
        conn.rollback()
        conn.close()
    assert _one(admin, f"SELECT count(*) FROM public.{PARTITION}") == public_rows
    assert _shadow_state(admin) == before
