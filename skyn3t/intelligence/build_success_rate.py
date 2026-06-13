"""Record per-stack Studio build success for week-over-week trending."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

_STATS_PATH = Path("./data/build_success_rate.json")


def record_outcome(*, stack: str, success: bool, slug: str, score: int | None = None) -> None:
    data = _load()
    key = (stack or "unknown").strip().lower() or "unknown"
    bucket = data.setdefault("stacks", {}).setdefault(
        key,
        {"success": 0, "failure": 0, "samples": []},
    )
    if success:
        bucket["success"] += 1
    else:
        bucket["failure"] += 1
    bucket["samples"].append(
        {
            "slug": slug,
            "success": success,
            "score": score,
            "ts": time.time(),
        }
    )
    bucket["samples"] = bucket["samples"][-50:]
    data["updated_at"] = time.time()
    _save(data)


def stack_rates() -> List[Dict[str, Any]]:
    data = _load()
    rows: List[Dict[str, Any]] = []
    for stack, bucket in (data.get("stacks") or {}).items():
        success = int(bucket.get("success") or 0)
        failure = int(bucket.get("failure") or 0)
        total = success + failure
        rate = (success / total) if total else 0.0
        rows.append(
            {
                "stack": stack,
                "success": success,
                "failure": failure,
                "success_rate": round(rate, 3),
            }
        )
    rows.sort(key=lambda r: r["success"] + r["failure"], reverse=True)
    return rows


def _load() -> Dict[str, Any]:
    if not _STATS_PATH.exists():
        return {}
    try:
        data = json.loads(_STATS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: Dict[str, Any]) -> None:
    _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
