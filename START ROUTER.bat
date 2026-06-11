@echo off
title Local Model Router
cd /d "%~dp0"

:: ── Load .env if it exists (API key etc.) ──────────────────────────────────
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" if not "%%A:~0,1%"=="#" set "%%A=%%B"
    )
)

:: ── Print startup info ─────────────────────────────────────────────────────
echo.
echo  =====================================================
echo   Local Model Router  ^|  http://127.0.0.1:9000
echo  =====================================================
if defined A0_LMM_ROUTER_API_KEY (
    echo   Auth : Bearer token required  [from .env]
) else (
    echo   Auth : none  ^(dev mode^)
)
echo   Dashboard : http://127.0.0.1:9000/ui
echo   Health    : http://127.0.0.1:9000/health
echo.
echo  Press Ctrl+C to stop.
echo  =====================================================
echo.

:: ── Open dashboard in browser after a short delay ─────────────────────────
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:9000/ui"

:: ── Start the router ───────────────────────────────────────────────────────
.venv\Scripts\python.exe -m local_model_router serve

pause
