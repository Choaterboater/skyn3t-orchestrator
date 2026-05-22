from __future__ import annotations

import json

import pytest

from skyn3t.config.model_routing import ModelRoutingStore
from skyn3t.core.model_router import describe_stage_route, tier_for_stage


@pytest.fixture(autouse=True)
def _isolated_routing_policy(monkeypatch, tmp_path):
    import skyn3t.config.model_routing as routing_config

    store = ModelRoutingStore(tmp_path / "model_routing.json")
    monkeypatch.setattr(routing_config, "_store", store)
    monkeypatch.delenv("SKYN3T_MODEL_ROUTING", raising=False)
    yield store
    monkeypatch.setattr(routing_config, "_store", None)


def test_persisted_policy_overrides_env_file(monkeypatch, tmp_path, _isolated_routing_policy):
    env_path = tmp_path / "routing-env.json"
    env_path.write_text(json.dumps({"reviewer": "balanced"}), encoding="utf-8")
    monkeypatch.setenv("SKYN3T_MODEL_ROUTING", str(env_path))

    _isolated_routing_policy.set_many({"reviewer": "or_cheap"})

    route = describe_stage_route("reviewer")

    assert tier_for_stage("reviewer") == "or_cheap"
    assert route["backend"] == "openrouter"
    assert route["model"] == "openrouter/owl-alpha"
    assert route["source"] == "persisted"


def test_delete_persisted_policy_reveals_default(_isolated_routing_policy):
    _isolated_routing_policy.set_many({"reviewer": "balanced"})
    assert describe_stage_route("reviewer")["source"] == "persisted"

    _isolated_routing_policy.delete("reviewer")
    route = describe_stage_route("reviewer")

    assert route["tier"] == "or_strong"
    assert route["backend"] == "openrouter"
    assert route["model"] == "xiaomi/mimo-v2.5-pro"
    assert route["source"] == "default"


def test_persisted_policy_surfaces_recommendation_provenance(_isolated_routing_policy):
    _isolated_routing_policy.set_entries(
        {"reviewer": {"tier": "or_cheap", "applied_via": "recommendation"}}
    )

    route = describe_stage_route("reviewer")

    assert route["tier"] == "or_cheap"
    assert route["source"] == "persisted"
    assert route["persisted_via"] == "recommendation"
    assert _isolated_routing_policy.entries()["reviewer"]["applied_via"] == "recommendation"
