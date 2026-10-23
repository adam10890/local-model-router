@echo off
setlocal
cd /d "%~dp0"
title Stop Imperium

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
if not defined OBSERVER_PORT set "OBSERVER_PORT=9000"
set "PYTHON=.venv\Scripts\python.exe"
if exist "runtime\python\python.exe" set "PYTHON=runtime\python\python.exe"

if not exist "%PYTHON%" (
    echo Imperium runtime is missing; the verified shutdown command is unavailable.
    exit /b 1
)

"%PYTHON%" -m local_model_router shutdown
if errorlevel 1 (
    echo Imperium could not verify ownership or drain active requests. It was left running.
    exit /b 1
)

"%PYTHON%" -m local_model_router setup --stop-runtime >nul 2>nul
echo Imperium stopped after draining requests and verifying process ownership.
