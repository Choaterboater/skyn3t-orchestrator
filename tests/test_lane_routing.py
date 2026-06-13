"""Lane-aware routing: autonomous drills run FREE, real projects route normally.

The model_router consults a per-build lane contextvar set by the Studio runner.
Autonomous (throwaway drill) builds are forced onto a free OpenRouter tier above
all paid/persisted routing; real builds fall through to the normal ladder.
"""

from skyn3t.core.model_router import _TIERS, _route_for_stage
from skyn3t.intelligence import cheap_smart


def test_autonomous_lane_forces_free_tier():
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
