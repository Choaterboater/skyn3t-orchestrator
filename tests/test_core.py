"""Tests for core SkyN3t components."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.core.self_healing import SelfHealingManager


class MockTestAgent(BaseAgent):
    """Test agent for unit tests."""

    def __init__(self, name: str, event_bus, role=None, reports_to=None):
        super().__init__(
            name=name,
            agent_type="test",
            provider="test_provider",
            event_bus=event_bus,
            role=role,
            reports_to=reports_to,
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


@pytest.mark.asyncio
class TestOrgChart:
    """Agent hierarchy: roles, reporting lines, and delegation."""

    async def test_agent_stores_role_and_reports_to(self, event_bus):
        from skyn3t.core.agent import BaseAgent, TaskResult

        class RoleAgent(BaseAgent):
            def __init__(self, name, event_bus, role=None, reports_to=None):
                super().__init__(
                    name=name,
                    agent_type="test",
                    provider="test_provider",
                    event_bus=event_bus,
                    role=role,
                    reports_to=reports_to,
                )

            async def initialize(self):
                pass

            async def execute(self, task):
                return TaskResult(task_id=task.task_id, success=True, output={})

            async def health_check(self):
                return True

        agent = RoleAgent("eng1", event_bus, role="engineer", reports_to="cto")
        assert agent.role == "engineer"
        assert agent.reports_to == "cto"
        stats = agent.get_stats()
        assert stats["role"] == "engineer"
        assert stats["reports_to"] == "cto"
        view = agent.get_config_view()
        assert view["role"] == "engineer"
        assert view["reports_to"] == "cto"

    async def test_apply_override_updates_role_and_reports_to(self, event_bus):
        agent = MockTestAgent("mgr", event_bus)
        agent.role = "manager"
        agent.reports_to = "ceo"
        result = agent.apply_override({"role": "director", "reports_to": "founder"})
        assert "role" in result["changed"]
        assert "reports_to" in result["changed"]
        assert agent.role == "director"
        assert agent.reports_to == "founder"

    async def test_orchestrator_registry_includes_hierarchy(self, event_bus):
        orch = Orchestrator(event_bus)
        agent = MockTestAgent("eng", event_bus)
        agent.role = "engineer"
        agent.reports_to = "cto"
        orch.register_agent(agent)
        reg = orch.agent_registry["eng"]
        assert reg["role"] == "engineer"
        assert reg["reports_to"] == "cto"

    async def test_get_subordinates(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        sub1 = MockTestAgent("sub1", event_bus)
        sub2 = MockTestAgent("sub2", event_bus)
        sub1.reports_to = "mgr"
        sub2.reports_to = "mgr"
        orch.register_agent(mgr)
        orch.register_agent(sub1)
        orch.register_agent(sub2)
        subs = orch.get_subordinates("mgr")
        assert len(subs) == 2
        assert {a.name for a in subs} == {"sub1", "sub2"}

    async def test_get_manager(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        sub = MockTestAgent("sub", event_bus)
        sub.reports_to = "mgr"
        orch.register_agent(mgr)
        orch.register_agent(sub)
        assert orch.get_manager("sub") == mgr
        assert orch.get_manager("mgr") is None
        assert orch.get_manager("ghost") is None

    async def test_get_reporting_chain(self, event_bus):
        orch = Orchestrator(event_bus)
        ceo = MockTestAgent("ceo", event_bus)
        vp = MockTestAgent("vp", event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        eng = MockTestAgent("eng", event_bus)
        vp.reports_to = "ceo"
        mgr.reports_to = "vp"
        eng.reports_to = "mgr"
        for a in [ceo, vp, mgr, eng]:
            orch.register_agent(a)
        assert orch.get_reporting_chain("eng") == ["mgr", "vp", "ceo"]
        assert orch.get_reporting_chain("mgr") == ["vp", "ceo"]
        assert orch.get_reporting_chain("ceo") == []

    async def test_reporting_chain_breaks_cycles(self, event_bus):
        orch = Orchestrator(event_bus)
        a = MockTestAgent("a", event_bus)
        b = MockTestAgent("b", event_bus)
        a.reports_to = "b"
        b.reports_to = "a"
        orch.register_agent(a)
        orch.register_agent(b)
        # Should stop after detecting the cycle instead of looping forever
        chain = orch.get_reporting_chain("a")
        assert len(chain) <= 2

    async def test_delegate_task_routes_to_subordinate(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        sub = MockTestAgent("sub", event_bus)
        sub.add_capability(AgentCapability(name="code", description="Code"))
        sub.reports_to = "mgr"
        await sub.start()
        orch.register_agent(mgr)
        orch.register_agent(sub)

        task = TaskRequest(title="write tests")
        task_id = await orch.delegate_task(task, "mgr", capability="code")
        assert task_id == task.task_id
        await sub.shutdown()

    async def test_delegate_task_returns_none_when_no_subordinate(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        orch.register_agent(mgr)
        task = TaskRequest(title="orphan")
        result = await orch.delegate_task(task, "mgr")
        assert result is None

    async def test_delegate_task_filters_by_capability(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        writer = MockTestAgent("writer", event_bus)
        designer = MockTestAgent("designer", event_bus)
        writer.reports_to = "mgr"
        designer.reports_to = "mgr"
        writer.add_capability(AgentCapability(name="code", description="Code"))
        designer.add_capability(AgentCapability(name="design", description="Design"))
        await writer.start()
        await designer.start()
        orch.register_agent(mgr)
        orch.register_agent(writer)
        orch.register_agent(designer)

        task = TaskRequest(title="css fix")
        # No subordinate has "analysis" capability
        assert await orch.delegate_task(task, "mgr", capability="analysis") is None
        # writer has "code" capability
        task2 = TaskRequest(title="bug fix")
        assert await orch.delegate_task(task2, "mgr", capability="code") == task2.task_id
        await writer.shutdown()
        await designer.shutdown()


@pytest.mark.asyncio
class TestAutoSpawn:
    """Dynamic subagent creation and lifecycle."""

    async def test_spawn_subordinate_creates_agent(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        orch.register_agent(mgr)

        sub = await orch.spawn_subordinate(
            manager_name="mgr",
            agent_type="BrainstormAgent",
            role="engineer",
            capabilities=["code"],
        )
        assert sub is not None
        assert sub.reports_to == "mgr"
        assert sub.role == "engineer"
        assert sub.lifecycle == "auto"
        assert "mgr-engineer" in sub.name
        assert any(c.name == "code" for c in sub.capabilities)
        assert sub.name in orch.agents

    async def test_spawn_subordinate_unknown_type_returns_none(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        orch.register_agent(mgr)
        sub = await orch.spawn_subordinate(
            manager_name="mgr",
            agent_type="NonExistentAgent",
        )
        assert sub is None

    async def test_spawn_avoids_name_collisions(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        orch.register_agent(mgr)
        sub1 = await orch.spawn_subordinate("mgr", "BrainstormAgent", role="eng")
        sub2 = await orch.spawn_subordinate("mgr", "BrainstormAgent", role="eng")
        assert sub1 is not None
        assert sub2 is not None
        assert sub1.name != sub2.name

    async def test_delegate_task_auto_spawns_when_no_subordinate(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        orch.register_agent(mgr)
        task = TaskRequest(title="research topic")
        task_id = await orch.delegate_task(
            task, "mgr", capability="research",
            auto_spawn=True, spawn_agent_type="BrainstormAgent",
        )
        assert task_id == task.task_id
        # A new auto agent should have been created
        auto_agents = [a for a in orch.agents.values() if a.lifecycle == "auto"]
        assert len(auto_agents) == 1
        assert auto_agents[0].reports_to == "mgr"

    async def test_delegate_task_auto_spawn_false_returns_none(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        orch.register_agent(mgr)
        task = TaskRequest(title="research topic")
        result = await orch.delegate_task(
            task, "mgr", capability="research", auto_spawn=False,
        )
        assert result is None

    async def test_terminate_idle_auto_agents_removes_stale(self, event_bus):
        orch = Orchestrator(event_bus)
        orch._auto_agent_ttl_seconds = 0.0  # immediate expiration
        mgr = MockTestAgent("mgr", event_bus)
        orch.register_agent(mgr)
        sub = await orch.spawn_subordinate("mgr", "BrainstormAgent", role="eng")
        assert sub is not None
        # Simulate idle state
        sub.status = "idle"
        sub.last_active_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await orch._terminate_idle_auto_agents()
        assert sub.name not in orch.agents

    async def test_terminate_idle_skips_manual_agents(self, event_bus):
        orch = Orchestrator(event_bus)
        orch._auto_agent_ttl_seconds = 0.0
        agent = MockTestAgent("keeper", event_bus)
        agent.lifecycle = "manual"
        agent.status = "idle"
        agent.last_active_at = datetime.now(timezone.utc)
        orch.register_agent(agent)
        await orch._terminate_idle_auto_agents()
        assert "keeper" in orch.agents

    async def test_terminate_idle_skips_busy_auto_agents(self, event_bus):
        orch = Orchestrator(event_bus)
        orch._auto_agent_ttl_seconds = 0.0
        mgr = MockTestAgent("mgr", event_bus)
        orch.register_agent(mgr)
        sub = await orch.spawn_subordinate("mgr", "BrainstormAgent", role="eng")
        sub.status = "busy"
        sub.last_active_at = datetime.now(timezone.utc)
        await orch._terminate_idle_auto_agents()
        assert sub.name in orch.agents


@pytest.mark.asyncio
class TestFanOut:
    """Hierarchical task decomposition and parallel delegation."""

    async def test_fan_out_delegates_to_subordinates(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        writer = MockTestAgent("writer", event_bus)
        designer = MockTestAgent("designer", event_bus)
        writer.reports_to = "mgr"
        designer.reports_to = "mgr"
        writer.add_capability(AgentCapability(name="code", description="Code"))
        designer.add_capability(AgentCapability(name="design", description="Design"))
        await writer.start()
        await designer.start()
        orch.register_agent(mgr)
        orch.register_agent(writer)
        orch.register_agent(designer)

        task = TaskRequest(title="build landing page")
        result = await orch.fan_out(
            "mgr",
            task,
            subtasks=[
                {"capability": "code", "title": "write backend"},
                {"capability": "design", "title": "create mockups"},
            ],
        )
        # Subtasks were submitted but agents are mocks that process
        # asynchronously, so we won't have results yet.
        assert result["parent_task_id"] == task.task_id
        assert result["completed"] + result["failed"] == 2
        await writer.shutdown()
        await designer.shutdown()

    async def test_fan_out_auto_spawn_missing_subordinate(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        orch.register_agent(mgr)
        task = TaskRequest(title="research topic")
        result = await orch.fan_out(
            "mgr",
            task,
            subtasks=[{"capability": "research", "title": "look up APIs", "agent_type": "BrainstormAgent"}],
            auto_spawn=True,
        )
        assert result["parent_task_id"] == task.task_id
        assert result["completed"] + result["failed"] == 1
        # An auto agent should have been created
        auto_agents = [a for a in orch.agents.values() if a.lifecycle == "auto"]
        assert len(auto_agents) == 1

    async def test_fan_out_returns_failure_when_no_match(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        orch.register_agent(mgr)
        task = TaskRequest(title="secret mission")
        result = await orch.fan_out(
            "mgr",
            task,
            subtasks=[{"capability": "spy", "title": "infiltrate"}],
            auto_spawn=False,
        )
        assert result["success"] is False
        assert result["failed"] == 1
        assert "spy" in result["results"][0].error

    async def test_fan_out_without_subtasks_delegates_whole_task(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        worker = MockTestAgent("worker", event_bus)
        worker.reports_to = "mgr"
        await worker.start()
        orch.register_agent(mgr)
        orch.register_agent(worker)
        task = TaskRequest(title="single job")
        result = await orch.fan_out("mgr", task)
        assert result["parent_task_id"] == task.task_id
        assert result["completed"] + result["failed"] == 1
        await worker.shutdown()

    async def test_fan_out_with_partial_success(self, event_bus):
        orch = Orchestrator(event_bus)
        mgr = MockTestAgent("mgr", event_bus)
        worker = MockTestAgent("worker", event_bus)
        worker.reports_to = "mgr"
        worker.add_capability(AgentCapability(name="code", description="Code"))
        await worker.start()
        orch.register_agent(mgr)
        orch.register_agent(worker)

        task = TaskRequest(title="mixed bag")
        result = await orch.fan_out(
            "mgr",
            task,
            subtasks=[
                {"capability": "code", "title": "good task"},
                {"capability": "design", "title": "missing task"},
            ],
        )
        assert result["success"] is False
        assert result["failed"] == 1
        assert result["completed"] == 1
        await worker.shutdown()


@pytest.mark.asyncio
class TestMessageBusRequestResponse:
    async def test_request_respond_round_trip(self, event_bus):
        from skyn3t.core.messaging import MessageBus

        bus = MessageBus(event_bus)

        # Simulate recipient agent listening for requests
        async def responder():
            msg = await bus.recv("reviewer", timeout=5.0)
            assert msg is not None
            assert msg.kind == "request"
            assert msg.content == "critique my work"
            await bus.respond(msg, "Looks good", {"verdict": "pass"})

        # Start responder in background
        task = asyncio.create_task(responder())

        # Requester sends request and awaits response
        response = await bus.request(
            from_agent="designer",
            to_agent="reviewer",
            content="critique my work",
            payload={"files": ["App.jsx"]},
            timeout=5.0,
        )

        await task

        assert response is not None
        assert response.kind == "response"
        assert response.content == "Looks good"
        assert response.payload["verdict"] == "pass"
        assert response.correlation_id is not None

    async def test_request_timeout_when_no_response(self, event_bus):
        from skyn3t.core.messaging import MessageBus

        bus = MessageBus(event_bus)
        response = await bus.request(
            from_agent="a",
            to_agent="b",
            content="hello",
            timeout=0.1,
        )
        assert response is None

    async def test_response_not_duplicated_to_inbox(self, event_bus):
        from skyn3t.core.messaging import MessageBus

        bus = MessageBus(event_bus)

        async def responder():
            msg = await bus.recv("reviewer", timeout=5.0)
            await bus.respond(msg, "ok", {})

        task = asyncio.create_task(responder())
        response = await bus.request(
            from_agent="designer",
            to_agent="reviewer",
            content="test",
            timeout=5.0,
        )
        await task

        assert response is not None
        # The response should have been consumed by the Future, not left in inbox
        inbox_msg = await bus.recv("designer", timeout=0.1)
        assert inbox_msg is None


@pytest.mark.asyncio
class TestBaseAgentConversationHooks:
    async def test_agent_request_uses_message_bus(self, event_bus):
        from skyn3t.core.messaging import get_default_bus

        agent = MockTestAgent("designer", event_bus)
        bus = get_default_bus(event_bus)

        async def responder():
            msg = await bus.recv("reviewer", timeout=5.0)
            assert msg is not None
            await bus.respond(msg, "approved", {})

        task = asyncio.create_task(responder())
        response = await agent.request(
            to_agent="reviewer",
            content="critique please",
            timeout=5.0,
        )
        await task

        assert response is not None
        assert response.content == "approved"

    async def test_on_message_default_returns_none(self, event_bus):
        agent = MockTestAgent("test", event_bus)
        from skyn3t.core.messaging import AgentMessage

        msg = AgentMessage(
            from_agent="other",
            to_agent="test",
            kind="request",
            content="hello",
        )
        result = agent.on_message(msg)
        assert result is None

    async def test_on_message_can_return_response(self, event_bus):
        class RespondingAgent(MockTestAgent):
            def on_message(self, msg):
                from skyn3t.core.messaging import AgentMessage

                if msg.kind == "request":
                    return AgentMessage(
                        from_agent=self.name,
                        to_agent=msg.from_agent,
                        kind="response",
                        content="pong",
                        correlation_id=msg.correlation_id,
                    )
                return None

        agent = RespondingAgent("echo", event_bus)
        await agent.request(
            to_agent="echo",
            content="ping",
            timeout=1.0,
        )
        # Note: on_message is sync, but request() expects async delivery.
        # This test verifies the hook exists; full round-trip requires
        # the message loop to call on_message().
        assert agent.on_message is not RespondingAgent.__bases__[0].on_message
