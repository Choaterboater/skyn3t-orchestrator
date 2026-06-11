"""OpenRouter model catalog — fetch, cache, validate, and route fallbacks.

Syncs GET https://openrouter.ai/api/v1/models into ``data/openrouter_models.json``
with a 24h TTL. Offline or failed fetches fall back to the on-disk cache and
hardcoded tier IDs in ``model_router._TIERS``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skyn3t.core.openrouter_catalog")

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_TTL_SECONDS = 86_400  # 24h
CACHE_FILENAME = "openrouter_models.json"

# Keywords used to pick a replacement when a tier model disappears.
_TIER_FALLBACK_KEYWORDS: Dict[str, List[str]] = {
    "or_cheap": ["owl", "free", "mini", "flash"],
    "or_ui": ["flash", "mimo", "ui"],
    "or_backend": ["coder", "code", "qwen"],
    "or_strong": ["pro", "opus", "sonnet", "mimo"],
    "or_docs": ["free", "oss", "120b", "gpt"],
}

_background_task: Optional[asyncio.Task[None]] = None
_catalog_index: Optional[Dict[str, Dict[str, Any]]] = None
_catalog_loaded_at: float = 0.0


@dataclass
class CatalogSnapshot:
    """In-memory view of the OpenRouter catalog."""

    synced_at: float
    ttl_seconds: int
    models: List[Dict[str, Any]]
    source: str  # cache | network | empty

    @property
    def stale(self) -> bool:
        if not self.synced_at:
            return True
        return (time.time() - self.synced_at) > self.ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        return {
            "synced_at": self.synced_at,
            "ttl_seconds": self.ttl_seconds,
            "stale": self.stale,
            "source": self.source,
            "count": len(self.models),
            "models": self.models,
        }


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_falsy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"0", "false", "no", "off"}


def is_sync_enabled(settings: Any | None = None) -> bool:
    """True when background OpenRouter catalog sync should run."""
    if _env_falsy("SKYN3T_OPENROUTER_SYNC"):
        return False
    if _env_truthy("SKYN3T_OPENROUTER_SYNC"):
        return True
    if settings is None:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
    return bool(getattr(settings, "openrouter_api_key", None))


def catalog_cache_path(settings: Any | None = None) -> Path:
    if settings is None:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
    return Path(settings.data_dir) / CACHE_FILENAME


def _parse_model(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mid = str(raw.get("id") or "").strip()
    if not mid:
        return None
    pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    supported = raw.get("supported_parameters")
    if not isinstance(supported, list):
        supported = []
    architecture = raw.get("architecture")
    if not isinstance(architecture, dict):
        architecture = {}
    return {
        "id": mid,
        "name": str(raw.get("name") or mid),
        "description": str(raw.get("description") or ""),
        "context_length": raw.get("context_length"),
        "pricing": pricing,
        "supported_parameters": [str(p) for p in supported if p],
        "architecture": architecture,
    }


def _read_cache_file(path: Path) -> Optional[CatalogSnapshot]:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        models_raw = raw.get("models")
        if not isinstance(models_raw, list):
            return None
        models = [m for m in (_parse_model(x) for x in models_raw if isinstance(x, dict)) if m]
        return CatalogSnapshot(
            synced_at=float(raw.get("synced_at") or 0.0),
            ttl_seconds=int(raw.get("ttl_seconds") or DEFAULT_TTL_SECONDS),
            models=models,
            source="cache",
        )
    except Exception:
        logger.debug("openrouter catalog cache read failed", exc_info=True)
        return None


def _write_cache_file(path: Path, snapshot: CatalogSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "synced_at": snapshot.synced_at,
        "ttl_seconds": snapshot.ttl_seconds,
        "models": snapshot.models,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _refresh_index(snapshot: CatalogSnapshot) -> None:
    global _catalog_index, _catalog_loaded_at
    _catalog_index = {m["id"]: m for m in snapshot.models if m.get("id")}
    _catalog_loaded_at = time.time()


def load_catalog(*, settings: Any | None = None) -> CatalogSnapshot:
    """Load catalog from disk cache (never raises)."""
    snap = _read_cache_file(catalog_cache_path(settings))
    if snap is None:
        return CatalogSnapshot(
            synced_at=0.0,
            ttl_seconds=DEFAULT_TTL_SECONDS,
            models=[],
            source="empty",
        )
    _refresh_index(snap)
    return snap


async def fetch_models_from_api(*, api_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch models from OpenRouter (raises on hard network failure)."""
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("httpx required for openrouter catalog sync") from exc

    headers: Dict[str, str] = {}
    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(OPENROUTER_MODELS_URL, headers=headers or None)
        response.raise_for_status()
        data = response.json()
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    models: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = _parse_model(row)
        if parsed:
            models.append(parsed)
    models.sort(key=lambda m: str(m.get("name") or m.get("id")).lower())
    return models


async def sync_catalog(*, force: bool = False, settings: Any | None = None) -> Dict[str, Any]:
    """Refresh the on-disk catalog when stale or ``force`` is set."""
    if settings is None:
        from skyn3t.config.settings import get_settings

        settings = get_settings()

    path = catalog_cache_path(settings)
    cached = _read_cache_file(path)
    if cached and not force and not cached.stale:
        _refresh_index(cached)
        return {
            "status": "fresh",
            "source": "cache",
            "count": len(cached.models),
            "synced_at": cached.synced_at,
        }

    try:
        models = await fetch_models_from_api(
            api_key=getattr(settings, "openrouter_api_key", None),
        )
        snapshot = CatalogSnapshot(
            synced_at=time.time(),
            ttl_seconds=DEFAULT_TTL_SECONDS,
            models=models,
            source="network",
        )
        _write_cache_file(path, snapshot)
        _refresh_index(snapshot)
        logger.info("openrouter catalog synced: %d models", len(models))
        return {
            "status": "synced",
            "source": "network",
            "count": len(models),
            "synced_at": snapshot.synced_at,
        }
    except Exception as exc:
        logger.warning("openrouter catalog sync failed: %s", exc)
        if cached:
            _refresh_index(cached)
            return {
                "status": "cache_fallback",
                "source": "cache",
                "count": len(cached.models),
                "synced_at": cached.synced_at,
                "error": str(exc),
            }
        return {"status": "failed", "source": "empty", "count": 0, "error": str(exc)}


async def get_catalog_async(*, settings: Any | None = None) -> CatalogSnapshot:
    """Return cached catalog, syncing first when enabled and stale."""
    if settings is None:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
    snap = load_catalog(settings=settings)
    if is_sync_enabled(settings) and snap.stale:
        await sync_catalog(settings=settings)
        snap = load_catalog(settings=settings)
    return snap


def model_exists(model_id: str) -> bool:
    """Whether ``model_id`` is present in the loaded catalog index."""
    if not model_id:
        return False
    if _catalog_index is None or (time.time() - _catalog_loaded_at) > 60:
        load_catalog()
    return bool(_catalog_index and model_id in _catalog_index)


def find_best_fallback(tier_name: str, original_model: str) -> Optional[str]:
    """Pick the best catalog replacement for a missing tier model."""
    if _catalog_index is None or not _catalog_index:
        load_catalog()
    if not _catalog_index:
        return None

    keywords = list(_TIER_FALLBACK_KEYWORDS.get(tier_name, []))
    orig = original_model.lower()
    for part in orig.replace(":", "/").split("/"):
        if part and part not in keywords:
            keywords.append(part)

    best_id: Optional[str] = None
    best_score = -1
    for mid, meta in _catalog_index.items():
        hay = f"{mid} {meta.get('name', '')} {meta.get('description', '')}".lower()
        score = sum(1 for kw in keywords if kw and kw in hay)
        ctx = meta.get("context_length") or 0
        if isinstance(ctx, int) and ctx >= 32_000:
            score += 1
        if score > best_score:
            best_score = score
            best_id = mid
    return best_id if best_score > 0 else None


def resolve_openrouter_model(tier_name: str, model_id: Optional[str]) -> Optional[str]:
    """Validate a tier model against the catalog; fall back when missing."""
    if not model_id:
        return model_id
    if model_exists(model_id):
        return model_id
    fallback = find_best_fallback(tier_name, model_id)
    if fallback and fallback != model_id:
        logger.warning(
            "openrouter tier %s model %s not in catalog — using fallback %s",
            tier_name,
            model_id,
            fallback,
        )
        return fallback
    logger.warning(
        "openrouter tier %s model %s not in catalog — keeping configured id",
        tier_name,
        model_id,
    )
    return model_id


def validate_tier_models() -> List[Dict[str, Any]]:
    """Return validation rows for OpenRouter tiers in ``model_router._TIERS``."""
    from skyn3t.core.model_router import _TIERS

    load_catalog()
    rows: List[Dict[str, Any]] = []
    for tier_name, (backend, model) in _TIERS.items():
        if backend != "openrouter" or not model:
            continue
        exists = model_exists(model)
        fallback = None if exists else find_best_fallback(tier_name, model)
        rows.append(
            {
                "tier": tier_name,
                "model": model,
                "exists": exists,
                "fallback": fallback,
            }
        )
    return rows


def schedule_background_sync() -> None:
    """Non-blocking startup sync plus daily refresh when enabled."""
    global _background_task
    if not is_sync_enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _background_task is not None and not _background_task.done():
        return
    _background_task = loop.create_task(_background_sync_loop())


async def _background_sync_loop() -> None:
    try:
        result = await sync_catalog(force=False)
        logger.info(
            "openrouter background sync: status=%s count=%s",
            result.get("status"),
            result.get("count"),
        )
        validate_tier_models()
    except Exception:
        logger.debug("openrouter initial background sync failed", exc_info=True)
    while True:
        try:
            await asyncio.sleep(DEFAULT_TTL_SECONDS)
            if not is_sync_enabled():
                continue
            result = await sync_catalog(force=True)
            logger.info(
                "openrouter periodic sync: status=%s count=%s",
                result.get("status"),
                result.get("count"),
            )
            validate_tier_models()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.debug("openrouter periodic sync failed", exc_info=True)
