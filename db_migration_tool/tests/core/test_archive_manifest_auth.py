"""M-04: archive checksum 필수화와 manifest 인증(passphrase 기반 HMAC-SHA256).

신뢰 경계는 docs/base/archive-trust-boundary.md 참고. 핵심:
- 신규 export 항목은 checksum이 없으면 저장되지 않는다.
- passphrase로 export한 아카이브는 import 때도 passphrase가 필수이며, manifest나
  데이터 파일(+checksum)을 바꾸면 인증에서 걸린다.
- 인증/체크섬이 없는 기존(legacy) 아카이브는 명시적 확인 플래그가 있을 때만
  경고와 함께 허용한다(조용한 통과 금지).
"""

from __future__ import annotations

import json
import shutil
import unicodedata

import pytest

from src.core.archive_manifest import (
    ARCHIVE_FORMAT_VERSION,
    ArchiveManifestStore,
    ArchivePartitionEntry,
    ManifestAuthError,
)
from src.core.table_types import TableType

FAST_KDF = 1000  # 테스트 속도용. 실제 기본값은 DEFAULT_KDF_ITERATIONS
PASS = "correct horse battery staple"
COLUMNS = ["path_id", "issued_date", "changed_value", "connection_status"]
NAME = "point_history_240101"


def _store(path, **kwargs) -> ArchiveManifestStore:
    kwargs.setdefault("kdf_iterations", FAST_KDF)
    return ArchiveManifestStore(path, **kwargs)


def _export_one(store: ArchiveManifestStore, *, content: str = "1,2024-01-01 00:00:00,1.5,0\n"):
    manifest = store.load_or_create(source={"kind": "postgres"}, target={"kind": "file"})
    store.upsert_parent_table(
        manifest,
        parent_table="point_history",
        table_type=TableType.POINT_HISTORY,
        columns=[{"name": "path_id", "data_type": "integer"}],
    )
    tmp = store.partitions_dir / f".{NAME}.tmp"
    tmp.write_text(content, encoding="utf-8")
    meta = store.compute_file_metadata(tmp)
    entry = ArchivePartitionEntry(
        partition_name=NAME,
        table_type="PH",
        parent_table="point_history",
        row_count=1,
        file_path=f"partitions/{NAME}.csv",
        columns=list(COLUMNS),
        bytes_written=meta["bytes_written"],
        checksum_sha256=meta["checksum_sha256"],
    )
    store.commit_partition(
        manifest, entry, temp_path=tmp, final_path=store.build_partition_file_path(NAME)
    )
    return manifest, entry


def _write_legacy_v2_archive(archive, *, checksum: bool = True) -> None:
    """1.2.7 이하가 만든 아카이브 모양(인증 없음, version 2)을 그대로 흉내낸다."""
    (archive / "partitions").mkdir(parents=True)
    data_file = archive / "partitions" / f"{NAME}.csv"
    data_file.write_text("1,2024-01-01 00:00:00,1.5,0\n", encoding="utf-8")
    import hashlib

    raw = data_file.read_bytes()
    entry = {
        "partition_name": NAME,
        "table_type": "PH",
        "parent_table": "point_history",
        "row_count": 1,
        "file_path": f"partitions/{NAME}.csv",
        "columns": COLUMNS,
        "bytes_written": len(raw),
        "checksum_sha256": hashlib.sha256(raw).hexdigest() if checksum else None,
    }
    manifest = {
        "format": "psql93-migration-archive",
        "version": 2,
        "created_at": "2026-09-01T00:00:00",
        "updated_at": "2026-09-01T00:00:00",
        "source": {"kind": "postgres"},
        "target": {"kind": "file"},
        "parent_tables": {"point_history": {"table_type": "PH", "columns": []}},
        "partitions": [entry],
    }
    (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _rewrite(store: ArchiveManifestStore, mutate) -> None:
    data = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    mutate(data)
    store.manifest_path.write_text(json.dumps(data), encoding="utf-8")


# ── 서명/검증 ───────────────────────────────────────────────────────────


def test_export_with_passphrase_writes_authenticated_manifest(tmp_path):
    store = _store(tmp_path / "a", passphrase=PASS)
    _export_one(store)

    data = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    assert data["version"] == ARCHIVE_FORMAT_VERSION
    assert data["auth"]["scheme"] == "hmac-sha256"
    assert data["auth"]["kdf"] == "pbkdf2-sha256"
    assert len(bytes.fromhex(data["auth"]["salt"])) >= 16
    assert PASS not in store.manifest_path.read_text(encoding="utf-8")

    reader = _store(tmp_path / "a", passphrase=PASS)
    manifest = reader.load()
    assert manifest.is_signed and manifest.is_authenticated
    assert reader.require_trusted(manifest) == []


def test_wrong_passphrase_is_rejected(tmp_path):
    _export_one(_store(tmp_path / "a", passphrase=PASS))
    with pytest.raises(ManifestAuthError, match="passphrase"):
        _store(tmp_path / "a", passphrase="wrong").load()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["partitions"][0].__setitem__("row_count", 999),
        lambda d: d["partitions"][0].__setitem__("file_path", "partitions/other.csv"),
        lambda d: d["parent_tables"]["point_history"]["columns"].append(
            {"name": "x", "data_type": "text"}
        ),
        lambda d: d["auth"].__setitem__("iterations", FAST_KDF + 1),
        lambda d: d.__setitem__("extra_field", 1),
    ],
    ids=["row_count", "file_path", "parent_columns", "kdf_params", "unknown_field"],
)
def test_any_manifest_tampering_is_detected(tmp_path, mutate):
    store = _store(tmp_path / "a", passphrase=PASS)
    _export_one(store)
    _rewrite(store, mutate)
    with pytest.raises(ManifestAuthError):
        _store(tmp_path / "a", passphrase=PASS).load()


def test_file_and_checksum_tampered_together_is_detected(tmp_path):
    """archive+manifest 동시 변조: 파일을 바꾸고 manifest checksum도 맞춰 고친다."""
    store = _store(tmp_path / "a", passphrase=PASS)
    _export_one(store)
    data_file = store.build_partition_file_path(NAME)
    data_file.write_text("666,2024-01-01 00:00:00,6.6,1\n", encoding="utf-8")
    forged = store.compute_file_metadata(data_file)

    def mutate(d):
        d["partitions"][0]["checksum_sha256"] = forged["checksum_sha256"]
        d["partitions"][0]["bytes_written"] = forged["bytes_written"]

    _rewrite(store, mutate)
    with pytest.raises(ManifestAuthError):
        _store(tmp_path / "a", passphrase=PASS).load()


def test_reformatted_json_still_verifies(tmp_path):
    store = _store(tmp_path / "a", passphrase=PASS)
    _export_one(store)
    data = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    # 키 순서·공백·이스케이프가 달라도 같은 내용이면 검증된다(정규화 JSON).
    store.manifest_path.write_text(
        json.dumps(dict(reversed(list(data.items()))), indent=None, ensure_ascii=True),
        encoding="utf-8",
    )
    assert _store(tmp_path / "a", passphrase=PASS).load().is_authenticated


def test_archive_moved_to_another_location_verifies_with_same_passphrase(tmp_path):
    """기계별 키를 쓰지 않는다: 다른 PC/경로로 옮긴 아카이브도 passphrase만 있으면 된다."""
    _export_one(_store(tmp_path / "a", passphrase=PASS))
    shutil.copytree(tmp_path / "a", tmp_path / "moved" / "archive")
    reader = _store(tmp_path / "moved" / "archive", passphrase=PASS)
    manifest = reader.load()
    entry = ArchivePartitionEntry.from_dict(manifest.partitions[0])
    reader.verify_partition_file(entry)
    assert reader.require_trusted(manifest) == []


def test_passphrase_unicode_normalization_is_stable(tmp_path):
    korean = "비밀번호-아카이브"
    _export_one(_store(tmp_path / "a", passphrase=unicodedata.normalize("NFC", korean)))
    reader = _store(tmp_path / "a", passphrase=unicodedata.normalize("NFD", korean))
    assert reader.load().is_authenticated


def test_signed_primary_failing_mac_does_not_fall_back_to_backup(tmp_path):
    store = _store(tmp_path / "a", passphrase=PASS)
    manifest, _ = _export_one(store)
    store.save(manifest)
    assert store.backup_path.exists()
    _rewrite(store, lambda d: d["partitions"][0].__setitem__("row_count", 5))
    with pytest.raises(ManifestAuthError):
        _store(tmp_path / "a", passphrase=PASS).load()


def test_torn_primary_falls_back_to_verified_backup(tmp_path):
    store = _store(tmp_path / "a", passphrase=PASS)
    manifest, _ = _export_one(store)
    store.save(manifest)
    store.manifest_path.write_text("{torn", encoding="utf-8")
    loaded = _store(tmp_path / "a", passphrase=PASS).load()
    assert loaded.is_authenticated


# ── import 신뢰 정책 ─────────────────────────────────────────────────────


def test_signed_archive_requires_passphrase_at_import(tmp_path):
    _export_one(_store(tmp_path / "a", passphrase=PASS))
    reader = _store(tmp_path / "a")
    manifest = reader.load()  # 목록 표시용 읽기는 된다
    assert manifest.is_signed and not manifest.is_authenticated
    with pytest.raises(ManifestAuthError, match="passphrase"):
        reader.require_trusted(manifest)
    # 확인 플래그로도 서명된 아카이브의 passphrase 요구를 우회할 수 없다.
    bypass = _store(tmp_path / "a", allow_legacy_unverified=True)
    with pytest.raises(ManifestAuthError, match="passphrase"):
        bypass.require_trusted(bypass.load())


def test_stripped_auth_block_needs_explicit_confirmation(tmp_path):
    """다운그레이드 공격: auth 블록을 지우면 legacy처럼 보인다 → 조용히 통과하면 안 된다."""
    store = _store(tmp_path / "a", passphrase=PASS)
    _export_one(store)
    _rewrite(store, lambda d: d.pop("auth"))

    reader = _store(tmp_path / "a", passphrase=PASS)
    manifest = reader.load()
    assert not manifest.is_signed
    with pytest.raises(ManifestAuthError, match="인증"):
        reader.require_trusted(manifest)


def test_legacy_archive_requires_explicit_flag_and_warns(tmp_path):
    archive = tmp_path / "legacy"
    _write_legacy_v2_archive(archive)

    strict = _store(archive)
    with pytest.raises(ManifestAuthError, match="확인"):
        strict.require_trusted(strict.load())

    lenient = _store(archive, allow_legacy_unverified=True)
    warnings = lenient.require_trusted(lenient.load())
    assert warnings and any("인증" in w for w in warnings)


# ── checksum 필수화 ─────────────────────────────────────────────────────


def test_new_entry_without_checksum_cannot_be_saved(tmp_path):
    store = _store(tmp_path / "a")
    manifest = store.load_or_create()
    store.upsert_partition(
        manifest,
        ArchivePartitionEntry(
            partition_name=NAME,
            table_type="PH",
            parent_table="point_history",
            row_count=1,
            file_path=f"partitions/{NAME}.csv",
            columns=list(COLUMNS),
            bytes_written=10,
            checksum_sha256=None,
        ),
    )
    with pytest.raises(ValueError, match="checksum"):
        store.save(manifest)


def test_malformed_checksum_cannot_be_saved(tmp_path):
    store = _store(tmp_path / "a")
    manifest = store.load_or_create()
    store.upsert_partition(
        manifest,
        ArchivePartitionEntry(
            partition_name=NAME,
            table_type="PH",
            parent_table="point_history",
            row_count=1,
            file_path=f"partitions/{NAME}.csv",
            columns=list(COLUMNS),
            checksum_sha256="abc",
        ),
    )
    with pytest.raises(ValueError, match="checksum"):
        store.save(manifest)


def test_legacy_entry_without_checksum_fails_verification_unless_allowed(tmp_path):
    archive = tmp_path / "legacy"
    _write_legacy_v2_archive(archive, checksum=False)
    store = _store(archive)
    entry = ArchivePartitionEntry.from_dict(store.load().partitions[0])

    with pytest.raises(ValueError, match="checksum"):
        store.verify_partition_file(entry)
    with open(store.build_partition_file_path(NAME), "rb") as fp:
        with pytest.raises(ValueError, match="checksum"):
            store.verify_open_file(entry, fp)

    # 명시적 허용 시 크기만 확인하고 통과(호출자가 경고를 남긴다).
    store.verify_partition_file(entry, allow_missing_checksum=True)
    assert store.get_completed_status([NAME]) == {NAME: False}


def test_legacy_entries_are_carried_through_save_untouched(tmp_path):
    archive = tmp_path / "legacy"
    _write_legacy_v2_archive(archive, checksum=False)
    store = _store(archive)
    manifest = store.load()
    store.save(manifest)
    item = json.loads(store.manifest_path.read_text(encoding="utf-8"))["partitions"][0]
    assert item["checksum_sha256"] is None


# ── writer 쪽 정책 (다운그레이드·세탁 방지) ────────────────────────────────


def test_writer_without_passphrase_cannot_downgrade_signed_archive(tmp_path):
    _export_one(_store(tmp_path / "a", passphrase=PASS))
    writer = _store(tmp_path / "a")
    manifest = writer.load()
    with pytest.raises(ManifestAuthError, match="passphrase"):
        writer.save(manifest)
    assert json.loads(writer.manifest_path.read_text(encoding="utf-8"))["auth"]


def test_writer_with_wrong_passphrase_cannot_resign_signed_archive(tmp_path):
    _export_one(_store(tmp_path / "a", passphrase=PASS))
    writer = _store(tmp_path / "a", passphrase="other")
    with pytest.raises(ManifestAuthError):
        writer.load_or_create()


def test_writer_does_not_launder_tampered_disk_manifest(tmp_path):
    """다른 writer가 잘 서명된 메모리를 들고 있어도, 디스크가 변조됐으면 병합·재서명하지 않는다."""
    store = _store(tmp_path / "a", passphrase=PASS)
    manifest, _ = _export_one(store)
    _rewrite(store, lambda d: d["partitions"][0].__setitem__("row_count", 999))
    with pytest.raises(ManifestAuthError):
        store.save(manifest)


def test_signing_an_unsigned_archive_requires_explicit_adoption(tmp_path):
    archive = tmp_path / "legacy"
    _write_legacy_v2_archive(archive)

    with pytest.raises(ManifestAuthError, match="확인"):
        _store(archive, passphrase=PASS).load_or_create()

    warnings: list[str] = []
    adopter = _store(archive, passphrase=PASS, allow_legacy_unverified=True, warn=warnings.append)
    adopter.load_or_create()
    assert warnings and any("서명" in w for w in warnings)
    reader = _store(archive, passphrase=PASS)
    assert reader.load().is_authenticated
