#!/usr/bin/env bash
# SkyN3t Cursor improvement helper — next queued task + smoke checks.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${SKYN3T_PORT:-6660}"
BASE="http://127.0.0.1:${PORT}"
TASKS_FILE="${DATA_DIR:-data}/cursor_tasks.json"

echo "=== SkyN3t Cursor improvement smoke ==="
echo "Repo: $ROOT"
echo ""

# --- Next cursor task ---
if [[ -f "$TASKS_FILE" ]]; then
  echo "--- Next task (data/cursor_tasks.json) ---"
  python3 - <<'PY' "$TASKS_FILE"
import json, sys
path = sys.argv[1]
try:
    data = json.loads(open(path, encoding="utf-8").read())
    tasks = sorted(
        data.get("tasks") or [],
        key=lambda t: (-int(t.get("priority") or 0), float(t.get("created_at") or 0)),
    )
    if not tasks:
        print("(queue empty)")
    else:
        t = tasks[0]
        print(f"priority={t.get('priority')} source={t.get('source')}")
        print(f"brief: {t.get('brief', '')[:500]}")
        files = t.get("files") or []
        if files:
            print("files:", ", ".join(files[:8]))
except Exception as exc:
    print(f"(could not read tasks: {exc})")
PY
  echo ""
else
  echo "--- No cursor_tasks.json yet ($TASKS_FILE) ---"
  echo ""
fi

# --- Fleet / improvement APIs ---
echo "--- Fleet status ---"
if curl -sf --max-time 3 "${BASE}/api/fleet/status" 2>/dev/null | python3 -m json.tool 2>/dev/null | head -30; then
  :
else
  echo "(fleet API unreachable — start: ./scripts/run.sh web)"
fi
echo ""

echo "--- Improvement status ---"
if curl -sf --max-time 3 "${BASE}/api/improvement/status" 2>/dev/null | python3 -m json.tool 2>/dev/null | head -20; then
  :
else
  echo "(improvement API unreachable)"
fi
echo ""

# --- Targeted pytest ---
echo "--- Pytest (cursor/fleet subset) ---"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null; then
  PY=python3
else
  echo "python not found"
  exit 1
fi

if [[ -x "$ROOT/.venv/bin/pytest" ]]; then
  PYTEST="$ROOT/.venv/bin/pytest"
else
  PYTEST="$PY -m pytest"
fi

$PYTEST tests/test_agent_fleet.py tests/test_continuous_improvement.py \
  tests/test_cheap_smart.py tests/test_model_evolution.py -q \
  --ignore=tests/test_observability.py 2>&1 | tail -5

echo ""
echo "Done. In Cursor chat: Process cursor_tasks.json"
