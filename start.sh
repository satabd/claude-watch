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

# Bind only to the Wi-Fi interface so the server is reachable over Wi-Fi
# (192.168.0.*) but NOT over Ethernet. Override the device with
# WIFI_DEV=enX if your Wi-Fi adapter differs (see: networksetup
# -listallhardwareports). Falls back to 0.0.0.0 (all interfaces) if the
# Wi-Fi IP can't be resolved.
WIFI_DEV="${WIFI_DEV:-en1}"
BIND_HOST=$(ipconfig getifaddr "$WIFI_DEV" 2>/dev/null)
if [ -z "$BIND_HOST" ]; then
  echo "WARNING: could not resolve Wi-Fi IP on $WIFI_DEV; binding all interfaces (0.0.0.0)."
  BIND_HOST=0.0.0.0
fi

cat <<EOF

================================================================
 Claude Watcher running (Wi-Fi only, $WIFI_DEV):
   http://${BIND_HOST}:8765
 Reach it from this Mac at the same URL (not localhost).
 Press Ctrl-C to stop.
================================================================

EOF

exec "$PY" -m uvicorn server.main:app --host "$BIND_HOST" --port 8765 --log-level info
