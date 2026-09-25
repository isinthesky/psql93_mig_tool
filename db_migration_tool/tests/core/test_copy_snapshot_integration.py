"""실제 PostgreSQL에서 COPY 워커의 snapshot(H-03)·schema 한정(H-04)·취소(H-02)를 확인한다.

동시 쓰기를 만들어야 하므로 **버려도 되는 scratch DB 두 개**가 필요하다(운영 원본 bms93에는
쓸 수 없다). 환경변수가 없으면 건너뛴다.

    DBMIG_IT_SCRATCH_SRC=postgresql://user@127.0.0.1:55439/itsrc
    DBMIG_IT_SCRATCH_DST=postgresql://user@127.0.0.1:55439/itdst

테스트는 두 DB의 public 스키마에서 point_history 계열·partition_table_info와 trigger 함수를
지우고 다시 만든다(원본에는 shadow 스키마도 만든다). 다른 객체는 건드리지 않는다.
"""

from __future__ import annotations

import os
import threading
import time
from unittest.mock import patch

import psycopg2
import pytest

from src.core.copy_migration_worker import CopyMigrationWorker
from src.models.profile import ConnectionProfile

pytestmark = pytest.mark.integration

SRC_DSN = os.environ.get("DBMIG_IT_SCRATCH_SRC")
DST_DSN = os.environ.get("DBMIG_IT_SCRATCH_DST")

if not (SRC_DSN and DST_DSN):
    pytest.skip(
        "scratch PostgreSQL 두 개가 필요합니다(DBMIG_IT_SCRATCH_SRC/DST)", allow_module_level=True
    )

PART = "point_history_260101"
BASE_TS = 1_767_225_600_000  # 2026-01-01 00:00 UTC (ms)


def _config(dsn: str) -> dict:
    p = psycopg2.extensions.parse_dsn(dsn)
    return {
        "host": p.get("host", "127.0.0.1"),
        "port": int(p.get("port", 5432)),
        "database": p["dbname"],
        "username": p.get("user", ""),
        "password": p.get("password", ""),
    }


def _conn(dsn: str):
    c = psycopg2.connect(dsn)
    c.autocommit = True
    return c


def _reset(n_rows: int) -> None:
    with _conn(DST_DSN) as d, d.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.point_history CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.partition_table_info")
        cur.execute("DROP FUNCTION IF EXISTS public.point_history_partition_insert() CASCADE")
    with _conn(SRC_DSN) as s, s.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS shadow CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.point_history CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.partition_table_info")
        cur.execute(
            "CREATE TABLE public.point_history (path_id bigint NOT NULL, issued_date bigint NOT NULL,"
            " changed_value varchar(100), connection_status boolean)"
        )
        cur.execute(
            "CREATE TABLE public.partition_table_info (table_name varchar(100) NOT NULL,"
            " table_data varchar(10) NOT NULL, from_date bigint NOT NULL, to_date bigint NOT NULL,"
            " use_flag boolean NOT NULL, save_date timestamp NOT NULL,"
            " cluster_index boolean DEFAULT false)"
        )
        cur.execute(
            f"CREATE TABLE public.{PART} (PRIMARY KEY (path_id, issued_date))"
            " INHERITS (public.point_history)"
        )
        cur.execute(
            "INSERT INTO public.partition_table_info VALUES (%s, 'PH', %s, %s, true, now(), false)",
            (PART, BASE_TS, BASE_TS + 86_400_000),
        )
        cur.execute(
            f"INSERT INTO public.{PART} SELECT g * 10, %s + g, 'v' || g, g %% 2 = 0"
            " FROM generate_series(1, %s) g",
            (BASE_TS, n_rows),
        )
        # 선행 스키마의 동명 relation(H-04). search_path가 이것을 먼저 찾으면 잡음 행이 섞인다.
        cur.execute("CREATE SCHEMA shadow")
        cur.execute(f"CREATE TABLE shadow.{PART} (LIKE public.{PART})")
        cur.execute(
            f"INSERT INTO shadow.{PART} SELECT -g, 0, 'SHADOW', true FROM generate_series(1, 5) g"
        )


def _rows(dsn: str) -> list[tuple]:
    with _conn(dsn) as c, c.cursor() as cur:
        cur.execute(f"SELECT * FROM public.{PART} ORDER BY path_id, issued_date")
        return cur.fetchall()


class _Checkpoint:
    id = 1
    partition_name = PART
    status = "pending"
    last_path_id = None
    last_issued_date = None
    rows_processed = 0
    error_message = None


def _worker(mode: str, batch: int, resume: bool = False) -> CopyMigrationWorker:
    profile = ConnectionProfile(
        id=1, name="it", source_config=_config(SRC_DSN), target_config=_config(DST_DSN)
    )
    with (
        patch("src.core.base_migration_worker.HistoryManager"),
        patch("src.core.base_migration_worker.CheckpointManager"),
    ):
        w = CopyMigrationWorker(profile, [PART], 1, resume=resume, batch_size=batch, copy_mode=mode)
    w.checkpoint_manager.get_checkpoints.return_value = []
    w.checkpoint_manager.create_checkpoint.return_value = _Checkpoint()
    w.cp_calls = w.checkpoint_manager.update_checkpoint_status  # type: ignore[attr-defined]
    w.truncate_permission = True
    return w


def _statuses(w) -> list[tuple[str, dict]]:
    # spy로 감싸도 원래 MagicMock(_worker에서 보관)의 호출 기록을 본다
    return [(c.args[1], c.kwargs) for c in w.cp_calls.call_args_list]


def _concurrent_writes() -> None:
    """원본에 다른 세션의 insert 3(지난 범위·앞 범위)·update 1·delete 2를 커밋한다(행 수 +1)."""
    with _conn(SRC_DSN) as s, s.cursor() as cur:
        cur.execute(f"INSERT INTO public.{PART} VALUES (15, %s, 'late-below', true)", (BASE_TS,))
        cur.execute(
            f"INSERT INTO public.{PART} VALUES (99999999, %s, 'late-above', true)", (BASE_TS,)
        )
        cur.execute(f"INSERT INTO public.{PART} VALUES (25, %s, 'late-below-2', false)", (BASE_TS,))
        cur.execute(f"UPDATE public.{PART} SET changed_value = 'late-update' WHERE path_id = 20000")
        cur.execute(f"DELETE FROM public.{PART} WHERE path_id IN (10, 25000)")


@pytest.fixture
def shadow_first_search_path():
    """연결 search_path 앞에 shadow를 둔다 — 워커가 relation을 스스로 한정해야만 통과한다."""
    with patch(
        "src.database.connection_params.SEARCH_PATH_OPTIONS", "-c search_path=shadow,public"
    ):
        yield


class TestSnapshotOnRealPostgres:
    """H-03: 이관 중 원본에 커밋된 쓰기는 대상에 섞이지 않고, 완료 검증도 같은 snapshot으로 통과."""

    def test_python_copy_matches_snapshot_under_concurrent_writes(self):
        _reset(3000)
        snapshot = _rows(SRC_DSN)
        w = _worker("python", batch=500)
        real = w.checkpoint_manager.update_checkpoint_status
        fired = {"done": False}

        def spy(cid, status, **kw):
            real(cid, status, **kw)
            if status == "running" and kw.get("rows_processed") and not fired["done"]:
                fired["done"] = True
                _concurrent_writes()

        w.checkpoint_manager.update_checkpoint_status = spy
        w.is_running = True
        w._execute_migration()

        assert fired["done"]
        assert _rows(DST_DSN) == snapshot, "대상이 시작 시점 snapshot과 다릅니다"
        assert any(s == "completed" and kw["rows_processed"] == 3000 for s, kw in _statuses(w))

    def test_server_copy_count_uses_the_same_snapshot(self):
        _reset(3000)
        snapshot = _rows(SRC_DSN)
        w = _worker("server", batch=500)
        real_verify = w._verify_partition_row_count

        def verify(name):
            _concurrent_writes()  # COPY가 끝난 뒤, 완료 검증 COUNT 전
            return real_verify(name)

        w._verify_partition_row_count = verify
        w.is_running = True
        w._execute_migration()

        assert _rows(DST_DSN) == snapshot
        assert any(s == "completed" and kw["rows_processed"] == 3000 for s, kw in _statuses(w))


@pytest.mark.usefixtures("shadow_first_search_path")
class TestSchemaQualifiedOnRealPostgres:
    """H-04: search_path 앞 스키마의 동명 relation(잡음 5행)을 읽지 않는다."""

    @pytest.mark.parametrize("mode", ["python", "server"])
    def test_shadow_relation_is_ignored(self, mode):
        _reset(1200)
        expected = _rows(SRC_DSN)
        w = _worker(mode, batch=500)
        w.is_running = True
        w._execute_migration()

        assert _rows(DST_DSN) == expected
        assert any(s == "completed" and kw["rows_processed"] == 1200 for s, kw in _statuses(w))


def _active_copy_on_source() -> int:
    with _conn(SRC_DSN) as s, s.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid()"
            " AND state = 'active' AND query LIKE %s",
            (f"COPY (SELECT%{PART}%",),
        )
        return int(cur.fetchone()[0])


class TestCancelOnRealPostgres:
    """H-02: 실행 중인 단일 COPY 도중 stop() → 제한 시간 안에 끝나고 아무것도 남지 않는다."""

    def test_stop_mid_copy_ends_within_bound_and_leaves_nothing_running(self):
        _reset(2_000_000)
        # 배치 하나가 파티션 전체 — 중지가 배치 사이가 아니라 COPY 도중에 걸리게 한다.
        w = _worker("python", batch=2_000_000)
        w.is_running = True
        errors: list[BaseException] = []

        def run():
            try:
                w._execute_migration()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.monotonic() + 30
        while _active_copy_on_source() == 0:
            assert time.monotonic() < deadline, "원본 COPY가 시작되지 않았습니다"
            time.sleep(0.02)
        time.sleep(0.3)
        t0 = time.monotonic()
        w.stop()
        t.join(30)
        elapsed = time.monotonic() - t0

        assert not t.is_alive() and elapsed < 1.5, f"중지에 {elapsed:.2f}초"
        assert not errors
        assert not [x for x in threading.enumerate() if x.name.startswith("copy-producer")]
        assert w.source_conn.closed and w.target_conn.closed
        assert _active_copy_on_source() == 0

        # 진행 중이던 배치는 롤백 → 대상 0행, checkpoint도 전진하지 않았다.
        with _conn(DST_DSN) as d, d.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM public.{PART}")
            assert cur.fetchone()[0] == 0
        statuses = _statuses(w)
        assert not [kw for s, kw in statuses if s == "running" and kw.get("rows_processed")]
        assert "failed" not in [s for s, _ in statuses]


def _target_count() -> int:
    with _conn(DST_DSN) as d, d.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM public.{PART}")
        return int(cur.fetchone()[0])


def _approve_truncate(w) -> list[tuple[str, int]]:
    """UI 대신 TRUNCATE 확인에 '예'로 답한다(워커 스레드에서 바로 응답)."""
    from PySide6.QtCore import Qt

    asked: list[tuple[str, int]] = []

    def approve(table, rows):
        asked.append((table, rows))
        w.truncate_permission = True

    w.truncate_requested.connect(approve, Qt.ConnectionType.DirectConnection)
    return asked


class TestApprovedTruncateOnRealPostgres:
    """H-02 후속: 첫 COPY 도중 중지돼도 승인한 TRUNCATE는 남고, 재개가 낡은 행을 잇지 않는다.

    실제 TableCreator.ensure_partition_ready(TRUNCATE를 커밋하지 않고 넘긴다)와 실제 트랜잭션으로
    확인한다. 대상에는 같은 파티션을 예전에 복사한 행(행 수 같음)이 있고, 그 뒤 원본이 바뀌었다.
    """

    @pytest.mark.parametrize("mode", ["python", "server"])
    def test_stop_in_first_copy_then_resume_recopies_fresh_rows(self, mode):
        _reset(1200)
        w0 = _worker("python", batch=500)
        w0.is_running = True
        w0._execute_migration()
        assert _target_count() == 1200
        with _conn(SRC_DSN) as s, s.cursor() as cur:  # 예전 복사 이후 원본 변경
            cur.execute(f"UPDATE public.{PART} SET changed_value = 'fresh' WHERE path_id = 20")
        fresh = _rows(SRC_DSN)

        # 1차: 승인한 TRUNCATE 뒤 첫 COPY에서 중지
        w1 = _worker(mode, batch=500)
        asked = _approve_truncate(w1)
        if mode == "python":
            real_batch = w1._copy_batch_streaming

            def stop_then_batch(*args, **kwargs):
                w1.stop()
                return real_batch(*args, **kwargs)

            w1._copy_batch_streaming = stop_then_batch
        else:
            real_begin = w1._begin_source_snapshot

            def stop_then_begin():
                w1.stop()
                return real_begin()

            w1._begin_source_snapshot = stop_then_begin
        w1.is_running = True
        w1._execute_migration()

        assert asked == ([(PART, 1200)] if mode == "python" else [])
        assert "failed" not in [s for s, _ in _statuses(w1)]
        assert _target_count() == 0, "승인한 TRUNCATE가 중지와 함께 롤백돼 옛 행이 남았습니다"

        # 2차: 재개(Python COPY). 커밋된 배치가 없으니 처음부터 복사해 최신 내용이 된다.
        w2 = _worker(mode, batch=500, resume=True)
        cp = _Checkpoint()
        cp.status = "running"
        w2.checkpoint_manager.get_checkpoints.return_value = [cp]
        _approve_truncate(w2)
        w2.is_running = True
        w2._execute_migration()

        assert _rows(DST_DSN) == fresh, "재개 결과에 낡은 행이 남았습니다"
        assert any(s == "completed" and kw["rows_processed"] == 1200 for s, kw in _statuses(w2))
