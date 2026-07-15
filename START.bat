@echo off
setlocal
cd /d "%~dp0"
title Imperium - Local Model Router

if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" if not defined %%A set "%%A=%%B"
    )
)

if not defined IMPERIUM_HOME set "IMPERIUM_HOME=%LOCALAPPDATA%\Imperium"
if exist "%IMPERIUM_HOME%\.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%IMPERIUM_HOME%\.env") do (
        if not "%%A"=="" if not defined %%A set "%%A=%%B"
    )
)
if not defined A0_LMM_ROUTER_CONFIG (
    if exist "conf\llama_cpp_servers.yaml" (
        set "A0_LMM_ROUTER_CONFIG=%CD%\conf\llama_cpp_servers.yaml"
    ) else (
        set "A0_LMM_ROUTER_CONFIG=%IMPERIUM_HOME%\conf\llama_cpp_servers.yaml"
    )
)
if not defined OBSERVER_HOST set "OBSERVER_HOST=127.0.0.1"
if not defined OBSERVER_PORT set "OBSERVER_PORT=9000"
set "BASE_URL=http://%OBSERVER_HOST%:%OBSERVER_PORT%"
if not defined A0_LMM_ROUTER_AGENT_BASE_URL set "A0_LMM_ROUTER_AGENT_BASE_URL=%BASE_URL%/v1"

set "PYTHON=.venv\Scripts\python.exe"
if exist "runtime\python\python.exe" set "PYTHON=runtime\python\python.exe"
if not exist "%PYTHON%" (
    echo Imperium runtime is missing. Run SETUP.bat or install a release package.
    pause
    exit /b 1
)

powershell -NoProfile -Command "try{$health=Invoke-RestMethod '%BASE_URL%/health' -TimeoutSec 2;if($health.service -eq 'lmm-router-observer'){exit 0};exit 1}catch{exit 1}" >nul 2>nul
if not errorlevel 1 (
    echo Imperium is already running at %BASE_URL%.
    echo Run STOP.bat before START.bat to load updated code or configuration.
    start "" "%BASE_URL%/ui"
    exit /b 0
)

if not exist "%A0_LMM_ROUTER_CONFIG%" (
    echo Opening first-run setup. Docker is not required.
    "%PYTHON%" -m local_model_router setup
    if errorlevel 1 exit /b 1
    exit /b 0
)

if exist "%IMPERIUM_HOME%\state\installation-manifest.json" (
    "%PYTHON%" -m local_model_router setup --start-runtime >nul 2>nul
)

start "" /min powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest '%BASE_URL%/health' -UseBasicParsing -TimeoutSec 2|Out-Null;Start-Process '%BASE_URL%/ui';break}catch{Start-Sleep 1}}"
echo Imperium is starting at %BASE_URL%/ui
echo Keep this window open. Press Ctrl+C to stop the router.
"%PYTHON%" -m local_model_router serve
