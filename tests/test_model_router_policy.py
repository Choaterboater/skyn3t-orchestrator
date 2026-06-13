from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

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


# ── Reintroduction guard ────────────────────────────────────────────
#
# These three tests are a TRIPWIRE against the recurring regression where a
# tier model id drifts back to a PAID or NONEXISTENT model (e.g. or_cheap →
# google/gemini-3.1-flash-lite), which 403s on the free-only OpenRouter key
# and kills every build. They must FAIL if someone reintroduces such a pin.


# The repo's real on-disk catalog cache. The conftest autouse fixture points
# DATA_DIR at a tmp dir, so get_settings().data_dir does NOT find this file —
# we must reference the committed cache directly. tests/ -> repo root -> data/.
_REPO_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load_real_catalog_ids():
    """Load the committed on-disk catalog cache and return (snapshot, set_of_ids).

    Loads ``<repo>/data/openrouter_models.json`` directly (the conftest
    redirects DATA_DIR to a tmp dir, so get_settings() can't see the committed
    cache) and populates the in-memory index so the free-only path
    (``list_free_models`` / ``_force_free_model``) reads the real catalog.

    Returns ``(None, None)`` when the cache is empty/missing so callers can
    pytest.skip with a clear reason instead of asserting against an empty set.
    """
    from skyn3t.core import openrouter_catalog as catalog

    catalog._catalog_index = None
    catalog._catalog_loaded_at = 0.0
    # Point load_catalog at the committed cache regardless of the tmp DATA_DIR.
    snap = catalog.load_catalog(settings=SimpleNamespace(data_dir=str(_REPO_DATA_DIR)))
    if snap.source == "empty" or not snap.models:
        return None, None
    ids = {str(m.get("id")) for m in snap.models if m.get("id")}
    return snap, ids


def test_free_only_forces_every_tier_to_free(monkeypatch):
    """SKYN3T_FREE_ONLY=1 must force EVERY tier to a real ``:free`` model.

    Runtime free-only guarantee: when the OpenRouter key has no paid budget,
    no tier may resolve to a paid/non-free id — otherwise the build 403s on the
    first stage. Guards against a paid pin (e.g. google/gemini-3.1-flash-lite)
    silently surviving the free-only rewrite.
    """
    from skyn3t.core import model_router as mr

    monkeypatch.setenv("SKYN3T_FREE_ONLY", "1")
    monkeypatch.setenv("SKYN3T_NO_CLAUDE", "1")

    snap, _ids = _load_real_catalog_ids()
    if snap is None:
        pytest.skip(
            "on-disk OpenRouter catalog cache is empty/missing "
            f"({mr.__name__}); cannot validate free-only forcing"
        )
    from skyn3t.core.openrouter_catalog import list_free_models

    assert list_free_models(), "catalog loaded but has no :free models"

    assert mr._free_only_enabled() is True

    for tier in mr._TIERS:
        backend, model = mr._tier_backend_model(tier, task_kind="backend")
        # Every tier must resolve to OpenRouter (no Claude/CLI under NO_CLAUDE).
        assert backend == "openrouter", (
            f"tier {tier!r} resolved to backend {backend!r}, expected openrouter "
            "under SKYN3T_FREE_ONLY+SKYN3T_NO_CLAUDE"
        )
        assert model, f"tier {tier!r} resolved to an empty model under free-only"
        assert str(model).lower().endswith(":free"), (
            f"tier {tier!r} resolved to non-free model {model!r} under "
            "SKYN3T_FREE_ONLY=1 — a paid/nonexistent pin slipped through the guard"
        )


def test_static_free_fallback_ids_are_real_and_free():
    """Every _STATIC_FREE_FALLBACK value must be a real ``:free`` catalog id.

    This map is the last-resort FLOOR used when the live catalog is unreachable.
    If any entry drifts to a nonexistent or paid id, the floor silently stops
    being free and the build can 403 — so validate it against the real catalog.
    """
    from skyn3t.core import model_router as mr

    snap, ids = _load_real_catalog_ids()
    if snap is None:
        pytest.skip(
            "on-disk OpenRouter catalog cache is empty/missing; "
            "cannot validate _STATIC_FREE_FALLBACK against catalog"
        )

    assert mr._STATIC_FREE_FALLBACK, "_STATIC_FREE_FALLBACK is empty"
    for tier, model in mr._STATIC_FREE_FALLBACK.items():
        assert str(model).lower().endswith(":free"), (
            f"_STATIC_FREE_FALLBACK[{tier!r}] = {model!r} is not a :free id — "
            "the floor must never drift to a paid model"
        )
        assert model in ids, (
            f"_STATIC_FREE_FALLBACK[{tier!r}] = {model!r} is not present in the "
            "OpenRouter catalog — the floor points at a nonexistent id"
        )


def test_no_static_tier_pins_to_claude():
    """No static _TIERS entry may name a Claude model (NO_CLAUDE policy).

    Static config must never hardcode a Claude id (e.g. anthropic/claude-* or
    a model id containing 'claude'). Note: the legacy balanced/strong/max tiers
    legitimately use backend ``claude_cli`` with model 'sonnet'/'opus'/'fable'
    — those model NAMES do not contain 'claude', so this guard only fires on an
    actual Claude *model id* reintroduction.
    """
    from skyn3t.core import model_router as mr

    offenders = []
    for tier, (_backend, model) in mr._TIERS.items():
        if model is None:
            continue
        low = str(model).lower()
        if "claude" in low or low.startswith("anthropic/"):
            offenders.append((tier, model))

    assert offenders == [], (
        "static _TIERS pins a Claude model id (violates NO_CLAUDE policy): "
        f"{offenders!r}"
    )
