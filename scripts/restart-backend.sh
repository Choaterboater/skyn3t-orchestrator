#!/usr/bin/env bash
# Restart the skyn3t orchestrator backend on :6660.
#
# Why this script exists:
#   - SIGTERM on this app sometimes hangs in graceful shutdown waiting
#     for background tasks. We send SIGKILL after a short grace period.
#   - The installed `skyn3t` binary can lag behind the source tree, so
#     /api/memory/skills and /api/memory/build_patterns 404. We
#     `pip install -e .` to keep them in sync.
#
# Usage:
#   bash scripts/restart-backend.sh           # restart
#   bash scripts/restart-backend.sh --no-pip  # skip the editable install

set -euo pipefail

PORT="${SKYN3T_PORT:-6660}"
HOST="${SKYN3T_HOST:-127.0.0.1}"
LOG="${SKYN3T_LOG:-logs/skyn3t-server.log}"
DO_PIP=1

for arg in "$@"; do
    case "$arg" in
        --no-pip) DO_PIP=0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# Run from the repo root regardless of where the script was invoked.
cd "$(dirname "$0")/.."
mkdir -p "$(dirname "$LOG")"

echo "[restart] looking for skyn3t processes on :$PORT…"
PIDS=$(pgrep -f "skyn3t start.*--port $PORT" || true)

if [[ -n "$PIDS" ]]; then
    echo "[restart] sending SIGTERM to: $PIDS"
    # shellcheck disable=SC2086
    kill $PIDS 2>/dev/null || true
    # Give it 3s to drain background tasks.
    for _ in 1 2 3; do
        sleep 1
        STILL=$(pgrep -f "skyn3t start.*--port $PORT" || true)
        [[ -z "$STILL" ]] && break
    done
    STILL=$(pgrep -f "skyn3t start.*--port $PORT" || true)
    if [[ -n "$STILL" ]]; then
        echo "[restart] hung in shutdown; SIGKILL: $STILL"
        # shellcheck disable=SC2086
        kill -9 $STILL 2>/dev/null || true
        sleep 1
    fi
else
    echo "[restart] none found"
fi

# Sanity-check the port is actually free before re-binding.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[restart] port $PORT is still bound by something else; aborting" >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
    exit 1
fi

if [[ "$DO_PIP" -eq 1 ]]; then
    echo "[restart] pip install -e . (so source routes are live)…"
    pip install -e . > /tmp/skyn3t-install.log 2>&1 || {
        echo "[restart] pip install failed; see /tmp/skyn3t-install.log" >&2
        tail -20 /tmp/skyn3t-install.log >&2
        exit 1
    }
fi

echo "[restart] starting skyn3t on $HOST:$PORT (log → $LOG)"
nohup skyn3t start --host "$HOST" --port "$PORT" > "$LOG" 2>&1 &
NEW_PID=$!
disown "$NEW_PID" 2>/dev/null || true
echo "[restart] launched pid=$NEW_PID"

# Wait up to ~12s for the port to come up.
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sleep 1
    if curl -sf "http://$HOST:$PORT/api/status" >/dev/null 2>&1; then
        echo "[restart] ✓ backend up after ${i}s"
        # Confirm the new memory routes are live.
        SKILLS=$(curl -s -o /dev/null -w "%{http_code}" "http://$HOST:$PORT/api/memory/skills" || echo "ERR")
        BUILD=$(curl -s -o /dev/null -w "%{http_code}" "http://$HOST:$PORT/api/memory/build_patterns" || echo "ERR")
        echo "[restart] /api/memory/skills → $SKILLS"
        echo "[restart] /api/memory/build_patterns → $BUILD"
        exit 0
    fi
done

echo "[restart] backend did not respond within 12s; tail of log:" >&2
tail -30 "$LOG" >&2
exit 1
