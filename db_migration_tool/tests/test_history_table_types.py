from src.core.table_types import (
    TableType,
    get_all_table_types,
    get_partition_primary_key_columns,
    get_table_type,
    infer_partition_range,
    should_cluster_partition_by_pkey,
)


def test_point_sec_history_is_supported_table_type():
    assert TableType.POINT_SEC_HISTORY in get_all_table_types()
    assert get_table_type("point_sec_history") == TableType.POINT_SEC_HISTORY


def test_infer_partition_range_supports_daily_and_monthly_suffixes():
    daily_from, daily_to = infer_partition_range(
        TableType.POINT_SEC_HISTORY, "point_sec_history_260331"
    )
    monthly_from, monthly_to = infer_partition_range(TableType.TREND_HISTORY, "trend_history_2604")

    assert daily_from is not None and daily_to is not None
    assert monthly_from is not None and monthly_to is not None
    assert daily_from < daily_to
    assert monthly_from < monthly_to


def test_partition_primary_keys_match_historical_ddl():
    assert get_partition_primary_key_columns(TableType.POINT_HISTORY) == ["path_id", "issued_date"]
    assert get_partition_primary_key_columns(TableType.POINT_SEC_HISTORY) == [
        "path_id",
        "issued_date",
    ]
    assert get_partition_primary_key_columns(TableType.TREND_HISTORY) == ["path_id", "issued_date"]
    assert get_partition_primary_key_columns(TableType.ENERGY_DISPLAY) == [
        "sensor_id",
        "issued_date",
    ]
    assert get_partition_primary_key_columns(TableType.RUNNING_TIME_HISTORY) == [
        "path_id",
        "issued_date",
        "save_type",
    ]


def test_cluster_policy_matches_historical_ddl():
    assert should_cluster_partition_by_pkey(TableType.POINT_HISTORY) is True
    assert should_cluster_partition_by_pkey(TableType.POINT_SEC_HISTORY) is True
    assert should_cluster_partition_by_pkey(TableType.TREND_HISTORY) is True
    assert should_cluster_partition_by_pkey(TableType.ENERGY_DISPLAY) is True
    assert should_cluster_partition_by_pkey(TableType.RUNNING_TIME_HISTORY) is False
