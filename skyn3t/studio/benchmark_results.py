"""Collect benchmark cohort outcomes from Studio project manifests."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List


def _load_manifest(path: Path) -> Dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def collect_benchmark_results(projects_root: Path | str) -> Dict[str, Any]:
    root = Path(projects_root)
    rows: List[Dict[str, Any]] = []
    if root.exists():
        for manifest_path in root.glob("*/project.json"):
            manifest = _load_manifest(manifest_path)
            if not manifest:
                continue
            case_id = str(manifest.get("benchmark_case") or "").strip()
            if not case_id:
                continue
            quality = manifest.get("quality_summary") if isinstance(manifest.get("quality_summary"), dict) else {}
            scorecard = (
                manifest.get("real_project_scorecard")
                if isinstance(manifest.get("real_project_scorecard"), dict)
                else {}
            )
            rows.append(
                {
                    "slug": manifest.get("slug") or manifest_path.parent.name,
                    "case_id": case_id,
                    "stack": manifest.get("benchmark_stack") or manifest.get("stack") or "",
                    "status": manifest.get("status"),
                    "reviewer_score": quality.get("score"),
                    "scorecard_score": scorecard.get("score"),
                    "scorecard_passed": scorecard.get("passed"),
                    "penalties": scorecard.get("penalties") or {},
                    "build_verdict": (manifest.get("build_verification") or {}).get("verdict")
                    if isinstance(manifest.get("build_verification"), dict)
                    else None,
                    "updated_at": manifest.get("updated_at") or manifest.get("completed_at"),
                }
            )
    rows.sort(key=lambda row: float(row.get("updated_at") or 0.0), reverse=True)

    by_case: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        bucket = by_case.setdefault(
            str(row["case_id"]),
            {"case_id": row["case_id"], "runs": 0, "passes": 0, "latest": None, "avg_score": None},
        )
        bucket["runs"] += 1
        if row.get("scorecard_passed"):
            bucket["passes"] += 1
        if bucket["latest"] is None:
            bucket["latest"] = row

    for case_id, bucket in by_case.items():
        scores = [
            float(row["scorecard_score"])
            for row in rows
            if row["case_id"] == case_id and row.get("scorecard_score") is not None
        ]
        bucket["avg_score"] = round(mean(scores), 2) if scores else None
        bucket["pass_rate"] = round(bucket["passes"] / bucket["runs"], 3) if bucket["runs"] else 0.0

    return {
        "runs": rows,
        "summary": sorted(by_case.values(), key=lambda row: str(row["case_id"])),
    }
