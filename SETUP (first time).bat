@echo off
title Local Model Router - Setup
cd /d "%~dp0"

echo.
echo  =====================================================
echo   Local Model Router - First-Time Setup
echo  =====================================================
echo.

:: ── Check Python ───────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found. Install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

:: ── Create venv if missing ─────────────────────────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo  Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 ( echo  ERROR: venv creation failed. & pause & exit /b 1 )
)

:: ── Install / upgrade dependencies ────────────────────────────────────────
echo  Installing dependencies...
.venv\Scripts\pip install -e ".[dev,mcp]" --quiet
if errorlevel 1 ( echo  ERROR: pip install failed. & pause & exit /b 1 )

:: ── Create .env if missing ─────────────────────────────────────────────────
if not exist ".env" (
    echo  Creating .env from .env.example...
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
    ) else (
        echo # Optional: set an API key to protect the router > ".env"
        echo # A0_LMM_ROUTER_API_KEY=change-me >> ".env"
    )
    echo  NOTE: Edit .env to set your API key if needed.
)

echo.
echo  Setup complete!
echo  Double-click  "START ROUTER.bat"  to launch.
echo.
pause
