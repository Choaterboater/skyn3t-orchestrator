"""Tests for core SkyN3t components."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.core.self_healing import SelfHealingManager


class MockTestAgent(BaseAgent):
    """Test agent for unit tests."""

    def __init__(self, name: str, event_bus):
        super().__init__(
            name=name,
            agent_type="test",
            provider="test_provider",
            event_bus=event_bus,
        )

    async def initialize(self):
        self.status = "idle"

    async def execute(self, task):
        return TaskResult(task_id=task.task_id, success=True, output={"result": "ok"})

    async def health_check(self):
        return True


class TestEventBus:
    def test_subscribe_and_publish(self):
        bus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.subscribe(handler, EventType.TASK_COMPLETED)
        event = Event(event_type=EventType.TASK_COMPLETED, source="test", payload={})
        bus.publish(event)

        assert len(received) == 1
        assert received[0].event_type == EventType.TASK_COMPLETED

    def test_global_subscriber(self):
        bus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.subscribe(handler)
        bus.publish(Event(event_type=EventType.TASK_STARTED, source="test", payload={}))

        assert len(received) == 1

    def test_unsubscribe(self):
        bus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.subscribe(handler, EventType.TASK_COMPLETED)
        bus.unsubscribe(handler, EventType.TASK_COMPLETED)
        bus.publish(Event(event_type=EventType.TASK_COMPLETED, source="test", payload={}))

        assert len(received) == 0

    def test_history(self):
        bus = EventBus()
        bus.publish(Event(event_type=EventType.TASK_COMPLETED, source="a", payload={}))
        bus.publish(Event(event_type=EventType.TASK_FAILED, source="b", payload={}))
        bus.publish(Event(event_type=EventType.TASK_COMPLETED, source="c", payload={}))

        history = bus.get_history(event_type=EventType.TASK_COMPLETED)
        assert len(history) == 2


@pytest.mark.asyncio
class TestBaseAgent:
    async def test_task_execution(self, event_bus):
        agent = MockTestAgent("test_agent", event_bus)
        await agent.start()

        task = TaskRequest(title="Test task", description="Do something")
        await agent.submit_task(task)

        # Wait for task to complete
        await asyncio.sleep(0.5)

        assert agent.status in ("idle", "busy")
        await agent.shutdown()

    async def test_agent_stats(self, event_bus):
        agent = MockTestAgent("test_agent", event_bus)
        await agent.start()

        stats = agent.get_stats()
        assert stats["name"] == "test_agent"
        assert stats["status"] in ("idle", "busy")

        await agent.shutdown()


@pytest.mark.asyncio
class TestOrchestrator:
    async def test_register_agent(self, event_bus):
        orch = Orchestrator(event_bus)
        agent = MockTestAgent("agent1", event_bus)
        orch.register_agent(agent)

        assert "agent1" in orch.agents
        assert orch.get_agent("agent1") == agent

    async def test_submit_task(self, event_bus):
        orch = Orchestrator(event_bus)
        agent = MockTestAgent("agent1", event_bus)
        await agent.start()
        orch.register_agent(agent)
        await orch.start()

        task_id = await orch.create_and_submit_task(
            title="Test task",
            agent_name="agent1",
        )
        assert task_id is not None

        await orch.stop()
        await agent.shutdown()

    async def test_find_by_capability(self, event_bus):
        orch = Orchestrator(event_bus)
        agent = MockTestAgent("agent1", event_bus)
        agent.add_capability(AgentCapability(name="test_cap", description="Test"))
        orch.register_agent(agent)

        found = orch.find_agents_by_capability("test_cap")
        assert len(found) == 1
        assert found[0].name == "agent1"

    async def test_system_status(self, event_bus):
        orch = Orchestrator(event_bus)
        status = orch.get_system_status()
        assert "agents" in status
        assert "running_tasks" in status


class TestSelfHealing:
    def test_request_healing(self, event_bus):
        shm = SelfHealingManager(event_bus)
        shm.request_healing("agent1", "error_rate")
        assert not shm.healing_queue.empty()

    def test_healing_history(self, event_bus):
        shm = SelfHealingManager(event_bus)
        shm.healing_history.append(
            MagicMock(
                agent_name="agent1",
                action_type="restart",
                reason="error",
                timestamp=MagicMock(isoformat=lambda: "2024-01-01"),
                attempts=1,
                resolved=True,
            )
        )
        history = shm.get_healing_history()
        assert len(history) == 1
        assert history[0]["agent"] == "agent1"
