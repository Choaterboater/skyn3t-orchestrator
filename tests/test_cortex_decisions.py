"""Tests for ``skyn3t.intelligence.cortex_decisions.publish_decision``.

The publisher is a single conduit for ``CORTEX_DECISION`` events
across cortex/router/recall subsystems. Tests pin:
- the canonical payload shape (system/action/reason/input)
- system whitelist (cortex|router|recall) — never poison the stream
- best-effort semantics (a broken bus must never raise)
- integration: the three producers (CortexBootstrap, model_router,
  CodeAgent recall) actually publish via this conduit
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List

import pytest

from skyn3t.core.events import Event, EventType
from skyn3t.intelligence.cortex_decisions import publish_decision


# ---------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------


class _BusRecorder:
    """Capture every event published. Stand-in for the real EventBus."""

    def __init__(self):
        self.events: List[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------
# Direct publisher contract
# ---------------------------------------------------------------------


def test_publish_decision_emits_canonical_event():
    bus = _BusRecorder()
    publish_decision(
        bus,
        system="router",
        action="demote_backend",
        reason="rate too low",
        input={"from_backend": "kimi_cli", "to_backend": "copilot_cli"},
        source="model_router",
    )
    assert len(bus.events) == 1
    e = bus.events[0]
    assert e.event_type == EventType.CORTEX_DECISION
    assert e.source == "model_router"
    assert e.payload == {
        "system": "router",
        "action": "demote_backend",
        "reason": "rate too low",
        "input": {"from_backend": "kimi_cli", "to_backend": "copilot_cli"},
    }


def test_publish_decision_normalizes_system_case():
    bus = _BusRecorder()
    publish_decision(bus, system="ROUTER", action="x")
    assert bus.events[0].payload["system"] == "router"


def test_publish_decision_rejects_invalid_system():
    """Invalid systems must be dropped — would otherwise pollute the
    Activity timeline with un-categorizable entries."""
    bus = _BusRecorder()
    publish_decision(bus, system="malware", action="evil")
    publish_decision(bus, system="", action="empty")
    publish_decision(bus, system=None, action="nullish")  # type: ignore[arg-type]
    assert bus.events == []


def test_publish_decision_defaults_source_to_system():
    bus = _BusRecorder()
    publish_decision(bus, system="recall", action="inject_ranked_fix")
    assert bus.events[0].source == "recall"


def test_publish_decision_no_op_on_missing_bus():
    """No bus is a graceful no-op — many code paths legitimately have
    no orchestrator to publish through."""
    publish_decision(None, system="router", action="x")  # type: ignore[arg-type]


def test_publish_decision_swallows_broken_bus():
    """A broken bus must NEVER abort the decision it's reporting on."""

    class _BrokenBus:
        def publish(self, _evt):
            raise RuntimeError("bus on fire")

    publish_decision(_BrokenBus(), system="cortex", action="boom")


def test_publish_decision_defaults_input_to_empty_dict():
    bus = _BusRecorder()
    publish_decision(bus, system="cortex", action="x")
    assert bus.events[0].payload["input"] == {}


# ---------------------------------------------------------------------
# Integration: CortexBootstrap publishes on skip
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cortex_bootstrap_publishes_decision_on_skip(monkeypatch):
    """When SKYN3T_CORTEX_DISABLE skips a component, the bootstrap
    must publish a CORTEX_DECISION so operators can see the skip
    in the Activity timeline, not just in the log."""
    from skyn3t.cortex.bootstrap import CortexBootstrap

    monkeypatch.setenv("SKYN3T_CORTEX_DISABLE", "auto_cleanup")

    bus = _BusRecorder()

    async def _stub_install(self):
        self._handlers_installed = True

    monkeypatch.setattr(CortexBootstrap, "_install_handlers", _stub_install)
    orch = SimpleNamespace(event_bus=bus, agents={})
    cb = CortexBootstrap(orch)
    await cb.start()

    decisions = [
        e for e in bus.events
        if e.event_type == EventType.CORTEX_DECISION
    ]
    assert any(
        d.payload["system"] == "cortex"
        and d.payload["action"] == "skip_component"
        and d.payload["input"].get("component") == "auto_cleanup"
        for d in decisions
    )


# ---------------------------------------------------------------------
# Integration: router publishes on demote
# ---------------------------------------------------------------------


def test_router_publishes_decision_on_demote(monkeypatch, tmp_path):
    """A real adaptive demotion produces a CORTEX_DECISION with the
    from/to backends and the rate that triggered it."""
    from skyn3t.core.model_router import resolve_model_for_file
    from skyn3t.intelligence.build_patterns import BuildPatternScoreboard

    # Disable ε-greedy so the demote is deterministic.
    monkeypatch.setenv("SKYN3T_ROUTER_EXPLORATION_EPS", "0")

    sb = BuildPatternScoreboard(store_path=tmp_path / "patterns.json")
    for _ in range(10):
        sb.record_backend(
            "react_vite", ["src/App.jsx"], "kimi_cli", "no",
        )

    bus = _BusRecorder()
    backend, _ = resolve_model_for_file(
        "src/components/Foo.jsx",
        stack="react_vite",
        scoreboard=sb,
        event_bus=bus,
    )

    decisions = [
        e for e in bus.events
        if e.event_type == EventType.CORTEX_DECISION
    ]
    assert backend != "kimi_cli", "static would have picked kimi_cli; demote should have fired"
    assert len(decisions) == 1
    payload = decisions[0].payload
    assert payload["system"] == "router"
    assert payload["action"] == "demote_backend"
    assert payload["input"]["from_backend"] == "kimi_cli"
    assert payload["input"]["to_backend"] == backend


def test_router_does_not_publish_when_no_event_bus(monkeypatch, tmp_path):
    """Pure-function call sites (no orchestrator handy) must still
    work — they get the demote, just no event."""
    from skyn3t.core.model_router import resolve_model_for_file
    from skyn3t.intelligence.build_patterns import BuildPatternScoreboard

    monkeypatch.setenv("SKYN3T_ROUTER_EXPLORATION_EPS", "0")
    sb = BuildPatternScoreboard(store_path=tmp_path / "patterns.json")
    for _ in range(10):
        sb.record_backend("react_vite", ["src/App.jsx"], "kimi_cli", "no")

    # No event_bus kwarg.
    backend, _ = resolve_model_for_file(
        "src/components/Foo.jsx",
        stack="react_vite",
        scoreboard=sb,
    )
    assert backend != "kimi_cli"  # demote still happened


# ---------------------------------------------------------------------
# Integration: CodeAgent recall publishes on inject
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_code_agent_recall_publishes_decision_on_inject():
    """When _collect_ranked_fix_blocks finds a ranked fix and adds it
    to its return list, a CORTEX_DECISION must record the injection."""
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.events import EventBus
    from skyn3t.memory.store import MemoryStore

    store = MemoryStore()
    await store.record_experience_index(
        embedding_id="emb-1",
        task_id="t",
        stack="react_vite",
        stage="contract_verifier",
        error_signature="contract:palette_schism",
        fix_applied="regenerate:App.jsx",
        fix_worked=True,
        success=True,
    )

    bus = EventBus()
    captured: List[Event] = []
    bus.subscribe(captured.append, EventType.CORTEX_DECISION)

    agent = CodeAgent("code", bus)
    await agent.initialize()
    blocks = await agent._collect_ranked_fix_blocks(["contract:palette_schism"])
    # Give the bus a moment to dispatch.
    import asyncio
    await asyncio.sleep(0)

    assert len(blocks) == 1
    assert len(captured) == 1
    payload = captured[0].payload
    assert payload["system"] == "recall"
    assert payload["action"] == "inject_ranked_fix"
    assert payload["input"]["signature"] == "contract:palette_schism"
    assert payload["input"]["fixes"][0]["fix_applied"] == "regenerate:App.jsx"
