#!/usr/bin/env bash
# =============================================================================
# SkyN3t Run Script
# =============================================================================
# Starts SkyN3t in the requested mode.
# Usage:
#   ./scripts/run.sh [web|cli] [options]
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="${PROJECT_DIR}/.venv"

MODE="${1:-web}"
shift || true

# Activate virtual environment if it exists
if [[ -d "$VENV_DIR" ]]; then
    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"
fi

if [[ -x "$VENV_DIR/bin/python" ]]; then
    PYTHON_BIN="$VENV_DIR/bin/python"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    echo "Python interpreter not found."
    exit 127
fi

# Ensure PYTHONPATH includes the project root
export PYTHONPATH="${PROJECT_DIR}:${PYTHONPATH:-}"

cd "$PROJECT_DIR"

case "$MODE" in
    web)
        echo "🌐 Starting SkyN3t web server..."
        exec "$PYTHON_BIN" -m skyn3t.cli.main start "$@"
        ;;
    cli)
        echo "💬 Starting SkyN3t CLI..."
        exec "$PYTHON_BIN" -m skyn3t.cli.main "$@"
        ;;
    init)
        echo "🔧 Initializing SkyN3t..."
        exec "$PYTHON_BIN" -m skyn3t.cli.main init
        ;;
    *)
        echo "Unknown mode: $MODE"
        echo "Usage: $0 [web|cli|init] [options]"
        exit 1
        ;;
esac
