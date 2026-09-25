"""활성화 레코드 — 이 키가 어느 PC에 묶였는지 기록한다(TOFU).

파일이 없으면 현재 PC로 새로 만든다. 있으면 머신 ID를 대조한다.

`last_seen`은 **절대 뒤로 가지 않는다**(`max`). 시계를 되돌린 실행에서 그 값을 그대로
써 버리면, 두 번째 실행부터는 역행이 사라져 검사가 스스로 무력해진다.

파일 I/O가 여기 모여 있어서 테스트가 `AppPaths.set_custom_root()`로 통째로 갈아끼울 수 있다.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.utils.app_paths import AppPaths

ACTIVATION_FILENAME = ".activation"
LICENSE_FILENAME = "license.key"
# 인스톨러가 설치 폴더에 남기는 초기 키. 앱이 첫 실행에서 사용자 폴더로 옮기고,
# 활성화가 성공하면 지운다(감사 M-07 — 평문 키를 디스크에 오래 두지 않는다).
LICENSE_SEED_FILENAME = "license.seed"
# 씨앗 전용 하위 폴더. 인스톨러가 이 폴더에만 Users 수정 권한을 준다 — 관리자 설치
# (Program Files)에서도 앱을 쓰는 일반 사용자가 씨앗을 지울 수 있어야 하기 때문이다.
SEED_DIRNAME = "seed"


@dataclass
class ActivationRecord:
    machine: str
    activated_at: datetime
    last_seen: datetime
    serial: str

    def to_dict(self) -> dict[str, str]:
        return {
            "machine": self.machine,
            "activated_at": self.activated_at.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "lic": self.serial,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ActivationRecord:
        return cls(
            machine=str(data["machine"]),
            activated_at=datetime.fromisoformat(str(data["activated_at"])),
            last_seen=datetime.fromisoformat(str(data["last_seen"])),
            serial=str(data.get("lic", "")),
        )


def activation_path() -> Path:
    return AppPaths.get_app_data_dir() / ACTIVATION_FILENAME


def license_path() -> Path:
    return AppPaths.get_app_data_dir() / LICENSE_FILENAME


# ── 라이선스 키 파일 ────────────────────────────────────────


def seed_path() -> Path | None:
    """인스톨러가 남긴 초기 키 파일(`<설치폴더>/seed/license.seed`). 개발 환경에서는 None."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).parent / SEED_DIRNAME / LICENSE_SEED_FILENAME


def _seed_candidates() -> list[Path]:
    """씨앗이 있을 수 있는 곳. 1.2.7 이하 인스톨러는 설치 폴더 바로 아래에 두었다."""
    primary = seed_path()
    if primary is None:
        return []
    return [primary, Path(sys.executable).parent / LICENSE_SEED_FILENAME]


def _consume_seed() -> str | None:
    """인스톨러가 놓아둔 씨앗을 사용자별 저장소로 옮긴다.

    인스톨러의 `{localappdata}`는 **설치를 실행한 계정**을 가리킨다. 관리자로 승격해
    설치하면 앱을 쓰는 사용자와 다른 계정이 되어 키가 엉뚱한 곳에 남는다. 그래서
    인스톨러는 계정과 무관한 설치 폴더에 씨앗만 두고, 실제 저장은 앱이 자기 계정에서 한다.

    여기서는 옮기기만 하고 지우지 않는다 — 키가 유효한지(활성화 성공)는 아직 모른다.
    삭제는 활성화 레코드를 쓴 직후 `discard_seed()`가 한다. 그래서 같은 PC의 다른 사용자는
    씨앗을 쓸 수 없다(평문 키 최소 수명과 맞바꾼 제약). 그 사용자는 앱에서 키를 등록한다.
    """
    for seed in _seed_candidates():
        try:
            text = seed.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not text:
            continue

        try:
            write_license_key(text)
        except OSError:
            # 옮기지 못해도 이번 실행에는 쓸 수 있게 값은 돌려준다(씨앗은 남는다).
            pass
        return text
    return None


def _license_key_persisted() -> bool:
    try:
        return bool(license_path().read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


def discard_seed() -> bool:
    """사용자 폴더에 키가 저장돼 있으면 씨앗을 지운다. 모두 지웠으면 True.

    활성화 레코드를 쓴 직후(`activate`/`touch`) 부른다. 사용자 폴더에 키가 없으면 씨앗이
    유일한 사본이므로 지우지 않는다. 삭제 실패(권한 등)는 예외로 올리지 않는다 — 라이선스
    확인을 실패시킬 이유가 아니며, 다음 실행의 `touch`에서 다시 시도한다.
    """
    candidates = _seed_candidates()
    if not candidates or not _license_key_persisted():
        return False
    removed = True
    for seed in candidates:
        try:
            seed.unlink(missing_ok=True)
        except OSError:
            removed = False
    return removed


def read_license_key() -> str | None:
    """저장된 키 문자열. 없으면 인스톨러 씨앗을 찾아본다."""
    path = license_path()
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except (OSError, UnicodeDecodeError):
        pass
    return _consume_seed()


def write_license_key(key: str) -> None:
    """키를 저장한다. 등록 창과 인스톨러가 쓴다."""
    path = license_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key.strip() + "\n", encoding="utf-8")


# ── 활성화 레코드 ───────────────────────────────────────────


def read_activation() -> ActivationRecord | None:
    """활성화 레코드. 없거나 읽을 수 없으면 None.

    손상된 파일은 없는 것과 같이 다룬다. 어차피 삭제하면 재활성화되므로(설계 §5),
    손상만 따로 막아 봐야 사용자를 가두는 것 말고는 얻는 게 없다.
    """
    try:
        data = json.loads(activation_path().read_text(encoding="utf-8"))
        return ActivationRecord.from_dict(data)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_activation(record: ActivationRecord) -> None:
    path = activation_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict(), ensure_ascii=False), encoding="utf-8")


def activate(machine_id: str, serial: str, now: datetime) -> ActivationRecord:
    """이 PC로 새로 활성화한다(TOFU 최초 기록 또는 재등록)."""
    record = ActivationRecord(machine=machine_id, activated_at=now, last_seen=now, serial=serial)
    write_activation(record)
    # 등록 성공 — 설치 폴더의 평문 씨앗은 더 필요 없다(감사 M-07).
    discard_seed()
    return record


def touch(record: ActivationRecord, now: datetime) -> ActivationRecord:
    """마지막 실행 시각을 갱신한다.

    **뒤로 가지 않는다.** 시계를 되돌린 실행에서 값을 낮추면 다음 실행부터
    역행이 감지되지 않아 검사가 무의미해진다.
    """
    record.last_seen = max(record.last_seen, now)
    write_activation(record)
    # 지난 실행에서 씨앗 삭제가 실패했으면(권한 등) 여기서 다시 시도한다.
    discard_seed()
    return record


def clock_went_backwards(record: ActivationRecord, now: datetime) -> bool:
    """시계가 마지막 실행보다 과거로 돌아갔는가."""
    return now < record.last_seen
