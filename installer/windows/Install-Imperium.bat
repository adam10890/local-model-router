@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-Imperium.ps1"
if errorlevel 1 (
    echo Imperium installation failed. The previous version was preserved.
    pause
    exit /b 1
)
