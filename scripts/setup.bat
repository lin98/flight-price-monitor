@echo off
rem Windows counterpart of setup.sh: creates .venv, installs dependencies, downloads Chromium. Safe to re-run.
rem Messages are in English on purpose: cmd.exe is not UTF-8 by default.
setlocal
cd /d "%~dp0.."
rem Reports and progress lines contain Chinese; force UTF-8 so nothing is decoded as cp950.
set PYTHONUTF8=1

rem A .venv left half-built by an earlier failure has no pip; rebuild it instead of failing again.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m pip --version >nul 2>nul
    if errorlevel 1 (
        echo Found an incomplete .venv, rebuilding it...
        rmdir /s /q .venv
    )
)

if not exist ".venv\Scripts\python.exe" (
    if defined FARE_WATCH_PYTHON (
        "%FARE_WATCH_PYTHON%" -m venv .venv || goto :nopython
    ) else (
        where py >nul 2>nul
        if errorlevel 1 (
            python -m venv .venv || goto :nopython
        ) else (
            py -3 -m venv .venv || goto :nopython
        )
    )
)
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" || goto :tooold
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto :failed
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :failed
".venv\Scripts\python.exe" -m playwright install chromium || goto :failed

echo.
echo Installed. Start the web UI with: scripts\web.bat
exit /b 0

:nopython
echo Python 3.9 or newer was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH", then run this file again.
exit /b 1

:tooold
echo Python 3.9 or newer is required. Install a newer one, delete the .venv folder, and run this file again.
exit /b 1

:failed
echo Installation failed. Check your network connection and run this file again.
exit /b 1
