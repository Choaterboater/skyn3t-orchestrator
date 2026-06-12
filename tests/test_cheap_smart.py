from __future__ import annotations

import json

import pytest

from skyn3t.config.model_routing import ModelRoutingStore
from skyn3t.core.model_router import (
    describe_stage_route,
    escalate_tier,
    resolve_model_for_file,
    tier_for_stage,
)
from skyn3t.intelligence import cheap_smart as cs


@pytest.fixture(autouse=True)
def _isolated_cheap_smart(monkeypatch, tmp_path):
    import skyn3t.config.model_routing as routing_config
    from skyn3t.core import openrouter_catalog as catalog

    store = ModelRoutingStore(tmp_path / "model_routing.json")
    monkeypatch.setattr(routing_config, "_store", store)
    monkeypatch.delenv("SKYN3T_MODEL_ROUTING", raising=False)
    monkeypatch.delenv("SKYN3T_CODE_TIER", raising=False)
    catalog._catalog_index = None
    catalog._catalog_loaded_at = 0.0
    monkeypatch.setattr(
        catalog,
        "resolve_openrouter_model",
        lambda _tier, model: model,
    )
    cs.clear_project_context()
    yield store
    cs.clear_project_context()
    monkeypatch.setattr(routing_config, "_store", None)
    catalog._catalog_index = None
    catalog._catalog_loaded_at = 0.0


def test_cheap_smart_enabled_by_default(monkeypatch):
    monkeypatch.delenv("SKYN3T_CHEAP_SMART", raising=False)
    assert cs.cheap_smart_enabled() is True


def test_cheap_smart_disabled_with_zero(monkeypatch):
    monkeypatch.setenv("SKYN3T_CHEAP_SMART", "0")
    assert cs.cheap_smart_enabled() is False


def test_code_stage_routes_cheap_when_cheap_smart_on(monkeypatch):
    # Cheap-first code routing is now gated by BOTH flags.
    monkeypatch.setenv("SKYN3T_CHEAP_SMART", "1")
    monkeypatch.setenv("SKYN3T_CHEAP_FIRST_CODE", "1")

    route = describe_stage_route("code")

    assert route["tier"] == "or_cheap"
    assert route["source"] == "cheap_smart"
    assert tier_for_stage("code") == "or_cheap"


def test_code_stage_stays_strong_when_cheap_smart_off(monkeypatch):
    monkeypatch.setenv("SKYN3T_CHEAP_SMART", "0")

    route = describe_stage_route("code")

    assert route["tier"] == "or_strong"
    assert route["source"] == "default"


def test_runtime_escalation_overrides_cheap_smart(monkeypatch):
    monkeypatch.setenv("SKYN3T_CHEAP_SMART", "1")
    cs.set_project_context("demo-project")
    cs.escalate_stage("code", project_slug="demo-project", reason="test")

    route = describe_stage_route("code")

    assert route["tier"] == "or_strong"
    assert route["source"] == "escalation"


def test_escalate_tier_mapping():
    assert escalate_tier("or_cheap") == "or_strong"
    assert escalate_tier("or_ui") == "or_strong"
    assert escalate_tier("or_backend") == "or_strong"


def test_resolve_model_for_file_escalates_on_retry(monkeypatch):
    monkeypatch.setenv("SKYN3T_CHEAP_SMART", "1")

    cheap_backend, cheap_model = resolve_model_for_file("src/components/Card.jsx")
    strong_backend, strong_model = resolve_model_for_file(
        "src/components/Card.jsx",
        escalate=True,
    )

    assert cheap_backend == "openrouter"
    assert strong_backend == "openrouter"
    assert cheap_model != strong_model or "pro" in (strong_model or "").lower()


def test_build_cheap_context_boost_includes_shape_and_checklist(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYN3T_CHEAP_SMART", "1")
    prefs = {
        "react-vite": {
            "shape": ["src/App.jsx", "src/components/Dashboard.jsx"],
            "winner_success_rate": 0.85,
        }
    }
    prefs_path = tmp_path / "build_pattern_preferences.json"
    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
    monkeypatch.setattr(cs, "_build_pattern_prefs_path", lambda: prefs_path)

    block = cs.build_cheap_context_boost(
        brief="Build a homelab dashboard",
        stack="react-vite",
        rel_path="src/components/Card.jsx",
    )

    assert "Cheap-smart execution checklist" in block
    assert "src/App.jsx" in block
    assert "UI file bar" in block


def test_auto_apply_cheaper_routing(monkeypatch, _isolated_cheap_smart):
    monkeypatch.setenv("SKYN3T_CHEAP_SMART", "1")

    def fake_recommendations():
        return [
            {
                "stage": "brainstorm",
                "current_tier": "or_strong",
                "current_backend": "openrouter",
                "recommended_tier": "or_cheap",
                "recommendation_kind": "cheaper",
                "confidence": "high",
                "applyable": True,
            }
        ]

    monkeypatch.setattr(
        "skyn3t.intelligence.routing_recommendations.list_stage_recommendations",
        fake_recommendations,
    )
    monkeypatch.setattr(
        "skyn3t.core.model_router.tier_details",
        lambda tier: {
            "or_cheap": ("openrouter", "openrouter/owl-alpha"),
            "or_strong": ("openrouter", "xiaomi/mimo-v2.5-pro"),
        }[tier],
    )
    monkeypatch.setattr(
        "skyn3t.core.model_router.relative_backend_cost",
        lambda backend: 0.5 if backend == "openrouter" else 1.0,
    )

    applied = cs.auto_apply_cheaper_routing()

    assert applied == [{"stage": "brainstorm", "tier": "or_cheap", "kind": "cheaper"}]
    assert _isolated_cheap_smart.entries()["brainstorm"]["tier"] == "or_cheap"
    assert _isolated_cheap_smart.entries()["brainstorm"]["applied_via"] == "recommendation"


def test_pick_best_model_for_task_prefers_specialist(monkeypatch, tmp_path):
    from skyn3t.core import openrouter_catalog as catalog

    catalog._catalog_index = None
    models = [
        {
            "id": "openrouter/owl-alpha",
            "name": "Owl Alpha",
            "description": "cheap general",
            "context_length": 8192,
            "pricing": {"prompt": "0"},
        },
        {
            "id": "qwen/qwen3-coder",
            "name": "Qwen3 Coder",
            "description": "backend code specialist",
            "context_length": 64000,
            "pricing": {"prompt": "0"},
        },
    ]
    payload = {"synced_at": 1.0, "ttl_seconds": 86400, "models": models}
    path = tmp_path / catalog.CACHE_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()
    catalog.load_catalog()

    picked = catalog.pick_best_model_for_task("or_backend", "backend")

    assert picked == "qwen/qwen3-coder"
