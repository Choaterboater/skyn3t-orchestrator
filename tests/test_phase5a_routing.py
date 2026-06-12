"""Phase 5A — PredictiveRoutingSelector (M4_routing).

Covers:
* select_best_model auto-mode picks the winning CHEAP model from
  recorded observations + tournament rankings.
* flag-off (and prefer='static') returns the existing static route.
* graceful degrade to static when there is no observation/tournament
  evidence.
* best_model_for ranks (stack, stage, feature) cells and never forces
  an expensive backend.
* routing_observations persists optional stack/feature cells without
  breaking the existing stage-keyed snapshot() shape.

No real LLM calls — observations are seeded fakes and the tournament
store lives in a tmp dir.
"""

from __future__ import annotations

from skyn3t.core import model_router
from skyn3t.intelligence import routing_observations as obs
from skyn3t.intelligence import routing_recommendations as recs
from skyn3t.intelligence.model_tournament import ModelTournamentStore, ModelTrial

# ── fixtures / helpers ──────────────────────────────────────────────


def _seed_observations(monkeypatch, snapshot: dict) -> None:
    monkeypatch.setattr(
        "skyn3t.intelligence.routing_observations.snapshot",
        lambda: snapshot,
    )


def _seed_tournament(monkeypatch, tmp_path, trials):
    store = ModelTournamentStore(path=tmp_path / "tournament.json")
    for trial in trials:
        store.record_trial(trial)
    monkeypatch.setattr(
        "skyn3t.intelligence.model_tournament.get_default_tournament_store",
        lambda: store,
    )
    return store


def _cheap_vs_strong_snapshot() -> dict:
    """code stage where the CHEAP backend is winning on stack=node."""
    return {
        "code": {
            "total_tokens": 50_000,
            "trajectory_samples": 12,
            "mixed_route_samples": 0,
            "route_stats": {
                "or_cheap": {
                    "tier": "or_cheap",
                    "backend": "openrouter",
                    "model": "openrouter/owl-alpha",
                    "samples": 8,
                    "successes": 7,
                    "success_rate": 0.875,
                    "total_tokens": 20_000,
                    "cells": {
                        "node::": {
                            "stack": "node",
                            "feature": None,
                            "samples": 6,
                            "successes": 6,
                            "success_rate": 1.0,
                            "total_tokens": 12_000,
                        }
                    },
                },
                "strong": {
                    "tier": "strong",
                    "backend": "claude_cli",
                    "model": "opus",
                    "samples": 6,
                    "successes": 4,
                    "success_rate": 0.667,
                    "total_tokens": 30_000,
                    "cells": {
                        "node::": {
                            "stack": "node",
                            "feature": None,
                            "samples": 5,
                            "successes": 3,
                            "success_rate": 0.6,
                            "total_tokens": 18_000,
                        }
                    },
                },
            },
        }
    }


# ── select_best_model ───────────────────────────────────────────────


def test_auto_mode_picks_winning_cheap_model(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYN3T_AUTO_ROUTE", "1")
    _seed_observations(monkeypatch, _cheap_vs_strong_snapshot())
    _seed_tournament(
        monkeypatch,
        tmp_path,
        [
            ModelTrial(
                model_id="openrouter/owl-alpha",
                task_id="code-1",
                domain_tags=["code", "node"],
                vendor_tags=["openrouter"],
                score=90,
                cost_usd=0.0,
                passed=True,
            ),
        ],
    )

    choice = model_router.select_best_model("code", stack="node")

    assert isinstance(choice, model_router.RouteChoice)
    assert choice.source == "predictive"
    # Cheap OpenRouter backend beats the pricier claude_cli on win-rate
    # AND cost — exactly the loop we want to close.
    assert choice.backend == "openrouter"
    assert choice.model == "openrouter/owl-alpha"
    assert choice.tier == "or_cheap"
    assert choice.score > 0.0


def test_flag_off_returns_static_route(monkeypatch, tmp_path):
    # Auto-route NOT enabled — must behave exactly like resolve_model.
    monkeypatch.delenv("SKYN3T_AUTO_ROUTE", raising=False)
    # Even with rich observations, the flag-off path ignores them.
    _seed_observations(monkeypatch, _cheap_vs_strong_snapshot())

    choice = model_router.select_best_model("reviewer", prefer="auto")

    assert choice.source == "static"
    static_backend, static_model = model_router.resolve_model("reviewer")
    assert choice.backend == static_backend
    assert choice.model == static_model


def test_prefer_static_short_circuits_predictive(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYN3T_AUTO_ROUTE", "1")

    def _boom(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("best_model_for should not run for prefer='static'")

    monkeypatch.setattr(recs, "best_model_for", _boom)

    choice = model_router.select_best_model("code", prefer="static")

    assert choice.source == "static"


def test_auto_mode_degrades_to_static_without_evidence(monkeypatch):
    monkeypatch.setenv("SKYN3T_AUTO_ROUTE", "1")
    # No matching stage in observations → no evidence.
    _seed_observations(monkeypatch, {})

    choice = model_router.select_best_model("code", stack="node")

    assert choice.source == "static"
    static_backend, _ = model_router.resolve_model("code")
    assert choice.backend == static_backend
    assert "no evidence" in choice.rationale


def test_auto_mode_skips_low_sample_cells(monkeypatch):
    monkeypatch.setenv("SKYN3T_AUTO_ROUTE", "1")
    _seed_observations(
        monkeypatch,
        {
            "code": {
                "route_stats": {
                    "or_cheap": {
                        "tier": "or_cheap",
                        "backend": "openrouter",
                        "model": "openrouter/owl-alpha",
                        "samples": 1,  # below _AUTO_ROUTE_MIN_SAMPLES
                        "successes": 1,
                        "success_rate": 1.0,
                    }
                },
            }
        },
    )

    choice = model_router.select_best_model("code", stack="node")

    # Not enough evidence → static.
    assert choice.source == "static"


# ── best_model_for ──────────────────────────────────────────────────


def test_best_model_for_never_forces_expensive_on_tie(monkeypatch, tmp_path):
    # Two backends with identical win-rate but different cost. The cheaper
    # one must win — auto-route must never drift toward the pricier model.
    # Lock claude_cli to a high relative cost so the tie breaks to OpenRouter
    # regardless of the live cost table.
    monkeypatch.setenv("SKYN3T_ROUTER_BACKEND_COSTS", '{"claude_cli": 3.0}')
    _seed_observations(
        monkeypatch,
        {
            "code": {
                "route_stats": {
                    "or_cheap": {
                        "tier": "or_cheap",
                        "backend": "openrouter",  # cost 0.5
                        "model": "openrouter/owl-alpha",
                        "samples": 10,
                        "successes": 8,
                        "success_rate": 0.8,
                    },
                    "strong": {
                        "tier": "strong",
                        "backend": "claude_cli",  # cost 3.0
                        "model": "opus",
                        "samples": 10,
                        "successes": 8,
                        "success_rate": 0.8,
                    },
                },
            }
        },
    )
    _seed_tournament(monkeypatch, tmp_path, [])

    result = recs.best_model_for(stage="code", stack="node", features=None)

    assert result is not None
    assert result["backend"] == "openrouter"
    assert result["model"] == "openrouter/owl-alpha"


def test_best_model_for_prefers_feature_cell(monkeypatch, tmp_path):
    # The stage-level aggregate favours the strong model, but the
    # feature-specific cell shows the cheap model winning for 'auth'.
    _seed_observations(
        monkeypatch,
        {
            "code": {
                "route_stats": {
                    "or_cheap": {
                        "tier": "or_cheap",
                        "backend": "openrouter",
                        "model": "openrouter/owl-alpha",
                        "samples": 4,
                        "successes": 1,
                        "success_rate": 0.25,
                        "cells": {
                            "node::auth": {
                                "stack": "node",
                                "feature": "auth",
                                "samples": 5,
                                "successes": 5,
                                "success_rate": 1.0,
                            }
                        },
                    },
                },
            }
        },
    )
    _seed_tournament(monkeypatch, tmp_path, [])

    result = recs.best_model_for(stage="code", stack="node", features=["auth"])

    assert result is not None
    assert result["backend"] == "openrouter"
    # Picked the high-confidence feature cell, not the weak aggregate.
    assert result["success_rate"] == 1.0
    assert result["samples"] == 5


def test_best_model_for_returns_none_without_observations(monkeypatch, tmp_path):
    _seed_observations(monkeypatch, {})
    _seed_tournament(monkeypatch, tmp_path, [])

    assert recs.best_model_for(stage="code", stack="node", features=None) is None


def test_tournament_boost_rewards_cheap_winner(monkeypatch, tmp_path):
    # A cheap model that has WON debates gets a boost — closing the loop
    # so cheap winners get picked more over time. The boost must not flip
    # the choice to a pricier backend, only reinforce the cheap one.
    _seed_observations(
        monkeypatch,
        {
            "code": {
                "route_stats": {
                    "or_cheap": {
                        "tier": "or_cheap",
                        "backend": "openrouter",
                        "model": "cheap/winner",
                        "samples": 6,
                        "successes": 5,
                        "success_rate": 0.833,
                    },
                },
            }
        },
    )
    _seed_tournament(
        monkeypatch,
        tmp_path,
        [
            ModelTrial(
                model_id="cheap/winner",
                task_id="code-x",
                domain_tags=["code", "node"],
                vendor_tags=["openrouter"],
                score=88,
                cost_usd=0.0,
                passed=True,
            ),
        ],
    )

    base = recs.best_model_for(stage="code", stack="node", features=None)
    assert base is not None
    assert base["backend"] == "openrouter"


# ── routing_observations stack/feature persistence ──────────────────


def test_observations_persist_stack_feature_cells(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "skyn3t.intelligence.routing_observations.get_settings",
        lambda: type("S", (), {"data_dir": tmp_path})(),
    )
    obs.reset_cache_for_tests()

    obs.record_trajectory(
        {
            "trajectory_id": "traj-stack-1",
            "stage": "code",
            "stack": "node",
            "features": ["auth"],
            "outcome": "success",
            "events": [
                {
                    "type": "llm_call",
                    "project_stage": "code",
                    "backend": "openrouter",
                    "model": "google/gemini-3.1-flash-lite",
                    "total_tokens": 1500,
                }
            ],
        }
    )

    snap = obs.snapshot()
    stat = snap["code"]["route_stats"]["or_cheap"]
    # Existing shape preserved.
    assert stat["samples"] == 1
    assert stat["success_rate"] == 1.0
    # New cell dimension persisted without disturbing existing keys.
    assert "node::auth" in stat["cells"]
    cell = stat["cells"]["node::auth"]
    assert cell["stack"] == "node"
    assert cell["feature"] == "auth"
    assert cell["samples"] == 1
    assert cell["success_rate"] == 1.0


def test_observations_backward_compatible_without_stack(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "skyn3t.intelligence.routing_observations.get_settings",
        lambda: type("S", (), {"data_dir": tmp_path})(),
    )
    obs.reset_cache_for_tests()

    obs.record_trajectory(
        {
            "trajectory_id": "traj-nostack",
            "stage": "reviewer",
            "outcome": "success",
            "events": [
                {
                    "type": "llm_call",
                    "project_stage": "reviewer",
                    "backend": "openrouter",
                    "model": "qwen/qwen3-coder-plus",
                    "total_tokens": 2000,
                }
            ],
        }
    )

    snap = obs.snapshot()
    stat = snap["reviewer"]["route_stats"]["or_strong"]
    assert stat["samples"] == 1
    # No stack/feature → no cells emitted; legacy readers unaffected.
    assert "cells" not in stat
