#!/usr/bin/env bash
# snapshot.sh — Tar the SkyN3t project into a portable archive.
#
# Excludes caches, virtualenvs, large regenerable data, logs, and .env.
# Output: /tmp/skyn3t-snapshot-YYYY-MM-DD-HHMM.tar.gz

set -euo pipefail

if [[ -t 1 ]]; then
    CYAN=$'\033[0;36m'; GREEN=$'\033[0;32m'; DIM=$'\033[2m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
    CYAN= GREEN= DIM= BOLD= RESET=
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_NAME="$(basename "$PROJECT_ROOT")"
PARENT_DIR="$(dirname "$PROJECT_ROOT")"

STAMP="$(date +%Y-%m-%d-%H%M)"
OUT="/tmp/skyn3t-snapshot-${STAMP}.tar.gz"

echo "${BOLD}SkyN3t snapshot${RESET}"
echo "${DIM}  source: $PROJECT_ROOT${RESET}"
echo "${DIM}  output: $OUT${RESET}"
echo

# Run tar from the parent so paths inside the archive are
# "<project>/..." instead of absolute.
tar \
    --exclude="${PROJECT_NAME}/__pycache__" \
    --exclude="${PROJECT_NAME}/**/__pycache__" \
    --exclude="${PROJECT_NAME}/.venv" \
    --exclude="${PROJECT_NAME}/venv" \
    --exclude="${PROJECT_NAME}/node_modules" \
    --exclude="${PROJECT_NAME}/data/vector_db" \
    --exclude="${PROJECT_NAME}/data/embedding_cache" \
    --exclude="${PROJECT_NAME}/logs" \
    --exclude="${PROJECT_NAME}/.pytest_cache" \
    --exclude="${PROJECT_NAME}/.mypy_cache" \
    --exclude="${PROJECT_NAME}/*.egg-info" \
    --exclude="${PROJECT_NAME}/.env" \
    --exclude="*.pyc" \
    --exclude="*.log" \
    --exclude="${PROJECT_NAME}/tmp_*" \
    --exclude="${PROJECT_NAME}/*.tar.gz" \
    -czf "$OUT" \
    -C "$PARENT_DIR" \
    "$PROJECT_NAME"

SIZE="$(du -h "$OUT" | awk '{print $1}')"
echo "${GREEN}Created:${RESET} ${CYAN}$OUT${RESET}  (${SIZE})"
echo
echo "Move it to the new machine, then:"
echo "  ${CYAN}mkdir -p ~/jarvis && cd ~/jarvis${RESET}"
echo "  ${CYAN}/path/to/restore-snapshot.sh $OUT${RESET}"
echo "  ${CYAN}./scripts/setup-new-machine.sh${RESET}"
echo
exit 0
