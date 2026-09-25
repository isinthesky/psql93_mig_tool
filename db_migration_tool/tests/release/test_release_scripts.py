"""릴리스 경로(인스톨러·빌드 스크립트)의 정적 계약 검사.

Windows 스크립트는 Mac CI에서 실행할 수 없으므로, 보안상 꼭 지켜야 하는 형태를
문자열 수준에서 고정한다. 실제 실행 검증은 Windows 릴리스 단계가 한다.

- M-07: 설치 프로그램이 라이선스 키를 명령줄로 받지 않고, 입력을 마스킹하고, 로그에 남기지 않는다.
- M-08: uv.lock 을 추적하고 빌드가 lock 을 강제한다. 버전 증가가 lock 을 깨지 않는다.
- M-09: VC++ 재배포 파일을 고정 SHA-256 과 Microsoft 서명으로 검증한 뒤에만 포함한다.
- M-11: 인증서가 있으면 서명·검증하고, 없으면 UNSIGNED BUILD 를 분명히 경고한다.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = APP_ROOT / "installer"
ISS = INSTALLER / "DBMigrationTool.iss"
BUILD_BAT = APP_ROOT / "build.bat"
BUILD_INSTALLER_BAT = INSTALLER / "build_installer.bat"
VERIFY_PS1 = INSTALLER / "verify_prerequisites.ps1"
CODESIGN_PS1 = INSTALLER / "codesign.ps1"
PREREQ_HASHES = INSTALLER / "prerequisites.sha256"
UV_LOCK = APP_ROOT / "uv.lock"


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")


def _code_lines(text: str, comment_prefixes: tuple[str, ...]) -> list[str]:
    """주석을 뺀 실제 명령 줄."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.upper().startswith(comment_prefixes):
            continue
        lines.append(line)
    return lines


def _bat_code(path: Path) -> list[str]:
    return _code_lines(_text(path), ("REM", "::"))


def _iss_code_section(text: str) -> str:
    return text.split("[Code]", 1)[1]


# ── M-07 인스톨러 secret ─────────────────────────────────────


class TestInstallerLicenseSecret:
    def test_license_input_is_masked(self):
        text = _text(ISS)
        match = re.search(r"LicensePage\.Add\('[^']*',\s*(True|False)\)", text)
        assert match, "라이선스 입력 필드 정의를 찾지 못했습니다"
        assert match.group(1) == "True", "라이선스 키 입력은 Password(마스킹) 필드여야 합니다"

    def test_licensekey_command_line_is_not_accepted(self):
        """키를 명령줄 값으로 읽는 경로가 없어야 한다(프로세스 목록·설치 로그·배포 스크립트 노출)."""
        code = _iss_code_section(_text(ISS))
        # /LICENSEKEY= 는 '거부'를 위해서만 등장한다: 값을 읽어 쓰는 함수가 없어야 한다.
        assert "GetLicenseKeyParam" not in code
        assert "LicensePage.Values[0] := " not in code, (
            "입력 필드를 명령줄 값으로 미리 채우지 않는다"
        )
        reject = re.search(r"function HasLicenseKeyParam.*?end;", code, re.S)
        assert reject, "/LICENSEKEY 를 감지해 설치를 중단하는 함수가 있어야 합니다"
        init = re.search(r"function InitializeSetup: Boolean;.*?\nend;", code, re.S)
        assert init and "HasLicenseKeyParam" in init.group(0)
        assert "Result := False" in init.group(0)

    def test_license_file_is_the_silent_alternative(self):
        code = _iss_code_section(_text(ISS))
        assert "/LICENSEFILE=" in code
        assert "LoadStringFromFile" in code

    def test_key_is_never_logged(self):
        code = _iss_code_section(_text(ISS))
        for call in re.findall(r"Log\((.*?)\);", code):
            assert "Key" not in call and "Values[0]" not in call, (
                f"키가 로그로 나갈 수 있음: {call}"
            )

    def test_seed_goes_to_user_modifiable_subfolder(self):
        text = _text(ISS)
        assert re.search(r'^Name: "\{app\}\\seed"; Permissions: users-modify', text, re.M), (
            "씨앗 전용 폴더에 Users 수정 권한이 있어야 앱이 씨앗을 지울 수 있다"
        )
        code = _iss_code_section(text)
        assert "{app}\\seed" in code
        assert re.search(r"DeleteFile\(LegacySeed\)", code), "1.2.7 이하 위치의 씨앗을 지운다"

    def test_uninstall_removes_seed_folder(self):
        text = _text(ISS)
        section = text.split("[UninstallDelete]", 1)[1].split("\n[", 1)[0]
        assert '{app}\\seed"' in section
        assert '{app}\\license.seed"' in section

    def test_app_seed_path_matches_installer(self):
        from src.licensing import activation

        code = _iss_code_section(_text(ISS))
        assert f"'{activation.SEED_DIRNAME}'" in code
        assert f"'{activation.LICENSE_SEED_FILENAME}'" in code


# ── M-08 lock 추적·강제 ─────────────────────────────────────


class TestDependencyLock:
    def test_uv_lock_not_ignored(self):
        if shutil.which("git") is None:
            pytest.skip("git 없음")
        result = subprocess.run(["git", "check-ignore", "-q", "uv.lock"], cwd=APP_ROOT, check=False)
        if result.returncode == 128:
            pytest.skip("git 저장소가 아님")
        assert result.returncode == 1, "uv.lock 이 .gitignore 에 걸려 있습니다"

    def test_uv_lock_exists_and_matches_project_version(self):
        lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
        project = tomllib.loads((APP_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        own = [p for p in lock["package"] if p["name"] == project["project"]["name"]]
        assert len(own) == 1
        assert own[0]["version"] == project["project"]["version"], (
            "uv.lock 의 프로젝트 버전이 pyproject 와 다르면 `uv sync --locked` 가 실패합니다"
        )

    def test_build_bat_enforces_lock(self):
        code = _bat_code(BUILD_BAT)
        sync = [line for line in code if line.startswith("uv sync")]
        assert sync, "build.bat 은 uv sync 로 lock 을 설치해야 합니다"
        assert all("--locked" in line or "--frozen" in line for line in sync)
        assert not any(line.startswith("uv pip install") for line in code), (
            "uv pip install 은 lock 을 무시합니다"
        )

    def test_lock_sync_precedes_version_bump(self):
        code = _bat_code(BUILD_BAT)
        sync_at = next(i for i, line in enumerate(code) if line.startswith("uv sync"))
        bump_at = next(i for i, line in enumerate(code) if "bump_version.py" in line)
        assert sync_at < bump_at


class TestBumpVersionKeepsLock:
    """build.bat 이 버전을 올려도 다음 `uv sync --locked` 가 깨지지 않아야 한다."""

    @pytest.fixture
    def bump(self):
        path = APP_ROOT / "tools" / "bump_version.py"
        spec = importlib.util.spec_from_file_location("bump_version_under_test", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_uv_lock_is_a_version_target(self, bump):
        assert any(path.name == "uv.lock" for path, _ in bump.TARGETS)

    def test_write_version_updates_copies_bytewise(self, bump, tmp_path, monkeypatch):
        copies = []
        originals = {}
        for path, pattern in bump.TARGETS:
            dst = tmp_path / path.name
            data = path.read_bytes()
            if path.name == "uv.lock":
                data = data.replace(b"\n", b"\r\n")  # Windows(autocrlf) 체크아웃 모사
            dst.write_bytes(data)
            originals[path.name] = data
            copies.append((dst, pattern))
        monkeypatch.setattr(bump, "TARGETS", copies)
        monkeypatch.setattr(bump, "ROOT", tmp_path)

        bump.write_version("9.8.7")

        for dst, pattern in copies:
            data = dst.read_bytes()
            match = re.search(pattern, data)
            assert match and match.group(2) == b"9.8.7", dst.name
            # 버전 외의 바이트(BOM·CRLF·한글)는 그대로다.
            before = originals[dst.name]
            assert len(data) - len(before) == len(b"9.8.7") - len(
                re.search(pattern, before).group(2)
            )
        lock = tomllib.loads((tmp_path / "uv.lock").read_bytes().decode("utf-8"))
        own = [p for p in lock["package"] if p["name"] == "db-migration-tool"]
        assert own[0]["version"] == "9.8.7"


# ── M-09 prerequisite 검증 gate ──────────────────────────────


class TestPrerequisiteGate:
    def test_hash_file_pins_vc_redist(self):
        entries = {}
        for line in _text(PREREQ_HASHES).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(\S+)", line)
            assert match, f"형식 오류: {line!r}"
            entries[match.group(2)] = match.group(1).lower()
        assert set(entries) == {"vc_redist.x64.exe"}
        # 2026-09-25 my-wsl-01 dist\prerequisites 에서 읽기 전용으로 확인한 값(14.44.35211.0).
        assert entries["vc_redist.x64.exe"] == (
            "cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b"
        )

    def test_hash_file_is_ascii(self):
        PREREQ_HASHES.read_bytes().decode("ascii")

    def test_verify_script_checks_hash_signer_and_status(self):
        code = "\n".join(_code_lines(_text(VERIFY_PS1), ("#",)))
        assert "Get-FileHash" in code and "SHA256" in code
        assert "Get-AuthenticodeSignature" in code
        assert "'Valid'" in code
        assert "Microsoft Corporation" in code
        assert "TimeStamperCertificate" in code
        assert "exit 1" in code

    def test_build_installer_runs_gate_before_iscc(self):
        code = _bat_code(BUILD_INSTALLER_BAT)
        gate_at = next(i for i, line in enumerate(code) if "verify_prerequisites.ps1" in line)
        iscc_at = next(i for i, line in enumerate(code) if line.startswith('"%ISCC%"'))
        assert gate_at < iscc_at
        assert "prerequisites.sha256" in code[gate_at]
        assert code[gate_at + 1].startswith("if errorlevel 1"), "gate 실패 시 중단해야 합니다"


# ── M-11 선택적 코드서명 훅 ─────────────────────────────────


class TestCodeSigningHook:
    def test_codesign_script_contract(self):
        code = "\n".join(_code_lines(_text(CODESIGN_PS1), ("#",)))
        assert "CODESIGN_CERT_THUMBPRINT" in code
        assert "CODESIGN_PFX" in code and "CODESIGN_PFX_PASSWORD" in code
        assert "UNSIGNED BUILD" in code
        assert re.search(r"sign\s.*/fd SHA256.*/tr", code)
        assert "verify /pa" in code
        assert "Get-AuthenticodeSignature" in code
        # PFX 비밀번호를 signtool 명령줄(/p)로 넘기지 않는다 — 프로세스 목록에 노출된다.
        assert not re.search(r"/p\s", code)

    def test_build_bat_signs_exe(self):
        code = _bat_code(BUILD_BAT)
        idx = next(i for i, line in enumerate(code) if "codesign.ps1" in line)
        assert "dist\\DBMigrationTool.exe" in code[idx]
        assert code[idx + 1].startswith("if errorlevel 1")
        pyi_at = next(i for i, line in enumerate(code) if "PyInstaller" in line)
        assert pyi_at < idx

    def test_build_installer_signs_setup_after_iscc(self):
        code = _bat_code(BUILD_INSTALLER_BAT)
        iscc_at = next(i for i, line in enumerate(code) if line.startswith('"%ISCC%"'))
        sign_at = [
            i for i, line in enumerate(code) if "codesign.ps1" in line and "-CheckOnly" not in line
        ]
        assert sign_at and sign_at[-1] > iscc_at
        assert "DBMigrationTool-Setup-" in "\n".join(code[iscc_at : sign_at[-1] + 1])
        assert code[sign_at[-1] + 1].startswith("if errorlevel 1")

    def test_powershell_scripts_are_ascii(self):
        """Windows PowerShell 5.1 은 BOM 없는 파일을 시스템 코드페이지로 읽는다 — ASCII 로 둔다."""
        for script in (VERIFY_PS1, CODESIGN_PS1):
            script.read_bytes().decode("ascii")
