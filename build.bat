@echo off
REM ===========================================================
REM  DiskCleaner - build a single-file DiskCleaner.exe
REM  Result: dist\DiskCleaner.exe (runs on Windows 10/11 without Python)
REM ===========================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.12+ not found in PATH. Install it from https://www.python.org/downloads/
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating virtual environment .venv ...
    python -m venv .venv
    if errorlevel 1 exit /b 1
)
set "PY=.venv\Scripts\python.exe"

echo [2/4] Installing dependencies ...
"%PY%" -m pip install --upgrade pip >nul
"%PY%" -m pip install -r requirements.txt -r requirements-dev.txt
if errorlevel 1 exit /b 1

echo [3/4] Running tests ...
"%PY%" -m pytest
if errorlevel 1 (
    echo [ERROR] Tests failed - build aborted.
    exit /b 1
)

set "ICON=assets\icon.ico"
if not exist "%ICON%" (
    echo [ERROR] %ICON% not found.
    exit /b 1
)

echo [4/4] Building DiskCleaner.exe ...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name DiskCleaner ^
    --icon "%ICON%" ^
    --add-data "assets;assets" ^
    --hidden-import send2trash ^
    --exclude-module tkinter ^
    --exclude-module pytest ^
    run.py
if errorlevel 1 (
    echo [ERROR] PyInstaller failed.
    exit /b 1
)

echo.
echo Done: %CD%\dist\DiskCleaner.exe
endlocal
