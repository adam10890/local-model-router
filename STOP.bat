@echo off
setlocal
title Stop Local Model Router
cd /d "%~dp0"

echo Stopping the Local Model Router stack...

REM START.bat titles its windows "Local Model Router*" (router + iGPU worker).
REM /T takes their children (python serve, llama-server).
taskkill /F /T /FI "WINDOWTITLE eq Local Model Router*" >nul 2>nul

REM Belt-and-suspenders: free the known ports. Router :9000, iGPU worker :8089.
for %%P in (9000 8089) do (
  for /f "tokens=5" %%I in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":%%P"') do (
    taskkill /F /PID %%I >nul 2>nul
  )
)

echo Done. (Docker Model Runner is a service - it idle-unloads; Hermes runs separately.)
timeout /t 2 /nobreak >nul
