#!/usr/bin/env bash
# =============================================================================
# SkyN3t Never-Stop Process Wrapper
# =============================================================================
# Restarts the web server if port 6660 stops listening. Simple loop — not
# systemd. Use when you want the dashboard + autonomy stack to survive crashes.
#
# Usage:
#   ./scripts/never_stop.sh
#   WEB_PORT=6660 ./scripts/never_stop.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="${PROJECT_DIR}/.venv"
WEB_PORT="${WEB_PORT:-6660}"
CHECK_INTERVAL="${NEVER_STOP_CHECK_SECONDS:-30}"

if [[ -d "$VENV_DIR" ]]; then
    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"
fi

export PYTHONPATH="${PROJECT_DIR}:${PYTHONPATH:-}"
cd "$PROJECT_DIR"

port_listening() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"${WEB_PORT}" -sTCP:LISTEN -P -n >/dev/null 2>&1
        return $?
    fi
    if command -v nc >/dev/null 2>&1; then
        nc -z localhost "${WEB_PORT}" >/dev/null 2>&1
        return $?
    fi
    # Fallback: curl health (may fail if server is up but route missing)
    curl -sf "http://127.0.0.1:${WEB_PORT}/api/health" >/dev/null 2>&1
}

echo "♾️  SkyN3t never-stop wrapper — port ${WEB_PORT}, check every ${CHECK_INTERVAL}s"
echo "   Press Ctrl+C to stop."

while true; do
    if ! port_listening; then
        echo "[$(date -Iseconds)] port ${WEB_PORT} down — restarting web server..."
        # Kill stale listeners on our port (best-effort)
        if command -v lsof >/dev/null 2>&1; then
            PIDS=$(lsof -tiTCP:"${WEB_PORT}" -sTCP:LISTEN 2>/dev/null || true)
            if [[ -n "${PIDS}" ]]; then
                # shellcheck disable=SC2086
                kill -9 ${PIDS} 2>/dev/null || true
            fi
        fi
        ./scripts/run.sh web &
        SERVER_PID=$!
        echo "[$(date -Iseconds)] started pid=${SERVER_PID}"
        sleep 5
    fi
    sleep "${CHECK_INTERVAL}"
done
