@echo off
rem Windows counterpart of search.sh: any-route fare search and the "history" subcommand.
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
"%PYTHON%" -m fare_watch.search %*
exit /b %errorlevel%
