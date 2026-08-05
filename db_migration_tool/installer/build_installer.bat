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

echo.
echo [INFO] 인스톨러를 컴파일합니다...
"%ISCC%" "installer\DBMigrationTool.iss"
if errorlevel 1 (
    echo.
    echo [ERROR] 인스톨러 빌드 실패
    exit /b 1
)

echo.
echo ==========================================
echo [SUCCESS] 인스톨러 빌드 완료
echo ==========================================
dir /b dist\installer\*.exe
endlocal
