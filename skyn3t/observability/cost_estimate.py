"""Shared OpenRouter cost estimation for token rollups and autonomy caps."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def blended_token_rate() -> float:
    """Blended $/token across default OpenRouter build tiers.

    Returns 0.0 when those tiers resolve to free models so callers do not
    fabricate spend. Falls back to the legacy 1/250_000 heuristic only when
    catalog pricing cannot be resolved.
    """
    legacy = 1.0 / 250_000.0
    try:
        from skyn3t.core.model_router import _TIERS
        from skyn3t.core.openrouter_catalog import load_catalog
    except Exception:
        return legacy
    wanted: List[str] = [
        entry[1]
        for t in ("or_cheap", "or_ui", "or_backend", "or_strong")
        if (entry := _TIERS.get(t)) and entry[0] == "openrouter" and entry[1]
    ]
    if not wanted:
        return legacy
    try:
        models = load_catalog().models or []
    except Exception:
        return legacy
    pricing_by_id: Dict[str, Any] = {
        m.get("id"): m.get("pricing") for m in models if m.get("id")
    }
    best = 0.0
    found = False
    for model_id in wanted:
        pricing = pricing_by_id.get(model_id)
        if not isinstance(pricing, dict):
            continue
        try:
            p = float(pricing.get("prompt") or pricing.get("input") or 0)
            c = float(pricing.get("completion") or pricing.get("output") or 0)
        except (TypeError, ValueError):
            p = c = 0.0
        best = max(best, (p + c) / 2.0)
        found = True
    return best if found else legacy


def estimate_cost_usd(tokens: int, *, cap: Optional[float] = None) -> float:
    """Estimate USD spend from a token count using the blended catalog rate."""
    if tokens <= 0:
        return 0.0
    rate = blended_token_rate()
    if rate <= 0:
        return 0.0
    cost = max(0.0, tokens * rate)
    if cap is not None and cap > 0:
        return min(float(cap), cost)
    return cost
