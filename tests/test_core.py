"""Tests for core SkyN3t components."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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

    async def test_start_resets_running_state_when_initialize_fails(self, event_bus):
        class FailingInitAgent(MockTestAgent):
            async def initialize(self):
                raise RuntimeError("init boom")

        agent = FailingInitAgent("broken_agent", event_bus)

        with pytest.raises(RuntimeError, match="init boom"):
            await agent.start()

        assert agent._running is False
        assert agent.status == "offline"
        errors = event_bus.get_history(EventType.AGENT_ERROR)
        assert errors
        assert errors[-1].payload["context"]["phase"] == "start"

    async def test_callback_failure_records_agent_error(self, event_bus):
        agent = MockTestAgent("callback_agent", event_bus)
        await agent.start()

        def bad_callback(_payload):
            raise RuntimeError("callback boom")

        task = TaskRequest(title="Callback task", callback=bad_callback)
        await agent.submit_task(task)
        await asyncio.sleep(0.5)

        result = agent._results[task.task_id]
        assert result.metadata["callback_failed"] is True
        assert "callback boom" in result.metadata["callback_error"]
        errors = event_bus.get_history(EventType.AGENT_ERROR)
        assert errors
        assert errors[-1].payload["context"]["phase"] == "callback"

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

    async def test_task_failure_honors_retry_budget_before_fallback(self, event_bus):
        orch = Orchestrator(event_bus)
        primary = MockTestAgent("primary", event_bus)
        fallback = MockTestAgent("fallback", event_bus)
        primary.add_capability(AgentCapability(name="test_cap", description="Test"))
        fallback.add_capability(AgentCapability(name="test_cap", description="Test"))
        orch.register_agent(primary)
        orch.register_agent(fallback)
        orch._fallback.register_chain("test_cap", ["primary", "fallback"])

        task = TaskRequest(title="Retry me", max_retries=1)
        orch.running_tasks[task.task_id] = task

        retry_on_agent = AsyncMock()
        retry_task = AsyncMock(return_value=True)
        with (
            patch.object(orch, "_retry_on_agent", retry_on_agent),
            patch.object(orch, "_retry_task", retry_task),
        ):
            await orch._handle_task_failure_async(
                Event(
                    event_type=EventType.TASK_FAILED,
                    source="primary",
                    payload={"task_id": task.task_id, "error": "boom"},
                )
            )

            assert task.retry_count == 1
            retry_on_agent.assert_awaited_once_with(task, "fallback", "primary")
            retry_task.assert_not_awaited()

            await orch._handle_task_failure_async(
                Event(
                    event_type=EventType.TASK_FAILED,
                    source="fallback",
                    payload={"task_id": task.task_id, "error": "still boom"},
                )
            )

        assert retry_on_agent.await_count == 1
        retry_task.assert_not_awaited()
        assert orch.task_results[task.task_id].success is False
        assert orch.task_results[task.task_id].error == "still boom"
        assert task.task_id not in orch.running_tasks

    async def test_task_completed_persists_scalar_output(self, event_bus):
        orch = Orchestrator(event_bus)
        task = TaskRequest(title="Scalar result")
        orch.running_tasks[task.task_id] = task
        orch._memory = SimpleNamespace(save_task=AsyncMock())

        orch._on_task_completed(
            Event(
                event_type=EventType.TASK_COMPLETED,
                source="agent1",
                payload={"task_id": task.task_id, "output": "hello world"},
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert orch.task_results[task.task_id].output == "hello world"
        saved_output = orch._memory.save_task.await_args.kwargs["output_data"]
        assert saved_output["value"] == "hello world"
        assert saved_output["_meta"]["agent_name"] == "agent1"

    async def test_retry_task_can_queue_busy_agents(self, event_bus):
        orch = Orchestrator(event_bus)
        agent = MockTestAgent("busy_agent", event_bus)
        agent.status = "busy"
        orch.register_agent(agent)

        task = TaskRequest(title="Retry me")
        queued = await orch._retry_task(task)

        assert queued is True
        queued_task = await asyncio.wait_for(agent._task_queue.get(), timeout=0.1)
        assert queued_task.task_id == task.task_id

    async def test_on_task_failed_deduplicates_inflight_failures(self, event_bus):
        orch = Orchestrator(event_bus)
        task = TaskRequest(title="Retry me", max_retries=3)
        orch.running_tasks[task.task_id] = task

        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_retry(*_args, **_kwargs):
            started.set()
            await release.wait()

        retry_on_agent = AsyncMock(side_effect=blocked_retry)
        retry_task = AsyncMock(return_value=True)
        event = Event(
            event_type=EventType.TASK_FAILED,
            source="primary",
            payload={"task_id": task.task_id, "error": "boom"},
        )

        with (
            patch.object(orch, "_get_fallback_agent", return_value="fallback"),
            patch.object(orch, "_retry_on_agent", retry_on_agent),
            patch.object(orch, "_retry_task", retry_task),
        ):
            orch._on_task_failed(event)
            await asyncio.wait_for(started.wait(), timeout=0.1)
            orch._on_task_failed(event)
            await asyncio.sleep(0)

            assert task.retry_count == 1
            assert retry_on_agent.await_count == 1
            retry_task.assert_not_awaited()
            assert task.task_id in orch._handling_task_failures

            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert task.task_id not in orch._handling_task_failures

    async def test_retry_on_agent_routes_through_submit_task(self, event_bus):
        orch = Orchestrator(event_bus)
        orch.register_agent(MockTestAgent("fallback", event_bus))
        task = TaskRequest(title="Retry me")
        submit_task = AsyncMock(return_value=task.task_id)

        with (
            patch.object(orch, "submit_task", submit_task),
            patch("skyn3t.core.orchestrator.asyncio.sleep", new=AsyncMock()),
        ):
            await orch._retry_on_agent(task, "fallback", "primary")

        submit_task.assert_awaited_once_with(task, agent_name="fallback")

    async def test_retry_task_routes_busy_agent_through_submit_task(self, event_bus):
        orch = Orchestrator(event_bus)
        agent = MockTestAgent("busy_agent", event_bus)
        agent.status = "busy"
        orch.register_agent(agent)
        task = TaskRequest(title="Retry me")
        submit_task = AsyncMock(return_value=task.task_id)

        with patch.object(orch, "submit_task", submit_task):
            queued = await orch._retry_task(task)

        assert queued is True
        submit_task.assert_awaited_once_with(task, agent_name="busy_agent")

    async def test_decomposed_parent_publishes_completion_and_persists(self, event_bus):
        class FakeDecomposer:
            def decompose(self, _task):
                return [object(), object()]

            async def execute_decomposed(self, subtasks, resolve_agent):
                assert len(subtasks) == 2
                assert resolve_agent is not None
                return [
                    TaskResult(task_id="sub-1", success=True, output={"part": "one"}),
                    TaskResult(task_id="sub-2", success=True, output={"part": "two"}),
                ]

        orch = Orchestrator(event_bus)
        orch._task_decomposer = FakeDecomposer()
        orch._memory = SimpleNamespace(save_task=AsyncMock())
        orch._consciousness = SimpleNamespace(add_to_session_history=AsyncMock())

        task = TaskRequest(title="Build a web app", description="Ship it")
        task_id = await orch.submit_task(task, auto_decompose=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert task_id == task.task_id
        completed = event_bus.get_history(EventType.TASK_COMPLETED)
        assert completed
        assert completed[-1].payload["task_id"] == task.task_id
        assert orch.task_results[task.task_id].metadata["decomposed"] is True
        assert orch.task_results[task.task_id].metadata["subtask_count"] == 2
        orch._memory.save_task.assert_awaited_once()
        orch._consciousness.add_to_session_history.assert_awaited_once()
        assert task.task_id not in orch.running_tasks

    async def test_find_by_capability(self, event_bus):
        orch = Orchestrator(event_bus)
        agent = MockTestAgent("agent1", event_bus)
        agent.add_capability(AgentCapability(name="test_cap", description="Test"))
        orch.register_agent(agent)

        found = orch.find_agents_by_capability("test_cap")
        assert len(found) == 1
        assert found[0].name == "agent1"

    async def test_submit_task_failure_publishes_failure_and_cleans_state(self, event_bus):
        orch = Orchestrator(event_bus)
        agent = MockTestAgent("agent1", event_bus)
        agent.submit_task = AsyncMock(side_effect=RuntimeError("queue boom"))
        orch.register_agent(agent)

        task = TaskRequest(title="Queue me", max_retries=0)
        task_id = await orch.submit_task(task, agent_name="agent1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert task_id == task.task_id
        assert task_id in orch.task_results
        assert orch.task_results[task_id].success is False
        assert orch.task_results[task_id].error == "queue boom"
        assert task_id not in orch.running_tasks

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
