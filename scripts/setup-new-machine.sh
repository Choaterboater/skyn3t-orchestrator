#!/usr/bin/env bash
# setup-new-machine.sh — One-shot bootstrap for SkyN3t on a fresh machine.
#
# Verifies prerequisites, installs the Python package, initializes data dirs,
# probes external CLIs, and writes a starter .env if none exists.

set -euo pipefail

# ─── colors ─────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    CYAN=$'\033[0;36m'
    GREEN=$'\033[0;32m'
    RED=$'\033[0;31m'
    YELLOW=$'\033[0;33m'
    BOLD=$'\033[1m'
    DIM=$'\033[2m'
    RESET=$'\033[0m'
else
    CYAN= GREEN= RED= YELLOW= BOLD= DIM= RESET=
fi

CHECK="${CYAN}✓${RESET}"
CROSS="${RED}✗${RESET}"
WARN="${YELLOW}!${RESET}"

# ─── locate project root ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo
echo "${BOLD}SkyN3t — new machine setup${RESET}"
echo "${DIM}project: $PROJECT_ROOT${RESET}"
echo

# ─── tracking arrays for the final summary ──────────────────────────────────
OK_ITEMS=()
MISSING_ITEMS=()
WARN_ITEMS=()

ok()      { OK_ITEMS+=("$1");      printf "  %s %s\n" "$CHECK" "$1"; }
missing() { MISSING_ITEMS+=("$1"); printf "  %s %s\n" "$CROSS" "$1"; }
warn()    { WARN_ITEMS+=("$1");    printf "  %s %s\n" "$WARN"  "$1"; }
fatal()   { printf "\n%s FATAL: %s\n" "$CROSS" "$1" >&2; exit 1; }

# ─── 1. python ──────────────────────────────────────────────────────────────
echo "${BOLD}1. Python${RESET}"
if ! command -v python3 >/dev/null 2>&1; then
    fatal "python3 not found on PATH. Install Python 3.10+ (brew install python@3.11 / apt install python3)."
fi
PYVER="$(python3 -c 'import sys; print("{0}.{1}".format(sys.version_info[0], sys.version_info[1]))')"
PYMAJOR="$(python3 -c 'import sys; print(sys.version_info[0])')"
PYMINOR="$(python3 -c 'import sys; print(sys.version_info[1])')"
if [[ "$PYMAJOR" -lt 3 ]] || { [[ "$PYMAJOR" -eq 3 ]] && [[ "$PYMINOR" -lt 10 ]]; }; then
    fatal "Python $PYVER detected; SkyN3t requires Python 3.10+."
fi
ok "python3 $PYVER"

# ─── 2. git ─────────────────────────────────────────────────────────────────
echo
echo "${BOLD}2. Git${RESET}"
if ! command -v git >/dev/null 2>&1; then
    fatal "git not found on PATH. Install git (brew install git / apt install git)."
fi
ok "$(git --version)"

# ─── 3. pip install -e . ────────────────────────────────────────────────────
echo
echo "${BOLD}3. Installing SkyN3t (pip install -e .)${RESET}"
PIP_ARGS=(-e .)
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    echo "${DIM}  using active venv: $VIRTUAL_ENV${RESET}"
    if python3 -m pip install "${PIP_ARGS[@]}"; then
        ok "package installed (venv)"
    else
        fatal "pip install failed"
    fi
elif [[ -d "$PROJECT_ROOT/.venv" ]]; then
    echo "${DIM}  found .venv/, activating${RESET}"
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.venv/bin/activate"
    if python3 -m pip install "${PIP_ARGS[@]}"; then
        ok "package installed (.venv)"
    else
        fatal "pip install failed"
    fi
else
    echo "${DIM}  no venv detected; installing with --user${RESET}"
    if python3 -m pip install --user "${PIP_ARGS[@]}"; then
        ok "package installed (user site)"
    else
        fatal "pip install failed"
    fi
fi

# ─── 4. skyn3t init ─────────────────────────────────────────────────────────
echo
echo "${BOLD}4. skyn3t init${RESET}"
if command -v skyn3t >/dev/null 2>&1; then
    if skyn3t init; then
        ok "data/, logs/, vector DB initialized"
    else
        warn "skyn3t init returned non-zero (continuing)"
    fi
else
    warn "skyn3t entrypoint not on PATH yet — try: python3 -m skyn3t init  (or open a new shell)"
    if python3 -m skyn3t init 2>/dev/null; then
        ok "skyn3t init via python3 -m"
    else
        warn "could not run skyn3t init automatically — run it manually after this script"
    fi
fi

# ─── 5. external CLI probes ─────────────────────────────────────────────────
echo
echo "${BOLD}5. External CLIs${RESET}"

probe_cli() {
    local name="$1" install_hint="$2"
    if command -v "$name" >/dev/null 2>&1; then
        local ver
        ver="$($name --version 2>&1 | head -n1 || echo 'installed')"
        ok "$name — $ver"
        return 0
    else
        missing "$name — not installed.   ${DIM}install: $install_hint${RESET}"
        return 1
    fi
}

probe_cli "claude"   "npm install -g @anthropic-ai/claude-code  (then: claude login)"
probe_cli "kimi"     "see Moonshot Kimi CLI docs                (then: kimi)"
probe_cli "copilot"  "gh extension install github/gh-copilot    (then: gh auth login)"

# ─── 6. .env handling ───────────────────────────────────────────────────────
echo
echo "${BOLD}6. .env${RESET}"
if [[ -f .env ]]; then
    ok ".env already exists (leaving alone)"
else
    if [[ -f .env.example ]]; then
        cp .env.example .env
        ok "copied .env.example → .env"
    else
        warn ".env.example missing — cannot create starter .env"
    fi
fi

# ── ensure SECRET_KEY is populated ──
if [[ -f .env ]]; then
    # match SECRET_KEY= or SECRET_KEY="" (blank value)
    if grep -Eq '^SECRET_KEY=("")?\s*$' .env; then
        NEW_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
        # portable in-place edit (BSD + GNU sed differ)
        TMP="$(mktemp)"
        awk -v key="$NEW_KEY" '
            /^SECRET_KEY=/ { print "SECRET_KEY=" key; next }
            { print }
        ' .env > "$TMP" && mv "$TMP" .env
        ok "generated fresh SECRET_KEY in .env"
    else
        ok "SECRET_KEY already set in .env"
    fi
fi

# ─── final summary ──────────────────────────────────────────────────────────
echo
echo "${BOLD}─── Summary ───────────────────────────────${RESET}"
echo "${BOLD}OK:${RESET}        ${#OK_ITEMS[@]}"
for it in "${OK_ITEMS[@]}";      do printf "  %s %s\n" "$CHECK" "$it"; done
if [[ ${#WARN_ITEMS[@]} -gt 0 ]]; then
    echo
    echo "${BOLD}Warnings:${RESET}  ${#WARN_ITEMS[@]}"
    for it in "${WARN_ITEMS[@]}";    do printf "  %s %s\n" "$WARN"  "$it"; done
fi
if [[ ${#MISSING_ITEMS[@]} -gt 0 ]]; then
    echo
    echo "${BOLD}Missing:${RESET}   ${#MISSING_ITEMS[@]}"
    for it in "${MISSING_ITEMS[@]}"; do printf "  %s %s\n" "$CROSS" "$it"; done
fi
echo
if [[ ${#MISSING_ITEMS[@]} -eq 0 ]]; then
    echo "${GREEN}${BOLD}All set.${RESET} Try: ${CYAN}skyn3t status${RESET} or ${CYAN}pytest tests/${RESET}"
else
    echo "${YELLOW}Bootstrap complete with missing CLIs above.${RESET}"
    echo "Install + log in to each, then re-run this script to verify."
fi
echo
exit 0
