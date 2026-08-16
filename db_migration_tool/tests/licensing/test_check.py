"""상태 판정과 활성화(TOFU) 동작.

여기서 지켜야 할 것은 하나다 — **어떤 상태에서도 앱이 열려야 하고, 재개 경로가
살아 있어야 한다.** 만료로 앱을 닫으면 중단된 마이그레이션을 복구할 수 없다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.licensing import (
    EXPIRING_SOON_DAYS,
    LicenseStatus,
    activation,
    check_license,
    register_key,
)

NOW = datetime(2026, 8, 5, 10, 0, 0)
TODAY = NOW.date()


@pytest.fixture(autouse=True)
def _isolated(app_dir, fixed_machine):
    """모든 테스트를 임시 폴더 + 고정 머신 ID 위에서 돌린다."""
    yield


def _install(key: str) -> None:
    activation.write_license_key(key)


class TestMissingAndInvalid:
    def test_no_key_is_missing(self, signing_key):
        state = check_license(NOW)
        assert state.status is LicenseStatus.MISSING
        assert state.is_restricted

    def test_garbage_key_is_invalid(self, signing_key):
        _install("이건키가아니다")
        assert check_license(NOW).status is LicenseStatus.INVALID

    def test_empty_file_is_missing(self, signing_key, app_dir):
        (app_dir / "license.key").write_text("   \n", encoding="utf-8")
        assert check_license(NOW).status is LicenseStatus.MISSING


class TestValidStates:
    def test_valid_key(self, signing_key):
        _install(signing_key(expires=TODAY + timedelta(days=200)))
        state = check_license(NOW)
        assert state.status is LicenseStatus.VALID
        assert not state.is_restricted

    def test_expiring_soon(self, signing_key):
        _install(signing_key(expires=TODAY + timedelta(days=EXPIRING_SOON_DAYS - 1)))
        state = check_license(NOW)
        assert state.status is LicenseStatus.EXPIRING
        assert not state.is_restricted, "만료 전에는 계속 다 쓸 수 있어야 한다"

    def test_expiry_day_is_not_expired(self, signing_key):
        """만료 당일. 하루 일찍 막으면 고객사 사고다."""
        _install(signing_key(expires=TODAY))
        state = check_license(NOW)
        assert state.status is LicenseStatus.EXPIRING
        assert not state.is_restricted

    def test_day_after_expiry_is_restricted(self, signing_key):
        _install(signing_key(expires=TODAY - timedelta(days=1)))
        state = check_license(NOW)
        assert state.status is LicenseStatus.EXPIRED
        assert state.is_restricted


class TestTofu:
    def test_first_run_binds_this_machine(self, signing_key, fixed_machine):
        _install(signing_key(serial="s-1"))
        assert activation.read_activation() is None

        check_license(NOW)

        record = activation.read_activation()
        assert record is not None
        assert record.machine == fixed_machine["id"]
        assert record.serial == "s-1"

    def test_other_machine_is_blocked(self, signing_key, fixed_machine):
        _install(signing_key())
        check_license(NOW)

        fixed_machine["id"] = "machine-bbbb"
        state = check_license(NOW)

        assert state.status is LicenseStatus.WRONG_MACHINE
        assert state.is_restricted

    def test_deleting_activation_rebinds(self, signing_key, app_dir, fixed_machine):
        """설계 §5 — .activation 삭제는 재활성화. 하드웨어 교체 복구 경로다."""
        _install(signing_key())
        check_license(NOW)

        (app_dir / ".activation").unlink()
        fixed_machine["id"] = "machine-cccc"
        state = check_license(NOW)

        assert state.status is LicenseStatus.VALID
        assert activation.read_activation().machine == "machine-cccc"

    def test_corrupt_activation_is_treated_as_absent(self, signing_key, app_dir):
        _install(signing_key())
        (app_dir / ".activation").write_text("{ 깨진 json", encoding="utf-8")
        assert check_license(NOW).status is LicenseStatus.VALID


class TestClockTampering:
    def test_turning_clock_back_is_caught(self, signing_key):
        _install(signing_key(expires=TODAY + timedelta(days=200)))
        check_license(NOW)

        state = check_license(NOW - timedelta(days=30))
        assert state.status is LicenseStatus.EXPIRED
        assert "시각" in state.message

    def test_last_seen_never_moves_backwards(self, signing_key):
        """이게 깨지면 두 번째 실행부터 역행 검사가 스스로 무력해진다."""
        _install(signing_key(expires=TODAY + timedelta(days=200)))
        check_license(NOW)
        assert activation.read_activation().last_seen == NOW

        past = NOW - timedelta(days=30)
        check_license(past)
        assert activation.read_activation().last_seen == NOW, "last_seen 이 과거로 내려갔다"

        # 되돌린 시각으로 다시 실행해도 여전히 잡혀야 한다
        assert check_license(past).status is LicenseStatus.EXPIRED

    def test_clock_moving_forward_is_fine(self, signing_key):
        _install(signing_key(expires=TODAY + timedelta(days=200)))
        check_license(NOW)

        later = NOW + timedelta(days=1)
        assert check_license(later).status is LicenseStatus.VALID
        assert activation.read_activation().last_seen == later


class TestRegisterKey:
    def test_registering_valid_key_activates(self, signing_key):
        state = register_key(signing_key(serial="reg-1"), NOW)
        assert state.status is LicenseStatus.VALID
        assert activation.read_activation().serial == "reg-1"

    def test_bad_key_does_not_overwrite_good_one(self, signing_key):
        """등록 창에서 오타가 나도 멀쩡히 쓰던 키를 잃으면 안 된다."""
        good = signing_key(serial="keep-me")
        register_key(good, NOW)

        state = register_key("쓰레기키", NOW)

        assert state.status is LicenseStatus.INVALID
        assert activation.read_license_key() == good.strip()
        assert check_license(NOW).status is LicenseStatus.VALID

    def test_registering_rebinds_to_current_machine(self, signing_key, fixed_machine):
        """갱신 키를 넣으면 현재 PC로 다시 묶인다 — 하드웨어 교체 후 복구 경로."""
        register_key(signing_key(), NOW)
        fixed_machine["id"] = "machine-new"

        assert check_license(NOW).status is LicenseStatus.WRONG_MACHINE
        assert register_key(signing_key(serial="renewed"), NOW).status is LicenseStatus.VALID


class TestNeverLocksOut:
    """설계 §2.1 — 라이선스가 데이터 복구를 막지 않는다는 불변 조건."""

    @pytest.mark.parametrize(
        "setup",
        [
            "missing",
            "invalid",
            "expired",
            "wrong_machine",
        ],
    )
    def test_app_still_opens_in_every_bad_state(self, signing_key, fixed_machine, setup):
        if setup == "invalid":
            _install("깨진키")
        elif setup == "expired":
            _install(signing_key(expires=TODAY - timedelta(days=1)))
        elif setup == "wrong_machine":
            _install(signing_key())
            check_license(NOW)
            fixed_machine["id"] = "machine-zzzz"

        state = check_license(NOW)

        # 예외를 던지지 않고, 상태를 돌려주고, 제한 모드일 뿐이어야 한다.
        assert state.status.is_restricted
        assert isinstance(state.message, str)
