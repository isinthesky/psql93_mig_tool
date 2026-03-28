import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg

from src.core.copy_migration_worker import CopyMigrationWorker
from src.database.local_db import LocalDatabase
from src.models.history import CheckpointManager, HistoryManager
from src.models.profile import ProfileManager
from src.utils.app_paths import AppPaths


@dataclass
class Part:
    name: str
    day: datetime.date
    from_ms: int
    to_ms: int
    approx_rows: int


def ms_to_date(ms: int):
    return datetime.fromtimestamp(ms / 1000).date()


def pick_smallest_consecutive_window(parts: list[Part], days: int) -> list[Part]:
    parts = sorted(parts, key=lambda p: p.day)
    by_day = {p.day: p for p in parts}
    days_list = sorted(by_day.keys())

    best_parts: list[Part] | None = None
    best_sum: int | None = None

    for i in range(0, len(days_list) - days + 1):
        window_days = days_list[i : i + days]

        # consecutive check
        if any((window_days[j] - window_days[j - 1]).days != 1 for j in range(1, len(window_days))):
            continue

        window_parts = [by_day[d] for d in window_days]
        s = sum(max(0, int(p.approx_rows or 0)) for p in window_parts)
        if s <= 0:
            continue

        if best_sum is None or s < best_sum:
            best_sum = s
            best_parts = window_parts

    if not best_parts:
        raise RuntimeError("Could not find a consecutive non-empty window")
    return best_parts


def load_profile_bms93_to_bms30():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA not set")

    AppPaths.set_custom_root(Path(appdata))

    # Ensure global DB instance is initialized with the correct root
    import src.database.local_db as local_db
    local_db._db_instance = None
    local_db.get_db()


    pm = ProfileManager()
    profiles = pm.get_all_profiles()

    candidates = [
        p
        for p in profiles
        if (p.source_config or {}).get("database") == "bms93"
        and (p.target_config or {}).get("database") == "bms30"
    ]
    if not candidates:
        raise RuntimeError("No profile with source=bms93 and target=bms30")
    return candidates[0]


def discover_ph_parts(profile, limit: int = 180) -> list[Part]:
    sc = profile.source_config
    conn = psycopg.connect(
        host=sc["host"],
        port=sc["port"],
        dbname=sc["database"],
        user=sc["username"],
        password=sc["password"],
        connect_timeout=10,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.table_name, p.from_date, p.to_date, COALESCE(c.reltuples::bigint, 0) AS approx_rows
            FROM partition_table_info p
            LEFT JOIN pg_class c ON c.relname = p.table_name
            WHERE p.table_data = 'PH'
              AND p.use_flag = true
            ORDER BY p.from_date DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    conn.close()

    return [
        Part(
            name=r[0],
            day=ms_to_date(int(r[1])),
            from_ms=int(r[1]),
            to_ms=int(r[2]),
            approx_rows=int(r[3] or 0),
        )
        for r in rows
    ]


def verify_counts(profile, table_names: list[str]) -> tuple[bool, list[dict]]:
    sc = profile.source_config
    tc = profile.target_config
    sconn = psycopg.connect(
        host=sc["host"],
        port=sc["port"],
        dbname=sc["database"],
        user=sc["username"],
        password=sc["password"],
        connect_timeout=10,
    )
    tconn = psycopg.connect(
        host=tc["host"],
        port=tc["port"],
        dbname=tc["database"],
        user=tc["username"],
        password=tc["password"],
        connect_timeout=10,
    )

    results = []
    ok_all = True
    with sconn.cursor() as scur, tconn.cursor() as tcur:
        for name in table_names:
            scur.execute(f"SELECT COUNT(*) FROM {name}")
            s_count = int(scur.fetchone()[0])
            tcur.execute(f"SELECT COUNT(*) FROM {name}")
            t_count = int(tcur.fetchone()[0])
            ok = s_count == t_count
            ok_all = ok_all and ok
            results.append(
                {"table": name, "source": s_count, "target": t_count, "ok": ok}
            )

    sconn.close()
    tconn.close()
    return ok_all, results


def run_once(profile, partitions: list[str], start_day: str, end_day: str, copy_mode: str, batch_size: int) -> dict:
    hm = HistoryManager()
    cm = CheckpointManager()

    hist = hm.create_history(
        profile_id=profile.id,
        start_date=start_day,
        end_date=end_day,
        source_status=f"(bench) {copy_mode}",
        target_status=f"(bench) {copy_mode}",
    )
    history_id = hist.id

    for name in partitions:
        cm.create_checkpoint(history_id, name)

    worker = CopyMigrationWorker(
        profile=profile,
        partitions=partitions,
        history_id=history_id,
        resume=False,
        batch_size=batch_size,
        copy_mode=copy_mode,
    )
    worker.skip_on_error = False

    # auto-approve truncates (no UI)
    def on_truncate(table, row_count):
        print(f"[TRUNCATE_PROMPT] {table} rows={row_count:,} -> auto-YES")
        worker.truncate_permission = True

    worker.truncate_requested.connect(on_truncate)

    def on_log(msg, level):
        print(f"[{copy_mode}][{level}] {msg}")

    worker.log.connect(on_log)

    t0 = time.time()
    worker.run()  # synchronous
    elapsed = time.time() - t0

    cps = cm.get_checkpoints(history_id)
    completed = [c for c in cps if c.status == "completed"]
    failed = [c for c in cps if c.status == "failed"]

    processed_rows = sum(int(c.rows_processed or 0) for c in completed)

    if len(completed) == len(cps):
        hm.update_history_status(history_id, "completed", processed_rows=processed_rows)
    else:
        hm.update_history_status(history_id, "running", processed_rows=processed_rows)

    return {
        "copy_mode": copy_mode,
        "history_id": history_id,
        "elapsed_sec": elapsed,
        "completed": len(completed),
        "failed": len(failed),
        "processed_rows": processed_rows,
        "checkpoint_methods": [getattr(c, "copy_method", None) for c in cps],
    }


def main():
    days = int(os.environ.get("BENCH_DAYS", "2"))
    batch_size = int(os.environ.get("BENCH_BATCH", "50000"))

    # Order matters: run python first (creates baseline), then server (will TRUNCATE), then auto.
    modes = os.environ.get("BENCH_MODES", "python,server,auto").split(",")
    modes = [m.strip() for m in modes if m.strip()]

    profile = load_profile_bms93_to_bms30()
    print(f"[PROFILE] id={profile.id} name={profile.name}")

    parts = discover_ph_parts(profile)
    window = pick_smallest_consecutive_window(parts, days=days)

    start_day = window[0].day.strftime("%Y-%m-%d")
    end_day = window[-1].day.strftime("%Y-%m-%d")
    partitions = [p.name for p in window]

    print(f"[RANGE] days={days} {start_day} ~ {end_day}")
    for p in window:
        print(f"  - {p.name} day={p.day} approx_rows={p.approx_rows:,}")
    print(f"[RANGE_SUM] approx_rows_sum={sum(p.approx_rows for p in window):,}")

    results = []
    for mode in modes:
        print(f"\n=== RUN: {mode} ===")
        r = run_once(profile, partitions, start_day, end_day, mode, batch_size)
        print(f"[RESULT] {r}")

        # verify after each run
        v0 = time.time()
        ok, detail = verify_counts(profile, partitions)
        v_elapsed = time.time() - v0
        print(f"[VERIFY] ok={ok} elapsed={v_elapsed:.2f}s")
        for d in detail:
            print(f"  - {d['table']}: source={d['source']:,} target={d['target']:,} ok={d['ok']}")

        r["verify_ok"] = ok
        r["verify_elapsed_sec"] = v_elapsed
        results.append(r)

    print("\n=== SUMMARY ===")
    # print as simple table
    for r in results:
        rows = int(r["processed_rows"] or 0)
        sec = float(r["elapsed_sec"] or 0)
        speed = rows / sec if sec > 0 else 0
        print(
            f"- {r['copy_mode']:>6}: elapsed={sec:7.1f}s  rows={rows:,}  speed={speed:,.0f} rows/sec  verify_ok={r['verify_ok']}"
        )


if __name__ == "__main__":
    main()
