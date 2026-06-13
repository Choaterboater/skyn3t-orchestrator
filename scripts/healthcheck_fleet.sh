#!/usr/bin/env bash
# Fleet health probe — exit non-zero when /api/fleet/status is unreachable or reports failure.
set -euo pipefail

HOST="${SKYN3T_HEALTH_HOST:-127.0.0.1}"
PORT="${SKYN3T_HEALTH_PORT:-6660}"
TOKEN="${SKYN3T_WEB_TOKEN:-}"
URL="http://${HOST}:${PORT}/api/fleet/status"

CURL_OPTS=(-s -S --max-time 8)
if [[ -n "$TOKEN" ]]; then
  CURL_OPTS+=(-H "Authorization: Bearer ${TOKEN}")
fi

body="$(curl "${CURL_OPTS[@]}" "$URL" || true)"
if [[ -z "$body" ]]; then
  echo "fleet status unreachable at $URL" >&2
  exit 1
fi

python3 - <<'PY' "$body"
import json, sys
raw = sys.argv[1]
try:
    data = json.loads(raw)
except Exception:
    sys.exit(1)
if isinstance(data, dict) and data.get("error"):
    sys.exit(1)
sys.exit(0)
PY
