@echo off
REM Self-test of the BUILT exe (dist\DiskCleaner.exe). Read-only, deletes nothing.
cd /d "%~dp0"
if not exist "dist\DiskCleaner.exe" (
    echo dist\DiskCleaner.exe not found - run build.bat first.
    pause
    exit /b 1
)
echo Running self-test of the built exe, please wait...
start /wait "" "dist\DiskCleaner.exe" --selftest
echo.
echo Report: %CD%\dist\selftest\report.txt
pause
