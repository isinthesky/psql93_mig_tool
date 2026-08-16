"""활성화 레코드 파일 처리와 인스톨러 씨앗 승격."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.licensing import activation

NOW = datetime(2026, 8, 5, 10, 0, 0)


@pytest.fixture(autouse=True)
def _isolated(app_dir):
    yield


class TestLicenseKeyFile:
    def test_absent_is_none(self):
        assert activation.read_license_key() is None

    def test_write_then_read(self):
        activation.write_license_key("  DBMT1-ABC  ")
        assert activation.read_license_key() == "DBMT1-ABC"

    def test_whitespace_only_is_none(self, app_dir):
        (app_dir / "license.key").write_text("\n  \n", encoding="utf-8")
        assert activation.read_license_key() is None


class TestActivationRecord:
    def test_absent_is_none(self):
        assert activation.read_activation() is None

    def test_activate_then_read(self):
        activation.activate("m-1", "serial-1", NOW)
        record = activation.read_activation()
        assert record.machine == "m-1"
        assert record.serial == "serial-1"
        assert record.activated_at == NOW
        assert record.last_seen == NOW

    def test_corrupt_file_is_none(self, app_dir):
        (app_dir / ".activation").write_text("not json at all", encoding="utf-8")
        assert activation.read_activation() is None

    def test_missing_field_is_none(self, app_dir):
        (app_dir / ".activation").write_text('{"machine": "m-1"}', encoding="utf-8")
        assert activation.read_activation() is None


class TestTouchMonotonic:
    """`last_seen`이 뒤로 가면 시계 역행 검사가 스스로 무력해진다."""

    def test_forward_updates(self):
        record = activation.activate("m-1", "s", NOW)
        later = NOW + timedelta(hours=5)
        activation.touch(record, later)
        assert activation.read_activation().last_seen == later

    def test_backward_does_not_lower(self):
        record = activation.activate("m-1", "s", NOW)
        activation.touch(record, NOW - timedelta(days=10))
        assert activation.read_activation().last_seen == NOW

    def test_clock_backwards_detection(self):
        record = activation.activate("m-1", "s", NOW)
        assert activation.clock_went_backwards(record, NOW - timedelta(seconds=1))
        assert not activation.clock_went_backwards(record, NOW)
        assert not activation.clock_went_backwards(record, NOW + timedelta(seconds=1))


class TestInstallerSeed:
    """인스톨러가 설치 폴더에 남긴 키를 앱이 자기 계정 폴더로 옮기는 경로.

    인스톨러의 `{localappdata}`는 설치를 실행한 계정을 가리키므로, 관리자로 설치하면
    앱을 쓰는 사용자와 다른 계정이 된다. 그래서 계정 무관한 설치 폴더를 경유한다.
    """

    def test_no_seed_when_not_frozen(self):
        """개발 환경(비 frozen)에서는 씨앗 경로가 없다."""
        assert activation.seed_path() is None

    def test_seed_is_promoted(self, tmp_path, monkeypatch):
        exe_dir = tmp_path / "install"
        exe_dir.mkdir()
        (exe_dir / activation.LICENSE_SEED_FILENAME).write_text("DBMT1-SEEDKEY\n", encoding="utf-8")

        monkeypatch.setattr(activation.sys, "frozen", True, raising=False)
        monkeypatch.setattr(activation.sys, "executable", str(exe_dir / "app.exe"))

        assert activation.read_license_key() == "DBMT1-SEEDKEY"
        # 사용자 폴더로 옮겨져 다음부터는 씨앗 없이도 읽힌다.
        assert activation.license_path().read_text(encoding="utf-8").strip() == "DBMT1-SEEDKEY"

    def test_existing_key_wins_over_seed(self, tmp_path, monkeypatch):
        """앱에서 갱신한 키가 있으면 씨앗이 덮어쓰지 않는다."""
        activation.write_license_key("DBMT1-RENEWED")

        exe_dir = tmp_path / "install"
        exe_dir.mkdir()
        (exe_dir / activation.LICENSE_SEED_FILENAME).write_text("DBMT1-OLDSEED", encoding="utf-8")
        monkeypatch.setattr(activation.sys, "frozen", True, raising=False)
        monkeypatch.setattr(activation.sys, "executable", str(exe_dir / "app.exe"))

        assert activation.read_license_key() == "DBMT1-RENEWED"

    def test_empty_seed_is_ignored(self, tmp_path, monkeypatch):
        exe_dir = tmp_path / "install"
        exe_dir.mkdir()
        (exe_dir / activation.LICENSE_SEED_FILENAME).write_text("   \n", encoding="utf-8")
        monkeypatch.setattr(activation.sys, "frozen", True, raising=False)
        monkeypatch.setattr(activation.sys, "executable", str(exe_dir / "app.exe"))

        assert activation.read_license_key() is None
