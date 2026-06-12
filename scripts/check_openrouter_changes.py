#!/usr/bin/env python3
"""Daily OpenRouter drift check.

Compares the model IDs pinned in ``skyn3t.core.model_router._TIERS`` against
OpenRouter's live ``/api/v1/models`` endpoint and reports any missing or
newly-added models that might affect routing decisions.

Intended to run once a day (cron, GitHub Action, or ``never_stop`` tick).
"""
from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any, Dict, List, Set, Tuple


def fetch_openrouter_models() -> Set[str]:
    """Return the set of live model IDs from OpenRouter."""
    url = "https://openrouter.ai/api/v1/models"
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return {str(m.get("id", "")).strip() for m in data.get("data", []) if m.get("id")}


def get_tracked_models() -> Dict[str, Tuple[str, str]]:
    """Return {tier_name: (backend, model_id)} from the live router table."""
    from skyn3t.core.model_router import _TIERS

    return {
        tier: (backend, model or "")
        for tier, (backend, model) in _TIERS.items()
        if backend == "openrouter" and model
    }


def main() -> int:
    try:
        live = fetch_openrouter_models()
    except Exception as exc:
        print(f"ERROR: could not fetch OpenRouter models: {exc}", file=sys.stderr)
        return 2

    tracked = get_tracked_models()
    if not tracked:
        print("No OpenRouter models are tracked in _TIERS.")
        return 0

    missing: List[Tuple[str, str]] = []
    ok: List[Tuple[str, str]] = []
    for tier, (_backend, model) in tracked.items():
        if model in live:
            ok.append((tier, model))
        else:
            missing.append((tier, model))

    print(f"Tracked OpenRouter tiers: {len(tracked)}")
    print(f"Live models on OpenRouter: {len(live)}")
    for tier, model in ok:
        print(f"  OK   {tier:12s} {model}")

    if missing:
        print("\nMISSING from OpenRouter (routing may fall back):")
        for tier, model in missing:
            print(f"  WARN {tier:12s} {model}")
        return 1

    print("\nAll tracked OpenRouter models are still available.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
