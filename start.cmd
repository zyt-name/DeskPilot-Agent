@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "PROBE_TMP=%TEMP%\xm-agent-pyprobe.txt"

if not exist "%PY%" goto :find_python

rem An existing .venv is only usable while its interpreter still satisfies 3.12+.
"%PY%" -c "import sys;sys.exit(0 if sys.version_info>=(3,12) else 1)" >nul 2>&1
if errorlevel 1 (
  echo [xm-agent] Python version incompatible: the existing .venv cannot run on Python 3.12+.
  echo [xm-agent] delete "%ROOT%.venv" and run this script again to rebuild it.
  pause
  exit /b 1
)
goto :run

:find_python
rem Minimum version matches requires-python in pyproject.toml.
if defined XM_AGENT_PYTHON (
  call :probe "%XM_AGENT_PYTHON%"
  goto :finish_probe
)

rem 1) Windows py launcher, 2) registered interpreters, 3) PATH, 4) default install dirs.
call :probe py -3
if defined BASE goto :create_venv

for %%H in (HKCU HKLM) do (
  for /f "tokens=2,*" %%A in ('reg query %%H\SOFTWARE\Python\PythonCore /s /v ExecutablePath 2^>nul') do (
    if /i "%%A"=="REG_SZ" call :probe "%%B"
  )
)
if defined BASE goto :create_venv

for /f "delims=" %%P in ('where python 2^>nul') do call :probe "%%P"
if defined BASE goto :create_venv
for /f "delims=" %%P in ('where python3 2^>nul') do call :probe "%%P"
if defined BASE goto :create_venv

for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "C:\Python3*" "%ProgramFiles%\Python3*") do call :probe "%%~fD\python.exe"
if defined BASE goto :create_venv

:finish_probe
del "%PROBE_TMP%" >nul 2>&1
if defined FIRST_BAD (
  echo [xm-agent] Python version incompatible: %FIRST_BAD% was found, but 3.12 or newer is required.
) else (
  echo [xm-agent] Python 3.12 or newer was not found on this machine.
)
echo [xm-agent] Install Python 3.12+ from python.org or the Microsoft Store, then run this script again.
echo [xm-agent] To use a specific interpreter, set XM_AGENT_PYTHON to its python.exe path.
pause
exit /b 1

:create_venv
del "%PROBE_TMP%" >nul 2>&1
echo [xm-agent] .venv not found, creating it with Python %BASE_VER% ...
%BASE% -m venv "%ROOT%.venv"
if errorlevel 1 (
  echo [xm-agent] failed to create .venv with %BASE%
  pause
  exit /b 1
)
echo [xm-agent] installing dependencies, the first run may take a few minutes...
"%PY%" -m pip install --disable-pip-version-check -e ".[dev,gui]"
if errorlevel 1 (
  echo [xm-agent] dependency install failed. Retry manually, or use a faster mirror:
  echo   .venv\Scripts\python.exe -m pip install -e ".[dev,gui]" -i https://pypi.tuna.tsinghua.edu.cn/simple
  pause
  exit /b 1
)

:run
set "PYTHONPATH=%ROOT%src;%PYTHONPATH%"

rem Initialize pinned third-party MCP resources (versioned under vendor\NAME-VERSION).
echo [xm-agent] checking MCP resources...
"%PY%" -m xm_agent resources ensure
if errorlevel 1 (
  echo [xm-agent] resource init incomplete. The console will still start;
  echo [xm-agent] open Settings and use "检测并修复资源" to retry.
)

rem Browser channel uses system Edge/Chrome, with bundled Chromium only as fallback.
rem The kernel is ~210MB, so it is installed on demand instead of every startup.
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" goto :browser_ok
if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" goto :browser_ok
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" goto :browser_ok
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" goto :browser_ok
if exist "%ROOT%.xm-agent\browser\engines\chromium-*\chrome.exe" goto :browser_ok
echo [xm-agent] no Edge/Chrome detected. The browser channel needs a kernel:
echo   "%PY%" -m xm_agent resources ensure --only chromium
:browser_ok

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
exit /b 0

:probe
rem %*: candidate command, either py -3 or a quoted python.exe path.
rem Prints "ok|bad <major>.<minor>" through the exit code and the version line.
if defined BASE exit /b 0
%* -c "import sys;print('.'.join(map(str,sys.version_info[:2])));sys.exit(0 if sys.version_info>=(3,12) else 1)" >"%PROBE_TMP%" 2>nul
if errorlevel 1 (set "PROBE_OK=0") else (set "PROBE_OK=1")
rem Ignore anything that is not a real interpreter (e.g. the Store alias stub).
findstr /r /c:"^[0-9][0-9]*\.[0-9][0-9]*$" "%PROBE_TMP%" >nul 2>&1
if errorlevel 1 exit /b 1
set "PROBE_VER="
set /p PROBE_VER=<"%PROBE_TMP%"
if "%PROBE_OK%"=="1" (
  set "BASE=%*"
  set "BASE_VER=%PROBE_VER%"
  exit /b 0
)
if not defined FIRST_BAD set "FIRST_BAD=%PROBE_VER%"
exit /b 1
