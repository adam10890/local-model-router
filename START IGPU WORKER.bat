@echo off
setlocal
title Local Model Router - iGPU Worker
cd /d "%~dp0"

if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" set "%%A=%%B"
    )
)

if not defined IGPU_VK_DEVICE set "IGPU_VK_DEVICE=1"
if not defined IGPU_PORT set "IGPU_PORT=8089"
if not defined IGPU_CTX set "IGPU_CTX=32768"
if not defined IGPU_PARALLEL set "IGPU_PARALLEL=1"
if not defined IGPU_ALIAS set "IGPU_ALIAS=utility_cpu"
if not defined IGPU_SERVER set "IGPU_SERVER=bin\llama-vulkan\llama-server.exe"
if not defined IGPU_MODEL set "IGPU_MODEL=C:\Users\frant\A0-Data-Permanent\A0_v.adam\models\gemma-4-E4B-it-OBLITERATED-Q5_K_M.gguf"

if not exist "%IGPU_SERVER%" (
    echo ERROR: llama-server not found:
    echo   %IGPU_SERVER%
    pause
    exit /b 1
)

if not exist "%IGPU_MODEL%" (
    echo ERROR: model not found:
    echo   %IGPU_MODEL%
    echo Set IGPU_MODEL in .env or edit this file.
    pause
    exit /b 1
)

set "GGML_VK_VISIBLE_DEVICES=%IGPU_VK_DEVICE%"

echo.
echo =====================================================
echo  iGPU Worker  ^|  http://127.0.0.1:%IGPU_PORT%
echo =====================================================
echo  Vulkan device : %IGPU_VK_DEVICE%
echo  Alias         : %IGPU_ALIAS%
echo  Context       : %IGPU_CTX%
echo  Parallel      : %IGPU_PARALLEL%
echo  Model         : %IGPU_MODEL%
echo.
echo Keep this window open. Press Ctrl+C to stop.
echo =====================================================
echo.

"%IGPU_SERVER%" -m "%IGPU_MODEL%" --host 127.0.0.1 --port %IGPU_PORT% -ngl 99 -c %IGPU_CTX% -np %IGPU_PARALLEL% --alias %IGPU_ALIAS% --no-jinja --chat-template gemma

echo.
echo Worker stopped.
pause
