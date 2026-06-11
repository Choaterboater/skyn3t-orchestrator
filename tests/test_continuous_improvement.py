"""Tests for the never-stop improvement flywheel."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyn3t.config.settings import get_settings
from skyn3t.cortex.autonomous_loop import AutonomousBrief, AutonomousCoordinator
from skyn3t.cortex.continuous_improvement import (
    ContinuousImprovementEngine,
    ImprovementMetrics,
    _load_metrics,
    _reset_daily_counters,
    _save_metrics,
    continuous_improvement_enabled,
)


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_continuous_improvement_enabled_default_on(monkeypatch):
    monkeypatch.delenv("SKYN3T_CONTINUOUS_IMPROVEMENT", raising=False)
    assert continuous_improvement_enabled() is True


def test_continuous_improvement_disabled_by_env(monkeypatch):
    monkeypatch.setenv("SKYN3T_CONTINUOUS_IMPROVEMENT", "0")
    assert continuous_improvement_enabled() is False


def test_metrics_persistence_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    metrics = ImprovementMetrics(
        today_date="2026-06-11",
        builds_today=2,
        stack_scores={"react_vite": [72.0, 68.0]},
        score_trend={"react_vite": 70.0},
    )
    _save_metrics(metrics)
    loaded = _load_metrics()
    assert loaded.builds_today == 2
    assert loaded.stack_scores["react_vite"] == [72.0, 68.0]
    assert loaded.score_trend["react_vite"] == 70.0
    path = tmp_path / "data" / "improvement_metrics.json"
    assert path.exists()


def test_reset_daily_counters():
    metrics = ImprovementMetrics(today_date="2020-01-01", builds_today=5)
    _reset_daily_counters(metrics)
    assert metrics.today_date == time.strftime("%Y-%m-%d")
    assert metrics.builds_today == 0


@pytest.mark.asyncio
async def test_studio_outcome_records_score_and_build_count(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYN3T_CONTINUOUS_IMPROVEMENT", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()

    bus = MagicMock()
    engine = ContinuousImprovementEngine(SimpleNamespace(), bus)
    await engine._on_studio_outcome(
        "PROJECT_COMPLETED",
        {
            "stack": "react_vite",
            "status": "done",
            "reviewer_score": 65,
        },
    )
    assert engine.metrics.builds_today == 1
    assert engine.metrics.stack_scores["react_vite"] == [65.0]
    assert engine.metrics.score_trend["react_vite"] == 65.0


@pytest.mark.asyncio
async def test_score_regression_bumps_tier_and_files_proposal(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYN3T_CONTINUOUS_IMPROVEMENT", "1")
    monkeypatch.setenv("SKYN3T_IMPROVEMENT_SCORE_REGRESSION", "75")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()

    store = MagicMock()
    store.create.return_value = SimpleNamespace(id="prop-regression")
    routing_store = MagicMock()

    with patch("skyn3t.cortex.get_store", return_value=store), patch(
        "skyn3t.config.model_routing.get_model_routing_store",
        return_value=routing_store,
    ):
        bus = MagicMock()
        engine = ContinuousImprovementEngine(SimpleNamespace(), bus)
        engine.metrics.stack_scores["next"] = [60.0, 62.0, 58.0]
        await engine._check_score_regression("next", 60.0, get_settings())

    routing_store.set_many.assert_called_once()
    store.create.assert_called_once()
    assert engine.metrics.stack_regression_actions["next"] == "tier_bump"


@pytest.mark.asyncio
async def test_competitive_practice_respects_daily_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
    monkeypatch.setenv("SKYN3T_IMPROVEMENT_COMPETITIVE_PRACTICE_DAILY_CAP", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()

    proposal = SimpleNamespace(
        id="ingest-1",
        kind="ingest",
        applied_at=time.time(),
        decided_at=time.time(),
        payload={"repo": "nousresearch/hermes-agent", "description": "gateway"},
    )
    store = MagicMock()
    store.list.return_value = [proposal]

    coord = MagicMock()
    coord.enqueue_brief = AsyncMock(return_value=True)
    orch = SimpleNamespace(_autonomous_coordinator=coord)
    engine = ContinuousImprovementEngine(orch, MagicMock())

    with patch("skyn3t.cortex.get_store", return_value=store):
        first = await engine._maybe_queue_competitive_practice(get_settings())
        engine.metrics.competitive_practice_today = 1
        second = await engine._maybe_queue_competitive_practice(get_settings())

    assert first is True
    assert second is False
    coord.enqueue_brief.assert_called_once()


@pytest.mark.asyncio
async def test_tick_publishes_improvement_tick(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()

    bus = MagicMock()
    engine = ContinuousImprovementEngine(SimpleNamespace(), bus)
    engine.metrics.last_model_sync_at = time.time()

    with patch(
        "skyn3t.intelligence.cheap_smart.auto_apply_cheaper_routing",
        return_value=[{"stage": "code", "tier": "or_cheap"}],
    ):
        await engine._tick(get_settings())

    assert engine.metrics.cheaper_routing_applied == 1
    bus.publish.assert_called()
    payload = bus.publish.call_args[0][0].payload
    assert payload.get("kind") == "IMPROVEMENT_TICK"
    assert payload.get("phase") == "tick"


@pytest.mark.asyncio
async def test_autonomous_enqueue_brief_public_api(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    monkeypatch.setattr(
        "skyn3t.cortex.autonomous_loop.STATE_PATH",
        tmp_path / "data" / "autonomous_loop_state.json",
    )
    orch = SimpleNamespace(
        memory_store=MagicMock(),
        get_studio_runner=lambda: None,
        _repo_scout=None,
    )
    coord = AutonomousCoordinator(orch, MagicMock())
    ok = await coord.enqueue_brief(
        AutonomousBrief(
            brief=f"Unique drill app {time.time()} with dark theme.",
            source="test",
        )
    )
    assert ok is True
    assert coord._pending.qsize() == 1


@pytest.mark.asyncio
async def test_engine_get_status_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    engine = ContinuousImprovementEngine(SimpleNamespace(), MagicMock())
    status = engine.get_status()
    assert status["enabled"] is True
    assert "score_trend" in status
    assert "metrics_path" in status


@pytest.mark.asyncio
async def test_fleet_scout_tick_defers_during_boot(monkeypatch):
    monkeypatch.setenv("SKYN3T_CORTEX_SCOUT_DEFER_BOOT_SECONDS", "300")
    get_settings.cache_clear()
    from skyn3t.cortex.continuous_improvement import run_fleet_learning_tick

    orch = SimpleNamespace(_booted_at=time.time(), _repo_scout=MagicMock())
    result = await run_fleet_learning_tick(orch, MagicMock(), kind="scout_ingest")
    assert result["ok"] is True
    assert "deferred" in result
    orch._repo_scout.start_background.assert_not_called()


@pytest.mark.asyncio
async def test_fleet_scout_tick_uses_background_after_boot(monkeypatch):
    monkeypatch.setenv("SKYN3T_CORTEX_SCOUT_DEFER_BOOT_SECONDS", "0")
    get_settings.cache_clear()
    from skyn3t.cortex.continuous_improvement import run_fleet_learning_tick

    scout = MagicMock()
    scout.is_running = False
    scout.start_background.return_value = {"ok": True, "started": True}
    orch = SimpleNamespace(_booted_at=time.time() - 600, _repo_scout=scout)
    result = await run_fleet_learning_tick(orch, MagicMock(), kind="scout_ingest")
    assert result["ok"] is True
    assert result.get("background") is True
    scout.start_background.assert_called_once()
