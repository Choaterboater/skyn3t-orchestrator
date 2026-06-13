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

# Load operator paths from .env when present (PROJECTS_DIR only — avoid
# sourcing the full file because operator .env may invoke missing tools).
if [[ -z "${PROJECTS_DIR:-}" && -f "${PROJECT_DIR}/.env" ]]; then
    _pd_line="$(grep -E '^[[:space:]]*PROJECTS_DIR=' "${PROJECT_DIR}/.env" | tail -1 || true)"
    if [[ -n "${_pd_line}" ]]; then
        PROJECTS_DIR="${_pd_line#*=}"
        PROJECTS_DIR="${PROJECTS_DIR%\"}"
        PROJECTS_DIR="${PROJECTS_DIR#\"}"
        PROJECTS_DIR="${PROJECTS_DIR%\'}"
        PROJECTS_DIR="${PROJECTS_DIR#\'}"
    fi
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

# mapfile is bash 4+; macOS ships bash 3.2 — use find + xargs instead.
PY_COUNT=0
if PY_FILES=$(find . -name '*.py' ! -path '*/__pycache__/*' -type f 2>/dev/null); then
    PY_COUNT=$(printf '%s\n' "$PY_FILES" | sed '/^$/d' | wc -l | tr -d ' ')
fi
if [[ "${PY_COUNT}" -gt 0 ]]; then
    echo "→ python -m py_compile (${PY_COUNT} file(s))"
    printf '%s\n' "$PY_FILES" | sed '/^$/d' | xargs python3 -m py_compile
    echo "✓ python scaffold OK"
    exit 0
fi

echo "error: no package.json or .py files in scaffold — unknown stack" >&2
exit 1
