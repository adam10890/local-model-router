@echo off
title Local Model Router
cd /d "%~dp0"

:: ── If the router is already running, just open the dashboard ─────────────
curl -s -o nul http://127.0.0.1:9000/health 2>nul
if not errorlevel 1 (
    echo Router is already running - opening the dashboard.
    start "" http://127.0.0.1:9000/ui
    timeout /t 3 /nobreak >nul
    exit /b 0
)

:: ── Sanity: venv must exist ────────────────────────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: virtual environment not found.
    echo Please run the setup file once first: SETUP ^(first time^).bat
    pause
    exit /b 1
)

:: ── Load .env if it exists (lines starting with # are skipped) ─────────────
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do set "%%A=%%B"
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
echo  The dashboard opens in your browser automatically
echo  as soon as the server is ready.
echo  Keep this window open. Press Ctrl+C to stop.
echo  =====================================================
echo.

:: ── Watcher: open the browser only after /health actually responds ────────
start "" /min powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest 'http://127.0.0.1:9000/health' -UseBasicParsing -TimeoutSec 2|Out-Null;Start-Process 'http://127.0.0.1:9000/ui';break}catch{Start-Sleep 1}}"

:: ── Start the router (foreground — this window is the server) ─────────────
.venv\Scripts\python.exe -m local_model_router serve

echo.
echo Router stopped.
pause
