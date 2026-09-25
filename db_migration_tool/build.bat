@echo off
chcp 65001 >nul
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

REM 의존성 설치 — uv.lock 을 그대로 강제한다 (감사 M-08).
REM --locked: pyproject.toml 과 uv.lock 이 어긋나면 다시 해석하지 않고 실패한다.
REM --all-extras: 개발 게이트(uv sync --all-extras)와 같은 환경. 기본 sync 는 lock 에 없는 패키지를
REM 지우므로 test extras 를 빼면 빌드할 때마다 pytest-qt 등이 사라진다.
REM 가상환경이 없으면 uv sync 가 만든다. lock 갱신은 빌드가 아니라 별도 커밋으로 한다
REM (BUILD_GUIDE.md "의존성 lock" 절).
echo.
echo [INFO] uv.lock 기준으로 의존성을 설치합니다...
uv sync --locked --all-extras
if errorlevel 1 (
    echo [ERROR] 의존성 설치 실패 - uv.lock 이 pyproject.toml 과 맞지 않으면 uv lock 후 커밋하세요.
    pause
    exit /b 1
)
echo [OK] 의존성 설치 완료

REM 가상환경 활성화
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate
    echo [OK] 가상환경 활성화
) else (
    echo [ERROR] 가상환경을 찾을 수 없습니다.
    pause
    exit /b 1
)

REM 버전 자동 증가 (patch +1)
REM pyproject.toml / src\version.py / installer\DBMigrationTool.iss / uv.lock 을 함께 고친다.
REM uv.lock 의 프로젝트 버전도 같이 올려야 다음 빌드의 uv sync --locked 가 통과한다.
echo.
echo [INFO] 버전을 올립니다...
python tools\bump_version.py
if errorlevel 1 (
    echo [ERROR] 버전 증가 실패
    pause
    exit /b 1
)

REM 이전 빌드 제거
REM dist 는 통째로 지우지 않는다. 인스톨러가 dist\prerequisites\vc_redist.x64.exe 를
REM 입력으로 쓰고 dist\installer\ 에 산출물이 쌓인다. 지우면 인스톨러 빌드가 실패한다.
REM exe 는 PyInstaller 가 --noconfirm 으로 덮어쓴다.
echo.
echo [INFO] 이전 빌드를 정리합니다...
if exist build rmdir /s /q build
echo [OK] 정리 완료

REM 빌드 실행
echo.
echo ==========================================
echo [INFO] 빌드를 시작합니다...
echo ==========================================
python -m PyInstaller DBMigrationTool.spec --clean
if errorlevel 1 goto :build_failed

REM 빌드 결과 확인
echo.
if not exist dist\DBMigrationTool.exe goto :build_failed

REM 코드서명 (감사 M-11, 선택). 인증서 환경변수가 없으면 UNSIGNED BUILD 경고만 출력한다.
REM 릴리스 빌드는 CODESIGN_REQUIRED=1 로 서명 없는 산출물을 실패로 만든다.
powershell -NoProfile -ExecutionPolicy Bypass -File "installer\codesign.ps1" -Path "dist\DBMigrationTool.exe"
if errorlevel 1 (
    echo [ERROR] 코드서명 실패
    pause
    exit /b 1
)

echo ==========================================
echo [SUCCESS] 빌드가 완료되었습니다!
echo ==========================================
echo.
echo 실행 파일: dist\DBMigrationTool.exe
dir dist\DBMigrationTool.exe

echo.
pause
exit /b 0

:build_failed
echo ==========================================
echo [ERROR] 빌드에 실패했습니다.
echo ==========================================

echo.
pause
exit /b 1
