"""Model Evolution Engine — proactive OpenRouter tier upgrades.

After each catalog sync, scores models per tier (cheap/ui/backend/strong/docs)
using context length, pricing, tool support, and recency hints in model ids.
Better models are persisted to ``data/model_tier_overrides.json``; downgrades
are skipped unless ``SKYN3T_MODEL_EVOLUTION_DOWNGRADE=1``.

Enabled by default when ``OPENROUTER_API_KEY`` is set (``SKYN3T_MODEL_EVOLUTION=1``).
When enabled, catalog sync TTL shrinks to 6h for faster discovery.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("skyn3t.core.model_evolution")

EVOLUTION_TTL_SECONDS = 21_600  # 6h
OVERRIDES_FILENAME = "model_tier_overrides.json"

OPENROUTER_TIERS: Tuple[str, ...] = (
    "or_cheap",
    "or_ui",
    "or_backend",
    "or_strong",
    "or_docs",
)

_TIER_KEYWORDS: Dict[str, List[str]] = {
    "or_cheap": ["owl", "free", "mini", "flash", "fast", "lite"],
    "or_ui": ["flash", "mimo", "ui", "vision", "frontend"],
    "or_backend": ["coder", "code", "qwen", "dev", "backend"],
    "or_strong": ["pro", "opus", "sonnet", "mimo", "reasoning"],
    "or_docs": ["free", "oss", "120b", "gpt", "instruct", "doc"],
}

_RECENCY_RE = re.compile(r"(?:^|[^0-9])(20[2-9][0-9]|v(?:\d+(?:\.\d+)?))(?:[^0-9]|$)", re.I)
_VERSION_RE = re.compile(r"v(\d+(?:\.\d+)?)", re.I)

_event_bus: Any = None
_overrides_cache: Optional[Dict[str, Any]] = None
_overrides_loaded_at: float = 0.0


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_falsy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"0", "false", "no", "off"}


def is_evolution_enabled(settings: Any | None = None) -> bool:
    """True when proactive tier evolution should run (default on with API key)."""
    if _env_falsy("SKYN3T_MODEL_EVOLUTION"):
        return False
    if _env_truthy("SKYN3T_MODEL_EVOLUTION"):
        return True
    if settings is None:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
    return bool(getattr(settings, "openrouter_api_key", None))


def allow_downgrade() -> bool:
    return _env_truthy("SKYN3T_MODEL_EVOLUTION_DOWNGRADE")


def allow_premium_models() -> bool:
    return _env_truthy("SKYN3T_MODEL_EVOLUTION_ALLOW_PREMIUM")


def _is_premium_model(model_id: str) -> bool:
    text = (model_id or "").lower()
    premium_markers = (
        "opus",
        "claude-opus",
        "gpt-5",
        "gpt-4.5",
        "o3-pro",
        "o1-pro",
    )
    return any(marker in text for marker in premium_markers)


def _no_claude_models() -> bool:
    """Owner directive: never select Claude models, even cheap ones on OpenRouter."""
    return _env_truthy("SKYN3T_NO_CLAUDE")


def _is_claude_model(model_id: str) -> bool:
    text = (model_id or "").lower()
    return "claude" in text or text.startswith("anthropic/")


def set_evolution_event_bus(event_bus: Any) -> None:
    """Optional hook so background evolution can publish SYSTEM_ALERT events."""
    global _event_bus
    _event_bus = event_bus


def overrides_path(settings: Any | None = None) -> Path:
    if settings is None:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
    return Path(settings.data_dir) / OVERRIDES_FILENAME


def _invalidate_overrides_cache() -> None:
    global _overrides_cache, _overrides_loaded_at
    _overrides_cache = None
    _overrides_loaded_at = 0.0


def load_overrides(*, settings: Any | None = None, max_age: float = 30.0) -> Dict[str, Any]:
    """Load persisted tier overrides (cached briefly in-process)."""
    global _overrides_cache, _overrides_loaded_at
    now = time.time()
    if _overrides_cache is not None and (now - _overrides_loaded_at) < max_age:
        return dict(_overrides_cache)

    path = overrides_path(settings)
    if not path.exists():
        _overrides_cache = {}
        _overrides_loaded_at = now
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            _overrides_cache = {}
        else:
            tiers = raw.get("tiers")
            if isinstance(tiers, dict):
                _overrides_cache = {"tiers": dict(tiers), **{k: v for k, v in raw.items() if k != "tiers"}}
            else:
                _overrides_cache = raw
        _overrides_loaded_at = now
        return dict(_overrides_cache)
    except Exception:
        logger.debug("model evolution overrides read failed", exc_info=True)
        _overrides_cache = {}
        _overrides_loaded_at = now
        return {}


def save_overrides(data: Dict[str, Any], *, settings: Any | None = None) -> None:
    path = overrides_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload.setdefault("updated_at", time.time())
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _invalidate_overrides_cache()


def tier_override_model(tier_name: str, *, settings: Any | None = None) -> Optional[str]:
    """Evolved model id for ``tier_name``, or None to use ``model_router._TIERS``."""
    if tier_name not in OPENROUTER_TIERS:
        return None
    data = load_overrides(settings=settings)
    tiers = data.get("tiers")
    if not isinstance(tiers, dict):
        return None
    entry = tiers.get(tier_name)
    if not isinstance(entry, dict):
        return None
    model = str(entry.get("model") or "").strip()
    return model or None


def _prompt_cost(meta: Dict[str, Any]) -> float:
    pricing = meta.get("pricing") if isinstance(meta.get("pricing"), dict) else {}
    for key in ("prompt", "input"):
        raw = pricing.get(key) if isinstance(pricing, dict) else None
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def _has_tool_support(meta: Dict[str, Any]) -> bool:
    supported = meta.get("supported_parameters")
    if not isinstance(supported, list):
        return False
    joined = " ".join(str(p).lower() for p in supported)
    return "tool" in joined


def _recency_bonus(model_id: str) -> float:
    bonus = 0.0
    for match in _RECENCY_RE.finditer(model_id):
        token = match.group(1)
        if token.startswith("20"):
            try:
                year = int(token)
                bonus = max(bonus, max(0.0, (year - 2023) * 0.5))
            except ValueError:
                pass
        elif token.lower().startswith("v"):
            try:
                ver = float(token[1:])
                bonus = max(bonus, min(ver * 0.3, 2.0))
            except ValueError:
                pass
    return bonus


def score_model_for_tier(tier_name: str, model_id: str, meta: Dict[str, Any]) -> float:
    """Higher is better. Used for tier assignment and task-kind refinement."""
    keywords = _TIER_KEYWORDS.get(tier_name, [])
    hay = f"{model_id} {meta.get('name', '')} {meta.get('description', '')}".lower()

    score = 0.0
    for kw in keywords:
        if kw and kw in hay:
            score += 2.0

    ctx = meta.get("context_length") or 0
    if isinstance(ctx, (int, float)) and ctx > 0:
        score += min(float(ctx) / 32_000.0, 3.0)
        if ctx >= 128_000:
            score += 0.5

    cost = _prompt_cost(meta)
    if cost == 0.0:
        score += 2.0 if tier_name in {"or_cheap", "or_docs"} else 0.5
    elif cost < 0.0001:
        score += 1.0
    else:
        score += max(0.0, 1.5 - cost * 10_000.0)

    if _has_tool_support(meta):
        score += 1.0 if tier_name in {"or_backend", "or_strong"} else 0.3

    score += _recency_bonus(model_id)

    if ":free" in model_id.lower():
        score += 1.5 if tier_name in {"or_cheap", "or_docs"} else 0.2

    return score


def find_best_for_tier(
    tier_name: str,
    catalog_index: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[str], float]:
    best_id: Optional[str] = None
    best_score = -1.0
    premium_ok = allow_premium_models()
    no_claude = _no_claude_models()
    for mid, meta in catalog_index.items():
        if not mid or not isinstance(meta, dict):
            continue
        if not premium_ok and _is_premium_model(mid):
            continue
        if no_claude and _is_claude_model(mid):
            continue
        s = score_model_for_tier(tier_name, mid, meta)
        if s > best_score:
            best_score = s
            best_id = mid
    return best_id, best_score


def _default_tier_model(tier_name: str) -> Optional[str]:
    from skyn3t.core.model_router import _TIERS

    entry = _TIERS.get(tier_name)
    if not entry:
        return None
    backend, model = entry
    if backend != "openrouter":
        return None
    return model


def _current_effective_model(
    tier_name: str,
    overrides: Dict[str, Any],
) -> Tuple[Optional[str], float, str]:
    """Return (model_id, score, source) for the active tier assignment."""
    tiers = overrides.get("tiers") if isinstance(overrides.get("tiers"), dict) else {}
    entry = tiers.get(tier_name) if isinstance(tiers, dict) else None
    if isinstance(entry, dict):
        model = str(entry.get("model") or "").strip()
        if model:
            score = float(entry.get("score") or 0.0)
            return model, score, "override"

    default = _default_tier_model(tier_name)
    if not default:
        return None, 0.0, "default"

    from skyn3t.core.openrouter_catalog import load_catalog

    snap = load_catalog()
    index = {m["id"]: m for m in snap.models if m.get("id")}
    meta = index.get(default, {})
    return default, score_model_for_tier(tier_name, default, meta), "default"


def run_evolution(
    *,
    event_bus: Any = None,
    settings: Any | None = None,
    allow_downgrade_override: Optional[bool] = None,
) -> Dict[str, Any]:
    """Score catalog models and persist tier upgrades. Returns evolution summary."""
    if not is_evolution_enabled(settings):
        return {"enabled": False, "upgrades": [], "tiers": {}}

    from skyn3t.core.openrouter_catalog import load_catalog

    snap = load_catalog()
    if not snap.models:
        return {"enabled": True, "upgrades": [], "tiers": {}, "reason": "empty_catalog"}

    catalog_index = {m["id"]: m for m in snap.models if m.get("id")}
    overrides = load_overrides(settings=settings, max_age=0.0)
    tiers_block = overrides.get("tiers")
    if not isinstance(tiers_block, dict):
        tiers_block = {}

    downgrade_ok = (
        allow_downgrade_override
        if allow_downgrade_override is not None
        else allow_downgrade()
    )
    min_gain = float(os.environ.get("SKYN3T_MODEL_EVOLUTION_MIN_GAIN", "0.25") or 0.25)

    upgrades: List[Dict[str, Any]] = []
    tier_summary: Dict[str, Any] = {}

    for tier_name in OPENROUTER_TIERS:
        best_id, best_score = find_best_for_tier(tier_name, catalog_index)
        current_id, current_score, source = _current_effective_model(tier_name, overrides)

        row: Dict[str, Any] = {
            "tier": tier_name,
            "current": current_id,
            "current_score": round(current_score, 3),
            "best": best_id,
            "best_score": round(best_score, 3),
            "source": source,
            "changed": False,
        }
        tier_summary[tier_name] = row

        if not best_id or best_score <= 0:
            continue

        should_apply = False
        if current_id is None:
            should_apply = True
        elif best_id == current_id:
            pass
        elif downgrade_ok:
            should_apply = best_score >= current_score
        else:
            should_apply = best_score > current_score + min_gain

        if not should_apply:
            continue

        if best_id == current_id:
            if isinstance(tiers_block.get(tier_name), dict):
                tiers_block[tier_name]["score"] = best_score
            continue

        prev = current_id
        tiers_block[tier_name] = {
            "model": best_id,
            "score": round(best_score, 3),
            "previous": prev,
            "evolved_at": time.time(),
            "reason": "catalog_evolution",
        }
        row["changed"] = True
        upgrade = {
            "tier": tier_name,
            "from": prev,
            "to": best_id,
            "from_score": round(current_score, 3),
            "to_score": round(best_score, 3),
        }
        upgrades.append(upgrade)
        logger.info(
            "model evolution: %s %s → %s (score %.2f → %.2f)",
            tier_name,
            prev,
            best_id,
            current_score,
            best_score,
        )

    if upgrades or tiers_block:
        save_overrides(
            {
                "tiers": tiers_block,
                "last_evolution_at": time.time(),
                "catalog_synced_at": snap.synced_at,
            },
            settings=settings,
        )

    bus = event_bus or _event_bus
    if upgrades and bus is not None:
        _publish_tier_evolved(bus, upgrades)

    return {
        "enabled": True,
        "upgrades": upgrades,
        "tiers": tier_summary,
        "downgrade_allowed": downgrade_ok,
    }


def _publish_tier_evolved(event_bus: Any, upgrades: List[Dict[str, Any]]) -> None:
    try:
        from skyn3t.core.events import Event, EventType

        event_bus.publish(
            Event(
                event_type=EventType.SYSTEM_ALERT,
                source="model_evolution",
                payload={
                    "alert_type": "MODEL_TIER_EVOLVED",
                    "kind": "MODEL_TIER_EVOLVED",
                    "upgrades": upgrades,
                    "count": len(upgrades),
                },
                priority=2,
            )
        )
    except Exception:
        logger.debug("model evolution alert publish failed", exc_info=True)


def evolution_status(*, settings: Any | None = None) -> Dict[str, Any]:
    """Snapshot for API/CLI — overrides, enabled flag, last run metadata."""
    data = load_overrides(settings=settings, max_age=0.0)
    tiers = data.get("tiers") if isinstance(data.get("tiers"), dict) else {}
    return {
        "enabled": is_evolution_enabled(settings),
        "ttl_seconds": EVOLUTION_TTL_SECONDS,
        "downgrade_allowed": allow_downgrade(),
        "last_evolution_at": data.get("last_evolution_at"),
        "catalog_synced_at": data.get("catalog_synced_at"),
        "updated_at": data.get("updated_at"),
        "tiers": tiers,
    }


def pick_evolved_model_for_task(
    tier_name: str,
    task_kind: str,
    *,
    base_model: Optional[str] = None,
) -> Optional[str]:
    """Task-kind refinement on top of evolution tier base (cheap-smart integration)."""
    from skyn3t.core.openrouter_catalog import pick_best_model_for_task

    base = base_model or tier_override_model(tier_name)
    picked = pick_best_model_for_task(
        tier_name,
        task_kind,
        base_model=base,
        prefer_evolution=True,
    )
    return picked or base
