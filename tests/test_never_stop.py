"""Tests for never-stop watchdog and queue replenishment."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyn3t.config.settings import get_settings
from skyn3t.cortex.autonomous_loop import AutonomousBrief, AutonomousCoordinator
from skyn3t.cortex.continuous_improvement import continuous_improvement_enabled
from skyn3t.cortex.never_stop import (
    NeverStopWatchdog,
    effective_loop_interval,
    never_stop_enabled,
)


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_never_stop_default_on_with_continuous_improvement(monkeypatch):
    monkeypatch.delenv("SKYN3T_NEVER_STOP", raising=False)
    monkeypatch.delenv("SKYN3T_CONTINUOUS_IMPROVEMENT", raising=False)
    assert never_stop_enabled() is True
    assert continuous_improvement_enabled() is True


def test_never_stop_disabled_explicitly(monkeypatch):
    monkeypatch.setenv("SKYN3T_NEVER_STOP", "0")
    assert never_stop_enabled() is False


def test_effective_loop_interval_min_30_when_never_stop(monkeypatch):
    monkeypatch.setenv("SKYN3T_NEVER_STOP", "1")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILD_INTERVAL_SECONDS", "10")
    get_settings.cache_clear()
    settings = get_settings()
    assert effective_loop_interval(settings, "autonomous_build_interval_seconds", 900) == 30


def test_effective_loop_interval_min_60_when_never_stop_off(monkeypatch):
    monkeypatch.setenv("SKYN3T_NEVER_STOP", "0")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILD_INTERVAL_SECONDS", "10")
    get_settings.cache_clear()
    settings = get_settings()
    assert effective_loop_interval(settings, "autonomous_build_interval_seconds", 900) == 60


@pytest.mark.asyncio
async def test_watchdog_restarts_dead_continuous_improvement_task(monkeypatch):
    monkeypatch.setenv("SKYN3T_NEVER_STOP", "1")
    monkeypatch.setenv("SKYN3T_CONTINUOUS_IMPROVEMENT", "1")
    get_settings.cache_clear()

    bus = MagicMock()
    dead_task = MagicMock()
    dead_task.done.return_value = True
    dead_task.cancelled.return_value = False
    dead_task.exception.return_value = None

    stop_fn = AsyncMock()
    start_fn = AsyncMock()
    engine = SimpleNamespace(
        _running=True,
        _task=dead_task,
        stop=stop_fn,
        start=start_fn,
    )

    orch = SimpleNamespace(
        _running=True,
        _continuous_improvement=engine,
        _autonomous_coordinator=None,
        _agent_fleet_coordinator=None,
    )
    watchdog = NeverStopWatchdog(orch, bus)
    await watchdog._check_and_recover()

    stop_fn.assert_awaited_once()
    start_fn.assert_awaited_once()
    assert watchdog._recoveries_total == 1
    assert watchdog._last_recovery_at is not None
    bus.publish.assert_called()
    payload = bus.publish.call_args[0][0].payload
    assert payload.get("kind") == "NEVER_STOP_RECOVERED"
    assert payload.get("component") == "continuous_improvement"


@pytest.mark.asyncio
async def test_watchdog_skips_when_orchestrator_stopped(monkeypatch):
    monkeypatch.setenv("SKYN3T_NEVER_STOP", "1")
    get_settings.cache_clear()

    engine = MagicMock()
    engine._running = True
    dead_task = asyncio.get_event_loop().create_future()
    dead_task.set_result(None)
    engine._task = dead_task
    engine.stop = AsyncMock()
    engine.start = AsyncMock()

    orch = SimpleNamespace(
        _running=False,
        _continuous_improvement=engine,
        _autonomous_coordinator=None,
        _agent_fleet_coordinator=None,
    )
    watchdog = NeverStopWatchdog(orch, MagicMock())
    await watchdog._check_and_recover()
    engine.start.assert_not_called()


@pytest.mark.asyncio
async def test_replenish_queue_injects_brief_after_empty_window(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
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
    coord._running = True
    coord._queue_empty_since = time.time() - 400

    brief = AutonomousBrief(
        brief=f"Synthetic drill {time.time()}",
        source="build_pattern_gap",
        priority=60,
    )

    with (
        patch.object(coord, "_propose_from_build_patterns", new_callable=AsyncMock) as prop_bp,
        patch.object(coord, "_propose_from_competitive", new_callable=AsyncMock) as prop_ci,
        patch.object(coord, "_propose_from_scout", new_callable=AsyncMock) as prop_sc,
    ):
        prop_bp.return_value = brief
        prop_ci.return_value = None
        prop_sc.return_value = None
        injected = await coord.replenish_queue_if_stale(get_settings(), empty_seconds=300)

    assert injected == 1
    assert coord._pending.qsize() == 1


@pytest.mark.asyncio
async def test_watchdog_get_status_shape(monkeypatch):
    monkeypatch.setenv("SKYN3T_NEVER_STOP", "1")
    get_settings.cache_clear()
    watchdog = NeverStopWatchdog(SimpleNamespace(_running=True), MagicMock())
    watchdog._running = True
    watchdog._last_recovery_at = time.time()
    status = watchdog.get_status()
    assert status["never_stop"] is True
    assert status["last_recovery_at"] is not None
    assert status["uptime_seconds"] >= 0
