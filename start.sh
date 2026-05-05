#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -f ".venv/Scripts/python.exe" ] && [ ! -f ".venv/bin/python" ]; then
  echo "Setting up Python venv..."
  python -m venv .venv
fi

PY=.venv/Scripts/python.exe
[ -f .venv/bin/python ] && PY=.venv/bin/python

"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet --default-timeout=120 -r server/requirements.txt

if [ ! -d "web/node_modules" ]; then
  echo "Installing web deps..."
  (cd web && npm install --no-audit --no-fund --loglevel=error)
fi

if [ ! -f "web/dist/index.html" ]; then
  echo "Building web app..."
  (cd web && npm run build)
fi

cat <<EOF

================================================================
 Claude Watcher running at http://localhost:8765
 Press Ctrl-C to stop.
================================================================

EOF

exec "$PY" -m uvicorn server.main:app --port 8765 --log-level info
