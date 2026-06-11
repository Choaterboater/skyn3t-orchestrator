#!/usr/bin/env bash
# =============================================================================
# Studio proof-run — validate a completed project scaffold builds locally.
# =============================================================================
# Usage:
#   ./scripts/studio_smoke.sh <project-slug>
#
# Reads PROJECTS_DIR from .env (default: ./projects). Runs the same class of
# checks BuildVerifier uses for Node/Python scaffolds: npm install + build,
# or python -m py_compile.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <project-slug>" >&2
    exit 1
fi

SLUG="$1"

# Load operator paths from .env when present.
if [[ -f "${PROJECT_DIR}/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    # shellcheck source=/dev/null
    source "${PROJECT_DIR}/.env" 2>/dev/null || true
    set +a
fi

PROJECTS_DIR="${PROJECTS_DIR:-${PROJECT_DIR}/projects}"
PROJECTS_DIR="${PROJECTS_DIR/#\~/${HOME}}"

ARTIFACT_DIR="${PROJECTS_DIR}/${SLUG}"
SCAFFOLD_DIR="${ARTIFACT_DIR}/scaffold"

if [[ ! -d "${SCAFFOLD_DIR}" ]]; then
    echo "error: scaffold not found at ${SCAFFOLD_DIR}" >&2
    echo "hint: check slug and PROJECTS_DIR (currently ${PROJECTS_DIR})" >&2
    exit 1
fi

echo "Studio smoke: ${SLUG}"
echo "  artifact: ${ARTIFACT_DIR}"
echo "  scaffold: ${SCAFFOLD_DIR}"

cd "${SCAFFOLD_DIR}"

if [[ -f package.json ]]; then
    if ! command -v npm >/dev/null 2>&1; then
        echo "error: npm not on PATH — cannot verify node scaffold" >&2
        exit 1
    fi
    echo "→ npm install"
    npm install --no-audit --no-fund --silent --prefer-offline
    if python3 - <<'PY'
import json
from pathlib import Path
pkg = json.loads(Path("package.json").read_text(encoding="utf-8"))
scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
raise SystemExit(0 if isinstance(scripts, dict) and scripts.get("build") else 1)
PY
    then
        echo "→ npm run build"
        npm run build --silent
    else
        echo "→ no build script in package.json (install-only pass)"
    fi
    echo "✓ node scaffold OK"
    exit 0
fi

mapfile -t PY_FILES < <(find . -name '*.py' ! -path '*/__pycache__/*' -type f 2>/dev/null || true)
if [[ ${#PY_FILES[@]} -gt 0 ]]; then
    echo "→ python -m py_compile (${#PY_FILES[@]} file(s))"
    python3 -m py_compile "${PY_FILES[@]}"
    echo "✓ python scaffold OK"
    exit 0
fi

echo "error: no package.json or .py files in scaffold — unknown stack" >&2
exit 1
