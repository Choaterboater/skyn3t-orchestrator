"""Phase 2 — Owner G: each-build-improves-next feedback metrics.

Covers ImprovementMetrics Item-7 fields (first-attempt outcomes + injection
hit-rate), their persistence roundtrip, the _on_studio_outcome derivation, the
_tick rollup published via IMPROVEMENT_TICK, and the Item-3 curate_if_due hook.

All tests use a fake orchestrator/event_bus and a tmp DATA_DIR — they never
start the loop nor touch live data/.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from skyn3t.config.settings import get_settings
from skyn3t.cortex.continuous_improvement import (
    ContinuousImprovementEngine,
    ImprovementMetrics,
    _load_metrics,
    _save_metrics,
)


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_item7_fields_persistence_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    metrics = ImprovementMetrics(
        first_attempt_results={"react_vite": [True, False, True]},
        first_attempt_trend={"react_vite": 0.667},
        injection_hits={"react_vite": 2},
        injection_total={"react_vite": 3},
        injection_hit_rate={"react_vite": 0.667},
    )
    _save_metrics(metrics)
    loaded = _load_metrics()
    assert loaded.first_attempt_results["react_vite"] == [True, False, True]
    assert loaded.first_attempt_trend["react_vite"] == 0.667
    assert loaded.injection_hits["react_vite"] == 2
    assert loaded.injection_total["react_vite"] == 3
    assert loaded.injection_hit_rate["react_vite"] == 0.667


@pytest.mark.asyncio
async def test_first_attempt_excludes_retry_slug():
    engine = ContinuousImprovementEngine(SimpleNamespace(), MagicMock())

    # First attempt (no -retry marker) that FAILED.
    await engine._on_studio_outcome(
        "PROJECT_FAILED",
        {"slug": "todo-app", "stack": "react_vite", "status": "failed"},
    )
    # A retry build that PASSED — must NOT be counted as a first attempt.
    await engine._on_studio_outcome(
        "PROJECT_COMPLETED",
        {"slug": "todo-app-retry", "stack": "react_vite", "status": "done"},
    )

    # Only the first (failed) attempt is in the rolling window.
    assert engine.metrics.first_attempt_results["react_vite"] == [False]


@pytest.mark.asyncio
async def test_injection_hit_rate_counts_injected_passing_builds():
    engine = ContinuousImprovementEngine(SimpleNamespace(), MagicMock())

    # Build with injected lessons that passed -> hit.
    await engine._on_studio_outcome(
        "PROJECT_COMPLETED",
        {
            "slug": "app-a",
            "stack": "next",
            "status": "done",
            "lessons_count": 3,
            "injected_skills_count": 0,
        },
    )
    # Build with injected skills that failed -> counted in total, not a hit.
    await engine._on_studio_outcome(
        "PROJECT_FAILED",
        {
            "slug": "app-b",
            "stack": "next",
            "status": "failed",
            "lessons_count": 0,
            "injected_skills_count": 2,
        },
    )
    # Build with NO injection -> ignored by hit-rate entirely.
    await engine._on_studio_outcome(
        "PROJECT_COMPLETED",
        {"slug": "app-c", "stack": "next", "status": "done"},
    )

    assert engine.metrics.injection_total["next"] == 2
    assert engine.metrics.injection_hits["next"] == 1
    assert engine._compute_injection_hit_rate()["next"] == 0.5


@pytest.mark.asyncio
async def test_first_attempt_window_caps_to_configured_window(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SKYN3T_IMPROVEMENT_SCORE_WINDOW", "3")
    get_settings.cache_clear()
    engine = ContinuousImprovementEngine(SimpleNamespace(), MagicMock())
    for i in range(5):
        await engine._on_studio_outcome(
            "PROJECT_COMPLETED",
            {"slug": f"app-{i}", "stack": "flask", "status": "done"},
        )
    # Capped to window=3 even though 5 first-attempt builds happened.
    assert len(engine.metrics.first_attempt_results["flask"]) == 3


@pytest.mark.asyncio
async def test_tick_publishes_feedback_metrics(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    bus = MagicMock()
    engine = ContinuousImprovementEngine(SimpleNamespace(), bus)
    import time as _time

    engine.metrics.last_model_sync_at = _time.time()
    engine.metrics.first_attempt_results = {"react_vite": [True, True, False, True]}
    engine.metrics.injection_total = {"react_vite": 4}
    engine.metrics.injection_hits = {"react_vite": 3}

    with patch(
        "skyn3t.intelligence.cheap_smart.auto_apply_cheaper_routing",
        return_value=[],
    ):
        await engine._tick(get_settings())

    payload = bus.publish.call_args[0][0].payload
    assert payload["phase"] == "tick"
    assert payload["first_attempt_trend"]["react_vite"] == 0.75
    assert payload["injection_hit_rate"]["react_vite"] == 0.75
    # Persisted onto metrics so get_status / _save_metrics surface them.
    assert engine.metrics.first_attempt_trend["react_vite"] == 0.75
    assert engine.metrics.injection_hit_rate["react_vite"] == 0.75


@pytest.mark.asyncio
async def test_tick_invokes_curate_if_due(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    bus = MagicMock()
    engine = ContinuousImprovementEngine(SimpleNamespace(), bus)
    import time as _time

    engine.metrics.last_model_sync_at = _time.time()

    fake_lib = MagicMock()
    with patch(
        "skyn3t.intelligence.cheap_smart.auto_apply_cheaper_routing",
        return_value=[],
    ), patch(
        "skyn3t.intelligence.skill_library.get_default_library",
        return_value=fake_lib,
    ):
        await engine._tick(get_settings())

    fake_lib.curate_if_due.assert_called_once()


@pytest.mark.asyncio
async def test_build_two_recall_feedback_edge(monkeypatch, tmp_path):
    """Integration: build-1 fails (no injection), build-2 recalls lessons and
    passes — the flywheel must register the injection hit and a recovered
    first-attempt trend, proving each build improves the next."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    bus = MagicMock()
    engine = ContinuousImprovementEngine(SimpleNamespace(), bus)

    # Build 1 — first attempt, no injected knowledge yet, fails.
    await engine._on_studio_outcome(
        "PROJECT_FAILED",
        {"slug": "dash-1", "stack": "react_vite", "status": "failed"},
    )
    # Build 2 — first attempt for a sibling build, recalls lessons learned from
    # build 1's failure (injected), and passes.
    await engine._on_studio_outcome(
        "PROJECT_COMPLETED",
        {
            "slug": "dash-2",
            "stack": "react_vite",
            "status": "done",
            "lessons_count": 2,
        },
    )

    assert engine.metrics.first_attempt_results["react_vite"] == [False, True]
    # The injected build passed -> injection hit recorded.
    assert engine.metrics.injection_total["react_vite"] == 1
    assert engine.metrics.injection_hits["react_vite"] == 1
    assert engine._compute_injection_hit_rate()["react_vite"] == 1.0
    # First-attempt trend reflects the recovery: 1 of 2 passed.
    assert engine._compute_first_attempt_trend()["react_vite"] == 0.5

    status = engine.get_status()
    assert "first_attempt_trend" in status
    assert "injection_hit_rate" in status
