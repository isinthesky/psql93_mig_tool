@echo off
setlocal

REM Run server-side COPY benchmark using the project's venv.
REM This avoids WSL/bash backslash-escape issues by running in cmd.exe.

set "ROOT=%~dp0.."
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "SCRIPT=%ROOT%\tools\bench_server_copy_ph_3days.py"

if not exist "%PY%" (
  echo [ERR] venv python not found: %PY%
  exit /b 2
)

if "%MIGTOOL_PW%"=="" (
  echo [ERR] MIGTOOL_PW env var is not set.
  echo       Example: set MIGTOOL_PW=your_password
  exit /b 3
)

cd /d "%ROOT%" || exit /b 4

"%PY%" -u "%SCRIPT%" --password-env MIGTOOL_PW
set EC=%ERRORLEVEL%
echo [DONE] exit=%EC%
exit /b %EC%
