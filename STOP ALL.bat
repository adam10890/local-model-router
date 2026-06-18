@echo off
setlocal
title Stop Local Model Router
cd /d "%~dp0"

echo Stopping the Local Model Router stack...

REM START ROUTER.bat + START IGPU WORKER.bat both title their window
REM "Local Model Router*". /T takes their children (python serve, llama-server).
taskkill /F /T /FI "WINDOWTITLE eq Local Model Router*" >nul 2>nul

REM ponytail: console-title matching is flaky, so also free the known ports.
REM Router :9000 (serve + MCP :8095 live inside it) and the iGPU worker :8089.
for %%P in (9000 8089) do (
  for /f "tokens=5" %%I in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":%%P"') do (
    taskkill /F /PID %%I >nul 2>nul
  )
)

echo Done. (Docker chat-slot :8080 and Hermes run separately - not touched.)
timeout /t 2 /nobreak >nul
