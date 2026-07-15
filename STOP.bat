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

if exist "%PYTHON%" "%PYTHON%" -m local_model_router setup --stop-runtime >nul 2>nul
taskkill /F /T /FI "WINDOWTITLE eq Imperium - Local Model Router" >nul 2>nul
echo Imperium stopped. Other applications and model servers were left untouched.
timeout /t 2 /nobreak >nul
