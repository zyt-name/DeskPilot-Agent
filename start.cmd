@echo off
setlocal
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [xm-agent] .venv not found. Set up the environment first:
  echo   py -3.12 -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -e ".[dev,gui]"
  pause
  exit /b 1
)

set "PYTHONPATH=%ROOT%src;%PYTHONPATH%"

rem Initialize pinned third-party MCP resources (versioned under vendor\NAME-VERSION).
echo [xm-agent] checking MCP resources...
"%PY%" -m xm_agent resources ensure
if errorlevel 1 (
  echo [xm-agent] resource init incomplete. The console will still start;
  echo [xm-agent] open Settings and use "检测并修复资源" to retry.
)

rem Pass "debug" to open devtools; other arguments are forwarded as-is.
set "EXTRA=%*"
if /i "%~1"=="debug" set "EXTRA=--debug"

echo [xm-agent] starting desktop console...
"%PY%" -m xm_agent gui --config "%ROOT%config\agent.example.toml" --db "%ROOT%.xm-agent\app.db" %EXTRA%

if errorlevel 1 (
  echo.
  echo [xm-agent] startup failed with exit code %errorlevel%
  pause
)
endlocal
