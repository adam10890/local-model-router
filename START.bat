@echo off
setlocal
cd /d "%~dp0"
title Local Model Router

REM Load .env (KEY=VALUE lines; # comments ignored)
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" set "%%A=%%B"
    )
)
if not defined OBSERVER_HOST set "OBSERVER_HOST=127.0.0.1"
if not defined OBSERVER_PORT set "OBSERVER_PORT=9000"
set "BASE_URL=http://%OBSERVER_HOST%:%OBSERVER_PORT%"

REM Already running? Just open the dashboard and exit.
powershell -NoProfile -Command "try{Invoke-WebRequest '%BASE_URL%/health' -UseBasicParsing -TimeoutSec 2|Out-Null;exit 0}catch{exit 1}" >nul 2>nul
if not errorlevel 1 (
    echo Router already running - opening the dashboard.
    start "" "%BASE_URL%/ui"
    timeout /t 3 /nobreak >nul
    exit /b 0
)

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: run SETUP.bat once first.
    pause
    exit /b 1
)

REM 1) Docker Model Runner - primary chat (Ornith) + planner (VibeThinker).
echo [1/3] Docker Model Runner...
set "DMR_CHAT_MODEL=hf.co/deepreinforce-ai/Ornith-1.0-9B-GGUF:Q8_0"
set "DMR_PLANNER_MODEL=hf.co/mradermacher/VibeThinker-3B-GGUF:Q8_0"
docker info >nul 2>nul
if errorlevel 1 (
    echo   Docker Desktop is not running - starting it...
    docker desktop start >nul 2>nul
    powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){docker info *> $null;if($LASTEXITCODE -eq 0){exit 0};Start-Sleep 2};exit 1"
)
if errorlevel 1 (
    echo   WARNING: Docker Desktop unavailable - skipping Docker Model Runner.
) else (
    docker desktop enable model-runner >nul 2>nul
    docker model pull "%DMR_CHAT_MODEL%"
    docker model pull "%DMR_PLANNER_MODEL%"
    ".venv\Scripts\python.exe" -c "import urllib.request,json; d=json.dumps({'model':'huggingface.co/deepreinforce-ai/ornith-1.0-9b-gguf:Q8_0','messages':[{'role':'user','content':'hi'}],'max_tokens':1}).encode(); urllib.request.urlopen(urllib.request.Request('http://localhost:12434/engines/v1/chat/completions',data=d,headers={'Content-Type':'application/json'}),timeout=120).read()" >nul 2>nul
)

REM 2) iGPU worker (utility lane on the AMD Radeon via Vulkan), own window.
echo [2/3] iGPU worker...
set "IGPU_SERVER=bin\llama-vulkan\llama-server.exe"
if not defined IGPU_MODEL set "IGPU_MODEL=C:\Users\frant\A0-Data-Permanent\A0_v.adam\models\gemma-4-E4B-it-OBLITERATED-Q5_K_M.gguf"
if exist "%IGPU_SERVER%" (
    if exist "%IGPU_MODEL%" (
        set "GGML_VK_VISIBLE_DEVICES=1"
        start "Local Model Router - iGPU Worker" "%IGPU_SERVER%" -m "%IGPU_MODEL%" --host 127.0.0.1 --port 8089 -ngl 99 -c 32768 -np 1 --alias utility_cpu --no-jinja --chat-template gemma
    ) else ( echo   skipped: iGPU model not found )
) else ( echo   skipped: bin\llama-vulkan not found )

REM 3) Router: serve + dashboard + MCP. Opens the dashboard once healthy.
echo [3/3] Router  ^|  %BASE_URL%/ui
start "" /min powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest '%BASE_URL%/health' -UseBasicParsing -TimeoutSec 2|Out-Null;Start-Process '%BASE_URL%/ui';break}catch{Start-Sleep 1}}"
echo.
echo Keep this window open. Ctrl+C stops the router; run STOP.bat to stop everything.
echo.
".venv\Scripts\python.exe" -m local_model_router serve
echo.
echo Router stopped.
pause
