"""실DB E2E 이관 검증 — 원본 파티션을 scratch 대상 DB로 옮기고 독립 집계로 대조한다.

CopyMigrationWorker를 GUI 없이 그대로 돌린다(Python COPY / Server COPY / 중단→재개).
완료 후 원본·대상에서 count, sum(path_id), sum(issued_date), sum(length(changed_value)),
connection_status 참 개수를 각각 계산해 비교한다. 워커가 스스로 센 숫자는 믿지 않는다.

안전장치
- 원본은 읽기만 한다.
- 대상 DB는 기본 `temp`만 허용한다(--allow-target-db로 명시해야 다른 DB 허용).
- 대상에 같은 이름의 테이블이 이미 있으면 그 시나리오는 건너뛴다(덮어쓰지 않음).
- 로컬 이력 DB는 임시 폴더를 쓴다(실제 %APPDATA% 프로필·이력에 손대지 않음).
- --drop-after를 주면 이 스크립트가 만든 테이블만 검증 후 삭제한다.

비밀번호는 환경변수로만 받는다: DBMIG_E2E_SRC_PW, DBMIG_E2E_DST_PW

예:
    uv run python tools/e2e_verify_copy.py \\
        --src-host facreport.iptime.org --src-port 5446 --src-db bms93 \\
        --dst-host facreport.iptime.org --dst-port 5445 --dst-db temp \\
        --python point_history_260418 --server point_history_260419 \\
        --resume point_history_260420 --drop-after
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402

from src.core.copy_migration_worker import CopyMigrationWorker  # noqa: E402
from src.database.local_db import LocalDatabase  # noqa: E402
from src.models.history import CheckpointManager, HistoryManager  # noqa: E402
from src.models.profile import ConnectionProfile  # noqa: E402
from src.utils.app_paths import AppPaths  # noqa: E402

_AGG = (
    "SELECT count(*), coalesce(sum(path_id),0), coalesce(sum(issued_date),0), "
    "coalesce(sum(length(coalesce(changed_value,''))),0), "
    "coalesce(sum(CASE WHEN connection_status THEN 1 ELSE 0 END),0) FROM {t}"
)


def _connect(cfg: dict):
    return psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["database"],
        user=cfg["username"],
        password=cfg["password"],
    )


def _aggregates(cfg: dict, table: str) -> tuple[int, ...]:
    with _connect(cfg) as conn, conn.cursor() as cur:
        cur.execute(_AGG.format(t=psycopg2.extensions.quote_ident(table, cur)))
        return tuple(int(x) for x in cur.fetchone())


def _exists(cfg: dict, table: str) -> bool:
    with _connect(cfg) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
        return bool(cur.fetchone()[0])


def _drop(cfg: dict, table: str) -> None:
    with _connect(cfg) as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE {psycopg2.extensions.quote_ident(table, cur)}")


def _run(worker: CopyMigrationWorker) -> str | None:
    worker.log.connect(
        lambda m, lvl="INFO": (
            print(f"    [{lvl}] {m}")
            if lvl in ("SUCCESS", "WARNING", "ERROR") or "재개" in m
            else None
        )
    )
    worker.is_running = True
    t0 = time.time()
    try:
        worker._execute_migration()
        err = None
    except Exception as exc:  # noqa: BLE001 — 결과로 보고한다
        err = str(exc)
    print(f"    elapsed {time.time() - t0:.1f}s error={err}")
    return err


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    for side in ("src", "dst"):
        ap.add_argument(f"--{side}-host", required=True)
        ap.add_argument(f"--{side}-port", type=int, required=True)
        ap.add_argument(f"--{side}-db", required=True)
        ap.add_argument(f"--{side}-user", default="postgres")
    ap.add_argument("--python", nargs="*", default=[], help="Python COPY 전체 이관할 파티션")
    ap.add_argument("--server", nargs="*", default=[], help="Server COPY 전체 이관할 파티션")
    ap.add_argument("--resume", nargs="*", default=[], help="Python COPY 중단→재개할 파티션")
    ap.add_argument("--stop-after", type=int, default=5, help="재개 시나리오에서 중지할 배치 수")
    ap.add_argument("--batch-size", type=int, default=100_000)
    ap.add_argument("--allow-target-db", default="temp", help="쓰기를 허용할 대상 DB 이름")
    ap.add_argument(
        "--drop-after", action="store_true", help="검증 후 이 스크립트가 만든 테이블 삭제"
    )
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

    _app = QCoreApplication.instance() or QCoreApplication([])  # noqa: F841 — 시그널용
    AppPaths.set_custom_root(Path(tempfile.mkdtemp(prefix="dbmig-e2e-")))
    LocalDatabase().initialize()
    hm, cm = HistoryManager(), CheckpointManager()
    profile = ConnectionProfile(id=0, name="e2e", source_config=src, target_config=dst)

    plan = [("python", t, False) for t in a.python]
    plan += [("server", t, False) for t in a.server]
    plan += [("python", t, True) for t in a.resume]
    ok = bool(plan)
    for mode, table, interrupt in plan:
        print(f"== {mode}{' (중단→재개)' if interrupt else ''} {table}")
        if _exists(dst, table):
            print("    대상에 이미 존재 — 덮어쓰지 않고 건너뜀")
            ok = False
            continue
        hid = hm.create_history(profile_id=0, start_date="e2e", end_date="e2e").id
        w = CopyMigrationWorker(
            profile, [table], hid, resume=False, batch_size=a.batch_size, copy_mode=mode
        )
        if interrupt:
            real, seen = w.checkpoint_manager.update_checkpoint_status, {"n": 0}

            def spy(cid, status, _real=real, _seen=seen, _w=w, **kw):
                _real(cid, status, **kw)
                if status == "running" and kw.get("rows_processed"):
                    _seen["n"] += 1
                    if _seen["n"] >= a.stop_after:
                        _w.is_running = False

            w.checkpoint_manager.update_checkpoint_status = spy
        _run(w)
        if interrupt:
            print(f"    중단 시점 대상 {_aggregates(dst, table)[0]:,}행 → 재개")
            _run(
                CopyMigrationWorker(
                    profile, [table], hid, resume=True, batch_size=a.batch_size, copy_mode=mode
                )
            )
        cps = [c for c in cm.get_checkpoints(hid) if c.partition_name == table]
        status = cps[-1].status if cps else None
        s_agg, d_agg = _aggregates(src, table), _aggregates(dst, table)
        match = s_agg == d_agg and status == "completed"
        ok = ok and match
        print(f"    checkpoint={status}\n    source={s_agg}\n    target={d_agg}")
        print(f"    RESULT {'MATCH' if match else 'MISMATCH'}")
        if a.drop_after:
            _drop(dst, table)
            print("    (검증 후 대상 테이블 삭제)")

    print("ALL OK" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
