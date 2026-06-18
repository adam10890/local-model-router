@echo off
setlocal
title Local Model Router
cd /d "%~dp0"

if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" set "%%A=%%B"
    )
)

if not defined OBSERVER_HOST set "OBSERVER_HOST=127.0.0.1"
if not defined OBSERVER_PORT set "OBSERVER_PORT=9000"
set "BASE_URL=http://%OBSERVER_HOST%:%OBSERVER_PORT%"

powershell -NoProfile -Command "try{Invoke-WebRequest '%BASE_URL%/health' -UseBasicParsing -TimeoutSec 2|Out-Null;exit 0}catch{exit 1}" >nul 2>nul
if not errorlevel 1 (
    echo Router is already running - opening the dashboard.
    start "" "%BASE_URL%/ui"
    timeout /t 3 /nobreak >nul
    exit /b 0
)

REM Ensure the planner backend (Docker Model Runner) is enabled. Non-fatal:
REM if Docker isn't running, the non-planner lanes still serve.
docker desktop enable model-runner >nul 2>nul

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: virtual environment not found.
    echo Run SETUP.bat once, then run this file again.
    pause
    exit /b 1
)

echo.
echo =====================================================
echo  Local Model Router  ^|  %BASE_URL%
echo =====================================================
if defined A0_LMM_ROUTER_API_KEY (
    echo  Auth      : Bearer token required [from .env]
) else (
    echo  Auth      : none (dev mode)
)
echo  Dashboard : %BASE_URL%/ui
echo  Health    : %BASE_URL%/health
echo.
echo Keep this window open. Press Ctrl+C to stop.
echo =====================================================
echo.

start "" /min powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest '%BASE_URL%/health' -UseBasicParsing -TimeoutSec 2|Out-Null;Start-Process '%BASE_URL%/ui';break}catch{Start-Sleep 1}}"

".venv\Scripts\python.exe" -m local_model_router serve

echo.
echo Router stopped.
pause
