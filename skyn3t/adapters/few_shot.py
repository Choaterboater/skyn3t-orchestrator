"""Pull recent successful artifacts/diffs from the repo to include as
few-shot examples in agent prompts.

Caches per-kind for 30 minutes.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("skyn3t.adapters.few_shot")

REPO_ROOT = Path(__file__).resolve().parents[2]
_CACHE: dict[tuple[str, int], tuple[float, List[str]]] = {}
_TTL = 1800.0


def _cache_get(k: tuple[str, int]) -> Optional[List[str]]:
    v = _CACHE.get(k)
    if v and (time.time() - v[0]) < _TTL:
        return v[1]
    return None


def _cache_set(k: tuple[str, int], val: List[str]) -> None:
    _CACHE[k] = (time.time(), val)


def successful_diffs(limit: int = 3) -> List[str]:
    """Return up to `limit` recent diffs from auto-merge commits."""
    cached = _cache_get(("diffs", limit))
    if cached is not None:
        return cached
    try:
        # Find chore(auto) commits on main
        proc = subprocess.run(
            [
                "git",
                "log",
                "--no-merges",
                "--pretty=format:%H",
                "--grep",
                "chore(auto)",
                "-n",
                str(limit * 3),
                "main",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        shas = [s for s in proc.stdout.splitlines() if s][:limit]
        diffs: List[str] = []
        for sha in shas:
            d = subprocess.run(
                ["git", "show", sha, "--no-color"],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                timeout=10,
            )
            text = d.stdout.strip()
            if text and len(text) < 6000:
                diffs.append(text)
        _cache_set(("diffs", limit), diffs)
        return diffs
    except Exception:
        logger.exception("successful_diffs failed")
        return []


def recent_brand_artifacts(limit: int = 2) -> List[str]:
    """Return recent brand.md / palette.json artifacts that scored well."""
    cached = _cache_get(("brand", limit))
    if cached is not None:
        return cached
    out: List[str] = []
    projects_dir = REPO_ROOT / "projects"
    if not projects_dir.exists():
        return []
    # Latest projects with brand.md and review verdict not "no-go"
    for p in sorted(projects_dir.iterdir(), reverse=True)[: limit * 2]:
        b = p / "brand.md"
        if b.exists():
            try:
                text = b.read_text(encoding="utf-8")
                if len(text) < 2500:
                    out.append(text)
                    if len(out) >= limit:
                        break
            except Exception:
                continue
    _cache_set(("brand", limit), out)
    return out


def few_shot_block(kind: str, count: int = 2) -> str:
    """Return a markdown few-shot block for the given task kind, or '' if none."""
    if kind == "code_diff":
        diffs = successful_diffs(count)
        if not diffs:
            return ""
        out = ["# Recent successful diffs (for reference; produce a SIMILAR diff for the new task)"]
        for d in diffs:
            out.append("```diff")
            out.append(d[:2500])
            out.append("```")
        return "\n".join(out)
    if kind == "brand":
        items = recent_brand_artifacts(count)
        if not items:
            return ""
        out = ["# Recent brand artifacts (for style reference)"]
        for t in items:
            out.append("```markdown")
            out.append(t[:1500])
            out.append("```")
        return "\n".join(out)
    return ""
