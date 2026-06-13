#!/usr/bin/env bash
# Weekly domain benchmark loop — compare a candidate against golden baselines.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env 2>/dev/null || true
  set +a
fi

STAMP="$(date +%Y%m%d)"
OUT="data/domain_benchmark_${STAMP}.json"
mkdir -p data

source .venv/bin/activate 2>/dev/null || true

CANDIDATE="${1:-aruba-central-field-triage}"
PROJECTS_DIR="${PROJECTS_DIR:-./projects}"
PROJECTS_DIR="${PROJECTS_DIR/#\~/${HOME}}"

python3 - <<'PY' "$CANDIDATE" "$PROJECTS_DIR" "$OUT"
import json
import sys
from pathlib import Path

slug, projects_dir, out_path = sys.argv[1:4]
artifact = Path(projects_dir) / slug
manifest_path = artifact / "project.json"
row = {
    "slug": slug,
    "artifact_dir": str(artifact),
    "manifest_present": manifest_path.exists(),
}
if manifest_path.exists():
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    row["status"] = manifest.get("status")
    row["score"] = (manifest.get("quality_summary") or {}).get("score")
    row["build_verdict"] = (manifest.get("build_verification") or {}).get("verdict")
    row["domain_benchmark"] = manifest.get("domain_benchmark")
out = Path(out_path)
out.write_text(json.dumps(row, indent=2), encoding="utf-8")
print(f"Wrote {out}")
PY

echo "✓ domain benchmark recorded at ${OUT}"
