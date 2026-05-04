@echo off
REM ========================================================================
REM run.bat - one-click launch for Siberian Health Parser on Windows
REM
REM Double-click this file from Explorer to start the local web UI on
REM http://127.0.0.1:8765/. Press Ctrl+C in the cmd window to stop.
REM
REM Requires: Python 3.12 + this repo cloned + ".venv" already created.
REM See README.md for first-time setup instructions.
REM ========================================================================

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo.
    echo [ERROR] Virtual environment ".venv" not found in this folder.
    echo.
    echo First-time setup is required. In a cmd window run:
    echo.
    echo     py -3.12 -m venv .venv
    echo     .venv\Scripts\activate
    echo     pip install --upgrade pip
    echo     pip install -e .
    echo     playwright install chromium
    echo.
    echo Then double-click run.bat again.
    echo.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

echo.
echo Starting Siberian Health Parser...
echo Open http://127.0.0.1:8765/ in your browser.
echo Press Ctrl+C in this window to stop the server.
echo.

python -m sibparser serve

set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
    echo.
    echo [ERROR] Server exited with code %EXITCODE%.
    pause
)

endlocal
