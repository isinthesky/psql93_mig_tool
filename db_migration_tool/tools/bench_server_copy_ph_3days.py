from __future__ import annotations

"""Server-side COPY benchmark (PG 9.3 -> PG 16) for point_history partitions.

Why
- Current CopyMigrationWorker converts rows to TSV in Python, which can be CPU-bound.
- This benchmark streams COPY bytes directly:
  source: COPY (SELECT ...) TO STDOUT
  target: COPY table (...) FROM STDIN

Important
- Uses TEXT format (tab-delimited, NULL=\\N) for cross-version safety.
- This is a *benchmark tool* (not resumable / not checkpoint-aware).

Run (Windows CMD recommended)
- In db_migration_tool directory:

  set MIGTOOL_PW=... 
  tools\\run_bench_server_copy.cmd

Run (WSL bash safe: NO backslash paths)
- From WSL:

  MIGTOOL_PW=... \
  /mnt/c/Users/hijde/Apps/psql93_mig_tool/db_migration_tool/.venv/Scripts/python.exe \
    -u /mnt/c/Users/hijde/Apps/psql93_mig_tool/db_migration_tool/tools/bench_server_copy_ph_3days.py \
    --password-env MIGTOOL_PW
"""

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg2
from psycopg2 import sql

# Ensure project root is on sys.path even when executed as tools\\script.py
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.table_creator import TableCreator  # noqa: E402
from src.core.table_types import TABLE_TYPE_CONFIG, TableType  # noqa: E402


@dataclass
class Conn:
    host: str
    port: int
    dbname: str
    user: str
    password: str


PH_COLS = TABLE_TYPE_CONFIG[TableType.POINT_HISTORY].columns


def stream_copy_full_text(
    src_conn,
    tgt_conn,
    *,
    partition: str,
    cols: list[str],
):
    """Stream COPY via an in-process pipe (source -> target)."""

    cols_sql = sql.SQL(", ").join(map(sql.Identifier, cols))
    tbl = sql.Identifier(partition)

    copy_to = sql.SQL(
        "COPY (SELECT {cols} FROM {tbl} ORDER BY issued_date, path_id) "
        "TO STDOUT WITH (FORMAT text, DELIMITER E'\\t', NULL '\\N')"
    ).format(cols=cols_sql, tbl=tbl)

    copy_from = sql.SQL(
        "COPY {tbl} ({cols}) FROM STDIN WITH (FORMAT text, DELIMITER E'\\t', NULL '\\N')"
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
    )

    ap.add_argument(
        "--sync-commit-off",
        action="store_true",
        default=True,
        help="SET synchronous_commit=off on target session (test DB recommended)",
    )

    ap.add_argument("--out", default="bench_results/server_copy.json")

    args = ap.parse_args()

    pw = os.environ.get(args.password_env)
    if not pw:
        raise SystemExit(f"Missing password env var: {args.password_env}")

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
        database=src.dbname,
        user=src.user,
        password=src.password,
    )
    tgt_conn = psycopg2.connect(
        host=tgt.host,
        port=tgt.port,
        database=tgt.dbname,
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
            creator.ensure_partition_ready(p, truncate_mode="auto")
            stream_copy_full_text(src_conn, tgt_conn, partition=p, cols=PH_COLS)
            tgt_conn.commit()
            per[p] = time.monotonic() - t1
            print(f"PART_DONE {p} sec={per[p]:.1f}", flush=True)

        elapsed = time.monotonic() - t0
        out = {
            "status": "done",
            "elapsed_sec": elapsed,
            "partitions": partitions,
            "per_partition_sec": per,
            "note": "server-side COPY via psycopg2 copy_expert + os.pipe; TEXT format; target synchronous_commit=off",
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
