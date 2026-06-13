from __future__ import annotations

import json
import time
from typing import Any, Dict, List

import pytest

from skyn3t.core import model_evolution as evolution
from skyn3t.core import openrouter_catalog as catalog
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.model_router import describe_stage_route


@pytest.fixture(autouse=True)
def _isolated_evolution(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SKYN3T_MODEL_EVOLUTION", raising=False)
    monkeypatch.delenv("SKYN3T_MODEL_EVOLUTION_DOWNGRADE", raising=False)
    monkeypatch.delenv("SKYN3T_MODEL_EVOLUTION_ALLOW_PREMIUM", raising=False)
    monkeypatch.delenv("SKYN3T_CHEAP_SMART", raising=False)
    monkeypatch.setenv("SKYN3T_CHEAP_SMART", "0")
    catalog._catalog_index = None
    catalog._catalog_loaded_at = 0.0
    evolution._invalidate_overrides_cache()
    evolution.set_evolution_event_bus(None)
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    catalog._catalog_index = None
    catalog._catalog_loaded_at = 0.0
    evolution._invalidate_overrides_cache()
    evolution.set_evolution_event_bus(None)


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
            "id": "openrouter/owl-beta-v2",
            "name": "Owl Beta v2",
            "description": "newer cheap free coding model",
            "context_length": 128000,
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["temperature", "tools", "max_tokens"],
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
            "supported_parameters": ["temperature", "tools"],
            "architecture": {"modality": "text"},
        },
        {
            "id": "xiaomi/mimo-v2.5-pro",
            "name": "MiMo Pro",
            "description": "strong reasoning",
            "context_length": 128000,
            "pricing": {"prompt": "0.001", "completion": "0.002"},
            "supported_parameters": ["temperature", "tools"],
            "architecture": {"modality": "text"},
        },
    ]


def _write_catalog(tmp_path, models: List[Dict[str, Any]]) -> None:
    payload = {
        "synced_at": time.time(),
        "ttl_seconds": catalog.EVOLUTION_TTL_SECONDS,
        "models": models,
    }
    (tmp_path / catalog.CACHE_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    catalog.load_catalog()


def test_is_evolution_enabled_defaults_with_api_key(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.delenv("SKYN3T_MODEL_EVOLUTION", raising=False)
    assert evolution.is_evolution_enabled(SimpleNamespace(openrouter_api_key=None)) is False
    assert evolution.is_evolution_enabled(SimpleNamespace(openrouter_api_key="sk-or")) is True

    monkeypatch.setenv("SKYN3T_MODEL_EVOLUTION", "0")
    assert evolution.is_evolution_enabled(SimpleNamespace(openrouter_api_key="sk-or")) is False


def test_catalog_ttl_shorter_when_evolution_enabled(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.delenv("SKYN3T_MODEL_EVOLUTION", raising=False)
    assert catalog.catalog_ttl_seconds(SimpleNamespace(openrouter_api_key=None)) == catalog.DEFAULT_TTL_SECONDS
    assert (
        catalog.catalog_ttl_seconds(SimpleNamespace(openrouter_api_key="sk-or"))
        == evolution.EVOLUTION_TTL_SECONDS
    )


def test_run_evolution_upgrades_cheap_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _write_catalog(tmp_path, _sample_models())

    result = evolution.run_evolution()

    assert result["enabled"] is True
    upgrades = result["upgrades"]
    cheap_upgrade = next((u for u in upgrades if u["tier"] == "or_cheap"), None)
    assert cheap_upgrade is not None
    assert cheap_upgrade["to"] == "openrouter/owl-beta-v2"

    overrides = evolution.load_overrides(max_age=0.0)
    assert overrides["tiers"]["or_cheap"]["model"] == "openrouter/owl-beta-v2"


def test_run_evolution_skips_operator_locked_override(tmp_path, monkeypatch):
    """A dashboard-pinned (locked) tier model must survive automatic evolution.

    Without the lock, evolution would upgrade or_cheap to owl-beta-v2 (see the
    test above). With locked=True the operator's choice is preserved.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _write_catalog(tmp_path, _sample_models())
    evolution.save_overrides(
        {"tiers": {"or_cheap": {"model": "xiaomi/mimo-v2-flash", "locked": True}}}
    )

    result = evolution.run_evolution()

    assert not any(u["tier"] == "or_cheap" for u in result["upgrades"])
    overrides = evolution.load_overrides(max_age=0.0)
    assert overrides["tiers"]["or_cheap"]["model"] == "xiaomi/mimo-v2-flash"
    assert overrides["tiers"]["or_cheap"]["locked"] is True


def test_run_evolution_does_not_downgrade_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _write_catalog(tmp_path, _sample_models())

    evolution.save_overrides(
        {
            "tiers": {
                "or_cheap": {
                    "model": "openrouter/owl-ultra-2026",
                    "score": 99.0,
                    "evolved_at": time.time(),
                }
            }
        }
    )

    result = evolution.run_evolution()
    cheap_changes = [u for u in result["upgrades"] if u["tier"] == "or_cheap"]
    assert cheap_changes == []
    assert evolution.tier_override_model("or_cheap") == "openrouter/owl-ultra-2026"


def test_run_evolution_publishes_alert(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _write_catalog(tmp_path, _sample_models())

    bus = EventBus()
    captured = []

    def _capture(event):
        captured.append(event)

    bus.subscribe(_capture, EventType.SYSTEM_ALERT)

    evolution.run_evolution(event_bus=bus)

    assert captured
    alert = captured[0]
    assert alert.payload.get("alert_type") == "MODEL_TIER_EVOLVED"
    assert alert.payload.get("count") >= 1


def test_run_evolution_skips_premium_opus_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    models = _sample_models() + [
        {
            "id": "anthropic/claude-opus-4.1",
            "name": "Claude Opus 4.1",
            "description": "premium reasoning opus model",
            "context_length": 200000,
            "pricing": {"prompt": "0.0001", "completion": "0.0001"},
            "supported_parameters": ["temperature", "tools"],
            "architecture": {"modality": "text"},
        }
    ]
    _write_catalog(tmp_path, models)

    result = evolution.run_evolution()

    assert result["tiers"]["or_strong"]["best"] != "anthropic/claude-opus-4.1"
    assert evolution.tier_override_model("or_strong") != "anthropic/claude-opus-4.1"


def test_run_evolution_can_opt_into_premium_models(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("SKYN3T_MODEL_EVOLUTION_ALLOW_PREMIUM", "1")
    models = [
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
            "id": "anthropic/claude-opus-4.1",
            "name": "Claude Opus 4.1",
            "description": "premium reasoning opus model",
            "context_length": 200000,
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["temperature", "tools"],
            "architecture": {"modality": "text"},
        }
    ]
    _write_catalog(tmp_path, models)

    result = evolution.run_evolution()

    assert result["tiers"]["or_strong"]["best"] == "anthropic/claude-opus-4.1"


def test_model_router_uses_evolved_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _write_catalog(tmp_path, _sample_models())
    evolution.save_overrides(
        {
            "tiers": {
                "or_cheap": {
                    "model": "openrouter/owl-beta-v2",
                    "score": 10.0,
                    "evolved_at": time.time(),
                }
            }
        }
    )

    route = describe_stage_route("brainstorm")

    assert route["tier"] == "or_cheap"
    assert route["model"] == "openrouter/owl-beta-v2"


def test_pick_best_model_for_task_respects_base_model(tmp_path):
    _write_catalog(tmp_path, _sample_models())

    picked = catalog.pick_best_model_for_task(
        "or_ui",
        "ui",
        base_model="xiaomi/mimo-v2-flash",
        prefer_evolution=True,
    )
    assert picked is None


def test_evolution_status_payload_for_api(monkeypatch, tmp_path):
    """Shape consumed by GET /api/models/openrouter ``evolution`` block."""
    from types import SimpleNamespace

    from skyn3t.config.settings import get_settings

    _write_catalog(tmp_path, _sample_models())
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    get_settings.cache_clear()
    settings = SimpleNamespace(openrouter_api_key="sk-or-test", data_dir=str(tmp_path))
    result = evolution.run_evolution(settings=settings)
    assert result["enabled"] is True

    status = evolution.evolution_status(settings=settings)
    assert status["enabled"] is True
    assert status["ttl_seconds"] == evolution.EVOLUTION_TTL_SECONDS
    assert "tiers" in status
    assert status["tiers"]["or_cheap"]["model"] == "openrouter/owl-beta-v2"


def test_recency_bonus_prefers_newer_family_versions():
    """Family point-releases must rank: qwen3.7 > qwen3.6 > qwen3(-coder),
    and deepseek-v4 > deepseek-v3.2 — none of which the old year/vN-only
    parser could distinguish."""
    assert evolution._recency_bonus("qwen/qwen3.7-plus") > evolution._recency_bonus(
        "qwen/qwen3.6-flash"
    )
    assert evolution._recency_bonus("qwen/qwen3.6-flash") > evolution._recency_bonus(
        "qwen/qwen3-coder-plus"
    )
    assert evolution._recency_bonus("deepseek/deepseek-v4-flash") > evolution._recency_bonus(
        "deepseek/deepseek-v3.2"
    )


def test_score_does_not_let_name_keywords_dominate():
    """An old coder-named model that is slightly cheaper must NOT out-score a
    newer GENERAL model that is meaningfully cheaper. Before the fix the
    coder/code/qwen keyword stack (+6) buried recency + cost."""
    old_coder = {
        "id": "qwen/qwen3-coder-plus",
        "name": "Qwen3 Coder Plus",
        "description": "qwen coder code specialist backend dev",
        "context_length": 256_000,
        "pricing": {"prompt": "0.00000065", "completion": "0.00000325"},
        "supported_parameters": ["temperature", "tools"],
    }
    newer_cheaper = {
        "id": "qwen/qwen3.7-plus",
        "name": "Qwen3.7 Plus",
        "description": "qwen newer general model",
        "context_length": 256_000,
        "pricing": {"prompt": "0.00000032", "completion": "0.00000128"},
        "supported_parameters": ["temperature", "tools"],
    }
    for tier in ("or_strong", "or_backend"):
        old_score = evolution.score_model_for_tier(tier, old_coder["id"], old_coder)
        new_score = evolution.score_model_for_tier(tier, newer_cheaper["id"], newer_cheaper)
        assert new_score >= old_score, (
            f"{tier}: newer cheaper {new_score:.3f} should be >= old coder {old_score:.3f}"
        )


def test_find_best_for_tier_picks_newer_cheaper_over_old_coder(tmp_path):
    """Catalog with both a qwen3-coder-plus-like and a qwen3.7-plus-like entry:
    the backend tier must pick the newer, cheaper qwen3.7-plus."""
    models = [
        {
            "id": "qwen/qwen3-coder-plus",
            "name": "Qwen3 Coder Plus",
            "description": "qwen coder code backend dev specialist",
            "context_length": 1_000_000,
            "pricing": {"prompt": "0.00000065", "completion": "0.00000325"},
            "supported_parameters": ["temperature", "tools"],
            "architecture": {"modality": "text"},
        },
        {
            "id": "qwen/qwen3.7-plus",
            "name": "Qwen3.7 Plus",
            "description": "qwen newer general flagship",
            "context_length": 1_000_000,
            "pricing": {"prompt": "0.00000032", "completion": "0.00000128"},
            "supported_parameters": ["temperature", "tools"],
            "architecture": {"modality": "text"},
        },
    ]
    _write_catalog(tmp_path, models)
    index = {m["id"]: m for m in models}
    best_id, _ = evolution.find_best_for_tier("or_backend", index)
    assert best_id == "qwen/qwen3.7-plus"


@pytest.mark.asyncio
async def test_sync_catalog_triggers_evolution(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    models = _sample_models()

    async def fake_fetch(**kwargs):
        return models

    monkeypatch.setattr(catalog, "fetch_models_from_api", fake_fetch)
    await catalog.sync_catalog(force=True)

    assert evolution.tier_override_model("or_cheap") == "openrouter/owl-beta-v2"
