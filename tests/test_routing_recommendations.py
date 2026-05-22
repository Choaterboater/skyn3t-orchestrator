from __future__ import annotations

from skyn3t.intelligence import routing_recommendations as recs


def _fake_route(stage: str, tier: str, source: str = "default") -> dict:
    backend, model = {
        "or_cheap": ("openrouter", "openrouter/owl-alpha"),
        "or_strong": ("openrouter", "xiaomi/mimo-v2.5-pro"),
    }[tier]
    return {
        "stage": stage,
        "tier": tier,
        "backend": backend,
        "model": model,
        "source": source,
    }


def test_recommendations_bias_heavy_stage_back_to_cheaper_default(monkeypatch):
    monkeypatch.setattr(
        "skyn3t.core.model_router.list_stage_routes",
        lambda: [_fake_route("brainstorm", "or_strong", source="persisted")],
    )
    monkeypatch.setattr("skyn3t.core.model_router.default_tier_for_stage", lambda stage: "or_cheap")
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
    monkeypatch.setattr(recs, "_live_stage_tokens", lambda: {"brainstorm": 250_000})
    monkeypatch.setattr(recs, "_trajectory_summary", lambda: {"brainstorm": {"total_tokens": 250_000, "trajectory_samples": 2, "mixed_route_samples": 0, "route_stats": {}}})
    monkeypatch.setattr(
        "skyn3t.intelligence.stage_latency.snapshot",
        lambda: {"brainstorm": {"avg_seconds": 120.0}},
    )

    rows = recs._compute_stage_recommendations()

    assert rows[0]["recommended_tier"] == "or_cheap"
    assert rows[0]["recommendation_kind"] == "cheaper"
    assert rows[0]["applyable"] is True


def test_recommendations_keep_quality_route_for_judgment_stage(monkeypatch):
    monkeypatch.setattr(
        "skyn3t.core.model_router.list_stage_routes",
        lambda: [_fake_route("reviewer", "or_strong")],
    )
    monkeypatch.setattr("skyn3t.core.model_router.default_tier_for_stage", lambda stage: "or_strong")
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
    monkeypatch.setattr(recs, "_live_stage_tokens", lambda: {"reviewer": 90_000})
    monkeypatch.setattr(
        recs,
        "_trajectory_summary",
        lambda: {
            "reviewer": {
                "total_tokens": 90_000,
                "trajectory_samples": 14,
                "mixed_route_samples": 0,
                "route_stats": {
                    "or_strong": {
                        "tier": "or_strong",
                        "backend": "openrouter",
                        "model": "xiaomi/mimo-v2.5-pro",
                        "samples": 10,
                        "success_rate": 0.9,
                    },
                    "or_cheap": {
                        "tier": "or_cheap",
                        "backend": "openrouter",
                        "model": "openrouter/owl-alpha",
                        "samples": 10,
                        "success_rate": 0.5,
                    },
                },
            }
        },
    )
    monkeypatch.setattr(
        "skyn3t.intelligence.stage_latency.snapshot",
        lambda: {"reviewer": {"avg_seconds": 110.0}},
    )

    rows = recs._compute_stage_recommendations()

    assert rows[0]["recommended_tier"] == "or_strong"
    assert rows[0]["recommendation_kind"] == "keep"
    assert rows[0]["applyable"] is False


def test_recommendations_read_cached_observations(monkeypatch):
    monkeypatch.setattr(
        "skyn3t.core.model_router.list_stage_routes",
        lambda: [_fake_route("reviewer", "or_strong")],
    )
    monkeypatch.setattr("skyn3t.core.model_router.default_tier_for_stage", lambda stage: "or_strong")
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
    monkeypatch.setattr(recs, "_live_stage_tokens", lambda: {})
    monkeypatch.setattr(
        "skyn3t.intelligence.routing_observations.snapshot",
        lambda: {
            "reviewer": {
                "total_tokens": 5000,
                "trajectory_samples": 6,
                "mixed_route_samples": 0,
                "route_stats": {
                    "or_strong": {
                        "tier": "or_strong",
                        "backend": "openrouter",
                        "model": "xiaomi/mimo-v2.5-pro",
                        "samples": 6,
                        "success_rate": 0.85,
                    }
                },
            }
        },
    )
    monkeypatch.setattr(
        "skyn3t.intelligence.stage_latency.snapshot",
        lambda: {"reviewer": {"avg_seconds": 70.0}},
    )

    rows = recs._compute_stage_recommendations()

    assert rows[0]["signals"]["trajectory_stage_tokens"] == 5000
    assert rows[0]["signals"]["trajectory_samples"] == 6
