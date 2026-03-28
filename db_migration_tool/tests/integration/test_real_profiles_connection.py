import os
from pathlib import Path

import pytest

from src.database.local_db import LocalDatabase
from src.database.postgres_utils import PostgresOptimizer
from src.models.profile import ProfileManager
from src.utils.app_paths import AppPaths


@pytest.mark.integration
def test_saved_profiles_bms93_bms30_can_connect():
    """Optional integration test.

    Uses the user's saved profile DB (APPDATA/db_migration.db) and checks that
    both source(bms93) and target(bms30) connections are reachable.

    This test is skipped by default because it depends on local network/DB.

    Enable:
        set DBMIG_RUN_REAL_PROFILE_TESTS=1
        pytest -k saved_profiles -m integration
    """

    flag = (os.environ.get("DBMIG_RUN_REAL_PROFILE_TESTS") or "").strip().lower()
    if flag not in ("1", "true", "yes"):  # allow cmd/powershell quirks
        pytest.skip("Set DBMIG_RUN_REAL_PROFILE_TESTS=1 to run real DB connection test")

    appdata = os.environ.get("APPDATA")
    if not appdata:
        pytest.skip("APPDATA is not set")

    root = Path(appdata)

    # Force the app to use the real user appdata folder where db_migration.db exists.
    AppPaths.set_custom_root(root)

    # Ensure schema is initialized/migrated
    LocalDatabase().initialize()

    pm = ProfileManager()
    profiles = pm.get_all_profiles()
    assert profiles, "No saved profiles found in APPDATA/db_migration.db"

    candidates = [
        p
        for p in profiles
        if (p.source_config or {}).get("database") == "bms93"
        and (p.target_config or {}).get("database") == "bms30"
    ]
    if not candidates:
        pytest.skip("No profile with source=bms93 and target=bms30 found")

    prof = candidates[0]

    ok, msg = PostgresOptimizer.check_connection_quick(prof.source_config)
    assert ok, f"Source(bms93) connection failed: {msg}"

    ok, msg = PostgresOptimizer.check_connection_quick(prof.target_config)
    assert ok, f"Target(bms30) connection failed: {msg}"
