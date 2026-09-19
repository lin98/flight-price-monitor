@echo off
rem Windows counterpart of run_grouped.sh: one incremental batch of the fixed TPE/KHH-PUS monitor, for Task Scheduler.
rem Messages are in English on purpose: cmd.exe is not UTF-8 by default.
setlocal
cd /d "%~dp0.."
rem Reports and progress lines contain Chinese; force UTF-8 so nothing is decoded as cp950.
set PYTHONUTF8=1
rem Interpreter order: FARE_WATCH_PYTHON, then the project .venv (made by setup.bat), then python on PATH.
if defined FARE_WATCH_PYTHON (
    set "PYTHON=%FARE_WATCH_PYTHON%"
) else if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)
if not defined FARE_WATCH_MAX_FETCHES set FARE_WATCH_MAX_FETCHES=24

rem fli is an optional shortcut and playwright is its fallback, so either one alone is a usable setup.
set HAS_FLI=0
set HAS_PW=0
"%PYTHON%" -c "import fli" >nul 2>nul && set HAS_FLI=1
"%PYTHON%" -c "import playwright" >nul 2>nul && set HAS_PW=1
if "%HAS_FLI%%HAS_PW%"=="00" (
    echo fare-watch: "%PYTHON%" has neither fli nor playwright -- no usable source. Run scripts\setup.bat first. 1>&2
    exit /b 78
)
if "%HAS_PW%"=="0" echo fare-watch: no playwright; running fli-only 1>&2

"%PYTHON%" -m fare_watch --grouped --source fli --merge-stored --max-fetches %FARE_WATCH_MAX_FETCHES% --once %*
exit /b %errorlevel%
