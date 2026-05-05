#!/bin/bash
# Reliability/safety pass test runner.
#
# DEV/TEST RULE: never write to a real Claude session JSONL.
# All mutations target a dedicated *sandbox bucket* under the user's WSL
# `~/.claude/projects/`. The bucket name is prefixed with `_watcher-test-`
# and an isoseconds timestamp so it sorts last in the sidebar tree and is
# easy to identify and remove. The sandbox bucket is created at the start
# of the run and purged at the end (even on failure).
#
# Each phase prints a single line: "PASS|FAIL  <description>  <detail>"

set +e

# Prevent MINGW path translation from rewriting POSIX paths to C:/Program Files/Git/...
export MSYS_NO_PATHCONV=1

API=http://localhost:8765

# ---- sandbox bucket setup --------------------------------------------------
TS=$(date -u +%Y%m%dT%H%M%S)
SANDBOX_BUCKET="_watcher-test-${TS}"
SANDBOX_REMOTE_DIR="/home/sat/.claude/projects/${SANDBOX_BUCKET}"
SANDBOX_FILE_NAME="sandbox-${TS}.jsonl"
SANDBOX_REMOTE="${SANDBOX_REMOTE_DIR}/${SANDBOX_FILE_NAME}"

# We don't know the exact local mirror path without knowing the remote's
# `name` field, so default to the canonical wsl-ubuntu host path. If the
# user has named their host something else, override SANDBOX_LOCAL.
HOST_NAME=${WATCHER_TEST_HOST_NAME:-wsl-ubuntu}
SANDBOX_LOCAL_DIR="/c/Users/satab/.claude/watcher/remotes/${HOST_NAME}/${SANDBOX_BUCKET}"
SANDBOX_LOCAL="${SANDBOX_LOCAL_DIR}/${SANDBOX_FILE_NAME}"

cleanup() {
  # Remove the sandbox bucket on the remote and locally. Run on EXIT so
  # interrupts don't leave artifacts behind.
  wsl -d Ubuntu -- rm -rf "$SANDBOX_REMOTE_DIR" 2>/dev/null
  rm -rf "$SANDBOX_LOCAL_DIR" 2>/dev/null
}
trap cleanup EXIT

# Find the wsl-ubuntu host id
ID=$(curl -s "$API/api/remotes" | python -c "import json,sys; r=[h for h in json.load(sys.stdin)['items'] if h['name']=='${HOST_NAME}']; print(r[0]['id'] if r else '')")
if [ -z "$ID" ]; then
  echo "FAIL: no '${HOST_NAME}' host configured. Override with WATCHER_TEST_HOST_NAME."
  exit 1
fi
echo "host=${HOST_NAME} (id=$ID), sandbox bucket=${SANDBOX_BUCKET}"

# Create the sandbox bucket on the remote with a one-line seed file. A bucket
# with no JSONL is invisible to the watcher's full scan; we want it indexed.
wsl -d Ubuntu -- mkdir -p "$SANDBOX_REMOTE_DIR"
wsl -d Ubuntu -- bash -c "echo '{\"type\":\"watcher-test-init\",\"stamp\":\"${TS}\"}' > '$SANDBOX_REMOTE'"

# Wait for the watcher's full-scan or incremental loop to discover it. Default
# FULL_SCAN_INTERVAL is 30s; we kick `Sync now` to short-circuit that wait.
curl -s -X POST "$API/api/remotes/$ID/sync" >/dev/null
sleep 2
echo

trigger_append() {
  local marker=$1
  EVENT="{\"type\":\"watcher-test\",\"marker\":\"$marker\",\"stamp\":\"$(date -Iseconds)\"}"
  wsl -d Ubuntu -- bash -c "echo '$EVENT' >> '$SANDBOX_REMOTE'"
}

wait_for_size() {
  local target=$1
  local timeout_ms=${2:-6000}
  local start=$(date +%s%N)
  while :; do
    local cur=$(stat -c %s "$SANDBOX_LOCAL" 2>/dev/null || echo 0)
    local elapsed_ms=$(( ($(date +%s%N) - start) / 1000000 ))
    if [ "$cur" -ge "$target" ]; then
      echo "$elapsed_ms"; return 0
    fi
    if [ "$elapsed_ms" -ge "$timeout_ms" ]; then
      echo "$elapsed_ms"; return 1
    fi
    sleep 0.2
  done
}

# ===== T1: append in WSL → mirror grows automatically (no Sync click)
echo "--- T1: append → live update without manual sync ---"
WSL_BEFORE=$(wsl -d Ubuntu -- stat -c %s "$SANDBOX_REMOTE")
trigger_append "T1"
WSL_AFTER=$(wsl -d Ubuntu -- stat -c %s "$SANDBOX_REMOTE")
ELAPSED=$(wait_for_size "$WSL_AFTER" 6000)
if [ $? -eq 0 ]; then
  echo "PASS  T1  mirror caught up in ${ELAPSED}ms (sandbox $WSL_BEFORE → $WSL_AFTER)"
else
  echo "FAIL  T1  mirror did not catch up within ${ELAPSED}ms"
fi
echo

# ===== T2: disable host → live updates stop
echo "--- T2: disable host stops live updates ---"
curl -s -X PATCH "$API/api/remotes/$ID" -H "Content-Type: application/json" -d '{"enabled":false}' >/dev/null
sleep 3
LOCAL_BEFORE=$(stat -c %s "$SANDBOX_LOCAL" 2>/dev/null || echo 0)
trigger_append "T2"
sleep 4
LOCAL_AFTER=$(stat -c %s "$SANDBOX_LOCAL" 2>/dev/null || echo 0)
if [ "$LOCAL_AFTER" -eq "$LOCAL_BEFORE" ]; then
  echo "PASS  T2  mirror unchanged after append while disabled ($LOCAL_BEFORE bytes)"
else
  echo "FAIL  T2  mirror grew $LOCAL_BEFORE → $LOCAL_AFTER while host was disabled"
fi
STATUS=$(curl -s "$API/api/remotes" | python -c "import json,sys; print([h for h in json.load(sys.stdin)['items'] if h['name']=='${HOST_NAME}'][0].get('status'))")
echo "       host status=$STATUS"
echo

# ===== T3: re-enable host → live updates resume
echo "--- T3: re-enable host resumes live updates ---"
curl -s -X PATCH "$API/api/remotes/$ID" -H "Content-Type: application/json" -d '{"enabled":true}' >/dev/null
sleep 4
LOCAL_BEFORE=$(stat -c %s "$SANDBOX_LOCAL")
trigger_append "T3"
WSL_AFTER=$(wsl -d Ubuntu -- stat -c %s "$SANDBOX_REMOTE")
ELAPSED=$(wait_for_size "$WSL_AFTER" 8000)
if [ $? -eq 0 ]; then
  echo "PASS  T3  resumed; caught up in ${ELAPSED}ms"
else
  echo "FAIL  T3  did not resume; elapsed ${ELAPSED}ms"
fi
echo

# ===== T4: SSH failure does not crash the server
echo "--- T4: bad host config does not crash server ---"
BADID=$(curl -s -X POST "$API/api/remotes" -H "Content-Type: application/json" \
  -d '{"name":"_watcher-test-bad","host":"127.0.0.1","port":1,"username":"nobody"}' \
  | python -c "import json,sys; print(json.load(sys.stdin).get('id',''))")
sleep 5
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "$API/api/health")
PROJECTS_OK=$(curl -s -o /dev/null -w "%{http_code}" "$API/api/projects")
BAD_STATUS=$(curl -s "$API/api/remotes" | python -c "import json,sys; r=[h for h in json.load(sys.stdin)['items'] if h['name']=='_watcher-test-bad']; print(r[0].get('status') if r else 'missing', '|', r[0].get('next_retry_ms') if r else None)")
if [ "$HEALTH" = "200" ] && [ "$PROJECTS_OK" = "200" ]; then
  echo "PASS  T4  server healthy after SSH failure"
  echo "       bad host status=$BAD_STATUS"
else
  echo "FAIL  T4  health=$HEALTH projects=$PROJECTS_OK"
fi
[ -n "$BADID" ] && curl -s -X DELETE "$API/api/remotes/$BADID" >/dev/null
echo

# ===== T5: delete host cancels watcher and removes mirror
echo "--- T5: delete host removes mirror ---"
NEWID=$(curl -s -X POST "$API/api/remotes" -H "Content-Type: application/json" \
  -d '{"name":"_watcher-test-wsl","host":"127.0.0.1","port":2222,"username":"sat","key_path":"~/.ssh/id_ed25519","projects_path":"/home/sat/.claude/projects"}' \
  | python -c "import json,sys; print(json.load(sys.stdin).get('id',''))")
sleep 8
DIR_BEFORE="/c/Users/satab/.claude/watcher/remotes/_watcher-test-wsl"
EXISTS_BEFORE=$( [ -d "$DIR_BEFORE" ] && echo "yes" || echo "no" )
DEL=$(curl -s -X DELETE "$API/api/remotes/$NEWID")
sleep 2
EXISTS_AFTER=$( [ -d "$DIR_BEFORE" ] && echo "yes" || echo "no" )
echo "       dir before=$EXISTS_BEFORE  after=$EXISTS_AFTER  del-resp=$DEL"
if [ "$EXISTS_BEFORE" = "yes" ] && [ "$EXISTS_AFTER" = "no" ]; then
  echo "PASS  T5  mirror dir created on add, removed on delete"
else
  echo "FAIL  T5  unexpected state (before=$EXISTS_BEFORE after=$EXISTS_AFTER)"
fi
echo

# ===== T6: partial JSONL line does not break parser
# Write to the LOCAL sandbox mirror to test the local tailer's buffering.
echo "--- T6: partial-line write does not break parser ---"
PARTIAL_LINE='{"type":"watcher-test-partial","note":"this line is not closed yet'
echo -n "$PARTIAL_LINE" >> "$SANDBOX_LOCAL"
sleep 2
FULL_LINE='", "completed":true}'$'\n''{"type":"watcher-test-after-partial","stamp":"'$(date -Iseconds)'"}'
echo "$FULL_LINE" >> "$SANDBOX_LOCAL"
sleep 2
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "$API/api/health")
if [ "$HEALTH" = "200" ]; then
  echo "PASS  T6  server healthy after partial-line + completion"
else
  echo "FAIL  T6  server unhealthy: HTTP $HEALTH"
fi
echo

# ===== T7: bucket namespace is always remote:<host>:<bucket>
echo "--- T7: SSE bucket namespace ---"
SSE_OUT=/tmp/sse_test7.txt
> "$SSE_OUT"
timeout 8 curl -s -N "$API/sse/live" > "$SSE_OUT" 2>&1 &
SSE_PID=$!
sleep 1.5
trigger_append "T7"
sleep 4
kill $SSE_PID 2>/dev/null
wait 2>/dev/null
EVENTS_WITH_PREFIX=$(grep -c "\"bucket\": \"remote:${HOST_NAME}:" "$SSE_OUT")
EVENTS_WITHOUT_PREFIX=$(grep -E '"bucket": "[^"]*"' "$SSE_OUT" | grep -cv 'remote:')
if [ "$EVENTS_WITH_PREFIX" -gt 0 ] && [ "$EVENTS_WITHOUT_PREFIX" -eq 0 ]; then
  echo "PASS  T7  $EVENTS_WITH_PREFIX events with remote:<host>: prefix; 0 without"
else
  echo "FAIL  T7  with-prefix=$EVENTS_WITH_PREFIX  without-prefix=$EVENTS_WITHOUT_PREFIX"
fi

echo
echo "Sandbox cleanup runs on exit. To verify nothing leaked:"
echo "  wsl -d Ubuntu -- ls ~/.claude/projects/ | grep '^_watcher-test-' || echo '(clean)'"
