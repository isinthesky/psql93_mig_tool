@echo off
echo DB Migration Tool - Development Mode
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

REM 애플리케이션 실행
echo 애플리케이션을 시작합니다...
python src/main.py

REM 오류 발생 시 대기
if errorlevel 1 (
    echo.
    echo 오류가 발생했습니다.
    pause
)