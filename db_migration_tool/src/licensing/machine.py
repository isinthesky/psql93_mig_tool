"""이 PC를 식별하는 값.

`MachineGuid`(OS 설치 시 생기고 재설치 전까지 안 바뀜)와 C: 볼륨 일련번호(포맷 전까지
안 바뀜)를 해시한다.

쓰지 않는 것과 이유:
  - MAC 주소: USB 랜·도킹·가상 어댑터로 수시로 바뀐다. 멀쩡한 PC가 다른 PC로 보인다.
  - WMI(메인보드 UUID 등): 호출이 수백 ms~초씩 걸려 앱 시작을 눈에 띄게 늦춘다.

값을 못 구해도 예외를 던지지 않는다. 머신 ID를 못 읽는다고 마이그레이션 도구를
못 쓰게 만들 수는 없다 — 그런 경우 고정 대체값을 써서 바인딩만 사실상 해제된다.
"""

from __future__ import annotations

import hashlib
import sys

_ID_LENGTH = 16  # 해시 앞 16자면 충돌 걱정 없이 짧게 표시할 수 있다
_FALLBACK = "unknown-machine"


def _read_machine_guid() -> str:
    """HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid — 비관리자도 읽을 수 있다."""
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        # 32비트 프로세스에서도 64비트 뷰를 봐야 값이 일치한다.
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as handle:
            value, _ = winreg.QueryValueEx(handle, "MachineGuid")
            return str(value)
    except OSError:
        return ""


def _read_volume_serial() -> str:
    """시스템 드라이브의 볼륨 일련번호."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes

        serial = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p("C:\\"),
            None,
            0,
            ctypes.byref(serial),
            None,
            None,
            None,
            0,
        )
        return str(serial.value) if ok else ""
    except Exception:
        # ctypes 자체가 막힌 환경(일부 보안 정책)에서도 앱은 떠야 한다.
        return ""


def get_machine_id() -> str:
    """이 PC의 식별자. 같은 PC에서는 항상 같은 값이 나온다."""
    parts = [_read_machine_guid(), _read_volume_serial()]
    material = "|".join(p for p in parts if p)

    if not material:
        material = _FALLBACK

    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:_ID_LENGTH]


def format_for_display(machine_id: str) -> str:
    """사용자가 옮겨 적기 쉽게 4자씩 끊는다."""
    return "-".join(machine_id[i : i + 4] for i in range(0, len(machine_id), 4)).upper()
