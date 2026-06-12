#!/usr/bin/env bash
# External watchdog probe for SkyN3t.
# Exit 0 if the orchestrator health endpoint is reachable and reports OK.
# Use with systemd/launchd/Docker HEALTHCHECK to restart a dead process.
#
# Example systemd service:
#   [Service]
#   ExecStart=/path/to/.venv/bin/uvicorn skyn3t.web.app:app --host 127.0.0.1 --port 6660
#   Restart=on-failure
#   RestartSec=10
#
# Example launchd KeepAlive via a wrapper plist that runs this script.

set -euo pipefail

HOST="${SKYN3T_HEALTH_HOST:-127.0.0.1}"
PORT="${SKYN3T_HEALTH_PORT:-6660}"
TOKEN="${SKYN3T_WEB_TOKEN:-}"
URL="http://${HOST}:${PORT}/health"

CURL_OPTS=(-s -S --max-time 5 -o /dev/null -w "%{http_code}")
if [[ -n "$TOKEN" ]]; then
  CURL_OPTS+=(-H "Authorization: Bearer ${TOKEN}")
fi

status=$(curl "${CURL_OPTS[@]}" "$URL" || true)

if [[ "$status" == "200" ]]; then
  exit 0
fi

echo "SkyN3t health check failed: HTTP ${status:-unknown} at $URL" >&2
exit 1
