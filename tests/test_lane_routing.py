"""Lane-aware routing: autonomous drills run FREE, real projects route normally.

The model_router consults a per-build lane contextvar set by the Studio runner.
In free-only mode, autonomous (throwaway drill) builds are forced onto a free
OpenRouter tier above all paid/persisted routing; with a funded key they use the
normal cheap paid ladder (capped by the daily autonomous budget). Real builds
always fall through to the normal ladder.
"""

from skyn3t.core.model_router import _TIERS, _route_for_stage
from skyn3t.intelligence import cheap_smart


def test_autonomous_lane_forces_free_tier(monkeypatch):
    # Autonomous forces FREE only in free-only mode (the $0-key policy).
    monkeypatch.setenv("SKYN3T_FREE_ONLY", "1")
    cheap_smart.set_lane_context(True)
    try:
        route = _route_for_stage("code")
    finally:
        cheap_smart.clear_project_context()
    assert route["source"] == "lane_a_free"
    assert route["backend"] == "openrouter"
    assert route["tier"] in _TIERS
    # The default free tier resolves to a :free catalog model.
    assert ":free" in str(route["model"])


def test_autonomous_lane_uses_paid_ladder_when_funded(monkeypatch):
    # With a funded key (free-only OFF), autonomous drills use the normal cheap
    # paid ladder instead of the rate-limited free tier — capped by the daily
    # autonomous budget — so they actually complete instead of 429-walling.
    monkeypatch.delenv("SKYN3T_FREE_ONLY", raising=False)
    cheap_smart.set_lane_context(True)
    try:
        route = _route_for_stage("code")
    finally:
        cheap_smart.clear_project_context()
    assert route["source"] != "lane_a_free"


def test_real_lane_skips_the_free_branch():
    cheap_smart.set_lane_context(False)
    try:
        route = _route_for_stage("code")
    finally:
        cheap_smart.clear_project_context()
    assert route["source"] != "lane_a_free"


def test_lane_defaults_to_real_when_unset():
    cheap_smart.clear_project_context()
    assert cheap_smart.current_lane() == "real"


def test_lane_a_free_tier_env_override(monkeypatch):
    monkeypatch.setenv("SKYN3T_LANE_A_FREE_TIERS", '{"code": "or_docs", "default": "or_docs"}')
    assert cheap_smart.lane_a_free_tier("code") == "or_docs"
    assert cheap_smart.lane_a_free_tier("whatever") == "or_docs"


def test_lane_a_free_tier_falls_back_to_or_docs():
    assert cheap_smart.lane_a_free_tier("code") == "or_docs"


def test_no_claude_guard_rewrites_claude_tiers(monkeypatch):
    monkeypatch.setenv("SKYN3T_NO_CLAUDE", "1")
    from skyn3t.core.model_router import _tier_backend_model

    for tier in ("balanced", "strong", "max"):
        backend, _model = _tier_backend_model(tier)
        assert backend == "openrouter", f"{tier} leaked to {backend}"


def test_reasoning_stage_defaults_avoid_claude(monkeypatch):
    monkeypatch.setenv("SKYN3T_NO_CLAUDE", "1")
    from skyn3t.core.model_router import _tier_backend_model, default_tier_for_stage

    for stage in ("planner", "architect", "reviewer"):
        tier = default_tier_for_stage(stage)
        backend, _model = _tier_backend_model(tier)
        assert backend == "openrouter", f"{stage} -> {tier} -> {backend}"


def test_llm_client_coerces_claude_backend_to_openrouter(monkeypatch):
    """No path may spawn `claude -p`: even an explicit claude_cli backend (used
    by agent fallback chains) is coerced to OpenRouter under SKYN3T_NO_CLAUDE."""
    monkeypatch.setenv("SKYN3T_NO_CLAUDE", "1")
    from skyn3t.adapters.llm_client import LLMClient

    for backend in ("claude_cli", "anthropic", "auto"):
        client = LLMClient(backend=backend)
        assert client._backend_name == "openrouter", f"{backend} not coerced"


def test_llm_client_allows_claude_when_flag_off(monkeypatch):
    monkeypatch.delenv("SKYN3T_NO_CLAUDE", raising=False)
    from skyn3t.adapters.llm_client import LLMClient

    assert LLMClient(backend="claude_cli")._backend_name == "claude_cli"


def test_model_less_openrouter_defaults_to_free(monkeypatch):
    """A model-less OpenRouter client (the repair loop) must default to a FREE
    model so it doesn't hit the paid default and 403 on the over-limit key."""
    monkeypatch.setenv("SKYN3T_NO_CLAUDE", "1")
    from skyn3t.adapters.llm_client import LLMClient

    model = LLMClient(backend="openrouter").default_model
    if model:  # only assert when the catalog is present in this environment
        assert model.lower().endswith(":free")


def test_free_model_picker_excludes_claude_and_rotates():
    from skyn3t.core import openrouter_catalog as cat

    free = cat.list_free_models()
    if not free:
        return  # catalog not present in this environment
    assert all(m.lower().endswith(":free") for m in free)
    assert not any("claude" in m.lower() or m.lower().startswith("anthropic/") for m in free)
    if len(free) > 1:
        picks = {cat.pick_free_model() for _ in range(min(6, len(free) * 2))}
        assert len(picks) > 1, "free picker should rotate, not pin one model"
