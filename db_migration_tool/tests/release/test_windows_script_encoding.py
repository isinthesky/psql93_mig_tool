"""Windows 스크립트의 줄바꿈·인코딩 검사(감사 M-10).

`cmd.exe`는 LF 줄바꿈의 UTF-8 배치 파일에서 한글이 섞인 줄을 잘못 끊어 읽는다
(`installer/build_installer.bat`가 종료 코드 1로 실패하던 원인). 저장소 루트
`.gitattributes`가 `*.bat`/`*.cmd`/`*.ps1`/`*.iss`를 `eol=crlf`로 체크아웃하게 하고, 이 테스트가
1) 규칙이 실제로 적용되는지(`git check-attr`), 2) 작업 트리 파일이 CRLF인지,
3) 파일별로 정해진 인코딩을 지키는지 확인한다.

작업 트리 검사가 실패하면 `.gitattributes` 이전에 체크아웃된 사본일 수 있다:
파일을 지우고 `git checkout -- <file>` 로 다시 받는다.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_SUFFIXES = (".bat", ".cmd", ".ps1", ".iss")

# 파일별 인코딩 계약. run_dev.bat 은 CP949(한글 콘솔 코드페이지)이며 재인코딩하지 않는다.
ENCODINGS = {
    "build.bat": "utf-8",
    "installer/build_installer.bat": "utf-8",
    "installer/DBMigrationTool.iss": "utf-8-bom",
    "installer/verify_prerequisites.ps1": "ascii",
    "installer/codesign.ps1": "ascii",
    "run_dev.bat": "cp949",
}


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=APP_ROOT, capture_output=True, text=True, check=False)


@pytest.fixture(scope="module")
def tracked_windows_scripts() -> list[str]:
    if shutil.which("git") is None:
        pytest.skip("git 없음")
    listed = _git("ls-files", "--cached", "--others", "--exclude-standard", "--", ".")
    if listed.returncode != 0:
        pytest.skip("git 저장소가 아님")
    files = [f for f in listed.stdout.splitlines() if f.lower().endswith(WINDOWS_SUFFIXES)]
    assert files, "Windows 스크립트를 하나도 찾지 못했습니다"
    return files


def test_gitattributes_forces_crlf(tracked_windows_scripts):
    result = _git("check-attr", "eol", "--", *tracked_windows_scripts)
    assert result.returncode == 0, result.stderr
    wrong = [line for line in result.stdout.splitlines() if not line.endswith(": eol: crlf")]
    assert not wrong, f".gitattributes 가 CRLF 를 강제하지 않는 파일: {wrong}"


def test_working_tree_is_crlf(tracked_windows_scripts):
    bad = []
    for rel in tracked_windows_scripts:
        data = (APP_ROOT / rel).read_bytes()
        bare_lf = data.count(b"\n") - data.count(b"\r\n")
        if bare_lf or (data and not data.endswith(b"\r\n")):
            bad.append(f"{rel} (bare LF {bare_lf})")
    assert not bad, "작업 트리에 LF 줄이 남은 Windows 스크립트: " + ", ".join(bad)


@pytest.mark.parametrize(("rel", "encoding"), sorted(ENCODINGS.items()))
def test_encoding_contract(rel, encoding):
    data = (APP_ROOT / rel).read_bytes()
    if encoding == "utf-8-bom":
        assert data.startswith(b"\xef\xbb\xbf"), "Inno Setup 은 BOM 이 없으면 한글을 깨뜨린다"
        data[3:].decode("utf-8")
    elif encoding == "utf-8":
        assert not data.startswith(b"\xef\xbb\xbf"), "cmd.exe 는 BOM 을 첫 명령의 일부로 읽는다"
        data.decode("utf-8")
    elif encoding == "cp949":
        with pytest.raises(UnicodeDecodeError):
            data.decode("utf-8")  # UTF-8 로 재인코딩되지 않았는지
        data.decode("cp949")
    else:
        data.decode(encoding)


def test_utf8_batch_files_switch_codepage():
    """UTF-8 배치 파일은 한글 echo 전에 `chcp 65001` 로 코드페이지를 맞춘다."""
    for rel, encoding in ENCODINGS.items():
        if rel.endswith(".bat") and encoding == "utf-8":
            lines = (APP_ROOT / rel).read_bytes().decode("utf-8").splitlines()
            head = [line.strip().lower() for line in lines[:3]]
            assert "chcp 65001 >nul" in head, rel
