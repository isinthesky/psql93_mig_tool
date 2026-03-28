import os
from pathlib import Path

import psycopg

from src.models.profile import ProfileManager
from src.utils.app_paths import AppPaths


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


def main():
    table_data = os.environ.get("TABLE_DATA", "ED").strip().upper()
    limit = int(os.environ.get("LIMIT", "30"))
    probe = int(os.environ.get("PROBE", "5"))

    profile = load_profile_bms93_to_bms30()
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
            SELECT p.table_name,
                   p.from_date,
                   p.to_date,
                   COALESCE(c.reltuples::bigint, 0) AS approx_rows,
                   pg_table_size(c.oid) AS table_bytes
            FROM partition_table_info p
            JOIN pg_class c ON c.relname = p.table_name
            WHERE p.table_data = %s
              AND p.use_flag = true
            ORDER BY p.from_date DESC
            LIMIT %s
            """,
            (table_data, limit),
        )
        rows = cur.fetchall()

        print(f"TABLE_DATA={table_data} found={len(rows)}")
        for r in rows:
            print(f"- {r[0]} approx_rows={int(r[3]):,} size={int(r[4]):,}B")

        print("\nPROBE(select 1 limit 1):")
        for r in rows[:probe]:
            name = r[0]
            try:
                cur.execute(f"SELECT 1 FROM {name} LIMIT 1")
                one = cur.fetchone()
                has_any = one is not None
            except Exception as e:
                has_any = False
                print(f"  - {name}: ERROR {e}")
                continue
            print(f"  - {name}: has_any={has_any}")

    conn.close()


if __name__ == "__main__":
    main()
