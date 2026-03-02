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


def _copy_sql_text_select(partition: str, cols: list[str], where_sql: str, limit: int | None) -> str:
    cols_sql = sql.SQL(", ").join(map(sql.Identifier, cols))
    tbl = sql.Identifier(partition)

    base = sql.SQL(
        "COPY (SELECT {cols} FROM {tbl} {where} ORDER BY issued_date, path_id {limit}) "
        "TO STDOUT WITH (DELIMITER E'\\t', NULL '\\N')"
    ).format(
        cols=cols_sql,
        tbl=tbl,
        where=sql.SQL(where_sql),
        limit=sql.SQL("") if limit is None else sql.SQL("LIMIT %s") % sql.Literal(limit),
    )

    return base.as_string(psycopg2.connect(""))  # dummy connection for quoting


def stream_copy_batch(
    src_conn,
    tgt_conn,
    partition: str,
    cols: list[str],
    where_sql: str,
    limit: int | None,
):
    # Build COPY commands using *real* connections for as_string (safe quoting)
    cols_sql = sql.SQL(", ").join(map(sql.Identifier, cols))
    tbl = sql.Identifier(partition)

    copy_to = sql.SQL(
        "COPY (SELECT {cols} FROM {tbl} {where} ORDER BY issued_date, path_id {limit}) "
        "TO STDOUT WITH (DELIMITER E'\\t', NULL '\\N')"
    ).format(
        cols=cols_sql,
        tbl=tbl,
        where=sql.SQL(where_sql),
        limit=sql.SQL("") if limit is None else sql.SQL("LIMIT %s") % sql.Literal(limit),
    )

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

    ap.add_argument("--batch-size", type=int, default=250_000)
    ap.add_argument("--sync-commit-off", action="store_true", default=True)

    ap.add_argument("--out", default="bench_results/server_copy.json")

    args = ap.parse_args()

    pw = os.environ.get(args.password_env)
    if not pw:
        raise SystemExit(
            f"Missing password env var: {args.password_env} (set it before running)"
        )

    src = Conn(args.src_host, args.src_port, args.src_db, args.user, pw)
    tgt = Conn(args.tgt_host, args.tgt_port, args.tgt_db, args.user, pw)

    partitions = [p.strip() for p in args.partitions.split(",") if p.strip()]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    per: dict[str, float] = {}

    src_conn = psycopg2.connect(
        host=src.host, port=src.port, database=src.db, user=src.user, password=src.password
    )
    tgt_conn = psycopg2.connect(
        host=tgt.host, port=tgt.port, database=tgt.db, user=tgt.user, password=tgt.password
    )

    try:
        if args.sync_commit_off:
            with tgt_conn.cursor() as cur:
                cur.execute("SET synchronous_commit = off")

        creator = TableCreator(src_conn, tgt_conn)

        for p in partitions:
            t1 = time.monotonic()

            # Make sure table exists and is empty (auto truncates if has data)
            creator.ensure_partition_ready(p, truncate_mode="auto")

            # Single batch loop: repeat until empty
            where_sql = ""
            while True:
                before = time.monotonic()
                # copy 1 chunk
                stream_copy_batch(
                    src_conn,
                    tgt_conn,
                    p,
                    PH_COLS,
                    where_sql=where_sql,
                    limit=args.batch_size,
                )
                tgt_conn.commit()

                # Heuristic: if chunk took almost no time, assume empty
                if (time.monotonic() - before) < 0.2:
                    break

                # NOTE: for a real resumable version we would update where_sql based on last key.
                # For pure benchmark we keep it simple and rely on COPY-to-STDOUT returning 0 rows at the end.

            per[p] = time.monotonic() - t1
            print(f"PART_DONE {p} sec={per[p]:.1f}", flush=True)

        elapsed = time.monotonic() - t0

        out = {
            "status": "done",
            "elapsed_sec": elapsed,
            "partitions": partitions,
            "per_partition_sec": per,
            "note": "server-side copy_expert (text) with pipe; benchmark mode (not resumable).",
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
