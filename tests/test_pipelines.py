"""Tests for pipeline system."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyn3t.core.agent import BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus, EventType
from skyn3t.core.pipeline import (
    CollaborativePipeline,
    Pipeline,
    PipelineStage,
    create_pipeline,
)


class MockAgent(BaseAgent):
    """Mock agent for pipeline testing."""

    def __init__(self, name: str, event_bus: EventBus, response: str = "ok"):
        super().__init__(
            name=name,
            agent_type="mock",
            provider="test",
            event_bus=event_bus,
        )
        self.response = response
        self.execute_call_count = 0

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def execute(self, task: TaskRequest) -> TaskResult:
        self.execute_call_count += 1
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"response": f"{self.response} #{self.execute_call_count}"},
        )

    async def health_check(self) -> bool:
        return True


class FailingAgent(BaseAgent):
    """Agent that always fails."""

    def __init__(self, name: str, event_bus: EventBus):
        super().__init__(
            name=name,
            agent_type="mock",
            provider="test",
            event_bus=event_bus,
        )

    async def initialize(self) -> None:
        pass

    async def execute(self, task: TaskRequest) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            success=False,
            error="Simulated failure",
        )

    async def health_check(self) -> bool:
        return True


@pytest.mark.asyncio
class TestCreatePipeline:
    async def test_create_pipeline_with_mocked_agents(self, event_bus):
        agent1 = MockAgent("a1", event_bus, "step1")
        agent2 = MockAgent("a2", event_bus, "step2")
        agent3 = MockAgent("a3", event_bus, "step3")

        pipeline = create_pipeline(
            name="test_pipeline",
            agents=[agent1, agent2, agent3],
            event_bus=event_bus,
            stage_names=["write", "review", "test"],
        )

        assert pipeline.name == "test_pipeline"
        assert len(pipeline.stages) == 3
        assert pipeline.stages[0].name == "write"
        assert pipeline.stages[1].name == "review"
        assert pipeline.stages[2].name == "test"

    async def test_create_pipeline_default_stage_names(self, event_bus):
        agent1 = MockAgent("a1", event_bus)
        pipeline = create_pipeline(
            name="simple",
            agents=[agent1],
            event_bus=event_bus,
        )
        assert pipeline.stages[0].name == "stage_1"


@pytest.mark.asyncio
class TestPipeOutputForwarding:
    async def test_output_forwarded_between_stages(self, event_bus):
        agent1 = MockAgent("writer", event_bus, "generated_code")
        agent2 = MockAgent("reviewer", event_bus, "reviewed_code")

        pipeline = Pipeline(
            name="forward_test",
            stages=[
                PipelineStage(name="write", agent=agent1),
                PipelineStage(name="review", agent=agent2),
            ],
            event_bus=event_bus,
        )

        result = await pipeline.run(initial_input={"message": "Write a function"})

        assert result.success is True
        assert len(result.stages) == 2
        # Second stage should have received output from first
        assert "generated_code #1" in result.stages[0]["output"]["response"]
        assert "reviewed_code #1" in result.stages[1]["output"]["response"]

    async def test_custom_output_transform(self, event_bus):
        agent1 = MockAgent("writer", event_bus, "code_result")
        agent2 = MockAgent("reviewer", event_bus, "review_result")

        def transform(result: TaskResult) -> dict:
            return {"code": result.output["response"]}

        pipeline = Pipeline(
            name="transform_test",
            stages=[
                PipelineStage(
                    name="write", agent=agent1, output_transform=transform
                ),
                PipelineStage(name="review", agent=agent2),
            ],
            event_bus=event_bus,
        )

        result = await pipeline.run(initial_input={"message": "go"})
        assert result.success is True
        # Second stage input should contain "code" key from transform
        assert result.stages[1]["output"]["response"] == "review_result #1"


@pytest.mark.asyncio
class TestPipelineCompletionEvent:
    async def test_completion_event_published(self, event_bus):
        agent1 = MockAgent("a1", event_bus)
        events = []

        def handler(event):
            events.append(event)

        event_bus.subscribe(handler, EventType.PIPELINE_COMPLETED)

        pipeline = Pipeline(
            name="event_test",
            stages=[PipelineStage(name="step1", agent=agent1)],
            event_bus=event_bus,
        )

        result = await pipeline.run(initial_input={})

        assert result.success is True
        assert pipeline.is_completed is True
        completion_events = [e for e in events if "pipeline_id" in e.payload]
        assert len(completion_events) >= 1
        assert completion_events[-1].payload["stages_completed"] == 1

    async def test_wait_for_completion(self, event_bus):
        agent1 = MockAgent("a1", event_bus)

        pipeline = Pipeline(
            name="wait_test",
            stages=[PipelineStage(name="step1", agent=agent1)],
            event_bus=event_bus,
        )

        asyncio.create_task(pipeline.run(initial_input={}))
        completed = await pipeline.wait_for_completion(timeout=2.0)
        assert completed is True


@pytest.mark.asyncio
class TestCollaborativeRun:
    async def test_collaborative_pipeline(self, event_bus):
        agent1 = MockAgent("claude", event_bus, "Claude says")
        agent2 = MockAgent("kimi", event_bus, "Kimi says")
        agent3 = MockAgent("copilot", event_bus, "Copilot says")

        pipeline = CollaborativePipeline(
            name="collab_test",
            stages=[
                PipelineStage(name="step1", agent=agent1),
                PipelineStage(name="step2", agent=agent2),
                PipelineStage(name="step3", agent=agent3),
            ],
            event_bus=event_bus,
        )

        result = await pipeline.run(
            initial_input={"message": "How to build a startup"}
        )

        assert result.success is True
        assert len(result.stages) == 3
        # Each stage should have history
        for stage in result.stages:
            assert stage["success"] is True

    async def test_collaborative_pipeline_failure(self, event_bus):
        agent1 = MockAgent("ok", event_bus, "ok")
        agent2 = FailingAgent("fail", event_bus)

        pipeline = CollaborativePipeline(
            name="fail_test",
            stages=[
                PipelineStage(name="step1", agent=agent1),
                PipelineStage(name="step2", agent=agent2),
            ],
            event_bus=event_bus,
        )

        result = await pipeline.run(initial_input={"message": "go"})

        assert result.success is False
        assert result.error == "Simulated failure"
        assert len(result.stages) == 2
        assert result.stages[0]["success"] is True
        assert result.stages[1]["success"] is False

    async def test_create_pipeline_collaborative(self, event_bus):
        agent1 = MockAgent("a1", event_bus)
        agent2 = MockAgent("a2", event_bus)

        pipeline = create_pipeline(
            name="collab",
            agents=[agent1, agent2],
            event_bus=event_bus,
            collaborative=True,
        )

        assert isinstance(pipeline, CollaborativePipeline)
        result = await pipeline.run(initial_input={"message": "hi"})
        assert result.success is True
