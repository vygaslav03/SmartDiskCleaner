@echo off
REM Publishes this folder to https://github.com/vygaslav03/SmartDiskCleaner and tags release v1.0.0.
REM The first push opens a GitHub sign-in window (Git Credential Manager) - log in there.
cd /d "%~dp0"
where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Git is not installed. Install it from https://git-scm.com/download/win and run this file again.
    pause
    exit /b 1
)
if not exist ".git" git init
git config user.name >nul 2>nul || git config user.name "vygaslav03"
git config user.email >nul 2>nul || git config user.email "vygaslav03@users.noreply.github.com"
git add -A
git diff --cached --quiet || git commit -m "Smart Disk Cleaner 1.0.0: cleaner, analyzer, duplicates, history, Winapp2, CI"
git branch -M main
git remote get-url origin >nul 2>nul || git remote add origin https://github.com/vygaslav03/SmartDiskCleaner.git
echo.
echo Pushing to GitHub...
git push -u origin main
if errorlevel 1 (
    echo Remote already has commits - merging them, keeping local files on conflicts...
    git pull origin main --allow-unrelated-histories --no-edit -X ours
    git push -u origin main
    if errorlevel 1 (
        echo [ERROR] Push failed. Copy the messages above and send them to Claude.
        pause
        exit /b 1
    )
)
git rev-parse v1.0.0 >nul 2>nul || git tag -a v1.0.0 -m "Smart Disk Cleaner 1.0.0"
git push origin v1.0.0
echo.
echo Done! Repository: https://github.com/vygaslav03/SmartDiskCleaner
echo CI and the release build: https://github.com/vygaslav03/SmartDiskCleaner/actions
pause
