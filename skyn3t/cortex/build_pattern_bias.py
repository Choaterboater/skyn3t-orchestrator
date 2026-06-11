"""Apply build-pattern bias proposals without CodeImprover.

MetaAgent files these as ``kind=feature`` with ``payload.kind=build_pattern_bias``.
They describe scaffold shape statistics — not a repo patch — so approval should
persist the winning-shape skill and record the operator's preference.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("skyn3t.cortex.build_pattern_bias")

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFS_PATH = REPO_ROOT / "data" / "build_pattern_preferences.json"


def persist_build_pattern_skill(
    *,
    stack: str,
    winner_shape: List[str],
    winner_success_rate: float,
    winner_samples: int,
    loser_success_rate: float,
    distinguishing_files: List[str],
) -> str:
    """Write or refresh the stack's winning-shape skill. Returns skill name."""
    from skyn3t.intelligence.skill_library import Skill, get_default_library

    name = f"{stack}-winning-shape"
    body_lines = [
        f"# Prefer this shape for `{stack}` builds.",
        "",
        f"Operator-approved pattern (observed on {winner_samples} graded builds, "
        f"**{winner_success_rate:.0%}** success vs alternative at "
        f"**{loser_success_rate:.0%}**).",
        "",
        "## Winning shape",
        "",
    ]
    body_lines.extend(f"- `{p}`" for p in winner_shape)
    if distinguishing_files:
        body_lines.extend(
            [
                "",
                "## Load-bearing files (present in winner, absent from loser)",
                "",
            ]
        )
        body_lines.extend(f"- `{p}`" for p in distinguishing_files)

    skill = Skill(
        name=name,
        tags=[stack, "build-success", "scaffold-shape", "operator-approved"],
        success_count=max(1, int(winner_samples * winner_success_rate)),
        failure_count=max(0, winner_samples - int(winner_samples * winner_success_rate)),
        source="cortex:build_pattern_bias",
        body="\n".join(body_lines),
    )
    # The counts above are cumulative-derived from the scoreboard and this
    # writes to a STABLE slug ({stack}-winning-shape) that re-fires on every
    # approval/graduation. Use "set" so repeated graduations are idempotent and
    # never inflate the skill's success/failure counts.
    get_default_library().upsert(skill, count_mode="set")
    return name


def _write_stack_preference(stack: str, payload: Dict[str, Any]) -> None:
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    prefs: Dict[str, Any] = {}
    if PREFS_PATH.exists():
        try:
            prefs = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("could not read %s; overwriting", PREFS_PATH)
    prefs[stack] = {
        "shape": list(payload.get("winner_shape") or []),
        "winner_success_rate": payload.get("winner_success_rate"),
        "loser_success_rate": payload.get("loser_success_rate"),
        "distinguishing_files": list(payload.get("distinguishing_files") or []),
        "applied_at": time.time(),
    }
    PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def write_stack_preference(stack: str, payload: Dict[str, Any]) -> None:
    """Public wrapper for persisting operator-approved scaffold preferences."""
    _write_stack_preference(stack, payload)


def _apply_build_pattern_bias_core(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous activation core (no awaits).

    This is the single place that actually persists the winning-shape skill
    and records the stack preference. The async ``apply_build_pattern_bias``
    (cortex approval handler) and the sync ``apply_build_pattern_bias_sync``
    (MetaAgent graduation) both delegate here so behavior is identical.
    """
    stack = str(payload.get("stack") or "").strip()
    winner_shape = [str(p).strip() for p in (payload.get("winner_shape") or []) if str(p).strip()]
    if not stack:
        return {"ok": False, "error": "build_pattern_bias missing stack"}
    if not winner_shape:
        return {"ok": False, "error": "build_pattern_bias missing winner_shape"}

    try:
        skill_name = persist_build_pattern_skill(
            stack=stack,
            winner_shape=winner_shape,
            winner_success_rate=float(payload.get("winner_success_rate") or 0.0),
            winner_samples=int(payload.get("winner_samples") or 0),
            loser_success_rate=float(payload.get("loser_success_rate") or 0.0),
            distinguishing_files=[
                str(p).strip()
                for p in (payload.get("distinguishing_files") or [])
                if str(p).strip()
            ],
        )
        _write_stack_preference(stack, payload)
    except Exception as exc:
        logger.exception("apply_build_pattern_bias failed for stack=%s", stack)
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "status": "applied",
        "stack": stack,
        "skill": skill_name,
        "details": (
            f"Recorded preferred scaffold shape for {stack} "
            f"({len(winner_shape)} paths). Future Studio scaffolds can read "
            f"skill `{skill_name}` and data/build_pattern_preferences.json."
        ),
    }


def apply_build_pattern_bias_sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Sync-friendly entry for callers without an await point.

    MetaAgent's threshold scan (``_check_build_pattern_biases``) runs as a
    synchronous method inside the observe loop and needs to auto-promote a
    graduated pattern without scheduling a coroutine. Since the activation
    body never awaits, this calls the shared sync core directly.
    """
    return _apply_build_pattern_bias_core(payload)


async def apply_build_pattern_bias(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Activate a build-pattern bias proposal (no LLM diff).

    Async signature is preserved for the cortex ``feature`` approval handler
    (handlers.py awaits this). The work itself is synchronous.
    """
    return _apply_build_pattern_bias_core(payload)
