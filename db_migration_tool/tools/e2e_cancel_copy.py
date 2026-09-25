"""실DB E2E — Python COPY 이관 도중 stop() → 제한 시간 안 종료·잔여 자원 0 → 재개 → 집계 대조.

감사 H-02 완료 판정("COPY/commit 단계 취소가 제한 시간 안에 끝나고 thread·connection·producer가
남지 않음")을 실제 PostgreSQL(원본 9.3 → 대상 scratch)에서 확인한다.

1. 워커를 QThread로 실행(start). 배치가 N개 커밋된 뒤 다음 배치의 원본 COPY가 실제로 도는 것을
   pg_stat_activity로 확인하고 stop()을 부른다.
2. wait(제한 시간) 안에 스레드가 끝나는지, 생산자 스레드·워커 연결·원본/대상의 해당 COPY 세션이
   남지 않는지, 대상 행 수 == checkpoint에 기록된 커밋 행 수인지, 오류 시그널이 없는지 본다.
3. 계획 기준 재개(prepare_resume) → 완료 후 원본·대상 집계 5종 대조(e2e_verify_copy와 같은 방식).

안전장치는 e2e_verify_copy.py와 같다: 원본 읽기 전용, 대상 DB는 --allow-target-db(기본 temp)만,
대상에 같은 테이블이 있으면 건너뜀, 로컬 이력은 임시 폴더, --drop-after는 이 스크립트가 만든
테이블만 삭제. 비밀번호는 환경변수 DBMIG_E2E_SRC_PW / DBMIG_E2E_DST_PW로만 받는다.

예:
    uv run python tools/e2e_cancel_copy.py \\
        --src-host facreport.iptime.org --src-port 5446 --src-db bms93 \\
        --dst-host facreport.iptime.org --dst-port 5445 --dst-db temp \\
        --partition point_history_260503 --drop-after
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_verify_copy as base  # noqa: E402
from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402

from src.core.copy_migration_worker import CopyMigrationWorker  # noqa: E402
from src.database.local_db import LocalDatabase  # noqa: E402
from src.models.history import CheckpointManager, HistoryManager  # noqa: E402
from src.models.profile import ConnectionProfile  # noqa: E402
from src.utils.app_paths import AppPaths  # noqa: E402


def _active_copy_sessions(cfg: dict, table: str, direction: str) -> int:
    """이 파티션을 다루는 active COPY 세션 수(모니터링 연결 자신 제외, 읽기 전용 조회)."""
    pattern = f"COPY (SELECT%{table}%" if direction == "out" else f'COPY "public"."{table}"%'
    with base._connect(cfg) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
            "AND state = 'active' AND query LIKE %s",
            (pattern,),
        )
        return int(cur.fetchone()[0])


def _pump(app: QCoreApplication, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    for side in ("src", "dst"):
        ap.add_argument(f"--{side}-host", required=True)
        ap.add_argument(f"--{side}-port", type=int, required=True)
        ap.add_argument(f"--{side}-db", required=True)
        ap.add_argument(f"--{side}-user", default="postgres")
    ap.add_argument("--partition", required=True, help="중지→재개할 파티션(하나)")
    ap.add_argument(
        "--mode",
        choices=("python", "server"),
        default="python",
        help="중지할 실행 모드(server는 단일 COPY 도중 중지, 재개는 앱과 같이 Python COPY)",
    )
    ap.add_argument("--stop-after", type=int, default=3, help="이만큼 배치가 커밋된 뒤 중지")
    ap.add_argument("--batch-size", type=int, default=100_000)
    ap.add_argument("--bound", type=float, default=10.0, help="중지 허용 시간(초)")
    ap.add_argument("--allow-target-db", default="temp", help="쓰기를 허용할 대상 DB 이름")
    ap.add_argument("--drop-after", action="store_true")
    a = ap.parse_args()

    if a.dst_db != a.allow_target_db:
        print(f"대상 DB '{a.dst_db}'는 허용되지 않았습니다(--allow-target-db). 중단합니다.")
        return 2

    src = {
        "host": a.src_host,
        "port": a.src_port,
        "database": a.src_db,
        "username": a.src_user,
        "password": os.environ["DBMIG_E2E_SRC_PW"],
    }
    dst = {
        "host": a.dst_host,
        "port": a.dst_port,
        "database": a.dst_db,
        "username": a.dst_user,
        "password": os.environ["DBMIG_E2E_DST_PW"],
    }
    table = a.partition

    app = QCoreApplication.instance() or QCoreApplication([])
    AppPaths.set_custom_root(Path(tempfile.mkdtemp(prefix="dbmig-e2e-cancel-")))
    LocalDatabase().initialize()
    hm, cm = HistoryManager(), CheckpointManager()
    profile = ConnectionProfile(id=0, name="e2e", source_config=src, target_config=dst)

    print(f"== {a.mode} 중지→재개 {table}")
    if base._exists(dst, table):
        print("    대상에 이미 존재 — 덮어쓰지 않고 건너뜀")
        return 1

    hid = hm.create_planned_history(profile, [table], "e2e", "e2e").id
    assert hid is not None
    w = CopyMigrationWorker(
        profile, [table], hid, resume=False, batch_size=a.batch_size, copy_mode=a.mode
    )
    errors: list[str] = []
    w.error.connect(errors.append, Qt.ConnectionType.DirectConnection)
    w.log.connect(
        lambda m, lvl="INFO": print(f"    [{lvl}] {m}") if lvl in ("WARNING", "ERROR") else None,
        Qt.ConnectionType.DirectConnection,
    )
    committed = {"n": 0}
    reached = threading.Event()
    real = w.checkpoint_manager.update_checkpoint_status

    def spy(cid, status, _real=real, **kw):
        _real(cid, status, **kw)
        if status == "running" and kw.get("rows_processed"):
            committed["n"] += 1
            if committed["n"] >= a.stop_after:
                reached.set()
        elif a.mode == "server" and status == "running":
            reached.set()  # Server COPY는 파티션 통짜 — 시작 표시 뒤 COPY 도중에 중지한다

    w.checkpoint_manager.update_checkpoint_status = spy

    t_start = time.monotonic()
    w.start()
    while not reached.is_set():
        if not w.isRunning():
            print("    워커가 중지 지점 전에 끝났습니다")
            return 1
        _pump(app, 0.05)
    # 다음 배치의 원본 COPY가 실제로 도는 순간을 잡는다(배치 사이가 아니라 COPY 도중 중지).
    deadline = time.monotonic() + 30
    if a.mode == "python":
        # 배치 사이가 아니라 스트림 도중에 멈추도록, 소비자가 이번 배치 데이터를 받기 시작했고
        # 아직 EOF 전인 순간을 워커 내부 상태로 잡는다(네트워크 왕복 지연 없음).
        while True:
            st = w._active_stream
            if st is not None and st.consumed_chars > 0 and not st._eof:
                break
            if time.monotonic() > deadline:
                print("    진행 중 배치 스트림을 관측하지 못했습니다")
                return 1
            time.sleep(0.001)
        active_at_stop = 1
    else:
        while _active_copy_sessions(src, table, "out") == 0:
            if time.monotonic() > deadline:
                print("    원본 COPY를 관측하지 못했습니다")
                return 1
            time.sleep(0.02)
        _pump(app, 3.0)
        active_at_stop = _active_copy_sessions(src, table, "out")
    print(
        f"    {committed['n']}배치 커밋 뒤 stop() (시작 후 {time.monotonic() - t_start:.1f}s, "
        f"COPY 도중={active_at_stop > 0})"
    )

    t0 = time.monotonic()
    w.stop("e2e_cancel")
    finished = w.wait(int(a.bound * 1000))
    elapsed = time.monotonic() - t0
    _pump(app, 0.2)

    producers = [t.name for t in threading.enumerate() if t.name.startswith("copy-producer")]
    conns_closed = bool(w.source_conn is not None and w.source_conn.closed) and bool(
        w.target_conn is not None and w.target_conn.closed
    )
    time.sleep(1.0)  # 서버가 취소된 백엔드를 정리할 시간
    src_left = _active_copy_sessions(src, table, "out")
    dst_left = _active_copy_sessions(dst, table, "in")
    target_rows = base._aggregates(dst, table)[0]
    cps = [c for c in cm.get_checkpoints(hid) if c.partition_name == table]
    cp = cps[-1] if cps else None
    cp_rows = int(cp.rows_processed or 0) if cp else -1
    cp_status = cp.status if cp else None

    print(f"    stop→스레드 종료 {elapsed:.2f}s (제한 {a.bound:.0f}s) finished={finished}")
    print(f"    남은 생산자 스레드={producers} 워커 연결 닫힘={conns_closed}")
    print(f"    원본 active COPY 세션={src_left} 대상 active COPY 세션={dst_left}")
    print(f"    대상 행 수={target_rows:,} checkpoint={cp_status}/{cp_rows:,} 오류 시그널={errors}")
    cancel_ok = (
        active_at_stop > 0
        and finished
        and elapsed < a.bound
        and not producers
        and conns_closed
        and src_left == 0
        and dst_left == 0
        and target_rows == cp_rows
        and cp_status != "failed"
        and not errors
    )
    print(f"    CANCEL {'OK' if cancel_ok else 'FAIL'}")

    check = hm.prepare_resume(hid, profile)
    print(f"    재개 검증 {check.verdict} pending={check.pending}")
    resume_ok = bool(check.allowed and check.pending)
    if resume_ok:
        base._run(
            CopyMigrationWorker(
                profile,
                check.pending,
                hid,
                resume=True,
                batch_size=a.batch_size,
                copy_mode="python",
            )
        )
    cps = [c for c in cm.get_checkpoints(hid) if c.partition_name == table]
    status = cps[-1].status if cps else None
    s_agg, d_agg = base._aggregates(src, table), base._aggregates(dst, table)
    match = s_agg == d_agg and status == "completed"
    print(f"    checkpoint={status}\n    source={s_agg}\n    target={d_agg}")
    print(f"    RESULT {'MATCH' if match else 'MISMATCH'}")
    if a.drop_after:
        base._drop(dst, table)
        print("    (검증 후 대상 테이블 삭제)")

    ok = cancel_ok and resume_ok and match
    print("ALL OK" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
