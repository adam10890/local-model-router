@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Rollback-Imperium.ps1"
if errorlevel 1 (
    echo Imperium rollback failed. The active installation was preserved.
    pause
    exit /b 1
)
pause
