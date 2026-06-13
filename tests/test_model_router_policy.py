from __future__ import annotations

import json

import pytest

from skyn3t.config.model_routing import ModelRoutingStore
from skyn3t.core.model_router import describe_stage_route, has_stage_policy, tier_for_stage
from skyn3t.studio.templates import TEMPLATES


@pytest.fixture(autouse=True)
def _isolated_routing_policy(monkeypatch, tmp_path):
    import skyn3t.config.model_routing as routing_config
    from skyn3t.core import openrouter_catalog as catalog

    store = ModelRoutingStore(tmp_path / "model_routing.json")
    monkeypatch.setattr(routing_config, "_store", store)
    monkeypatch.delenv("SKYN3T_MODEL_ROUTING", raising=False)
    monkeypatch.delenv("SKYN3T_CODE_TIER", raising=False)
    monkeypatch.setenv("SKYN3T_CHEAP_SMART", "0")
    catalog._catalog_index = None
    catalog._catalog_loaded_at = 0.0
    monkeypatch.setattr(
        catalog,
        "resolve_openrouter_model",
        lambda _tier, model: model,
    )
    yield store
    monkeypatch.setattr(routing_config, "_store", None)
    catalog._catalog_index = None
    catalog._catalog_loaded_at = 0.0


def test_force_claude_cli_fails_over_openrouter_tiers(monkeypatch, _isolated_routing_policy):
    """SKYN3T_LLM_FORCE_CLAUDE_CLI reroutes OpenRouter tiers to claude_cli.

    Lets the system keep working on the Claude Code subscription when the
    OpenRouter key is exhausted (HTTP 403 "Key limit exceeded").
    """
    _isolated_routing_policy.set_many({"code": "or_strong"})
    monkeypatch.delenv("SKYN3T_LLM_FORCE_CLAUDE_CLI", raising=False)
    assert describe_stage_route("code")["backend"] == "openrouter"

    monkeypatch.setenv("SKYN3T_LLM_FORCE_CLAUDE_CLI", "1")
    route = describe_stage_route("code")
    assert route["backend"] == "claude_cli"


def test_persisted_policy_overrides_env_file(monkeypatch, tmp_path, _isolated_routing_policy):
    env_path = tmp_path / "routing-env.json"
    env_path.write_text(json.dumps({"reviewer": "balanced"}), encoding="utf-8")
    monkeypatch.setenv("SKYN3T_MODEL_ROUTING", str(env_path))

    _isolated_routing_policy.set_many({"reviewer": "or_cheap"})

    route = describe_stage_route("reviewer")

    assert tier_for_stage("reviewer") == "or_cheap"
    assert route["backend"] == "openrouter"
    assert route["model"] == "google/gemini-3.1-flash-lite"
    assert route["source"] == "persisted"


def test_delete_persisted_policy_reveals_default(_isolated_routing_policy):
    _isolated_routing_policy.set_many({"reviewer": "balanced"})
    assert describe_stage_route("reviewer")["source"] == "persisted"

    _isolated_routing_policy.delete("reviewer")
    route = describe_stage_route("reviewer")

    # Default reviewer policy rides the Claude subscription (strong tier);
    # production deployments without Claude remap it via the persisted store.
    assert route["tier"] == "strong"
    assert route["backend"] == "claude_cli"
    assert route["model"] == "opus"
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


def test_code_stage_defaults_to_strong_route():
    route = describe_stage_route("code")

    assert tier_for_stage("code") == "or_strong"
    assert route["backend"] == "openrouter"
    assert route["model"] == "qwen/qwen3-coder-plus"
    assert route["source"] == "default"


def test_code_tier_env_overrides_code_stages(monkeypatch):
    monkeypatch.setenv("SKYN3T_CODE_TIER", "or_backend")

    code_route = describe_stage_route("code")
    agent_route = describe_stage_route("code_agent")

    assert tier_for_stage("code") == "or_backend"
    assert code_route["backend"] == "openrouter"
    assert code_route["model"] == "qwen/qwen3-coder-next"
    assert code_route["source"] == "env_code_tier"
    assert agent_route["source"] == "env_code_tier"
    # Non-code stages stay on their defaults.
    assert tier_for_stage("reviewer") == "strong"


def test_code_tier_env_overrides_per_file_routing(monkeypatch):
    from skyn3t.core.model_router import resolve_model_for_file

    monkeypatch.setenv("SKYN3T_CODE_TIER", "or_cheap")

    backend, model = resolve_model_for_file("src/components/Dashboard.jsx")
    assert backend == "openrouter"
    assert model == "google/gemini-3.1-flash-lite"


def test_model_routing_file_beats_code_tier_env(monkeypatch, tmp_path):
    env_path = tmp_path / "routing-env.json"
    env_path.write_text(json.dumps({"code": "or_strong"}), encoding="utf-8")
    monkeypatch.setenv("SKYN3T_MODEL_ROUTING", str(env_path))
    monkeypatch.setenv("SKYN3T_CODE_TIER", "or_cheap")

    route = describe_stage_route("code")

    assert route["tier"] == "or_strong"
    assert route["source"] == "env"


def test_architecture_stage_defaults_to_strong_route():
    route = describe_stage_route("architecture")

    # Architecture defaults to the Claude subscription (strong tier).
    assert tier_for_stage("architecture") == "strong"
    assert route["backend"] == "claude_cli"
    assert route["model"] == "opus"
    assert route["source"] == "default"


def test_consistency_reviewer_defaults_to_strong_route():
    route = describe_stage_route("consistency_reviewer")

    assert tier_for_stage("consistency_reviewer") == "or_strong"
    assert route["backend"] == "openrouter"
    assert route["model"] == "qwen/qwen3-coder-plus"
    assert route["source"] == "default"


def test_all_known_studio_stages_have_explicit_policy():
    template_stages = {
        stage.name
        for template in TEMPLATES
        for stage in template.stages
    }
    supplemental_stages = {
        "build_verifier",
        "boot_verifier",
        "integration_verifier",
        "contract_verifier",
        "packaging_agent",
        "consistency_reviewer",
    }

    missing = sorted(
        stage_name
        for stage_name in template_stages | supplemental_stages
        if not has_stage_policy(stage_name)
    )

    assert missing == []
