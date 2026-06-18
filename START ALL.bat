@echo off
setlocal
cd /d "%~dp0"
title Start Local Model Router

echo Starting the Local Model Router stack...

REM Docker Model Runner serves the planner (VibeThinker). Bring it up FIRST
REM (blocks until ready) so the router's planner lane has its backend.
call "%~dp0START DMR.bat"

REM Router: serve + dashboard + MCP. Self-checks; if already up it just
REM opens the dashboard.
start "" "%~dp0START ROUTER.bat"

REM iGPU background worker (utility lane on the AMD Radeon via Vulkan).
REM Delete this line if you don't run the iGPU worker.
start "" "%~dp0START IGPU WORKER.bat"

echo Launched. Each runs in its own window; close them or run "STOP ALL.bat".
timeout /t 2 /nobreak >nul
