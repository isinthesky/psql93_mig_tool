"""라이선스 확인 — 앱이 쓰는 유일한 입구는 `check_license()`다.

## 이 모듈이 지키는 것

**만료가 데이터를 중간 상태로 방치하지 않게 한다.** 이 앱은 수천만 행을 체크포인트로
나눠 옮기고, 중단되면 다음 실행에서 재개한다. 라이선스 때문에 앱이 열리지 않으면
재개 경로가 사라지고 소스와 대상이 어긋난 채 남는다. 그래서:

  - 검사는 **앱 시작 시 1회만** 한다. 실행 중 재검사하지 않는다.
  - 어떤 상태에서도 **중단된 작업의 재개는 허용**한다. 막는 것은 새 작업뿐이다.
  - 미인증·만료·타 PC 모두 '차단'이 아니라 '제한 모드'다.

## 상태 우선순위

검사 순서가 곧 우선순위다. 앞 단계에서 결론이 나면 뒤는 보지 않는다.

    MISSING > INVALID > WRONG_MACHINE > EXPIRED > EXPIRING > VALID

복합 조건(예: 만료됐는데 다른 PC이기도 함)은 앞선 것 하나로 결정된다.
어차피 `EXPIRED`와 `WRONG_MACHINE`은 둘 다 제한 모드라 사용자가 겪는 차이는
안내 문구뿐이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from . import activation, machine
from .payload import LicenseFormatError, LicensePayload, verify_key

# 만료 며칠 전부터 경고할지. 계약 갱신 리드타임에 맞춰 조정한다.
EXPIRING_SOON_DAYS = 30


class LicenseStatus(Enum):
    """앱이 어떤 모드로 뜰지 정하는 값."""

    VALID = "valid"
    EXPIRING = "expiring"  # 유효하지만 만료가 가깝다
    EXPIRED = "expired"
    MISSING = "missing"  # 키 파일이 없다
    INVALID = "invalid"  # 형식 오류·서명 불일치·미지원 버전
    WRONG_MACHINE = "wrong_machine"  # 다른 PC에서 활성화된 키

    @property
    def is_restricted(self) -> bool:
        """제한 모드로 띄워야 하는가. VALID/EXPIRING만 전부 허용이다."""
        return self not in (LicenseStatus.VALID, LicenseStatus.EXPIRING)


@dataclass(frozen=True)
class LicenseState:
    """검사 결과. UI가 이것만 보고 화면을 정한다."""

    status: LicenseStatus
    payload: LicensePayload | None = None
    message: str = ""
    machine_id: str = ""

    @property
    def is_restricted(self) -> bool:
        return self.status.is_restricted

    @property
    def expires_on(self) -> date | None:
        return self.payload.expires if self.payload else None

    def days_left(self, today: date | None = None) -> int | None:
        if self.payload is None:
            return None
        return self.payload.days_left(today or date.today())


def check_license(now: datetime | None = None) -> LicenseState:
    """라이선스를 확인한다. 앱 시작 시 **한 번만** 부른다.

    부작용: 활성화 레코드를 만들거나(TOFU 최초 실행) `last_seen`을 갱신한다.
    """
    now = now or datetime.now()
    today = now.date()
    machine_id = machine.get_machine_id()

    raw_key = activation.read_license_key()
    if raw_key is None:
        return LicenseState(
            LicenseStatus.MISSING,
            message="라이선스가 등록되지 않았습니다.",
            machine_id=machine_id,
        )

    try:
        payload = verify_key(raw_key)
    except LicenseFormatError as exc:
        return LicenseState(LicenseStatus.INVALID, message=str(exc), machine_id=machine_id)

    record = activation.read_activation()
    if record is None:
        # TOFU — 이 PC를 정당한 PC로 받아들이고 기록한다.
        activation.activate(machine_id, payload.serial, now)
    elif record.machine != machine_id:
        return LicenseState(
            LicenseStatus.WRONG_MACHINE,
            payload=payload,
            message="다른 PC에서 활성화된 라이선스입니다.",
            machine_id=machine_id,
        )
    else:
        if activation.clock_went_backwards(record, now):
            # last_seen 은 낮추지 않는다. 낮추면 다음 실행부터 검사가 무력해진다.
            activation.touch(record, now)
            return LicenseState(
                LicenseStatus.EXPIRED,
                payload=payload,
                message="시스템 시각이 과거로 변경되었습니다. 시각을 바로잡아 주세요.",
                machine_id=machine_id,
            )
        activation.touch(record, now)

    if payload.is_expired(today):
        return LicenseState(
            LicenseStatus.EXPIRED,
            payload=payload,
            message=f"라이선스가 {payload.expires:%Y-%m-%d}에 만료되었습니다.",
            machine_id=machine_id,
        )

    days = payload.days_left(today)
    if days <= EXPIRING_SOON_DAYS:
        return LicenseState(
            LicenseStatus.EXPIRING,
            payload=payload,
            message=f"라이선스가 {days}일 뒤 만료됩니다.",
            machine_id=machine_id,
        )

    return LicenseState(LicenseStatus.VALID, payload=payload, machine_id=machine_id)


def register_key(key: str, now: datetime | None = None) -> LicenseState:
    """사용자가 등록 창에 넣은 키를 저장하고 즉시 확인한다.

    저장 전에 검증한다 — 잘못된 키로 기존의 멀쩡한 키를 덮어쓰면 안 된다.
    """
    now = now or datetime.now()
    machine_id = machine.get_machine_id()

    try:
        payload = verify_key(key)
    except LicenseFormatError as exc:
        return LicenseState(LicenseStatus.INVALID, message=str(exc), machine_id=machine_id)

    activation.write_license_key(key)
    # 새 키를 넣으면 이 PC로 다시 묶는다(갱신·재등록 모두 이 경로).
    activation.activate(machine_id, payload.serial, now)
    return check_license(now)


__all__ = [
    "EXPIRING_SOON_DAYS",
    "LicenseState",
    "LicenseStatus",
    "check_license",
    "register_key",
]
