"""File archive manifest helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.core.table_types import TableType, infer_partition_range


class ScanCancelled(Exception):
    """조회가 사용자 요청으로 중단됐다.

    실패가 아니라 취소다. UI가 이 둘을 구분해야 '오류'로 잘못 알리지 않는다.
    """


ARCHIVE_FORMAT_NAME = "psql93-migration-archive"
ARCHIVE_FORMAT_VERSION = 2
MANIFEST_FILENAME = "manifest.json"
MANIFEST_BACKUP_FILENAME = "manifest.json.bak"
PARTITIONS_DIRNAME = "partitions"

_SAFE_PARTITION_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


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
    checksum_sha256: str | None = None
    verified_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchivePartitionEntry:
        allowed = {item.name for item in fields(cls)}
        payload = {key: value for key, value in dict(data).items() if key in allowed}
        return cls(**payload)


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
    def from_dict(cls, data: dict[str, Any]) -> ArchiveManifest:
        allowed = {item.name for item in fields(cls)}
        payload = {key: value for key, value in dict(data).items() if key in allowed}
        return cls(**payload)


class ArchiveManifestStore:
    """Archive manifest read/write helper."""

    def __init__(self, archive_path: str | Path):
        self.archive_dir = self.resolve_archive_dir(archive_path)
        self.manifest_path = self.archive_dir / MANIFEST_FILENAME
        self.backup_path = self.archive_dir / MANIFEST_BACKUP_FILENAME
        self.partitions_dir = self.archive_dir / PARTITIONS_DIRNAME
        self._lock_path = self.archive_dir / ".manifest.lock"

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

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if not path.exists():
            return
        dir_fd = None
        try:
            flags = getattr(os, "O_RDONLY", 0)
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            dir_fd = os.open(str(path), flags)
            os.fsync(dir_fd)
        except Exception:
            # 플랫폼별 제약(예: Windows) 때문에 디렉터리 fsync가 실패할 수 있다.
            pass
        finally:
            if dir_fd is not None:
                os.close(dir_fd)

    def _atomic_write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex[:12]}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as fp:
                fp.write(text)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp_path, path)
            self._fsync_directory(path.parent)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            raise

    def replace_file_atomically(self, temp_path: str | Path, final_path: str | Path) -> None:
        src = Path(temp_path)
        dst = Path(final_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        self._fsync_directory(dst.parent)

    def ensure_archive(self) -> None:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.partitions_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_manifest_schema(data: dict[str, Any]) -> None:
        fmt = data.get("format")
        if fmt != ARCHIVE_FORMAT_NAME:
            raise ValueError(
                f"지원하지 않는 manifest 형식입니다: {fmt!r} (expected={ARCHIVE_FORMAT_NAME!r})"
            )
        version = data.get("version")
        if not isinstance(version, int) or version < 1 or version > ARCHIVE_FORMAT_VERSION:
            raise ValueError(
                f"지원하지 않는 manifest 버전입니다: {version!r} (max supported={ARCHIVE_FORMAT_VERSION})"
            )
        if not isinstance(data.get("partitions", []), list):
            raise ValueError("manifest partitions 필드가 list가 아닙니다.")

    def load(self) -> ArchiveManifest:
        candidates = [self.manifest_path, self.backup_path]
        errors: list[str] = []
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                self._validate_manifest_schema(data)
                return ArchiveManifest.from_dict(data)
            except Exception as exc:
                errors.append(f"{candidate}: {exc}")

        if errors:
            raise ValueError("manifest.json 로드 실패: " + " | ".join(errors))
        raise FileNotFoundError(f"manifest.json 파일을 찾을 수 없습니다: {self.manifest_path}")

    def load_or_create(
        self,
        *,
        source: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
    ) -> ArchiveManifest:
        if self.manifest_path.exists() or self.backup_path.exists():
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

    def _acquire_lock(self):
        """manifest 쓰기 직렬화를 위한 파일 락 획득. 실패 시 예외 발생."""
        self.ensure_archive()
        lock_fd = open(self._lock_path, "w")
        # os.name 대신 sys.platform 으로 분기해야 타입 체커가 플랫폼별 모듈
        # (msvcrt / fcntl)을 각각 해당 플랫폼에서만 검사한다.
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return lock_fd

    @staticmethod
    def _release_lock(lock_fd):
        try:
            lock_fd.close()
        except Exception:
            pass

    def save(self, manifest: ArchiveManifest, *, create_backup: bool = True) -> None:
        lock_fd = self._acquire_lock()
        try:
            # lock 하에서 디스크의 최신 manifest를 reload하여 병합 (lost-update 방지)
            if self.manifest_path.exists():
                try:
                    disk_data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                    disk_manifest = ArchiveManifest.from_dict(disk_data)

                    # parent_tables 병합 (in-memory 우선)
                    merged_parents = dict(disk_manifest.parent_tables)
                    merged_parents.update(manifest.parent_tables)
                    manifest.parent_tables = merged_parents

                    # partitions 병합 (partition_name 기준, in-memory 우선)
                    disk_by_name = {p.get("partition_name"): p for p in disk_manifest.partitions}
                    mem_by_name = {p.get("partition_name"): p for p in manifest.partitions}
                    disk_by_name.update(mem_by_name)
                    manifest.partitions = list(disk_by_name.values())
                except Exception:
                    pass

            manifest.updated_at = datetime.now().isoformat()
            payload = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2)

            if create_backup and self.manifest_path.exists():
                self._atomic_write_text(
                    self.backup_path,
                    self.manifest_path.read_text(encoding="utf-8"),
                )

            self._atomic_write_text(self.manifest_path, payload)
        finally:
            self._release_lock(lock_fd)

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

    def resolve_safe_path(self, relative_path: str) -> Path:
        """아카이브 루트 하위 경로만 허용, 절대경로/.. 탈출 차단."""
        if not relative_path:
            raise ValueError("빈 파일 경로입니다.")
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise ValueError(f"절대 경로는 허용되지 않습니다: {relative_path}")
        resolved = (self.archive_dir / candidate).resolve()
        archive_root = self.archive_dir.resolve()
        if not str(resolved).startswith(str(archive_root) + os.sep) and resolved != archive_root:
            raise ValueError(f"아카이브 루트를 벗어나는 경로입니다: {relative_path}")
        return resolved

    @staticmethod
    def _validate_partition_name(partition_name: str) -> str:
        if not partition_name or not _SAFE_PARTITION_NAME_RE.match(partition_name):
            raise ValueError(f"안전하지 않은 파티션 이름입니다: {partition_name!r}")
        return partition_name

    def build_partition_file_path(self, partition_name: str) -> Path:
        self._validate_partition_name(partition_name)
        return self.partitions_dir / f"{partition_name}.csv"

    def build_partition_temp_file_path(self, partition_name: str) -> Path:
        self._validate_partition_name(partition_name)
        return self.partitions_dir / f".{partition_name}.csv.tmp"

    def compute_file_metadata(
        self,
        file_path: str | Path,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """파일의 크기와 SHA-256을 구한다.

        Args:
            should_stop: 중간에 멈춰야 하는지 묻는 콜백. 수 GB 파일이면 이 루프가
                수 분씩 돌기 때문에, 취소 훅이 없으면 창을 닫아도 워커가 안 멈춘다.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"아카이브 데이터 파일이 없습니다: {path}")

        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as fp:
            while True:
                if should_stop is not None and should_stop():
                    raise ScanCancelled(f"체크섬 계산이 취소되었습니다: {path.name}")
                chunk = fp.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)

        return {
            "bytes_written": size,
            "checksum_sha256": digest.hexdigest(),
            "verified_at": datetime.now().isoformat(),
        }

    @staticmethod
    def _verify_entry_against_metadata(
        entry: ArchivePartitionEntry, metadata: dict[str, Any]
    ) -> None:
        expected_size = int(entry.bytes_written or 0)
        if expected_size and metadata["bytes_written"] != expected_size:
            raise ValueError(
                f"파일 크기 불일치: expected={expected_size}, actual={metadata['bytes_written']}"
            )
        expected_checksum = (entry.checksum_sha256 or "").strip().lower()
        if expected_checksum and metadata["checksum_sha256"] != expected_checksum:
            raise ValueError(
                "파일 체크섬 불일치: "
                f"expected={expected_checksum}, actual={metadata['checksum_sha256']}"
            )

    def verify_partition_file(
        self,
        entry: ArchivePartitionEntry,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        file_path = self.resolve_safe_path(entry.file_path)
        metadata = self.compute_file_metadata(file_path, should_stop=should_stop)
        self._verify_entry_against_metadata(entry, metadata)
        return metadata

    def verify_open_file(self, entry: ArchivePartitionEntry, fp) -> dict[str, Any]:
        """열린 파일 핸들을 통해 무결성 검증 (TOCTOU 방어)."""
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            size += len(chunk)
            digest.update(chunk)
        metadata = {
            "bytes_written": size,
            "checksum_sha256": digest.hexdigest(),
            "verified_at": datetime.now().isoformat(),
        }
        self._verify_entry_against_metadata(entry, metadata)
        return metadata

    def get_partition_entry(self, partition_name: str) -> ArchivePartitionEntry | None:
        manifest = self.load()
        for item in manifest.partitions:
            if item.get("partition_name") == partition_name:
                return ArchivePartitionEntry.from_dict(item)
        return None

    def get_completed_status(
        self,
        partition_names: list[str],
        *,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, bool]:
        """파티션별로 아카이브에 온전히 들어 있는지 확인한다.

        파일마다 체크섬을 다시 계산하므로 아카이브가 크면 오래 걸린다.
        크기 비교로 대체하지 않는다 — '완료' 오탐은 사용자가 그 파티션을
        건너뛰게 만들고, 손상된 데이터가 조용히 남는다.

        Args:
            should_stop: 중간에 멈춰야 하는지 묻는 콜백.
            on_progress: (done, total) 진행 상황 콜백.
        """
        manifest = self.load()
        by_name = {item.get("partition_name"): item for item in manifest.partitions}
        results: dict[str, bool] = {}
        total = len(partition_names)
        for index, name in enumerate(partition_names, start=1):
            if should_stop is not None and should_stop():
                raise ScanCancelled("완료 여부 확인이 취소되었습니다")

            item = by_name.get(name)
            if not item:
                results[name] = False
            else:
                try:
                    self.verify_partition_file(
                        ArchivePartitionEntry.from_dict(item),
                        should_stop=should_stop,
                    )
                    results[name] = True
                except ScanCancelled:
                    raise
                except Exception:
                    results[name] = False

            if on_progress is not None:
                on_progress(index, total)
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
