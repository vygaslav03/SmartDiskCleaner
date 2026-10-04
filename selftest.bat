@echo off
REM Self-test: opens every page, runs READ-ONLY scans, saves screenshots to selftest\. Deletes nothing.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
"%PY%" run.py --selftest
echo.
echo Report: %CD%\selftest\report.txt
pause
