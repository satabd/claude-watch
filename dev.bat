@echo off
REM Dev mode: runs backend + Vite dev server in two windows for fast iteration.
setlocal
cd /d "%~dp0"

start "claude-watcher backend" cmd /k ".venv\Scripts\python.exe -m uvicorn server.main:app --reload --port 8765 --log-level info"
timeout /t 2 /nobreak > nul
start "claude-watcher web" cmd /k "cd web && npm run dev"

echo Both started. Open http://localhost:5174 (proxies API/SSE to :8765).
