from __future__ import annotations

"""Server-side COPY benchmark for point_history partitions.

Goal
- Avoid Python row-by-row serialization by streaming COPY bytes directly:
  source: COPY (SELECT ...) TO STDOUT
  target: COPY table FROM STDIN

Notes
- Uses TEXT format (tab-delimited, NULL=\N) for cross-version safety (PG9.3 -> PG16).
- Still supports batching (LIMIT) so we *can* keep the same shape as the app's worker.
- This script is intentionally standalone and does NOT write to the app's local sqlite history.

Usage (from db_migration_tool root)
- Windows/WSL:
  .venv\\Scripts\\python.exe -u tools\\bench_server_copy_ph_3days.py --password-env MIGTOOL_PW

  or from WSL bash (avoid backslash escaping):
  /mnt/c/.../db_migration_tool/.venv/Scripts/python.exe -u /mnt/c/.../db_migration_tool/tools/bench_server_copy_ph_3days.py --password-env MIGTOOL_PW

Env
- By default reads password from env var specified by --password-env.
"""

import argparse
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg2
from psycopg2 import sql

from src.core.table_creator import TableCreator
from src.core.table_types import TABLE_TYPE_CONFIG, TableType


@dataclass
class Conn:
    host: str
    port: int
    db: str
    user: str
    password: str


PH_COLS = TABLE_TYPE_CONFIG[TableType.POINT_HISTORY].columns


def stream_copy_full(src_conn, tgt_conn, partition: str, cols: list[str]):
    """서버사이드 COPY 스트리밍(전체 테이블).

    source: COPY (SELECT cols FROM partition ORDER BY issued_date, path_id) TO STDOUT
    target: COPY partition (cols) FROM STDIN

    TEXT 포맷을 사용해 PG 9.3 -> 16 간 호환성을 확보한다.
    """

    cols_sql = sql.SQL(", ").join(map(sql.Identifier, cols))
    tbl = sql.Identifier(partition)

    copy_to = sql.SQL(
        "COPY (SELECT {cols} FROM {tbl} ORDER BY issued_date, path_id) "
        "TO STDOUT WITH (DELIMITER E'\\t', NULL '\\N')"
    ).format(cols=cols_sql, tbl=tbl)

    copy_from = sql.SQL(
        "COPY {tbl} ({cols}) FROM STDIN WITH (DELIMITER E'\\t', NULL '\\N')"
    ).format(tbl=tbl, cols=cols_sql)

    rfd, wfd = os.pipe()
    rfile = os.fdopen(rfd, "rb", closefd=True)
    wfile = os.fdopen(wfd, "wb", closefd=True)

    errors: list[Exception] = []

    def _pump_src():
        try:
            with src_conn.cursor() as cur:
                cur.copy_expert(copy_to.as_string(src_conn), wfile)
        except Exception as e:
            errors.append(e)
        finally:
            try:
                wfile.close()
            except Exception:
                pass

    t = threading.Thread(target=_pump_src, daemon=True)
    t.start()

    try:
        with tgt_conn.cursor() as cur:
            cur.copy_expert(copy_from.as_string(tgt_conn), rfile)
    finally:
        try:
            rfile.close()
        except Exception:
            pass

    t.join(timeout=3600)
    if errors:
        raise errors[0]


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--src-host", default="192.168.0.48")
    ap.add_argument("--src-port", type=int, default=5446)
    ap.add_argument("--src-db", default="bms93")

    ap.add_argument("--tgt-host", default="192.168.0.48")
    ap.add_argument("--tgt-port", type=int, default=5445)
    ap.add_argument("--tgt-db", default="bms30")

    ap.add_argument("--user", default="migtool")
    ap.add_argument("--password-env", default="MIGTOOL_PW")

    ap.add_argument(
        "--partitions",
        default="point_history_260109,point_history_260110,point_history_260111",
        help="comma-separated partition table names",
    )

    ap.add_argument("--sync-commit-off", action="store_true", default=True)
    ap.add_argument("--out", default="bench_results/server_copy.json")

    args = ap.parse_args()

    pw = os.environ.get(args.password_env)
    if not pw:
        raise SystemExit(f"Missing password env var: {args.password_env} (set it before running)")

    src = Conn(args.src_host, args.src_port, args.src_db, args.user, pw)
    tgt = Conn(args.tgt_host, args.tgt_port, args.tgt_db, args.user, pw)

    partitions = [p.strip() for p in args.partitions.split(",") if p.strip()]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    per: dict[str, float] = {}

    src_conn = psycopg2.connect(
        host=src.host,
        port=src.port,
        database=src.db,
        user=src.user,
        password=src.password,
    )
    tgt_conn = psycopg2.connect(
        host=tgt.host,
        port=tgt.port,
        database=tgt.db,
        user=tgt.user,
        password=tgt.password,
    )

    try:
        if args.sync_commit_off:
            with tgt_conn.cursor() as cur:
                cur.execute("SET synchronous_commit = off")

        creator = TableCreator(src_conn, tgt_conn)

        for p in partitions:
            t1 = time.monotonic()

            # 대상 테이블 + partition_table_info 준비 (기존 데이터 있으면 TRUNCATE)
            creator.ensure_partition_ready(p, truncate_mode="auto")

            # 전체 테이블 서버사이드 COPY
            stream_copy_full(src_conn, tgt_conn, p, PH_COLS)
            tgt_conn.commit()

            per[p] = time.monotonic() - t1
            print(f"PART_DONE {p} sec={per[p]:.1f}", flush=True)

        elapsed = time.monotonic() - t0

        out = {
            "status": "done",
            "elapsed_sec": elapsed,
            "partitions": partitions,
            "per_partition_sec": per,
            "note": "server-side copy_expert (text) with pipe; full-table copy.",
        }
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"SERVER_COPY_DONE total_sec={elapsed:.1f} out={out_path}", flush=True)

    except Exception as e:
        elapsed = time.monotonic() - t0
        out = {
            "status": "error",
            "elapsed_sec": elapsed,
            "error": str(e),
            "partitions": partitions,
            "per_partition_sec": per,
        }
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        raise

    finally:
        try:
            src_conn.close()
        except Exception:
            pass
        try:
            tgt_conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
