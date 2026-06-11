"""Tests for parallel agent fleet coordinator."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyn3t.config.settings import get_settings
from skyn3t.cortex.agent_fleet import (
    AgentFleetCoordinator,
    FleetSlot,
    effective_build_daily_cap,
    effective_fleet_size,
    effective_studio_concurrency,
    fleet_should_run,
    orchestrator_backpressure,
)
from skyn3t.cortex.autonomous_loop import AutonomousBrief, AutonomousCoordinator
from skyn3t.studio.runner import StudioRunner


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    StudioRunner.configure_max_concurrent(3)


class _FakeRunner:
    MAX_CONCURRENT_PROJECTS = 3

    def __init__(self):
        self.started: list[dict] = []

    def list_projects(self):
        return []

    async def start(self, template, brief, **kwargs):
        self.started.append({"template": template, "brief": brief, **kwargs})
        return {"slug": kwargs.get("slug") or "auto-test", "status": "running"}


@pytest.fixture
def fake_autonomous(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "skyn3t.cortex.autonomous_loop.STATE_PATH",
        tmp_path / "data" / "autonomous_loop_state.json",
    )
    runner = _FakeRunner()
    orch = SimpleNamespace(
        memory_store=MagicMock(),
        running_tasks={},
        _max_concurrent=10,
        get_studio_runner=lambda: runner,
        _repo_scout=None,
    )
    coord = AutonomousCoordinator(orch, MagicMock())
    orch._autonomous_coordinator = coord
    return coord, runner, orch


def test_fleet_should_run_with_size(monkeypatch):
    monkeypatch.setenv("SKYN3T_AGENT_FLEET_SIZE", "20")
    monkeypatch.setenv("SKYN3T_CONTINUOUS_IMPROVEMENT", "0")
    get_settings.cache_clear()
    assert fleet_should_run() is True
    assert effective_fleet_size() == 20


def test_effective_build_daily_cap_scales_with_fleet(monkeypatch):
    monkeypatch.setenv("SKYN3T_AGENT_FLEET_SIZE", "20")
    monkeypatch.delenv("SKYN3T_AUTONOMOUS_BUILD_DAILY_CAP", raising=False)
    get_settings.cache_clear()
    assert effective_build_daily_cap() == 20


def test_effective_build_daily_cap_respects_explicit_low_cap(monkeypatch):
    monkeypatch.setenv("SKYN3T_AGENT_FLEET_SIZE", "20")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILD_DAILY_CAP", "1")
    get_settings.cache_clear()
    assert effective_build_daily_cap() == 1


def test_effective_studio_concurrency_caps_fleet(monkeypatch):
    monkeypatch.setenv("SKYN3T_AGENT_FLEET_SIZE", "20")
    monkeypatch.setenv("SKYN3T_AGENT_FLEET_MAX_CONCURRENT_BUILDS", "5")
    get_settings.cache_clear()
    assert effective_studio_concurrency() == 5


def test_effective_studio_concurrency_respects_small_fleet(monkeypatch):
    monkeypatch.setenv("SKYN3T_AGENT_FLEET_SIZE", "3")
    monkeypatch.setenv("SKYN3T_AGENT_FLEET_MAX_CONCURRENT_BUILDS", "5")
    get_settings.cache_clear()
    assert effective_studio_concurrency() == 3


def test_orchestrator_backpressure_when_busy():
    orch = SimpleNamespace(running_tasks={f"t{i}": object() for i in range(8)}, _max_concurrent=10)
    reason = orchestrator_backpressure(orch)
    assert reason is not None
    assert "busy" in reason.lower()


def test_orchestrator_backpressure_clear_when_idle():
    orch = SimpleNamespace(running_tasks={}, _max_concurrent=10)
    assert orchestrator_backpressure(orch) is None


@pytest.mark.asyncio
async def test_fleet_semaphore_limits_parallel_builds(monkeypatch, fake_autonomous):
    monkeypatch.setenv("SKYN3T_AGENT_FLEET_SIZE", "2")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILD_INTERVAL_SECONDS", "1")
    get_settings.cache_clear()

    coord, runner, orch = fake_autonomous
    coord.set_fleet_delegates_builds(True)

    start_calls = 0
    original_start = coord.start_build_for_brief

    async def slow_start(brief, *, slot_id=0):
        nonlocal start_calls
        start_calls += 1
        await asyncio.sleep(0.15)
        return await original_start(brief, slot_id=slot_id)

    coord.start_build_for_brief = slow_start  # type: ignore[method-assign]

    fleet = AgentFleetCoordinator(orch, MagicMock())
    fleet._fleet_sem = asyncio.Semaphore(2)
    fleet._learning_sem = asyncio.Semaphore(1)
    fleet._learning_parallel = 1
    fleet._slots = [FleetSlot(slot_id=i) for i in range(2)]

    await coord.enqueue_brief(AutonomousBrief(brief="Build A dark todo", source="test"))
    await coord.enqueue_brief(AutonomousBrief(brief="Build B habit tracker", source="test"))

    in_flight = 0
    peak = 0

    async def tracked_build(slot, brief):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await fleet._run_build_slot(slot, brief)
        finally:
            in_flight -= 1

    s0, s1 = fleet._slots
    t0 = asyncio.create_task(tracked_build(s0, coord.pop_highest_priority_brief()))
    t1 = asyncio.create_task(tracked_build(s1, coord.pop_highest_priority_brief()))
    await asyncio.gather(t0, t1)

    assert peak <= 2
    assert len(runner.started) == 2


@pytest.mark.asyncio
async def test_fleet_respects_daily_cap(monkeypatch, fake_autonomous):
    monkeypatch.setenv("SKYN3T_AGENT_FLEET_SIZE", "5")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILD_DAILY_CAP", "1")
    get_settings.cache_clear()

    coord, runner, orch = fake_autonomous
    coord.state.daily_builds = 1
    coord.state.today_date = time.strftime("%Y-%m-%d")
    coord.set_fleet_delegates_builds(True)

    fleet = AgentFleetCoordinator(orch, MagicMock())
    slot = FleetSlot(slot_id=0)
    fleet._fleet_sem = asyncio.Semaphore(1)

    await coord.enqueue_brief(AutonomousBrief(brief="Should not start", source="test"))
    brief = coord.pop_highest_priority_brief()
    assert brief is not None

    await fleet._run_build_slot(slot, brief)
    assert len(runner.started) == 0
    assert slot.last_error is not None
    assert "daily cap" in slot.last_error.lower()


@pytest.mark.asyncio
async def test_pop_highest_priority_brief_order(fake_autonomous):
    coord, _, _ = fake_autonomous
    await coord.enqueue_brief(
        AutonomousBrief(brief="Low priority", source="a", priority=10)
    )
    await coord.enqueue_brief(
        AutonomousBrief(brief="High priority", source="b", priority=90)
    )
    chosen = coord.pop_highest_priority_brief()
    assert chosen is not None
    assert chosen.brief == "High priority"


@pytest.mark.asyncio
async def test_fleet_start_configures_studio_concurrency(monkeypatch, fake_autonomous):
    monkeypatch.setenv("SKYN3T_AGENT_FLEET_SIZE", "20")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
    get_settings.cache_clear()

    _, _, orch = fake_autonomous
    fleet = AgentFleetCoordinator(orch, MagicMock())

    with patch.object(AgentFleetCoordinator, "_dispatcher_loop", new_callable=AsyncMock):
        await fleet.start()

    assert StudioRunner.MAX_CONCURRENT_PROJECTS == 5
    await fleet.stop()


@pytest.mark.asyncio
async def test_seed_startup_briefs_fills_queue(monkeypatch, fake_autonomous):
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
    get_settings.cache_clear()

    coord, _, _ = fake_autonomous
    with patch(
        "skyn3t.cortex.competitive_intel.competitive_practice_brief",
        side_effect=[
            "Build drill A for Hermes gateway pattern",
            "Build drill B for MetaSwarm BEADS pattern",
            "Build drill C for Forge orchestration pattern",
        ],
    ):
        added = await coord.seed_startup_briefs(min_depth=3)

    assert added >= 2
    assert coord.get_status()["queue_depth"] >= 2


@pytest.mark.asyncio
async def test_dispatcher_tick_assigns_idle_slot(monkeypatch, fake_autonomous):
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
    get_settings.cache_clear()

    coord, runner, orch = fake_autonomous
    coord.set_fleet_delegates_builds(True)
    await coord.enqueue_brief(
        AutonomousBrief(brief="Quick dark todo for fleet tick", source="test", priority=90)
    )

    fleet = AgentFleetCoordinator(orch, MagicMock())
    fleet._running = True
    fleet._fleet_sem = asyncio.Semaphore(20)
    fleet._learning_sem = asyncio.Semaphore(1)
    fleet._learning_parallel = 1
    fleet._slots = [FleetSlot(slot_id=0)]

    assigned = await fleet._dispatcher_tick()
    assert assigned == 1
    assert fleet._slots[0]._task is not None
    await fleet._slots[0]._task
    assert len(runner.started) == 1
    assert fleet._slots[0].state == "idle"


@pytest.mark.asyncio
async def test_fleet_get_status_shape(monkeypatch, fake_autonomous):
    monkeypatch.setenv("SKYN3T_AGENT_FLEET_SIZE", "3")
    get_settings.cache_clear()

    _, _, orch = fake_autonomous
    fleet = AgentFleetCoordinator(orch, MagicMock())
    fleet._running = True
    fleet._slots = [FleetSlot(slot_id=i) for i in range(3)]

    status = fleet.get_status()
    assert status["fleet_size"] == 3
    assert len(status["slots"]) == 3
    assert "daily_cap" in status
