#!/usr/bin/env bash
# Create or remove a git worktree for parallel CodeAgent isolation.
# Usage:
#   ./scripts/worktree.sh create <scaffold_dir> <track_id>
#   ./scripts/worktree.sh remove <scaffold_dir> <worktree_path>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  echo "Run ./scripts/setup.sh first" >&2
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

ACTION="${1:-}"
SCAFFOLD="${2:-}"
TRACK="${3:-}"

if [[ -z "$ACTION" || -z "$SCAFFOLD" ]]; then
  echo "Usage: $0 create <scaffold_dir> <track_id>" >&2
  echo "       $0 remove <scaffold_dir> <worktree_path>" >&2
  exit 1
fi

python3 - "$ACTION" "$SCAFFOLD" "$TRACK" <<'PY'
import sys
from pathlib import Path

from skyn3t.worktree import ensure_worktree, remove_worktree

action, scaffold, third = sys.argv[1:4]
scaffold_path = Path(scaffold).expanduser().resolve()

if action == "create":
    info = ensure_worktree(scaffold_path, track_id=third or "manual")
    print(f"worktree={info.worktree_path}")
    print(f"branch={info.branch}")
    print(f"created={info.created}")
elif action == "remove":
    ok = remove_worktree(scaffold_path, Path(third).expanduser().resolve(), force=True)
    print("removed" if ok else "remove_failed")
    sys.exit(0 if ok else 1)
else:
    print(f"unknown action: {action}", file=sys.stderr)
    sys.exit(1)
PY
