from datetime import date

from src.core.archive_manifest import ArchiveManifestStore, ArchivePartitionEntry
from src.core.table_types import TableType


def test_manifest_round_trip_and_completed_status(tmp_path):
    store = ArchiveManifestStore(tmp_path / "archive")
    manifest = store.load_or_create(source={"kind": "postgres"}, target={"kind": "file"})

    store.upsert_parent_table(
        manifest,
        parent_table="point_history",
        table_type=TableType.POINT_HISTORY,
        columns=[
            {
                "name": "path_id",
                "data_type": "integer",
                "character_maximum_length": None,
                "is_nullable": "NO",
                "column_default": None,
            }
        ],
    )
    entry = ArchivePartitionEntry(
        partition_name="point_history_240101",
        table_type="PH",
        parent_table="point_history",
        row_count=12,
        file_path="partitions/point_history_240101.csv",
        columns=["path_id", "issued_date", "changed_value", "connection_status"],
        from_timestamp=1704067200000,
        to_timestamp=1704153599000,
        bytes_written=128,
    )
    store.upsert_partition(manifest, entry)
    store.save(manifest)

    data_file = store.archive_dir / entry.file_path
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text("1,2,3,4\n", encoding="utf-8")

    loaded = store.get_partition_entry("point_history_240101")
    assert loaded is not None
    assert loaded.row_count == 12
    assert store.get_completed_status(["point_history_240101"])["point_history_240101"] is True


def test_filter_partitions_by_date_and_type(tmp_path):
    store = ArchiveManifestStore(tmp_path / "archive")
    manifest = store.load_or_create(source={"kind": "postgres"}, target={"kind": "file"})
    store.upsert_partition(
        manifest,
        ArchivePartitionEntry(
            partition_name="point_history_240101",
            table_type="PH",
            parent_table="point_history",
            row_count=5,
            file_path="partitions/point_history_240101.csv",
            columns=["path_id", "issued_date", "changed_value", "connection_status"],
            from_timestamp=1704067200000,
            to_timestamp=1704153599000,
        ),
    )
    store.upsert_partition(
        manifest,
        ArchivePartitionEntry(
            partition_name="trend_history_2402",
            table_type="TH",
            parent_table="trend_history",
            row_count=7,
            file_path="partitions/trend_history_2402.csv",
            columns=["path_id", "issued_date", "changed_value", "connection_status"],
            from_timestamp=1706745600000,
            to_timestamp=1709251199000,
        ),
    )
    store.save(manifest)

    results = store.filter_partitions(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        table_types=[TableType.POINT_HISTORY],
    )

    assert len(results) == 1
    assert results[0]["table_name"] == "point_history_240101"
    assert results[0]["table_type"] == TableType.POINT_HISTORY
