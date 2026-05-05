@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Setting up Python venv...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\pip install --default-timeout=120 -r server\requirements.txt
)

if not exist "web\node_modules" (
    echo Installing web deps...
    cd web
    call npm install --no-audit --no-fund --loglevel=error
    cd ..
)

if not exist "web\dist\index.html" (
    echo Building web app...
    cd web
    call npm run build
    cd ..
)

echo.
echo ================================================================
echo  Claude Watcher running at http://localhost:8765
echo  Press Ctrl-C to stop.
echo ================================================================
echo.

.venv\Scripts\python.exe -m uvicorn server.main:app --port 8765 --log-level info
