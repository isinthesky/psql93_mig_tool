"""M-02: manifest 동시 저장 — 항목별 version과 CAS.

감사 문서(code-audit-remediation-2026-08-26 §3.3 M-02): lock 안에서 디스크를 다시 읽어도
오래된 메모리 값이 최신 디스크 값을 덮어쓸 수 있었다. 여기서는 그 lost update를
재현하고, 같은 항목을 동시에 고치면 조용히 덮지 않고 충돌로 실패하는지 확인한다.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from src.core.archive_manifest import (
    ArchiveManifestStore,
    ArchivePartitionEntry,
    ManifestConflictError,
)

APP_ROOT = Path(__file__).resolve().parents[2]
COLUMNS = ["path_id", "issued_date", "changed_value", "connection_status"]


def _entry(name: str, *, row_count: int = 1, checksum: str | None = None) -> ArchivePartitionEntry:
    return ArchivePartitionEntry(
        partition_name=name,
        table_type="PH",
        parent_table="point_history",
        row_count=row_count,
        file_path=f"partitions/{name}.csv",
        columns=list(COLUMNS),
        bytes_written=1,
        checksum_sha256=checksum or f"{row_count:064x}",
    )


def _disk(store: ArchiveManifestStore) -> dict[str, dict]:
    data = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    return {item["partition_name"]: item for item in data["partitions"]}


def test_stale_in_memory_entry_does_not_revert_newer_disk_entry(tmp_path):
    archive = tmp_path / "archive"
    seed = ArchiveManifestStore(archive)
    manifest = seed.load_or_create(source={"kind": "postgres"}, target={"kind": "file"})
    seed.upsert_partition(manifest, _entry("point_history_240101", row_count=1))
    seed.save(manifest)

    # A는 작업 시작 시점에 manifest를 읽어 두고 오래 들고 있다(워커와 같은 패턴).
    store_a = ArchiveManifestStore(archive)
    manifest_a = store_a.load()

    # B가 같은 파티션을 다시 내보내 디스크 값을 새로 바꾼다.
    store_b = ArchiveManifestStore(archive)
    manifest_b = store_b.load()
    store_b.upsert_partition(manifest_b, _entry("point_history_240101", row_count=2))
    store_b.save(manifest_b)

    # A는 다른 파티션만 추가했을 뿐인데, 예전 코드는 A의 낡은 p1이 B의 값을 덮었다.
    store_a.upsert_partition(manifest_a, _entry("point_history_240102", row_count=7))
    store_a.save(manifest_a)

    disk = _disk(seed)
    assert disk["point_history_240101"]["row_count"] == 2
    assert disk["point_history_240102"]["row_count"] == 7
    # save 뒤 A의 메모리도 최신 디스크 값으로 맞춰진다.
    names = {item["partition_name"]: item for item in manifest_a.partitions}
    assert names["point_history_240101"]["row_count"] == 2


def test_concurrent_update_of_same_entry_raises_conflict(tmp_path):
    archive = tmp_path / "archive"
    seed = ArchiveManifestStore(archive)
    manifest = seed.load_or_create()
    seed.upsert_partition(manifest, _entry("point_history_240101", row_count=1))
    seed.save(manifest)

    store_a = ArchiveManifestStore(archive)
    store_b = ArchiveManifestStore(archive)
    manifest_a = store_a.load()
    manifest_b = store_b.load()

    store_a.upsert_partition(manifest_a, _entry("point_history_240101", row_count=10))
    store_a.save(manifest_a)

    store_b.upsert_partition(manifest_b, _entry("point_history_240101", row_count=20))
    with pytest.raises(ManifestConflictError, match="point_history_240101"):
        store_b.save(manifest_b)

    assert _disk(seed)["point_history_240101"]["row_count"] == 10


def test_concurrent_creation_of_same_new_entry_raises_conflict(tmp_path):
    archive = tmp_path / "archive"
    ArchiveManifestStore(archive).load_or_create()
    store_a = ArchiveManifestStore(archive)
    store_b = ArchiveManifestStore(archive)
    manifest_a = store_a.load()
    manifest_b = store_b.load()

    store_a.upsert_partition(manifest_a, _entry("point_history_240101", row_count=1))
    store_a.save(manifest_a)
    store_b.upsert_partition(manifest_b, _entry("point_history_240101", row_count=2))
    with pytest.raises(ManifestConflictError):
        store_b.save(manifest_b)


def test_identical_concurrent_write_is_not_a_conflict(tmp_path):
    archive = tmp_path / "archive"
    ArchiveManifestStore(archive).load_or_create()
    store_a = ArchiveManifestStore(archive)
    store_b = ArchiveManifestStore(archive)
    manifest_a = store_a.load()
    manifest_b = store_b.load()

    store_a.upsert_partition(manifest_a, _entry("point_history_240101", row_count=3))
    store_a.save(manifest_a)
    store_b.upsert_partition(manifest_b, _entry("point_history_240101", row_count=3))
    store_b.save(manifest_b)  # 같은 값이면 충돌로 보지 않는다

    assert _disk(store_a)["point_history_240101"]["entry_version"] == 1


def test_entry_version_increments_on_each_write_and_untouched_entries_keep_version(tmp_path):
    store = ArchiveManifestStore(tmp_path / "archive")
    manifest = store.load_or_create()
    store.upsert_partition(manifest, _entry("point_history_240101", row_count=1))
    store.upsert_partition(manifest, _entry("point_history_240102", row_count=1))
    store.save(manifest)
    store.upsert_partition(manifest, _entry("point_history_240101", row_count=2))
    store.save(manifest)
    store.save(manifest)  # 변경 없음

    disk = _disk(store)
    assert disk["point_history_240101"]["entry_version"] == 2
    assert disk["point_history_240102"]["entry_version"] == 1


def test_stale_parent_table_metadata_does_not_overwrite_disk(tmp_path):
    archive = tmp_path / "archive"
    seed = ArchiveManifestStore(archive)
    manifest = seed.load_or_create()
    manifest.parent_tables["point_history"] = {"table_type": "PH", "columns": [{"name": "a"}]}
    seed.save(manifest)

    store_a = ArchiveManifestStore(archive)
    manifest_a = store_a.load()
    store_b = ArchiveManifestStore(archive)
    manifest_b = store_b.load()
    manifest_b.parent_tables["point_history"] = {"table_type": "PH", "columns": [{"name": "b"}]}
    store_b.save(manifest_b)

    store_a.upsert_partition(manifest_a, _entry("point_history_240101"))
    store_a.save(manifest_a)

    data = json.loads(seed.manifest_path.read_text(encoding="utf-8"))
    assert data["parent_tables"]["point_history"]["columns"] == [{"name": "b"}]


def test_save_does_not_clobber_entries_when_primary_manifest_is_unreadable(tmp_path):
    archive = tmp_path / "archive"
    seed = ArchiveManifestStore(archive)
    manifest = seed.load_or_create()

    # writer는 p1이 생기기 전에 읽어 둔다 — 메모리에 p1이 없다.
    writer = ArchiveManifestStore(archive)
    fresh = writer.load()

    seed.upsert_partition(manifest, _entry("point_history_240101"))
    seed.save(manifest)
    seed.save(manifest)  # 백업에도 p1이 담기도록 한 번 더 저장
    assert seed.backup_path.exists()

    seed.manifest_path.write_text("{torn", encoding="utf-8")
    writer.upsert_partition(fresh, _entry("point_history_240102"))
    writer.save(fresh)

    disk = _disk(seed)
    assert set(disk) == {"point_history_240101", "point_history_240102"}


def test_save_refuses_when_no_readable_manifest_exists_on_disk(tmp_path):
    archive = tmp_path / "archive"
    seed = ArchiveManifestStore(archive)
    manifest = seed.load_or_create()
    seed.upsert_partition(manifest, _entry("point_history_240101"))
    seed.save(manifest)
    seed.manifest_path.write_text("{torn", encoding="utf-8")
    seed.backup_path.write_text("{torn", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest"):
        seed.save(manifest)


def test_commit_partition_replaces_file_only_when_cas_succeeds(tmp_path):
    archive = tmp_path / "archive"
    seed = ArchiveManifestStore(archive)
    seed.load_or_create()

    store_a = ArchiveManifestStore(archive)
    store_b = ArchiveManifestStore(archive)
    manifest_a = store_a.load()
    manifest_b = store_b.load()
    final = store_a.build_partition_file_path("point_history_240101")

    tmp_b = store_b.partitions_dir / ".b.tmp"
    tmp_b.write_text("B\n", encoding="utf-8")
    meta_b = store_b.compute_file_metadata(tmp_b)
    store_b.commit_partition(
        manifest_b,
        _entry("point_history_240101", checksum=meta_b["checksum_sha256"]),
        temp_path=tmp_b,
        final_path=final,
    )

    tmp_a = store_a.partitions_dir / ".a.tmp"
    tmp_a.write_text("A\n", encoding="utf-8")
    meta_a = store_a.compute_file_metadata(tmp_a)
    with pytest.raises(ManifestConflictError):
        store_a.commit_partition(
            manifest_a,
            _entry("point_history_240101", checksum=meta_a["checksum_sha256"]),
            temp_path=tmp_a,
            final_path=final,
        )

    # 파일과 manifest가 같은 writer(B)의 것으로 남아 서로 일치한다.
    assert final.read_text(encoding="utf-8") == "B\n"
    assert _disk(seed)["point_history_240101"]["checksum_sha256"] == meta_b["checksum_sha256"]
    # 실패한 쪽 메모리에는 거부된 항목이 남지 않아, 이후 save가 다시 충돌하지 않는다.
    store_a.save(manifest_a)
    assert _disk(seed)["point_history_240101"]["checksum_sha256"] == meta_b["checksum_sha256"]


# ── 두 writer 경쟁: 스레드 ────────────────────────────────────────────────


def _seed_shared(archive: Path, names: list[str]) -> None:
    store = ArchiveManifestStore(archive)
    manifest = store.load_or_create()
    for name in names:
        store.upsert_partition(manifest, _entry(name, row_count=0))
    store.save(manifest)


def test_thread_writers_with_long_lived_manifests_lose_no_updates(tmp_path):
    archive = tmp_path / "archive"
    writers = 4
    rounds = 15
    names = [f"point_history_2401{idx:02d}" for idx in range(1, writers * 2 + 1)]
    _seed_shared(archive, names)

    barrier = threading.Barrier(writers)
    errors: list[BaseException] = []

    def run(worker_index: int) -> None:
        try:
            store = ArchiveManifestStore(archive)
            manifest = store.load()  # 오래 들고 있는 메모리 사본
            mine = [n for i, n in enumerate(names) if i % writers == worker_index]
            barrier.wait()
            for round_no in range(1, rounds + 1):
                for name in mine:
                    store.upsert_partition(manifest, _entry(name, row_count=round_no))
                store.save(manifest)
        except BaseException as exc:  # pragma: no cover - 실패 시 보고용
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not errors, errors
    disk = _disk(ArchiveManifestStore(archive))
    assert {name: disk[name]["row_count"] for name in names} == dict.fromkeys(names, rounds)
    assert {disk[name]["entry_version"] for name in names} == {rounds + 1}


# ── 두 writer 경쟁: 프로세스 ──────────────────────────────────────────────

_CHILD_SCRIPT = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from src.core.archive_manifest import ArchiveManifestStore, ArchivePartitionEntry

    archive, go_file = Path(sys.argv[2]), Path(sys.argv[3])
    rounds, names = int(sys.argv[4]), sys.argv[5].split(",")
    store = ArchiveManifestStore(archive)
    manifest = store.load()
    deadline = time.time() + 60
    while not go_file.exists():
        if time.time() > deadline:
            raise SystemExit("go file timeout")
        time.sleep(0.005)
    for round_no in range(1, rounds + 1):
        for name in names:
            store.upsert_partition(
                manifest,
                ArchivePartitionEntry(
                    partition_name=name,
                    table_type="PH",
                    parent_table="point_history",
                    row_count=round_no,
                    file_path=f"partitions/{name}.csv",
                    columns=["path_id", "issued_date", "changed_value", "connection_status"],
                    bytes_written=1,
                    checksum_sha256=f"{round_no:064x}",
                ),
            )
        store.save(manifest)
    """
)


def test_process_writers_with_long_lived_manifests_lose_no_updates(tmp_path):
    archive = tmp_path / "archive"
    writers = 3
    rounds = 12
    names = [f"point_history_2402{idx:02d}" for idx in range(1, writers * 2 + 1)]
    _seed_shared(archive, names)

    script = tmp_path / "child_writer.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    go_file = tmp_path / "go"
    procs = []
    for worker_index in range(writers):
        mine = [n for i, n in enumerate(names) if i % writers == worker_index]
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    str(APP_ROOT),
                    str(archive),
                    str(go_file),
                    str(rounds),
                    ",".join(mine),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    go_file.write_text("go", encoding="utf-8")
    results = [proc.communicate(timeout=180) for proc in procs]
    for proc, (_out, err) in zip(procs, results, strict=True):
        assert proc.returncode == 0, err.decode("utf-8", "replace")

    disk = _disk(ArchiveManifestStore(archive))
    assert {name: disk[name]["row_count"] for name in names} == dict.fromkeys(names, rounds)
    data = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    assert data["revision"] >= writers * rounds
