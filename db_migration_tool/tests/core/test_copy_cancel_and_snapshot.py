"""COPY 워커의 취소(H-02)·일관 snapshot(H-03)·schema 한정(H-04)을 가짜 연결로 고정한다.

가짜 연결은 psycopg2가 워커에 보이는 면만 흉내 낸다.

- 원본: 트랜잭션 격리를 흉내 낸다. 트랜잭션 첫 문장이 ``SET TRANSACTION ISOLATION LEVEL
  REPEATABLE READ``면 첫 데이터 조회 시점의 행 집합(snapshot)을 트랜잭션 끝까지 쓰고, 아니면
  (READ COMMITTED) 문장마다 최신 행을 본다. ``COPY (SELECT ... WHERE 키 > ... LIMIT n)``의
  keyset 조건을 해석해 배치를 돌려준다.
- 대상: COPY FROM이 버퍼를 EOF까지 읽어 pending에 두고, commit에서 committed로 옮긴다.
- 양쪽 모두 ``cancel()``을 부르면 진행 중인(또는 막힌) 문장이 QueryCanceled로 끝난다.

감사 문서 §3 H-02/H-03/H-04 완료 판정:
- connect/COPY/commit 각 단계 취소가 제한 시간 안에 끝나고 thread·connection·producer가 남지 않음
- 동시 insert/update/delete에서 선택한 snapshot과 대상 결과가 정확히 일치
- 선행 스키마에 동명 객체가 있어도 ``public``의 의도한 객체만 조회·변경
"""

from __future__ import annotations

import csv
import inspect
import io
import re
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg2
import psycopg2.errors
import psycopg2.sql
import pytest

from src.core.copy_migration_worker import CopyMigrationWorker, CopyStreamBuffer
from src.models.profile import ConnectionProfile

PARTITION = "point_history_260101"
QUALIFIED = f'"public"."{PARTITION}"'

# 취소가 '제한 시간 안에' 끝났는지 보는 기준(초). 가짜 연결이 취소 없이 스스로 풀리는
# 시간(_FAKE_BLOCK_SECONDS)보다 충분히 짧아야 수정 전 코드가 이 기준으로 실패한다.
_CANCEL_BOUND_SECONDS = 3.0
_FAKE_BLOCK_SECONDS = 8.0


def _fake_quote_ident(name: str, _context: Any) -> str:
    return '"' + name.replace('"', '""') + '"'


@pytest.fixture(autouse=True)
def _quote_without_server(monkeypatch):
    """psycopg2.sql의 as_string()은 식별자 quote에 실제 연결을 요구한다. 가짜 연결용으로 대체."""
    monkeypatch.setattr(psycopg2.sql.ext, "quote_ident", _fake_quote_ident)


def _text(query: Any) -> str:
    if isinstance(query, psycopg2.sql.Composable):
        return query.as_string(None)
    return str(query)


# ---------------------------------------------------------------------------
# 가짜 원본
# ---------------------------------------------------------------------------

_KEYSET = re.compile(
    r'WHERE "path_id" > (-?\d+) OR \("path_id" = (-?\d+) AND "issued_date" > (-?\d+)\)'
)
_LIMIT = re.compile(r"LIMIT (\d+)")


class FakeSource:
    """원본 연결. rows는 커밋된 최신 상태(다른 세션의 쓰기는 rows를 바로 바꾼다)."""

    def __init__(self, rows: list[tuple[int, int, str, bool]]):
        self.rows = list(rows)
        self.closed = 0
        self.statements: list[tuple[int, str]] = []  # (트랜잭션 번호, SQL)
        self.tx_no = 0
        self._tx_statements = 0
        self._repeatable = False
        self._snapshot: list[tuple[int, int, str, bool]] | None = None
        self.cancel_event = threading.Event()
        self.cancel_calls = 0
        self.copy_calls = 0
        # 테스트 훅: copy_out 호출 번호(1부터) → 호출 직전에 실행할 함수
        self.before_copy: dict[int, Any] = {}
        # 이 번호의 COPY는 일부를 쓴 뒤 cancel()될 때까지 막힌다
        self.block_copy_no: int | None = None
        self.copy_blocked = threading.Event()

    # --- 연결 API ---
    def cursor(self):
        return _FakeCursor(self)

    def cancel(self):
        self.cancel_calls += 1
        self.cancel_event.set()

    def commit(self):
        self._end_tx()

    def rollback(self):
        self._end_tx()

    def close(self):
        self.closed = 1

    # --- 내부 ---
    def _end_tx(self):
        self.tx_no += 1
        self._tx_statements = 0
        self._repeatable = False
        self._snapshot = None

    def _view(self) -> list[tuple[int, int, str, bool]]:
        if self._repeatable:
            if self._snapshot is None:
                self._snapshot = list(self.rows)
            return self._snapshot
        return list(self.rows)

    def execute(self, query: Any, params: Any = None):
        text = _text(query)
        self.statements.append((self.tx_no, text))
        if text.upper().startswith("SET TRANSACTION"):
            if self._tx_statements:
                raise psycopg2.errors.ActiveSqlTransaction(
                    "SET TRANSACTION ISOLATION LEVEL must be called before any query"
                )
            self._repeatable = "REPEATABLE READ" in text.upper()
            self._tx_statements += 1
            return None
        self._tx_statements += 1
        if "COUNT(*)" in text.upper():
            return (len(self._view()),)
        return None

    def copy_out(self, query: Any, file: Any):
        text = _text(query)
        self.statements.append((self.tx_no, text))
        self.copy_calls += 1
        no = self.copy_calls
        hook = self.before_copy.get(no)
        if hook:
            hook()
        self._tx_statements += 1
        rows = sorted(self._view(), key=lambda r: (r[0], r[1]))
        m = _KEYSET.search(text)
        if m:
            k, d = int(m.group(1)), int(m.group(3))
            rows = [r for r in rows if (r[0], r[1]) > (k, d)]
        lim = _LIMIT.search(text)
        if lim:
            rows = rows[: int(lim.group(1))]
        text_format = "FORMAT text" in text
        for i, r in enumerate(rows):
            if self.cancel_event.is_set():
                raise psycopg2.errors.QueryCanceled("canceling statement due to user request")
            if text_format:
                line = f"{r[0]}\t{r[1]}\t{r[2]}\t{'t' if r[3] else 'f'}\n"
                file.write(line.encode())
            else:
                buf = io.StringIO()
                csv.writer(buf, lineterminator="\n").writerow(
                    [r[0], r[1], r[2], "t" if r[3] else "f"]
                )
                file.write(buf.getvalue())
            if no == self.block_copy_no and i == 0:
                self.copy_blocked.set()
                if self.cancel_event.wait(_FAKE_BLOCK_SECONDS):
                    raise psycopg2.errors.QueryCanceled("canceling statement due to user request")
                raise psycopg2.errors.QueryCanceled("fake: 취소 없이 풀림(statement_timeout)")


# ---------------------------------------------------------------------------
# 가짜 대상
# ---------------------------------------------------------------------------


class FakeTarget:
    def __init__(self):
        self.pending: list[tuple[int, int]] = []
        self.committed: list[tuple[int, int]] = []
        self.closed = 0
        self.commits = 0
        self.rollbacks = 0
        self.statements: list[str] = []
        self.cancel_event = threading.Event()
        self.cancel_calls = 0
        self.block_commit_no: int | None = None
        self.commit_blocked = threading.Event()
        self._commit_calls = 0

    def cursor(self):
        return _FakeCursor(self)

    def cancel(self):
        self.cancel_calls += 1
        self.cancel_event.set()

    def commit(self):
        self._commit_calls += 1
        if self.pending and self._commit_calls == self.block_commit_no:
            self.commit_blocked.set()
            self.cancel_event.wait(_FAKE_BLOCK_SECONDS)
            self.pending = []
            raise psycopg2.errors.QueryCanceled("canceling statement due to user request")
        self.committed.extend(self.pending)
        self.pending = []
        self.commits += 1

    def rollback(self):
        self.pending = []
        self.rollbacks += 1

    def close(self):
        self.closed = 1

    def execute(self, query: Any, params: Any = None):
        text = _text(query)
        self.statements.append(text)
        if "COUNT(*)" in text.upper():
            return (len(self.committed) + len(self.pending),)
        if "DESC LIMIT 1" in text:
            rows = sorted(self.committed)
            return rows[-1] if rows else None
        return None

    def copy_in(self, query: Any, file: Any):
        self.statements.append(_text(query))
        data: list[str] = []
        if isinstance(file, CopyStreamBuffer):
            while True:
                chunk = file.read(8192)
                if chunk == "":
                    break
                data.append(chunk)
            text = "".join(data)
            parsed = list(csv.reader(io.StringIO(text)))
        else:  # server COPY: os.pipe 파일(bytes, TEXT 형식)
            text = file.read().decode()
            parsed = [line.split("\t") for line in text.splitlines()]
        for rec in parsed:
            if rec:
                self.pending.append((int(rec[0]), int(rec[1])))


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result: Any = None
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self._result = self.conn.execute(query, params)

    def fetchone(self):
        return self._result

    def copy_expert(self, query, file):
        if isinstance(self.conn, FakeSource):
            self.conn.copy_out(query, file)
        else:
            before = len(self.conn.pending)
            self.conn.copy_in(query, file)
            self.rowcount = len(self.conn.pending) - before


# ---------------------------------------------------------------------------
# 워커 준비
# ---------------------------------------------------------------------------


def _rows(n: int, step: int = 10) -> list[tuple[int, int, str, bool]]:
    return [(step * (i + 1), 1000, f"v{i}", i % 2 == 0) for i in range(n)]


class _Checkpoint:
    def __init__(self):
        self.id = 7
        self.partition_name = PARTITION
        self.status = "pending"
        self.last_path_id = None
        self.last_issued_date = None
        self.rows_processed = 0
        self.error_message = None


def _make_worker(source: FakeSource, target: FakeTarget, *, copy_mode="python", batch=4):
    profile = ConnectionProfile(
        id=1, name="p", source_config={"host": "s"}, target_config={"host": "t"}
    )
    with (
        patch("src.core.base_migration_worker.HistoryManager"),
        patch("src.core.base_migration_worker.CheckpointManager"),
    ):
        w = CopyMigrationWorker(profile, [PARTITION], 1, batch_size=batch, copy_mode=copy_mode)
    w._log = lambda message, level="INFO": None
    w.checkpoint_manager.get_checkpoints.return_value = []
    w.checkpoint_manager.create_checkpoint.return_value = _Checkpoint()
    conns = iter([source, target])
    w._create_psycopg2_connection = MagicMock(side_effect=lambda _cfg: next(conns))
    w._detect_and_apply_version_optimizations = MagicMock()
    w._check_copy_permissions = MagicMock()
    w._prepare_target_table = MagicMock(return_value=(True, 0))
    return w


def _checkpoint_calls(worker) -> list[tuple[str, dict]]:
    return [
        (c.args[1], c.kwargs)
        for c in worker.checkpoint_manager.update_checkpoint_status.call_args_list
    ]


class _Runner:
    """워커의 _execute_migration을 별도 스레드에서 돌린다(QThread.run과 같은 조건)."""

    def __init__(self, worker):
        self.worker = worker
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.worker._execute_migration()
        except BaseException as exc:  # noqa: BLE001 — 결과로 검사한다
            self.error = exc

    def start(self):
        self.worker.is_running = True
        self.thread.start()
        return self


def _producer_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("copy-producer")]


@pytest.fixture
def estimate():
    with patch(
        "src.core.copy_migration_worker.PostgresOptimizer.estimate_table_size",
        return_value={"row_count": 20, "total_size_mb": 1.0, "exists": True},
    ) as m:
        yield m


# ---------------------------------------------------------------------------
# H-02 취소
# ---------------------------------------------------------------------------


class TestStopCancelsRunningCopy:
    def test_stop_during_python_copy_ends_quickly_and_keeps_committed_checkpoint(self, estimate):
        """3번째 배치 COPY가 막힌 동안 stop() → 제한 시간 안에 끝나고, 커밋된 2배치까지만 기록."""
        source, target = FakeSource(_rows(20)), FakeTarget()
        source.block_copy_no = 3
        worker = _make_worker(source, target)
        runner = _Runner(worker).start()

        assert source.copy_blocked.wait(5), "3번째 배치에 도달하지 못했습니다"
        t0 = time.monotonic()
        worker.stop()
        runner.thread.join(_FAKE_BLOCK_SECONDS + 5)
        elapsed = time.monotonic() - t0

        assert elapsed < _CANCEL_BOUND_SECONDS, f"취소가 {elapsed:.1f}초 걸렸습니다"
        assert runner.error is None, f"사용자 중지는 오류가 아닙니다: {runner.error!r}"
        assert source.cancel_calls >= 1, "실행 중인 원본 COPY를 cancel()하지 않았습니다"
        assert not _producer_threads(), "생산자 스레드가 남았습니다"
        assert source.closed and target.closed, "연결이 닫히지 않았습니다"

        # 대상에는 커밋된 2배치(8행)만 있고, checkpoint도 정확히 거기까지만 전진했다.
        assert len(target.committed) == 8 and target.pending == []
        calls = _checkpoint_calls(worker)
        statuses = [s for s, _ in calls]
        assert "failed" not in statuses and "completed" not in statuses
        running = [kw for s, kw in calls if s == "running"]
        assert running[-1]["rows_processed"] == 8
        assert running[-1]["last_path_id"] == 80

    def test_stop_during_blocked_commit_is_cancelled_and_not_recorded(self, estimate):
        """2번째 배치 commit이 막힌 동안 stop() → commit 취소, checkpoint는 1배치 그대로."""
        source, target = FakeSource(_rows(20)), FakeTarget()
        target.block_commit_no = 2
        worker = _make_worker(source, target)
        runner = _Runner(worker).start()

        assert target.commit_blocked.wait(5), "2번째 commit에 도달하지 못했습니다"
        t0 = time.monotonic()
        worker.stop()
        runner.thread.join(_FAKE_BLOCK_SECONDS + 5)
        elapsed = time.monotonic() - t0

        assert elapsed < _CANCEL_BOUND_SECONDS, f"commit 취소가 {elapsed:.1f}초 걸렸습니다"
        assert target.cancel_calls >= 1, "막힌 commit을 cancel()하지 않았습니다"
        assert runner.error is None
        assert len(target.committed) == 4
        calls = _checkpoint_calls(worker)
        assert "failed" not in [s for s, _ in calls]
        running = [kw for s, kw in calls if s == "running"]
        assert running[-1]["rows_processed"] == 4
        assert not _producer_threads()

    def test_stop_after_copy_before_commit_does_not_start_commit(self, estimate):
        """COPY가 끝난 뒤 commit 직전에 중지되면 새 commit을 시작하지 않는다."""
        source, target = FakeSource(_rows(20)), FakeTarget()
        worker = _make_worker(source, target)
        original_assert = CopyStreamBuffer.assert_fully_consumed
        seen = {"n": 0}

        def assert_then_stop(buf):
            original_assert(buf)
            seen["n"] += 1
            if seen["n"] == 2:
                worker.stop()

        with patch.object(CopyStreamBuffer, "assert_fully_consumed", assert_then_stop):
            worker.is_running = True
            worker._execute_migration()

        assert len(target.committed) == 4, "중지 뒤에 두 번째 배치를 커밋했습니다"
        running = [kw for s, kw in _checkpoint_calls(worker) if s == "running"]
        assert running[-1]["rows_processed"] == 4

    def test_stop_while_connecting_does_not_start_work(self, estimate):
        """연결 수립 중 중지 → 연결이 끝나면 곧바로 닫고 작업을 시작하지 않는다."""
        source, target = FakeSource(_rows(4)), FakeTarget()
        worker = _make_worker(source, target)

        def connect(cfg, conns=iter([source, target])):
            conn = next(conns)
            if conn is target:
                worker.stop()  # 두 번째 연결이 수립되는 동안 사용자가 중지
            return conn

        worker._create_psycopg2_connection = MagicMock(side_effect=connect)
        worker.is_running = True
        worker._execute_migration()

        worker._detect_and_apply_version_optimizations.assert_not_called()
        assert source.copy_calls == 0
        assert source.closed and target.closed

    def test_stop_during_server_copy_cancels_both_and_commits_nothing(self, estimate):
        source, target = FakeSource(_rows(20)), FakeTarget()
        source.block_copy_no = 1
        worker = _make_worker(source, target, copy_mode="server")
        runner = _Runner(worker).start()

        assert source.copy_blocked.wait(5)
        t0 = time.monotonic()
        worker.stop()
        runner.thread.join(_FAKE_BLOCK_SECONDS + 5)
        elapsed = time.monotonic() - t0

        assert elapsed < _CANCEL_BOUND_SECONDS, f"Server COPY 취소가 {elapsed:.1f}초 걸렸습니다"
        assert runner.error is None
        assert source.cancel_calls >= 1
        assert target.committed == [] and target.commits == 0
        assert "failed" not in [s for s, _ in _checkpoint_calls(worker)]
        assert not _producer_threads()
        assert source.closed and target.closed

    def test_stop_during_auto_server_copy_does_not_fall_back_to_python(self, estimate):
        """auto 모드에서 중지로 끊긴 Server COPY를 '실패'로 보고 Python COPY로 다시 돌리면 안 된다."""
        source, target = FakeSource(_rows(20)), FakeTarget()
        source.block_copy_no = 1
        worker = _make_worker(source, target, copy_mode="auto")
        runner = _Runner(worker).start()

        assert source.copy_blocked.wait(5)
        t0 = time.monotonic()
        worker.stop()
        runner.thread.join(_FAKE_BLOCK_SECONDS + 5)
        elapsed = time.monotonic() - t0

        assert elapsed < _CANCEL_BOUND_SECONDS, f"auto 취소가 {elapsed:.1f}초 걸렸습니다"
        assert runner.error is None
        assert source.copy_calls == 1, "중지 뒤 Python COPY로 전환했습니다"
        # 전환 경로는 checkpoint를 pending/0행으로 되돌린다 — 중지에서는 일어나면 안 된다.
        assert "pending" not in [s for s, _ in _checkpoint_calls(worker)]
        assert target.committed == []
        assert not _producer_threads()


class TestStreamBufferCancel:
    def test_blocked_write_returns_promptly_on_cancel(self):
        """큐가 가득 차 write()가 기다리는 중 취소되면 곧바로 풀려야 한다(30초 대기 금지)."""
        buf = CopyStreamBuffer(max_queue_size=1)
        buf.write("a\n")  # 큐를 채운다
        done = threading.Event()

        def producer():
            buf.write("b\n")
            done.set()

        threading.Thread(target=producer, daemon=True).start()
        time.sleep(0.2)
        buf.cancel()
        assert done.wait(2), "취소 뒤에도 write()가 큐 빈자리를 기다립니다"


# ---------------------------------------------------------------------------
# H-03 일관 snapshot
# ---------------------------------------------------------------------------


def _source_tx_of(source: FakeSource, needle: str) -> list[int]:
    return [tx for tx, s in source.statements if needle in s]


class TestSourceSnapshot:
    def test_python_copy_reads_all_batches_in_one_repeatable_read_transaction(self, estimate):
        source, target = FakeSource(_rows(10)), FakeTarget()
        worker = _make_worker(source, target)
        worker.is_running = True
        worker._execute_migration()

        set_tx = _source_tx_of(source, "SET TRANSACTION")
        copies = _source_tx_of(source, "TO STDOUT")
        counts = _source_tx_of(source, "COUNT(*)")
        assert len(set_tx) == 1, "파티션마다 한 번 snapshot 트랜잭션을 열어야 합니다"
        stmt = next(s for _, s in source.statements if "SET TRANSACTION" in s).upper()
        assert "REPEATABLE READ" in stmt and "READ ONLY" in stmt
        assert len(copies) >= 3
        assert set(copies) == set(set_tx), "모든 배치가 같은 원본 트랜잭션이어야 합니다"
        assert counts and counts[-1] == set_tx[0], "완료 검증 COUNT도 같은 snapshot이어야 합니다"
        assert len(target.committed) == 10

    def test_concurrent_writes_do_not_leak_into_the_partition(self, estimate):
        """첫 배치 커밋 뒤 원본에 insert(지난 범위·앞 범위)/delete/update가 들어와도
        대상은 snapshot과 정확히 같고 완료로 기록된다."""
        original = _rows(20)
        source, target = FakeSource(original), FakeTarget()

        def concurrent_writer():
            source.rows.append((15, 1000, "late-below", True))  # 이미 지난 범위
            source.rows.append((500, 1000, "late-above", True))  # 아직 안 읽은 범위
            source.rows = [r for r in source.rows if r[0] != 150]  # 아직 안 읽은 행 삭제

        source.before_copy[2] = concurrent_writer
        worker = _make_worker(source, target)
        worker.is_running = True
        worker._execute_migration()

        assert sorted(target.committed) == sorted((r[0], r[1]) for r in original)
        completed = [kw for s, kw in _checkpoint_calls(worker) if s == "completed"]
        assert completed and completed[-1]["rows_processed"] == 20

    def test_server_copy_and_its_count_share_one_snapshot(self, estimate):
        source, target = FakeSource(_rows(10)), FakeTarget()

        def write_after_copy():
            # server COPY 뒤·검증 COUNT 전에 들어온 원본 쓰기
            source.rows.append((999, 1000, "late", True))

        worker = _make_worker(source, target, copy_mode="server")
        real_verify = worker._verify_partition_row_count

        def verify(name):
            write_after_copy()
            return real_verify(name)

        worker._verify_partition_row_count = verify
        worker.is_running = True
        worker._execute_migration()

        set_tx = _source_tx_of(source, "SET TRANSACTION")
        copies = _source_tx_of(source, "TO STDOUT")
        counts = _source_tx_of(source, "COUNT(*)")
        assert len(set_tx) == 1 and copies == set_tx and counts[-1] == set_tx[0]
        assert len(target.committed) == 10
        assert any(s == "completed" for s, _ in _checkpoint_calls(worker))


# ---------------------------------------------------------------------------
# H-04 schema 한정
# ---------------------------------------------------------------------------

_UNQUALIFIED = re.compile(r'(?<!"public"\.)"' + PARTITION + '"')


class TestSchemaQualifiedRelations:
    @pytest.mark.parametrize("mode", ["python", "server"])
    def test_every_statement_names_public_relation(self, estimate, mode):
        source, target = FakeSource(_rows(6)), FakeTarget()
        worker = _make_worker(source, target, copy_mode=mode)
        worker.is_running = True
        worker._execute_migration()

        stmts = [s for _, s in source.statements] + target.statements
        touching = [s for s in stmts if PARTITION in s]
        assert touching
        bad = [s for s in touching if _UNQUALIFIED.search(s)]
        assert not bad, f"schema 한정 없는 relation: {bad}"
        assert all(QUALIFIED in s for s in touching)

    def test_resume_anchor_queries_are_qualified(self, estimate):
        source, target = FakeSource(_rows(6)), FakeTarget()
        target.committed = [(10, 1000), (20, 1000)]
        worker = _make_worker(source, target)
        worker._prepare_target_table = MagicMock(return_value=(False, 2))
        worker.should_resume = True
        worker.is_running = True
        worker._execute_migration()

        anchors = [s for s in target.statements if "DESC LIMIT 1" in s]
        assert anchors and all(QUALIFIED in s for s in anchors)
        # 재개는 대상 마지막 키 뒤부터 이어 붙여 중복·누락이 없다.
        assert sorted(target.committed) == sorted((r[0], r[1]) for r in _rows(6))

    def test_no_bare_partition_identifier_in_worker_source(self):
        import src.core.copy_migration_worker as mod

        src = inspect.getsource(mod)
        assert "sql.Identifier(partition_name)" not in src
