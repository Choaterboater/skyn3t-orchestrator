"""Tests for consciousness snapshots and CheckpointManager."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyn3t.persistence.checkpoint import CURRENT_SCHEMA_VERSION, CheckpointManager
from skyn3t.persistence.consciousness_snapshot import ConsciousnessSnapshot


class _FakeEventBus:
    def __init__(self):
        self._history = []

    def to_snapshot(self, limit: int = 250):
        return list(self._history[-limit:])

    def restore_snapshot(self, events):
        self._history = list(events)


class _FakeConsciousness:
    def __init__(self):
        self.state = {"working_memory": {"k": {"value": 1, "expires_at": 9999999999}}}

    async def to_snapshot(self):
        return dict(self.state)

    async def restore_snapshot(self, data):
        self.state = dict(data)


class _FakeMetaAgent:
    def __init__(self):
        self.state = {"actions": [{"a": 1}]}

    def to_snapshot(self):
        return dict(self.state)

    def restore_snapshot(self, data):
        self.state = dict(data)


class _FakeAutonomousCoordinator:
    def __init__(self):
        self.state = SimpleNamespace(to_dict=lambda: {"daily_builds": 2})

    def to_snapshot(self):
        return self.state.to_dict()

    def restore_snapshot(self, data):
        self.state = SimpleNamespace(to_dict=lambda: dict(data))


def _fake_orchestrator(tmp_path):
    from skyn3t.observability.token_tracker import get_default_tracker

    tracker = get_default_tracker()
    tracker._by_agent = {"agent-1": {"agent": "agent-1", "total_tokens": 10}}
    tracker._by_project = {}

    orch = SimpleNamespace(
        event_bus=_FakeEventBus(),
        _consciousness=_FakeConsciousness(),
        _meta_agent=_FakeMetaAgent(),
        _autonomous_coordinator=_FakeAutonomousCoordinator(),
        agents={},
        agent_registry={},
        _idempotency_keys={},
        _cancelled_tasks=set(),
    )
    return orch


@pytest.mark.asyncio
async def test_consciousness_snapshot_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SKYN3T_SNAPSHOT_DIR", str(tmp_path / "checkpoints"))
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()

    orch = _fake_orchestrator(tmp_path)
    helper = ConsciousnessSnapshot()
    checkpoint_id = await helper.create(orch)

    assert checkpoint_id.startswith("cp-")
    listed = helper.list()
    assert len(listed) == 1
    assert listed[0]["token_tracker_agents"] == 1

    # Mutate orchestrator state
    orch._consciousness.state = {"working_memory": {}}
    orch._meta_agent.state = {"actions": []}

    result = await helper.restore_latest(orch)
    assert result is not None
    assert "consciousness" in result["restored"]
    assert "meta_agent" in result["restored"]
    assert "token_tracker" in result["restored"]
    assert orch._consciousness.state["working_memory"]["k"]["value"] == 1
    assert orch._meta_agent.state["actions"] == [{"a": 1}]


def test_checkpoint_schema_version_bumped():
    assert CURRENT_SCHEMA_VERSION == 2


def test_checkpoint_manager_round_trip(tmp_path):
    manager = CheckpointManager(checkpoint_dir=str(tmp_path / "cps"), max_checkpoints=5)
    cid = manager.save(
        agent_states=[{"name": "a"}],
        task_states=[{"id": "t1"}],
        consciousness_state={"wm": {"k": "v"}},
    )
    loaded = manager.load_latest()
    assert loaded is not None
    assert loaded.checkpoint_id == cid
    assert loaded.schema_version == CURRENT_SCHEMA_VERSION
    assert loaded.consciousness_state == {"wm": {"k": "v"}}
