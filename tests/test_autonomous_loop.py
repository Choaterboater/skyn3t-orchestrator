"""Tests for autonomous learning schedule + Studio build loop."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skyn3t.config.settings import get_settings
from skyn3t.cortex.autonomous_loop import (
    AutonomousBrief,
    AutonomousCoordinator,
    LoopState,
    _brief_hash,
    _reset_daily_counters,
    ensure_autonomous_scout_schedule,
)


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "skyn3t.cortex.autonomous_loop.STATE_PATH",
        tmp_path / "data" / "autonomous_loop_state.json",
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeMemory:
    def __init__(self):
        self.jobs: list[dict] = []

    async def list_scheduled_jobs(self, enabled_only: bool = False):
        return list(self.jobs)

    async def save_scheduled_job(self, **kwargs):
        self.jobs.append(dict(kwargs))


class _FakeRunner:
    MAX_CONCURRENT_PROJECTS = 3

    def __init__(self):
        self.started: list[dict] = []
        self.token_total = 0

    def list_projects(self):
        return []

    def _project_token_total(self, slug: str):
        return self.token_total

    async def start(self, template, brief, **kwargs):
        self.started.append({"template": template, "brief": brief, **kwargs})
        return {"slug": kwargs.get("slug") or "auto-test", "status": "running"}


@pytest.mark.asyncio
async def test_ensure_scout_schedule_creates_job(monkeypatch):
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_LEARNING", "1")
    get_settings.cache_clear()
    orch = SimpleNamespace(memory_store=_FakeMemory())
    result = await ensure_autonomous_scout_schedule(orch)
    assert result.get("scheduled") is True
    assert result.get("existing") is False
    assert len(orch.memory_store.jobs) == 1
    assert orch.memory_store.jobs[0]["name"] == "autonomous-repo-scout"


@pytest.mark.asyncio
async def test_ensure_scout_schedule_skips_when_disabled(monkeypatch):
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_LEARNING", "0")
    get_settings.cache_clear()
    orch = SimpleNamespace(memory_store=_FakeMemory())
    result = await ensure_autonomous_scout_schedule(orch)
    assert result.get("scheduled") is False
    assert len(orch.memory_store.jobs) == 0


@pytest.mark.asyncio
async def test_autonomous_build_respects_daily_cap(monkeypatch):
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILD_DAILY_CAP", "1")
    get_settings.cache_clear()

    runner = _FakeRunner()
    orch = SimpleNamespace(
        memory_store=_FakeMemory(),
        running_tasks={},
        get_studio_runner=lambda: runner,
        _repo_scout=None,
    )
    bus = MagicMock()
    coord = AutonomousCoordinator(orch, bus)
    coord.state.daily_builds = 1
    coord.state.today_date = __import__("time").strftime("%Y-%m-%d")
    await coord._pending.put(
        AutonomousBrief(brief="Build a tiny todo app with dark theme.", source="test")
    )

    skip = await coord._maybe_start_build()
    assert skip is not None
    assert "daily cap" in skip.lower()
    assert len(runner.started) == 0


@pytest.mark.asyncio
async def test_autonomous_build_starts_when_enabled(monkeypatch):
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILD_DAILY_BUDGET_USD", "100.0")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILD_DAILY_CAP", "3")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_MIN_REVIEWER_SCORE", "88")
    get_settings.cache_clear()

    runner = _FakeRunner()
    orch = SimpleNamespace(
        memory_store=_FakeMemory(),
        running_tasks={},
        get_studio_runner=lambda: runner,
        _repo_scout=None,
    )
    bus = MagicMock()
    coord = AutonomousCoordinator(orch, bus)
    await coord._pending.put(
        AutonomousBrief(brief="Build a minimal habit tracker with streaks.", source="test")
    )

    skip = await coord._maybe_start_build()
    assert skip is None
    assert len(runner.started) == 1
    assert runner.started[0]["extra"].get("autonomous") is True
    assert runner.started[0]["extra"].get("quality_floor_score") == 88
    assert runner.started[0]["extra"].get("fail_on_needs_fixes") is True


@pytest.mark.asyncio
async def test_autonomous_quality_rejection_enqueues_recovery(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_PROOF_RUN", "0")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_MIN_REVIEWER_SCORE", "85")
    monkeypatch.setattr(
        "skyn3t.cortex.autonomous_loop.STATE_PATH",
        tmp_path / "data" / "autonomous_loop_state.json",
    )
    get_settings.cache_clear()

    orch = SimpleNamespace(
        memory_store=_FakeMemory(),
        running_tasks={},
        get_studio_runner=lambda: _FakeRunner(),
        _repo_scout=None,
    )
    bus = MagicMock()
    coord = AutonomousCoordinator(orch, bus)

    await coord._on_project_completed(
        {
            "slug": "auto-low-quality",
            "autonomous": True,
            "status": "needs_fixes",
            "verdict": "go-with-fixes",
            "reviewer_score": 72,
            "stack": "react_vite",
            "brief": "Build a habit tracker with streaks.",
            "message": "Review complete: go-with-fixes (72/100).",
        }
    )

    assert coord._pending.qsize() == 1
    queued = coord.pop_highest_priority_brief()
    assert queued is not None
    assert queued.source == "quality_gate"
    assert queued.priority == 90
    assert "reviewer 'go'" in queued.brief


@pytest.mark.asyncio
async def test_autonomous_build_charges_actual_token_usage(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILD_DAILY_CAP", "3")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILD_DAILY_BUDGET_USD", "10.0")
    monkeypatch.setattr(
        "skyn3t.cortex.autonomous_loop.STATE_PATH",
        tmp_path / "data" / "autonomous_loop_state.json",
    )
    get_settings.cache_clear()

    runner = _FakeRunner()
    runner.token_total = 125_000
    orch = SimpleNamespace(
        memory_store=_FakeMemory(),
        running_tasks={},
        get_studio_runner=lambda: runner,
        _repo_scout=None,
    )
    coord = AutonomousCoordinator(orch, MagicMock())
    await coord._pending.put(
        AutonomousBrief(brief="Build a polished notes app.", source="test")
    )

    skip = await coord._maybe_start_build()

    assert skip is None
    assert coord.state.daily_builds == 1
    assert coord.state.daily_spend_usd == pytest.approx(0.5)


def test_brief_hash_dedup():
    a = _brief_hash("Build  a   Todo App")
    b = _brief_hash("build a todo app")
    assert a == b


def test_reset_daily_counters():
    state = LoopState(today_date="1999-01-01", daily_builds=5, daily_spend_usd=9.0)
    _reset_daily_counters(state)
    assert state.daily_builds == 0
    assert state.daily_spend_usd == 0.0
