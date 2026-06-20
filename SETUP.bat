@echo off
setlocal
title Local Model Router - Setup
cd /d "%~dp0"

echo.
echo =====================================================
echo  Local Model Router - Setup
echo =====================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.10+ was not found.
    echo Install Python from https://python.org and run this again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: virtual environment creation failed.
        pause
        exit /b 1
    )
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install -e ".[dev,mcp]"
if errorlevel 1 (
    echo ERROR: dependency install failed.
    pause
    exit /b 1
)

if not exist ".env" (
    if exist ".env.example" (
        echo Creating .env from .env.example...
        copy ".env.example" ".env" >nul
    ) else (
        echo Creating minimal .env...
        > ".env" echo OBSERVER_HOST=127.0.0.1
        >> ".env" echo OBSERVER_PORT=9000
        >> ".env" echo # A0_LMM_ROUTER_API_KEY=change-me
    )
)

echo.
echo Optional config check:
".venv\Scripts\python.exe" -m local_model_router config-check

echo.
echo Setup complete. Start with: START.bat
echo.
pause
