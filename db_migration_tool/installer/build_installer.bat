@echo off
chcp 65001 >nul
setlocal

REM DB Migration Tool - 인스톨러 빌드
REM
REM 순서: PyInstaller로 dist\DBMigrationTool.exe 를 먼저 만든 뒤 이 스크립트를 돌린다.
REM   python -m PyInstaller DBMigrationTool.spec --clean --noconfirm
REM   installer\build_installer.bat

cd /d "%~dp0.."

echo ==========================================
echo DB Migration Tool - Installer Build
echo ==========================================
echo.

REM ISCC 위치. winget은 사용자 영역에 설치하므로 두 곳을 다 본다.
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if not exist "%ISCC%" (
    echo [ERROR] Inno Setup을 찾을 수 없습니다.
    echo.
    echo 설치: winget install --id JRSoftware.InnoSetup
    echo.
    exit /b 1
)
echo [OK] ISCC: %ISCC%

if not exist "dist\DBMigrationTool.exe" (
    echo [ERROR] dist\DBMigrationTool.exe 가 없습니다.
    echo.
    echo 먼저 실행 파일을 빌드하세요:
    echo   python -m PyInstaller DBMigrationTool.spec --clean --noconfirm
    echo.
    exit /b 1
)
echo [OK] dist\DBMigrationTool.exe

if not exist "dist\prerequisites\vc_redist.x64.exe" (
    echo [ERROR] dist\prerequisites\vc_redist.x64.exe 가 없습니다.
    echo         인스톨러가 VC++ 런타임을 함께 설치하려면 필요합니다.
    exit /b 1
)
echo [OK] dist\prerequisites\vc_redist.x64.exe

REM prerequisite 검증 gate (감사 M-09): 고정 SHA-256 + 타임스탬프가 있는 유효한
REM Microsoft Corporation Authenticode 서명이어야만 인스톨러에 넣는다.
REM 고정값 갱신 절차는 BUILD_GUIDE.md "VC++ prerequisite" 절을 따른다.
powershell -NoProfile -ExecutionPolicy Bypass -File "installer\verify_prerequisites.ps1" -PrereqDir "dist\prerequisites" -HashFile "installer\prerequisites.sha256"
if errorlevel 1 (
    echo [ERROR] prerequisite 검증 실패 - 해시 또는 서명이 고정값과 다릅니다.
    exit /b 1
)

REM 실행 파일 서명 상태 확인 (감사 M-11). 서명이 없으면 UNSIGNED BUILD 경고를 낸다.
REM CODESIGN_REQUIRED=1 이면 서명 없는 실행 파일로는 인스톨러를 만들지 않는다.
powershell -NoProfile -ExecutionPolicy Bypass -File "installer\codesign.ps1" -CheckOnly -Path "dist\DBMigrationTool.exe"
if errorlevel 1 (
    echo [ERROR] 실행 파일이 서명되지 않았습니다 - CODESIGN_REQUIRED=1
    exit /b 1
)

echo.
echo [INFO] 인스톨러를 컴파일합니다...
"%ISCC%" "installer\DBMigrationTool.iss"
if errorlevel 1 (
    echo.
    echo [ERROR] 인스톨러 빌드 실패
    exit /b 1
)

REM 인스톨러 서명 (감사 M-11, 선택). 인증서 환경변수가 없으면 UNSIGNED BUILD 경고만 출력한다.
set "APPVER="
for /f "tokens=3" %%V in ('findstr /b /c:"#define AppVersion" "installer\DBMigrationTool.iss"') do set "APPVER=%%~V"
set "SETUP_EXE=dist\installer\DBMigrationTool-Setup-%APPVER%.exe"
if not exist "%SETUP_EXE%" (
    echo [ERROR] 인스톨러 산출물을 찾을 수 없습니다: %SETUP_EXE%
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "installer\codesign.ps1" -Path "%SETUP_EXE%"
if errorlevel 1 (
    echo [ERROR] 인스톨러 코드서명 실패
    exit /b 1
)

echo.
echo ==========================================
echo [SUCCESS] 인스톨러 빌드 완료
echo ==========================================
dir /b dist\installer\*.exe
endlocal
