"""Phase 5A: ConversationLoopAPI (A2A request/response loop) tests.

Covers:
  * BaseAgent.pump_inbox drains the MessageBus inbox and dispatches on_message.
  * start/stop_inbox_pump activate the background drain (the dead layer).
  * request/response correlation works through the pump (auto-respond).
  * Orchestrator.converse runs a bounded loop, respects max_rounds, and
    converges early via the convergence callback.
  * Flag default OFF -> graceful linear degrade (no MessageBus dependency).
  * No orchestrator boot, no LLM calls, no real spend.
"""

from __future__ import annotations

import asyncio

from skyn3t.core.agent import BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.messaging import AgentMessage, get_default_bus
from skyn3t.core.orchestrator import (
    ConversationResult,
    Orchestrator,
    a2a_conversation_enabled,
)


class FakeAgent(BaseAgent):
    """Minimal agent with a scripted on_message; never calls an LLM."""

    def __init__(self, name: str, event_bus: EventBus, *, reply=None, role=None):
        super().__init__(
            name=name,
            agent_type="fake",
            provider="test",
            event_bus=event_bus,
            role=role,
        )
        self._reply = reply  # callable(msg) -> AgentMessage | None, or None
        self.received: list = []
        self.executed: list = []

    async def initialize(self) -> None:  # pragma: no cover - trivial
        return None

    async def health_check(self) -> bool:  # pragma: no cover - trivial
        return True

    async def execute(self, task: TaskRequest, stdin_data=None) -> TaskResult:
        self.executed.append(task)
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"response": f"{self.name}:done", "blockers": []},
        )

    def on_message(self, msg: AgentMessage):
        self.received.append(msg)
        if self._reply is None:
            return None
        return self._reply(msg)


def _make_reply(content: str, payload=None):
    def _reply(msg: AgentMessage) -> AgentMessage:
        return AgentMessage(
            from_agent=msg.to_agent,
            to_agent=msg.from_agent,
            kind="response",
            content=content,
            payload=payload or {},
        )

    return _reply


# ----------------------------------------------------------------------
# pump_inbox / dispatch
# ----------------------------------------------------------------------
def test_pump_inbox_once_dispatches_to_on_message():
    async def run():
        bus = EventBus()
        agent = FakeAgent("designer", bus)
        mbus = get_default_bus(bus)
        await mbus.send(
            AgentMessage(
                from_agent="x", to_agent="designer", kind="info", content="hi"
            )
        )
        n = await agent.pump_inbox(once=True, timeout=1.0)
        assert n == 1
        assert len(agent.received) == 1
        assert agent.received[0].content == "hi"

    asyncio.run(run())


def test_pump_inbox_drain_returns_zero_when_empty():
    async def run():
        bus = EventBus()
        agent = FakeAgent("idle", bus)
        n = await agent.pump_inbox(once=True, timeout=0.05)
        assert n == 0

    asyncio.run(run())


def test_request_response_correlation_via_pump():
    async def run():
        bus = EventBus()
        responder = FakeAgent(
            "reviewer", bus, reply=_make_reply("approved", {"blockers": []})
        )
        responder.start_inbox_pump()
        try:
            mbus = get_default_bus(bus)
            resp = await mbus.request(
                from_agent="orchestrator",
                to_agent="reviewer",
                content="please review",
                timeout=2.0,
            )
            assert resp is not None
            assert resp.kind == "response"
            assert resp.content == "approved"
            assert resp.payload.get("blockers") == []
            assert len(responder.received) == 1
        finally:
            await responder.stop_inbox_pump()

    asyncio.run(run())


def test_request_auto_acks_when_on_message_returns_none():
    """A request to an agent with no scripted reply still resolves (no hang)."""

    async def run():
        bus = EventBus()
        agent = FakeAgent("silent", bus, reply=None)
        agent.start_inbox_pump()
        try:
            mbus = get_default_bus(bus)
            resp = await mbus.request(
                from_agent="orchestrator",
                to_agent="silent",
                content="anything",
                timeout=2.0,
            )
            # Auto-ack empty response — future resolved, no timeout.
            assert resp is not None
            assert resp.kind == "response"
            assert resp.content == ""
        finally:
            await agent.stop_inbox_pump()

    asyncio.run(run())


def test_stop_inbox_pump_idempotent_and_clean():
    async def run():
        bus = EventBus()
        agent = FakeAgent("p", bus)
        agent.start_inbox_pump()
        agent.start_inbox_pump()  # idempotent: no second task
        await agent.stop_inbox_pump()
        await agent.stop_inbox_pump()  # safe to call twice
        assert agent._inbox_pump_task is None
        assert agent._inbox_pumping is False

    asyncio.run(run())


# ----------------------------------------------------------------------
# converse() — A2A path (flag on)
# ----------------------------------------------------------------------
def test_converse_a2a_converges_on_reviewer_no_blockers(monkeypatch):
    monkeypatch.setenv("SKYN3T_A2A_CONVERSATION", "1")
    assert a2a_conversation_enabled() is True

    async def run():
        orch = Orchestrator()  # not started — no cortex, no spend
        designer = FakeAgent("designer", orch.event_bus, reply=_make_reply("spec-v1"))
        reviewer = FakeAgent(
            "reviewer",
            orch.event_bus,
            reply=_make_reply("looks good", {"blockers": [], "role": "reviewer"}),
            role="reviewer",
        )
        orch.agents["designer"] = designer
        orch.agents["reviewer"] = reviewer
        designer.start_inbox_pump()
        reviewer.start_inbox_pump()
        try:
            result = await orch.converse(
                participants=["designer", "reviewer"],
                topic="build a login page",
                context={"stack": "fastapi"},
                max_rounds=4,
                round_timeout_s=2.0,
            )
        finally:
            await designer.stop_inbox_pump()
            await reviewer.stop_inbox_pump()

        assert isinstance(result, ConversationResult)
        assert result.converged is True
        # Reviewer reported no blockers in round 1 -> stop after round 1.
        assert result.rounds_used == 1
        # Each participant got at least one request.
        assert any(m.content == "build a login page" for m in designer.received)
        assert len(reviewer.received) >= 1
        # Turn log records both request and response turns.
        kinds = {t.kind for t in result.turns}
        assert kinds == {"request", "response"}

    asyncio.run(run())


def test_converse_a2a_respects_max_rounds_when_no_convergence(monkeypatch):
    monkeypatch.setenv("SKYN3T_A2A_CONVERSATION", "1")

    async def run():
        orch = Orchestrator()
        # Reviewer always reports a blocker -> never converges.
        reviewer = FakeAgent(
            "reviewer",
            orch.event_bus,
            reply=_make_reply("nope", {"blockers": ["fix x"], "role": "reviewer"}),
            role="reviewer",
        )
        orch.agents["reviewer"] = reviewer
        reviewer.start_inbox_pump()
        try:
            result = await orch.converse(
                participants=["reviewer"],
                topic="t",
                context={},
                max_rounds=3,
                round_timeout_s=2.0,
            )
        finally:
            await reviewer.stop_inbox_pump()

        assert result.converged is False
        assert result.rounds_used == 3
        assert len(reviewer.received) == 3  # one request per round

    asyncio.run(run())


def test_converse_skips_absent_participant_without_spawn(monkeypatch):
    """A named participant with no agent and no manager is skipped, not fatal."""
    monkeypatch.setenv("SKYN3T_A2A_CONVERSATION", "1")

    async def run():
        orch = Orchestrator()  # empty registry -> no manager to spawn under
        result = await orch.converse(
            participants=["ghost"],
            topic="hello",
            context={},
            max_rounds=1,
            round_timeout_s=1.0,
        )
        assert isinstance(result, ConversationResult)
        assert result.converged is False
        # Nothing to talk to: no turns recorded.
        assert result.turns == []

    asyncio.run(run())


# ----------------------------------------------------------------------
# converse() — linear degrade (flag off, default)
# ----------------------------------------------------------------------
def test_converse_defaults_off_uses_linear_handoff(monkeypatch):
    monkeypatch.delenv("SKYN3T_A2A_CONVERSATION", raising=False)
    assert a2a_conversation_enabled() is False

    async def run():
        orch = Orchestrator()
        designer = FakeAgent("designer", orch.event_bus)
        reviewer = FakeAgent("reviewer", orch.event_bus, role="reviewer")
        orch.agents["designer"] = designer
        orch.agents["reviewer"] = reviewer
        # No inbox pumps started: linear path must not depend on them.
        result = await orch.converse(
            participants=["designer", "reviewer"],
            topic="topic",
            context={"k": "v"},
            max_rounds=1,
        )
        assert isinstance(result, ConversationResult)
        # execute() returned blockers=[] -> default convergence True.
        assert result.converged is True
        assert len(designer.executed) == 1
        assert len(reviewer.executed) == 1
        # No A2A messages: nobody received an inbox message.
        assert designer.received == []
        assert reviewer.received == []

    asyncio.run(run())


# ----------------------------------------------------------------------
# events
# ----------------------------------------------------------------------
def test_converse_emits_conversation_events(monkeypatch):
    monkeypatch.delenv("SKYN3T_A2A_CONVERSATION", raising=False)

    async def run():
        orch = Orchestrator()
        seen: list = []
        for et in (
            EventType.AGENT_CONVERSATION_STARTED,
            EventType.AGENT_CONVERSATION_TURN,
            EventType.AGENT_CONVERSATION_ENDED,
        ):
            orch.event_bus.subscribe(lambda e: seen.append(e.event_type), et)
        agent = FakeAgent("designer", orch.event_bus, role="reviewer")
        orch.agents["designer"] = agent
        await orch.converse(
            participants=["designer"],
            topic="x",
            context={},
            max_rounds=1,
        )
        assert EventType.AGENT_CONVERSATION_STARTED in seen
        assert EventType.AGENT_CONVERSATION_ENDED in seen
        assert EventType.AGENT_CONVERSATION_TURN in seen

    asyncio.run(run())


def test_register_agent_starts_pump_when_loop_running():
    """register_agent activates the inbox pump when a loop is present."""

    async def run():
        orch = Orchestrator()
        agent = FakeAgent("worker", orch.event_bus)
        orch.register_agent(agent)
        # Pump should be live; give the loop a tick to schedule it.
        await asyncio.sleep(0)
        assert agent._inbox_pumping is True
        assert agent._inbox_pump_task is not None
        await agent.stop_inbox_pump()

    asyncio.run(run())
