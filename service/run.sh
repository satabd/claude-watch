#!/usr/bin/env bash
# Launcher for the Claude Watcher LaunchAgent (see
# service/com.claudewatcher.server.plist). Binds ONLY to the Wi-Fi
# interface so the server is reachable over Wi-Fi (192.168.0.*) and NOT
# over Ethernet. If the Wi-Fi IP isn't available yet — e.g. right after
# boot, before Wi-Fi associates — it exits non-zero so launchd retries,
# rather than falling back to 0.0.0.0 (which would expose Ethernet).
set -e
cd "$(dirname "$0")/.."

WIFI_DEV="${WIFI_DEV:-en1}"
PORT="${PORT:-8765}"

# launchd starts agents with a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin),
# which does NOT include Homebrew. The providers shell out to the `claude` /
# `codex` CLIs, so without this every AI action (translate, prompt writer,
# discuss, review) dies with FileNotFoundError while plain transcript
# viewing keeps working. Prepend the usual CLI install locations.
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$HOME/.local/bin:$HOME/Library/pnpm:$HOME/.cargo/bin:$HOME/bin:$PATH"

for bin in claude codex; do
  resolved=$(command -v "$bin" 2>/dev/null || true)
  echo "$(date '+%Y-%m-%d %H:%M:%S') provider CLI '$bin': ${resolved:-NOT FOUND}"
done

BIND_HOST=$(ipconfig getifaddr "$WIFI_DEV" 2>/dev/null || true)
if [ -z "$BIND_HOST" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') Wi-Fi IP not available on $WIFI_DEV yet; exiting for launchd retry." >&2
  exit 1
fi

PY=.venv/bin/python
echo "$(date '+%Y-%m-%d %H:%M:%S') starting uvicorn on ${BIND_HOST}:${PORT} (Wi-Fi $WIFI_DEV)"
exec "$PY" -m uvicorn server.main:app --host "$BIND_HOST" --port "$PORT" --log-level info
