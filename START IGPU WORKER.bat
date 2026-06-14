@echo off
title iGPU Worker (Radeon - Vulkan)
cd /d "%~dp0"

:: ── Which Vulkan device to use ─────────────────────────────────────────────
:: Check with:  bin\llama-vulkan\llama-server.exe --list-devices
:: 1 = AMD Radeon(TM) Graphics (integrated GPU)
:: 0 = RTX 4090 — leave it for the main fleet!
set GGML_VK_VISIBLE_DEVICES=1

:: ── What this worker serves (scribe model by default) ─────────────────────
set "MODEL=C:\Users\frant\A0-Data-Permanent\A0_v.adam\models\gemma-4-E4B-it-OBLITERATED-Q5_K_M.gguf"
set PORT=8089
set CTX=32768

if not exist "%MODEL%" (
    echo ERROR: model not found:
    echo   %MODEL%
    echo Edit the MODEL line in this file.
    pause
    exit /b 1
)

if not exist "bin\llama-vulkan\llama-server.exe" (
    echo ERROR: llama-server.exe not found under bin\llama-vulkan\
    pause
    exit /b 1
)

echo.
echo  =====================================================
echo   iGPU Worker  ^|  http://127.0.0.1:%PORT%
echo   Device : AMD Radeon integrated GPU ^(Vulkan^)
echo   Model  : gemma-4-E4B ^(scribe^)
echo   Ctx    : %CTX% total, 2 parallel sequences
echo.
echo   Background worker for the scribe role. Slower than
echo   the RTX 4090, but keeps it free for chat models.
echo   First load takes ~30-60s. Keep this window open.
echo  =====================================================
echo.

:: This finetune's embedded jinja template is unparseable and crashes the
:: output parser. --no-jinja --chat-template gemma uses the legacy gemma
:: formatter instead (llama.cpp's own recommended workaround).
bin\llama-vulkan\llama-server.exe -m "%MODEL%" --host 127.0.0.1 --port %PORT% -ngl 99 -c %CTX% -np 2 --alias scribe --no-jinja --chat-template gemma

echo.
echo Worker stopped.
pause
