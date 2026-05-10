#!/usr/bin/env bash
# restore-snapshot.sh — Extract a SkyN3t snapshot tarball into the current dir.
#
# Usage: ./restore-snapshot.sh /path/to/skyn3t-snapshot-*.tar.gz

set -euo pipefail

if [[ -t 1 ]]; then
    CYAN=$'\033[0;36m'; GREEN=$'\033[0;32m'; RED=$'\033[0;31m'
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
    CYAN= GREEN= RED= BOLD= DIM= RESET=
fi

if [[ $# -ne 1 ]]; then
    echo "${RED}usage:${RESET} $0 /path/to/skyn3t-snapshot-*.tar.gz" >&2
    exit 2
fi

TARBALL="$1"
if [[ ! -f "$TARBALL" ]]; then
    echo "${RED}error:${RESET} tarball not found: $TARBALL" >&2
    exit 1
fi

DEST="$(pwd)"
echo "${BOLD}Restoring SkyN3t snapshot${RESET}"
echo "${DIM}  tarball: $TARBALL${RESET}"
echo "${DIM}  dest:    $DEST${RESET}"
echo

# Strip the leading "<project>/" so files land directly in DEST.
tar -xzf "$TARBALL" -C "$DEST" --strip-components=1

echo "${GREEN}Done.${RESET}"
echo
echo "Next steps:"
echo "  ${CYAN}cd $DEST${RESET}"
echo "  ${CYAN}./scripts/setup-new-machine.sh${RESET}"
echo "  ${CYAN}# then log in to each CLI: claude login, gh auth login, kimi${RESET}"
echo
exit 0
