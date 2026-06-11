from __future__ import annotations

import json
import time
from typing import Any, Dict, List

import pytest

from skyn3t.core import openrouter_catalog as catalog


@pytest.fixture(autouse=True)
def _isolated_catalog(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SKYN3T_OPENROUTER_SYNC", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    catalog._catalog_index = None
    catalog._catalog_loaded_at = 0.0
    catalog._background_task = None
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    catalog._catalog_index = None
    catalog._catalog_loaded_at = 0.0
    catalog._background_task = None


def _sample_models() -> List[Dict[str, Any]]:
    return [
        {
            "id": "openrouter/owl-alpha",
            "name": "Owl Alpha",
            "description": "cheap coding model",
            "context_length": 8192,
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["temperature", "max_tokens"],
            "architecture": {"modality": "text"},
        },
        {
            "id": "xiaomi/mimo-v2-flash",
            "name": "MiMo Flash",
            "description": "fast ui generation",
            "context_length": 32000,
            "pricing": {"prompt": "0.0001", "completion": "0.0002"},
            "supported_parameters": ["temperature"],
            "architecture": {"modality": "text"},
        },
        {
            "id": "qwen/qwen3-coder",
            "name": "Qwen3 Coder",
            "description": "backend code specialist",
            "context_length": 64000,
            "pricing": {"prompt": "0.0002", "completion": "0.0003"},
            "supported_parameters": ["temperature", "max_tokens"],
            "architecture": {"modality": "text"},
        },
    ]


def _write_cache(tmp_path, models: List[Dict[str, Any]], *, synced_at: float | None = None) -> None:
    payload = {
        "synced_at": synced_at if synced_at is not None else time.time(),
        "ttl_seconds": catalog.DEFAULT_TTL_SECONDS,
        "models": models,
    }
    path = tmp_path / catalog.CACHE_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_sync_catalog_fetches_and_writes_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    models = _sample_models()

    async def fake_fetch(**kwargs):
        return models

    monkeypatch.setattr(catalog, "fetch_models_from_api", fake_fetch)
    result = await catalog.sync_catalog(force=True)

    assert result["status"] == "synced"
    assert result["count"] == 3
    cache_path = tmp_path / catalog.CACHE_FILENAME
    assert cache_path.exists()
    snap = catalog.load_catalog()
    assert len(snap.models) == 3
    assert snap.models[0]["id"] == "openrouter/owl-alpha"


@pytest.mark.asyncio
async def test_sync_catalog_uses_fresh_cache_without_network(monkeypatch, tmp_path):
    _write_cache(tmp_path, _sample_models())
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    async def fail_fetch(**kwargs):
        raise AssertionError("network should not be called for fresh cache")

    monkeypatch.setattr(catalog, "fetch_models_from_api", fail_fetch)
    result = await catalog.sync_catalog(force=False)

    assert result["status"] == "fresh"
    assert result["count"] == 3


@pytest.mark.asyncio
async def test_sync_catalog_falls_back_to_stale_cache_on_network_error(monkeypatch, tmp_path):
    stale_at = time.time() - catalog.DEFAULT_TTL_SECONDS - 10
    _write_cache(tmp_path, _sample_models(), synced_at=stale_at)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    async def fail_fetch(**kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(catalog, "fetch_models_from_api", fail_fetch)
    result = await catalog.sync_catalog(force=False)

    assert result["status"] == "cache_fallback"
    assert result["count"] == 3
    assert "offline" in str(result.get("error"))


def test_is_sync_enabled_defaults_with_api_key(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.delenv("SKYN3T_OPENROUTER_SYNC", raising=False)

    no_key = SimpleNamespace(openrouter_api_key=None)
    assert catalog.is_sync_enabled(no_key) is False

    with_key = SimpleNamespace(openrouter_api_key="sk-or-test")
    assert catalog.is_sync_enabled(with_key) is True

    monkeypatch.setenv("SKYN3T_OPENROUTER_SYNC", "0")
    assert catalog.is_sync_enabled(with_key) is False

    monkeypatch.setenv("SKYN3T_OPENROUTER_SYNC", "1")
    assert catalog.is_sync_enabled(no_key) is True


def test_resolve_openrouter_model_falls_back_when_missing(tmp_path):
    _write_cache(tmp_path, _sample_models())
    catalog.load_catalog()

    resolved = catalog.resolve_openrouter_model("or_backend", "deprecated/model")
    assert resolved == "qwen/qwen3-coder"


def test_resolve_openrouter_model_keeps_existing_id(tmp_path):
    _write_cache(tmp_path, _sample_models())
    catalog.load_catalog()

    assert catalog.resolve_openrouter_model("or_cheap", "openrouter/owl-alpha") == "openrouter/owl-alpha"


def test_validate_tier_models_reports_missing(tmp_path):
    _write_cache(
        tmp_path,
        [m for m in _sample_models() if m["id"] != "xiaomi/mimo-v2.5-pro"],
    )
    catalog.load_catalog()
    rows = catalog.validate_tier_models()
    strong = next(r for r in rows if r["tier"] == "or_strong")
    assert strong["exists"] is False
    assert strong["fallback"] is not None


@pytest.mark.asyncio
async def test_openrouter_models_api_endpoint(monkeypatch, tmp_path):
    _write_cache(tmp_path, _sample_models())
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    from skyn3t.web.app import openrouter_models

    body = await openrouter_models(refresh=False)
    assert body["count"] == 3
    assert body["sync_enabled"] is True
    assert isinstance(body["tier_validation"], list)
