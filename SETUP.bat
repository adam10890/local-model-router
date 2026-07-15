@echo off
setlocal
title Imperium - Developer Setup
cd /d "%~dp0"

echo Imperium developer setup
echo This source checkout uses Python only for development. Release packages include a private runtime.

python --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Download a self-contained Imperium release, or install Python 3.10+ for development.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" -m pip install -e ".[dev,mcp,agents]"
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)

if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul
echo Setup complete. START.bat will open the first-run wizard when no configuration exists.
pause
