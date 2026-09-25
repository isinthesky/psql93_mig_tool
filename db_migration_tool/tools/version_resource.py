"""exe 버전 리소스를 src/version.py 에서 만든다 — DBMigrationTool.spec 이 빌드 시점에 쓴다.

build.bat 은 bump_version.py 로 version.py 를 올린 직후 PyInstaller 를 돌린다. spec 이 그때
version.py 를 읽으므로 exe 의 파일/제품 버전이 인스톨러·앱 정보 창과 같은 값이 된다.
버전을 여기에 따로 적지 않는다(버전이 사는 곳은 bump_version.py 가 관리한다).

version.py 는 import 하지 않고 정규식으로 읽는다. spec 실행 중에 src 패키지를 import 하면
빌드 환경의 부수효과(Qt 등)를 끌어올 수 있다.

PyInstaller 의 versioninfo 모듈은 pefile(Windows 빌드 의존성)을 import 하므로
build_version_info() 안에서만 늦게 불러온다. 필드 계산은 어느 플랫폼에서나 테스트된다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NamedTuple

PRODUCT_NAME = "DB Migration Tool"
COMPANY_NAME = "CIMON"  # installer/DBMigrationTool.iss 의 AppPublisher 와 같다
EXE_NAME = "DBMigrationTool.exe"

# 영어(미국) / 유니코드. StringTable 이름과 VarFileInfo Translation 이 같은 쌍을 가리켜야 한다.
_LANG_ID = 0x0409
_CODEPAGE = 0x04B0

_VERSION_LINE = re.compile(r'(?m)^__version__ = "([^"]*)"')
_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class VersionFields(NamedTuple):
    numbers: tuple[int, int, int, int]
    strings: dict[str, str]


def read_app_version(app_root: Path) -> str:
    """앱 루트의 src/version.py 에서 버전 문자열을 읽는다."""
    text = (Path(app_root) / "src" / "version.py").read_text("utf-8")
    match = _VERSION_LINE.search(text)
    if not match:
        raise ValueError("src/version.py 에서 __version__ 을 찾지 못했습니다.")
    return match.group(1)


def version_fields(version: str) -> VersionFields:
    """X.Y.Z 를 버전 리소스의 숫자 4개와 문자열 표로 바꾼다(빌드 번호는 0)."""
    match = _SEMVER.fullmatch(version)
    if not match:
        raise ValueError(f"버전 형식이 올바르지 않습니다: {version!r} (X.Y.Z 형태)")
    major, minor, patch = (int(part) for part in match.groups())
    return VersionFields(
        numbers=(major, minor, patch, 0),
        strings={
            "CompanyName": COMPANY_NAME,
            "FileDescription": PRODUCT_NAME,
            "FileVersion": version,
            "InternalName": EXE_NAME.removesuffix(".exe"),
            "OriginalFilename": EXE_NAME,
            "ProductName": PRODUCT_NAME,
            "ProductVersion": version,
        },
    )


def build_version_info(version: str) -> Any:
    """PyInstaller EXE(version=...) 에 넘길 VSVersionInfo 를 만든다."""
    from PyInstaller.utils.win32 import versioninfo as vi

    fields = version_fields(version)
    return vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(filevers=fields.numbers, prodvers=fields.numbers),
        kids=[
            vi.StringFileInfo(
                [
                    vi.StringTable(
                        f"{_LANG_ID:04X}{_CODEPAGE:04X}",
                        [vi.StringStruct(name, value) for name, value in fields.strings.items()],
                    )
                ]
            ),
            vi.VarFileInfo([vi.VarStruct("Translation", [_LANG_ID, _CODEPAGE])]),
        ],
    )
