@echo off
echo DB Migration Tool - Build Script
echo.

REM 가상환경 활성화
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate
) else (
    echo 가상환경을 찾을 수 없습니다.
    echo python -m venv venv 명령으로 생성하세요.
    pause
    exit /b 1
)

REM PyInstaller 설치 확인
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo PyInstaller를 설치합니다...
    pip install PyInstaller
)

REM 이전 빌드 제거
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build

REM 빌드 실행
echo.
echo 빌드를 시작합니다...
pyinstaller db_migration_tool.spec

REM 빌드 결과 확인
if exist dist\DBMigrationTool.exe (
    echo.
    echo 빌드가 완료되었습니다!
    echo 실행 파일: dist\DBMigrationTool.exe
) else (
    echo.
    echo 빌드에 실패했습니다.
)

pause