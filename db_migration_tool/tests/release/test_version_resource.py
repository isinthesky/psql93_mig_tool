"""exe 버전 리소스 — 탐색기 '자세히' 탭과 설치 후 버전 확인이 src/version.py 와 같은 값을 말한다.

1.2.8까지 exe 에는 버전 리소스가 없어서, 설치된 exe 가 어느 빌드인지 파일만 보고는 알 수 없었다
(키 형식이 바뀐 1.2.8 전후를 가려야 하는데 설치 폴더 파일 시각에 의존했다).
빌드는 bump_version.py 가 version.py 를 올린 직후 PyInstaller 를 돌리므로, spec 이 빌드 시점에
version.py 를 읽으면 exe 버전이 인스톨러·앱 표시 버전과 어긋나지 않는다.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2]
TOOL = APP_ROOT / "tools" / "version_resource.py"
SPEC = APP_ROOT / "DBMigrationTool.spec"
VERSION_PY = APP_ROOT / "src" / "version.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("version_resource_under_test", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app_version() -> str:
    match = re.search(r'(?m)^__version__ = "(\d+\.\d+\.\d+)"', VERSION_PY.read_text("utf-8"))
    assert match
    return match.group(1)


class TestVersionFields:
    def test_reads_version_py_of_the_app_root(self):
        tool = _load_tool()
        assert tool.read_app_version(APP_ROOT) == _app_version()

    def test_numeric_tuple_has_four_parts_with_zero_build(self):
        tool = _load_tool()
        fields = tool.version_fields("1.2.9")
        assert fields.numbers == (1, 2, 9, 0)

    def test_string_table_carries_the_same_version(self):
        tool = _load_tool()
        strings = tool.version_fields("1.2.9").strings
        assert strings["FileVersion"] == "1.2.9"
        assert strings["ProductVersion"] == "1.2.9"
        assert strings["ProductName"] == "DB Migration Tool"
        assert strings["OriginalFilename"] == "DBMigrationTool.exe"

    @pytest.mark.parametrize("bad", ["1.2", "1.2.9.1", "v1.2.9", "1.2.x", ""])
    def test_rejects_non_semver_triplet(self, bad):
        tool = _load_tool()
        with pytest.raises(ValueError):
            tool.version_fields(bad)

    def test_missing_version_line_fails_loudly(self, tmp_path):
        tool = _load_tool()
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "version.py").write_text("VERSION = 'x'\n", "utf-8")
        with pytest.raises(ValueError):
            tool.read_app_version(tmp_path)

    def test_company_and_product_match_installer_defines(self):
        tool = _load_tool()
        iss = (APP_ROOT / "installer" / "DBMigrationTool.iss").read_bytes().decode("utf-8-sig")
        assert f'#define AppPublisher "{tool.COMPANY_NAME}"' in iss
        assert f'#define AppName "{tool.PRODUCT_NAME}"' in iss
        assert f'#define ExeName "{tool.EXE_NAME}"' in iss


class TestSpecWiring:
    def test_spec_passes_version_resource_built_from_version_py(self):
        text = SPEC.read_text("utf-8")
        assert "version_resource" in text
        assert re.search(r"(?m)^\s*version=build_version_info\(", text)


class TestVersionInfoStructure:
    """PyInstaller 의 versioninfo 는 pefile(Windows 빌드 의존성)이 있어야 import 된다."""

    def test_builds_vsversioninfo_with_matching_fixed_and_string_versions(self):
        versioninfo = pytest.importorskip("PyInstaller.utils.win32.versioninfo")
        tool = _load_tool()
        info = tool.build_version_info("1.2.9")
        assert isinstance(info, versioninfo.VSVersionInfo)
        assert info.ffi.fileVersionMS == (1 << 16) | 2
        assert info.ffi.fileVersionLS == (9 << 16) | 0
        assert info.ffi.productVersionMS == info.ffi.fileVersionMS
        text = str(info)
        assert "StringStruct('FileVersion', '1.2.9')" in text
        assert "StringStruct('ProductVersion', '1.2.9')" in text
