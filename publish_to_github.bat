@echo off
REM Publishes this folder to https://github.com/vygaslav03/SmartDiskCleaner and tags release v1.0.0.
REM Uses the anonymous GitHub noreply e-mail for THIS project only, so your private e-mail is never published.
cd /d "%~dp0"
where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Git is not installed. Install it from https://git-scm.com/download/win and run this file again.
    pause
    exit /b 1
)
if not exist ".git" git init

set "GHID="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "try { (Invoke-RestMethod -UseBasicParsing https://api.github.com/users/vygaslav03).id } catch { }"`) do set "GHID=%%i"
if defined GHID (
    set "NOREPLY=%GHID%+vygaslav03@users.noreply.github.com"
) else (
    set "NOREPLY=vygaslav03@users.noreply.github.com"
)
git config user.name "vygaslav03"
git config user.email "%NOREPLY%"
echo Commit e-mail for this project: %NOREPLY%

git add -A
git diff --cached --quiet || git commit -m "Smart Disk Cleaner 1.0.0: cleaner, analyzer, duplicates, history, Winapp2, CI"
REM Re-author existing local commits that still carry the private e-mail
for /f "usebackq delims=" %%e in (`git log -1 --format^=%%ae`) do set "LASTMAIL=%%e"
if /i not "%LASTMAIL%"=="%NOREPLY%" git commit --amend --no-edit --reset-author
git branch -M main
git remote get-url origin >nul 2>nul || git remote add origin https://github.com/vygaslav03/SmartDiskCleaner.git
echo.
echo Pushing to GitHub...
git push -u origin main
if errorlevel 1 (
    echo [ERROR] Push failed. Copy the messages above and send them to Claude.
    pause
    exit /b 1
)
git rev-parse v1.0.0 >nul 2>nul || git tag -a v1.0.0 -m "Smart Disk Cleaner 1.0.0"
git push origin v1.0.0
echo.
echo Done! Repository: https://github.com/vygaslav03/SmartDiskCleaner
echo CI and the release build: https://github.com/vygaslav03/SmartDiskCleaner/actions
pause
