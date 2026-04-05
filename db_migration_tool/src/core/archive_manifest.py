"""File archive manifest helpers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from src.core.table_types import TableType, infer_partition_range

ARCHIVE_FORMAT_NAME = "psql93-migration-archive"
ARCHIVE_FORMAT_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
PARTITIONS_DIRNAME = "partitions"


@dataclass
class ArchivePartitionEntry:
    partition_name: str
    table_type: str
    parent_table: str
    row_count: int
    file_path: str
    columns: list[str]
    from_timestamp: int | None = None
    to_timestamp: int | None = None
    bytes_written: int = 0
    exported_at: str | None = None
    copy_method: str = "FILE_ARCHIVE"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArchivePartitionEntry":
        return cls(**data)


@dataclass
class ArchiveManifest:
    format: str = ARCHIVE_FORMAT_NAME
    version: int = ARCHIVE_FORMAT_VERSION
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    source: dict[str, Any] = field(default_factory=dict)
    target: dict[str, Any] = field(default_factory=dict)
    parent_tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    partitions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArchiveManifest":
        return cls(**data)


class ArchiveManifestStore:
    """Archive manifest read/write helper."""

    def __init__(self, archive_path: str | Path):
        self.archive_dir = self.resolve_archive_dir(archive_path)
        self.manifest_path = self.archive_dir / MANIFEST_FILENAME
        self.partitions_dir = self.archive_dir / PARTITIONS_DIRNAME

    @staticmethod
    def resolve_archive_dir(archive_path: str | Path) -> Path:
        path = Path(archive_path).expanduser()
        if path.name == MANIFEST_FILENAME:
            return path.parent
        return path

    @staticmethod
    def sanitize_endpoint(config: dict[str, Any] | None) -> dict[str, Any]:
        endpoint = dict(config or {})
        endpoint.pop("password", None)
        return endpoint

    def ensure_archive(self) -> None:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.partitions_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> ArchiveManifest:
        if not self.manifest_path.exists():
            raise FileNotFoundError(f"manifest.json 파일을 찾을 수 없습니다: {self.manifest_path}")
        return ArchiveManifest.from_dict(json.loads(self.manifest_path.read_text(encoding="utf-8")))

    def load_or_create(
        self,
        *,
        source: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
    ) -> ArchiveManifest:
        if self.manifest_path.exists():
            manifest = self.load()
        else:
            manifest = ArchiveManifest()

        if source is not None:
            manifest.source = self.sanitize_endpoint(source)
        if target is not None:
            manifest.target = self.sanitize_endpoint(target)
        manifest.updated_at = datetime.now().isoformat()
        self.save(manifest)
        return manifest

    def save(self, manifest: ArchiveManifest) -> None:
        self.ensure_archive()
        manifest.updated_at = datetime.now().isoformat()
        self.manifest_path.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def upsert_parent_table(
        self,
        manifest: ArchiveManifest,
        *,
        parent_table: str,
        table_type: TableType,
        columns: list[dict[str, Any]],
    ) -> None:
        manifest.parent_tables[parent_table] = {
            "table_type": table_type.value,
            "table_name": table_type.table_name,
            "date_column": table_type.date_column,
            "date_is_timestamp": table_type.date_is_timestamp,
            "columns": columns,
        }

    def upsert_partition(self, manifest: ArchiveManifest, entry: ArchivePartitionEntry) -> None:
        payload = asdict(entry)
        for idx, existing in enumerate(manifest.partitions):
            if existing.get("partition_name") == entry.partition_name:
                manifest.partitions[idx] = payload
                break
        else:
            manifest.partitions.append(payload)

    def build_partition_file_path(self, partition_name: str) -> Path:
        return self.partitions_dir / f"{partition_name}.csv"

    def get_partition_entry(self, partition_name: str) -> ArchivePartitionEntry | None:
        manifest = self.load()
        for item in manifest.partitions:
            if item.get("partition_name") == partition_name:
                return ArchivePartitionEntry.from_dict(item)
        return None

    def get_partition_entries(self) -> list[ArchivePartitionEntry]:
        manifest = self.load()
        return [ArchivePartitionEntry.from_dict(item) for item in manifest.partitions]

    def get_completed_status(self, partition_names: list[str]) -> dict[str, bool]:
        manifest = self.load()
        by_name = {item.get("partition_name"): item for item in manifest.partitions}
        results: dict[str, bool] = {}
        for name in partition_names:
            item = by_name.get(name)
            if not item:
                results[name] = False
                continue
            file_path = self.archive_dir / item.get("file_path", "")
            results[name] = bool(file_path.exists())
        return results

    def filter_partitions(
        self,
        *,
        start_date: date,
        end_date: date,
        table_types: list[TableType],
    ) -> list[dict[str, Any]]:
        manifest = self.load()
        selected_codes = {table_type.value for table_type in table_types}
        partitions: list[dict[str, Any]] = []

        for item in manifest.partitions:
            entry = ArchivePartitionEntry.from_dict(item)
            if entry.table_type not in selected_codes:
                continue

            try:
                table_type = TableType(entry.table_type)
            except ValueError:
                continue

            from_ts = entry.from_timestamp
            to_ts = entry.to_timestamp
            if from_ts is None or to_ts is None:
                inferred_from, inferred_to = infer_partition_range(table_type, entry.partition_name)
                from_ts = from_ts if from_ts is not None else inferred_from
                to_ts = to_ts if to_ts is not None else inferred_to

            if from_ts is None or to_ts is None:
                continue

            partition_start = datetime.fromtimestamp(from_ts / 1000).date()
            partition_end = datetime.fromtimestamp(to_ts / 1000).date()
            if partition_start > end_date or partition_end < start_date:
                continue

            partitions.append(
                {
                    "table_name": entry.partition_name,
                    "table_type": table_type,
                    "table_type_code": table_type.value,
                    "start_date": partition_start,
                    "end_date": partition_end,
                    "row_count": int(entry.row_count or 0),
                    "from_timestamp": from_ts,
                    "to_timestamp": to_ts,
                    "file_path": entry.file_path,
                }
            )

        partitions.sort(key=lambda part: (part["table_type_code"], part["from_timestamp"] or 0))
        return partitions

    def get_parent_table_metadata(self, parent_table: str) -> dict[str, Any]:
        manifest = self.load()
        metadata = manifest.parent_tables.get(parent_table)
        if not metadata:
            raise KeyError(f"parent_table metadata not found: {parent_table}")
        return metadata
