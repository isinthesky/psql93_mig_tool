@echo off
echo ==========================================
echo DB Migration Tool - Build Script (uv)
echo ==========================================
echo.

REM uv 설치 확인
where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] uv가 설치되어 있지 않습니다.
    echo.
    echo uv를 설치하려면 다음 명령을 실행하세요:
    echo   PowerShell: irm https://astral.sh/uv/install.ps1 ^| iex
    echo   또는: pip install uv
    echo.
    pause
    exit /b 1
)
echo [OK] uv found

REM 가상환경 확인 및 생성
if not exist .venv (
    echo [INFO] 가상환경을 생성합니다...
    uv venv
    if errorlevel 1 (
        echo [ERROR] 가상환경 생성 실패
        pause
        exit /b 1
    )
    echo [OK] 가상환경 생성 완료
)

REM 가상환경 활성화
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate
    echo [OK] 가상환경 활성화
) else (
    echo [ERROR] 가상환경을 찾을 수 없습니다.
    pause
    exit /b 1
)

REM 개발 의존성 설치 (PyInstaller 포함)
echo.
echo [INFO] 개발 의존성을 설치합니다...
uv pip install -e ".[dev]"
if errorlevel 1 (
    echo [ERROR] 의존성 설치 실패
    pause
    exit /b 1
)
echo [OK] 의존성 설치 완료

REM 이전 빌드 제거
echo.
echo [INFO] 이전 빌드를 정리합니다...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
echo [OK] 정리 완료

REM 빌드 실행
echo.
echo ==========================================
echo [INFO] 빌드를 시작합니다...
echo ==========================================
python -m PyInstaller db_migration_tool.spec --clean

REM 빌드 결과 확인
echo.
if exist dist\DBMigrationTool.exe (
    echo ==========================================
    echo [SUCCESS] 빌드가 완료되었습니다!
    echo ==========================================
    echo.
    echo 실행 파일: dist\DBMigrationTool.exe
    dir dist\DBMigrationTool.exe
) else (
    echo ==========================================
    echo [ERROR] 빌드에 실패했습니다.
    echo ==========================================
)

echo.
pause