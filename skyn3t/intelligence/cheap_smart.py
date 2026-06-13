"""Cheap-first routing with context boost and smart escalation.

When ``SKYN3T_CHEAP_SMART=1`` (default ON):

* Code stages start on ``or_cheap`` / per-file ``or_ui`` / ``or_backend`` tiers
  instead of ``or_strong``, while reviewer/critique stays on strong models.
* Cheap-tier prompts get extra context (build patterns, lessons, competitive hints).
* Failures escalate runtime routing to stronger tiers for the rest of the project.
* High-confidence "use cheaper tier" routing recommendations auto-apply at
  pipeline start.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("skyn3t.intelligence.cheap_smart")

# Set by StudioRunner for the active project so model_router can read escalations.
_current_project_slug: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "cheap_smart_project_slug",
    default=None,
)

# Set by StudioRunner per build: "autonomous" (throwaway drills -> FREE models)
# or "real" (user/owner projects -> better-but-cheap). Read by model_router to
# force the free tier for drills. Carries a lane LABEL only, never a model id.
_current_lane: contextvars.ContextVar[str] = contextvars.ContextVar(
    "cheap_smart_lane",
    default="real",
)

# slug -> {stage_name -> tier_name}
_runtime_escalations: Dict[str, Dict[str, str]] = {}


def cheap_smart_enabled() -> bool:
    """True unless explicitly disabled via ``SKYN3T_CHEAP_SMART=0``."""
    raw = os.environ.get("SKYN3T_CHEAP_SMART", "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return False
    return True


def cheap_first_code_enabled() -> bool:
    """Cheap-FIRST codegen is now OPT-IN via ``SKYN3T_CHEAP_FIRST_CODE=1``.

    The cheap-first experiment routed all code stages to the flash-lite
    tier and build output quality cratered (owner, 2026-06-11: "nothing
    really improved on output"). Escalation tracking and context boost
    stay on; only the initial cheap routing for code is opt-in now.
    """
    raw = os.environ.get("SKYN3T_CHEAP_FIRST_CODE", "").strip().lower()
    return raw in ("1", "on", "true", "yes")


def cheap_smart_stage_tier(stage_name: Optional[str]) -> Optional[str]:
    """Cheap-first tier override for a stage when cheap-smart is active."""
    if not cheap_smart_enabled() or not cheap_first_code_enabled() or not stage_name:
        return None
    stage = str(stage_name).strip().lower()
    if stage in {"code", "code_agent", "code_improver"}:
        return "or_cheap"
    return None


def set_project_context(slug: Optional[str]) -> None:
    """Bind the active Studio project slug for escalation lookups."""
    _current_project_slug.set(slug or None)


def set_lane_context(is_autonomous: bool) -> None:
    """Tag the active build's lane: autonomous drills run FREE, real projects cheap."""
    _current_lane.set("autonomous" if is_autonomous else "real")


def current_lane() -> str:
    """Active build lane ("autonomous" or "real"); defaults to "real"."""
    return _current_lane.get()


def lane_a_free_tier(stage_name: Optional[str]) -> Optional[str]:
    """Free OpenRouter tier for an autonomous-drill (Lane A) stage.

    Operator-tunable via ``SKYN3T_LANE_A_FREE_TIERS`` (JSON stage->tier map with
    an optional ``"default"``). Falls back to the known-free ``or_docs`` tier.
    Returns a tier NAME only — the concrete model stays dynamic on the catalog.
    """
    mapping: Dict[str, str] = {}
    raw = os.environ.get("SKYN3T_LANE_A_FREE_TIERS", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                mapping = {str(k).lower(): str(v) for k, v in data.items()}
        except Exception:
            logger.debug("SKYN3T_LANE_A_FREE_TIERS parse failed", exc_info=True)
    stage = str(stage_name or "").strip().lower()
    return mapping.get(stage) or mapping.get("default") or "or_docs"


def clear_project_context(slug: Optional[str] = None) -> None:
    """Clear runtime escalation state for a finished project."""
    _current_project_slug.set(None)
    _current_lane.set("real")
    if slug:
        _runtime_escalations.pop(slug, None)


def escalate_stage(
    stage_name: str,
    *,
    tier: str = "or_strong",
    project_slug: Optional[str] = None,
    reason: str = "",
) -> None:
    """Bump a stage to ``tier`` for the remainder of this project run."""
    slug = project_slug or _current_project_slug.get()
    if not slug:
        logger.debug("cheap_smart: escalate %s skipped — no project slug", stage_name)
        return
    stage = str(stage_name or "").strip().lower()
    if not stage:
        return
    bucket = _runtime_escalations.setdefault(slug, {})
    if bucket.get(stage) == tier:
        return
    bucket[stage] = tier
    logger.info(
        "cheap_smart: escalated %s → %s for project=%s (%s)",
        stage,
        tier,
        slug,
        reason or "failure",
    )


def escalated_tier_for_stage(
    stage_name: Optional[str],
    *,
    project_slug: Optional[str] = None,
) -> Optional[str]:
    slug = project_slug or _current_project_slug.get()
    if not slug or not stage_name:
        return None
    return _runtime_escalations.get(slug, {}).get(str(stage_name).strip().lower())


def is_cheap_tier(tier_name: Optional[str]) -> bool:
    if not tier_name:
        return False
    tier = str(tier_name).strip().lower()
    return "cheap" in tier or tier in {"cheap", "or_cheap", "or_docs"}


def auto_apply_cheaper_routing(*, min_confidence: str = "high") -> List[Dict[str, str]]:
    """Apply high-confidence routing recommendations that downgrade tier cost."""
    if not cheap_smart_enabled():
        return []
    try:
        from skyn3t.config.model_routing import get_model_routing_store
        from skyn3t.core.model_router import relative_backend_cost, tier_details
        from skyn3t.intelligence.routing_recommendations import list_stage_recommendations
    except Exception:
        logger.debug("cheap_smart auto-apply import failed", exc_info=True)
        return []

    store = get_model_routing_store()
    applied: List[Dict[str, str]] = []
    for row in list_stage_recommendations():
        if not row.get("applyable"):
            continue
        if str(row.get("confidence") or "") != min_confidence:
            continue
        kind = str(row.get("recommendation_kind") or "")
        if kind not in {"efficiency", "cheaper"}:
            continue
        stage = str(row.get("stage") or "").strip().lower()
        rec_tier = str(row.get("recommended_tier") or "").strip()
        cur_tier = str(row.get("current_tier") or "").strip()
        if not stage or not rec_tier or rec_tier == cur_tier:
            continue
        # Never auto-downgrade judgment-sensitive stages (reviewer, architect).
        if "strong" in cur_tier and "cheap" not in rec_tier:
            continue
        rec_backend, _ = tier_details(rec_tier)
        cur_backend = str(row.get("current_backend") or "")
        if "cheap" in rec_tier and "cheap" not in cur_tier:
            pass
        elif rec_backend and cur_backend:
            if relative_backend_cost(str(rec_backend)) >= relative_backend_cost(cur_backend):
                continue
        store.set_many({stage: rec_tier}, applied_via="recommendation")
        applied.append({"stage": stage, "tier": rec_tier, "kind": kind})
    if applied:
        logger.info("cheap_smart: auto-applied %d routing recommendation(s)", len(applied))
    return applied


def _build_pattern_prefs_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "build_pattern_preferences.json"


def build_cheap_context_boost(
    *,
    brief: str = "",
    stack: str = "",
    rel_path: str = "",
    stage_name: str = "code",
) -> str:
    """Extra prompt context to lift cheap-model output quality."""
    if not cheap_smart_enabled():
        return ""

    sections: List[str] = []

    # Chain-of-thought scaffold for weaker models.
    sections.append(
        "## Cheap-smart execution checklist\n"
        "Before writing code, silently plan:\n"
        "1. What is the minimum shippable slice for this file?\n"
        "2. Which imports/exports must match sibling files?\n"
        "3. What would make a reviewer reject this?\n"
        "Then output ONLY valid file contents — no narration."
    )

    # Operator-approved winning scaffold shape.
    try:
        prefs_path = _build_pattern_prefs_path()
        if prefs_path.exists() and stack:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            entry = prefs.get(stack) if isinstance(prefs, dict) else None
            if isinstance(entry, dict):
                shape = entry.get("shape") or []
                rate = entry.get("winner_success_rate")
                if shape:
                    lines = [f"- `{p}`" for p in shape[:12]]
                    header = f"## Winning scaffold shape for `{stack}`"
                    if rate is not None:
                        header += f" ({float(rate):.0%} success)"
                    sections.append(header + "\n" + "\n".join(lines))
    except Exception:
        logger.debug("cheap_smart build_pattern prefs read failed", exc_info=True)

    # Distilled learnings the system has accumulated (the Learnings Store — the
    # same curated corpus the local micro-LLM reads). Curated guidance from past
    # builds beats noisy RAG chunks; pulled as plain context, no extra LLM call.
    try:
        from skyn3t.intelligence.learnings_store import get_default_store

        items = get_default_store().guidance_for(
            brief or stage_name, stack=stack, limit=3
        )
        if items:
            lines = [
                f"- {e.get('title', '')}: {e.get('content', '')}".strip()
                for e in items
            ]
            sections.append("## Learned guidance (from past builds)\n" + "\n".join(lines))
    except Exception:
        logger.debug("cheap_smart learnings guidance read failed", exc_info=True)

    # One competitive pattern hint when the brief matches a known gap.
    try:
        from skyn3t.cortex.competitive_intel import match_competitor

        for slug in ("NousResearch/hermes-agent", "getforge-io/forge", "openclaw/openclaw"):
            meta = match_competitor(slug)
            if not meta:
                continue
            patterns = list(meta.get("patterns") or [])
            if not patterns:
                continue
            name = str(meta.get("name") or slug)
            sections.append(
                f"## Competitive bar ({name})\n"
                f"Ship runnable software, not markdown theater. Borrow workflow ideas: "
                f"{patterns[0][:200]}"
            )
            break
    except Exception:
        logger.debug("cheap_smart competitive hint failed", exc_info=True)

    # File-type hint for UI vs backend paths.
    rl = (rel_path or "").lower().replace("\\", "/")
    if any(h in rl for h in ("components/", "pages/", "hooks/", "app.jsx", "app.tsx")):
        sections.append(
            "## UI file bar\n"
            "Use real layout/components (cards, grids, nav), semantic tokens, "
            "and loading/empty/error states — not JSON dumps or placeholder divs."
        )
    elif any(h in rl for h in ("server/", "api/", "routes/", "adapters/")):
        sections.append(
            "## Backend file bar\n"
            "Wire routes end-to-end, validate env vars, use ESM consistently, "
            "and export handlers the entrypoint can mount."
        )

    if stage_name:
        sections.append(f"(context boost for stage={stage_name}, stack={stack or 'unknown'})")

    return "\n\n".join(sections).strip()
