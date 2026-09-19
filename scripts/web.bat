@echo off
rem Windows entry point: installs on first run, then starts the local web UI.
rem Messages are in English on purpose: cmd.exe is not UTF-8 by default.
setlocal
cd /d "%~dp0.."
rem Reports and progress lines contain Chinese; force UTF-8 so nothing is decoded as cp950.
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
    echo First run: installing, about 1-2 minutes, only needed once...
    where py >nul 2>nul
    if errorlevel 1 (
        python -m venv .venv || goto :nopython
    ) else (
        py -3 -m venv .venv || goto :nopython
    )
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto :failed
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :failed
    ".venv\Scripts\python.exe" -m playwright install chromium || goto :failed
)

".venv\Scripts\python.exe" -m fare_watch.web_server %*
exit /b %errorlevel%

:nopython
echo Python 3.9 or newer was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH", then run this file again.
exit /b 1

:failed
echo Installation failed. Delete the .venv folder and run this file again.
exit /b 1
