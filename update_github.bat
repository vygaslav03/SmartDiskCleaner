@echo off
REM Commits all changes, pushes them to GitHub and (re)publishes release tag v1.0.0 on the latest commit.
cd /d "%~dp0"
git add -A
git diff --cached --quiet || git commit -m "Disk analyzer: interactive treemap synced with the folder tree"
git push origin main
if errorlevel 1 (
    echo [ERROR] Push failed. Send the messages above to Claude.
    pause
    exit /b 1
)
git tag -f -a v1.0.0 -m "Smart Disk Cleaner 1.0.0"
git push -f origin v1.0.0
echo.
echo Done. Watch the run: https://github.com/vygaslav03/SmartDiskCleaner/actions
pause
