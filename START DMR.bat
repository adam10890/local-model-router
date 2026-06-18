@echo off
setlocal
title Local Model Router - DMR
set "DMR_MODEL=hf.co/mradermacher/VibeThinker-3B-GGUF:Q8_0"

echo Enabling Docker Model Runner...
docker desktop enable model-runner >nul 2>nul

echo Ensuring planner model is present: %DMR_MODEL%
docker model pull "%DMR_MODEL%"

echo Warming the model into memory...
".venv\Scripts\python.exe" -c "import urllib.request,json; d=json.dumps({'model':'%DMR_MODEL%','messages':[{'role':'user','content':'hi'}],'max_tokens':1}).encode(); urllib.request.urlopen(urllib.request.Request('http://localhost:12434/engines/v1/chat/completions',data=d,headers={'Content-Type':'application/json'}),timeout=120).read()" >nul 2>nul

curl -s -m 5 http://localhost:12434/engines/v1/models >nul 2>nul
if errorlevel 1 (
  echo WARNING: DMR endpoint not answering on :12434 - is Docker Desktop running?
) else (
  echo DMR ready: planner reachable at http://localhost:12434/engines/v1
)

timeout /t 2 /nobreak >nul
