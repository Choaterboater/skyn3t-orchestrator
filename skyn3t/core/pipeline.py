"""Pipeline system for chaining agent tasks together."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from skyn3t.core.agent import BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import Event, EventBus, EventType


@dataclass
class PipelineStage:
    """A single stage in a pipeline."""

    name: str
    agent: BaseAgent
    input_transform: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    output_transform: Optional[Callable[[TaskResult], Dict[str, Any]]] = None


@dataclass
class PipelineResult:
    """Result of a full pipeline execution."""

    pipeline_id: str
    success: bool
    stages: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


class Pipeline:
    """A pipeline that chains multiple agents together."""

    def __init__(
        self,
        name: str,
        stages: List[PipelineStage],
        event_bus: Optional[EventBus] = None,
    ):
        self.name = name
        self.stages = stages
        self.event_bus = event_bus or EventBus()
        self.pipeline_id = str(uuid4())
        self._completed = asyncio.Event()
        self._results: List[Dict[str, Any]] = []

    async def run(
        self,
        initial_input: Dict[str, Any],
        pipe_output: bool = True,
    ) -> PipelineResult:
        """Run the pipeline with the given initial input."""
        current_input = initial_input
        stage_results: List[Dict[str, Any]] = []

        self.event_bus.publish(
            Event(
                event_type=EventType.PIPELINE_STARTED,
                source=self.name,
                payload={
                    "pipeline_id": self.pipeline_id,
                    "stage_count": len(self.stages),
                },
            )
        )

        for idx, stage in enumerate(self.stages):
            if stage.input_transform:
                current_input = stage.input_transform(current_input)

            task = TaskRequest(
                title=f"{self.name} - stage {idx + 1}: {stage.name}",
                description=stage.name,
                input_data=current_input,
            )

            self.event_bus.publish(
                Event(
                    event_type=EventType.TASK_EXECUTION_STARTED,
                    source=stage.agent.name,
                    payload={
                        "task_id": task.task_id,
                        "agent": stage.agent.name,
                        "pipeline_id": self.pipeline_id,
                        "stage": idx + 1,
                    },
                    correlation_id=task.task_id,
                )
            )

            result = await stage.agent.execute(task)

            stage_output = {
                "stage": idx + 1,
                "name": stage.name,
                "agent": stage.agent.name,
                "success": result.success,
                "output": result.output,
                "error": result.error,
            }
            stage_results.append(stage_output)

            if not result.success:
                self.event_bus.publish(
                    Event(
                        event_type=EventType.PIPELINE_STAGE_FAILED,
                        source=self.name,
                        payload={
                            "pipeline_id": self.pipeline_id,
                            "stage": idx + 1,
                            "agent": stage.agent.name,
                            "error": result.error,
                        },
                        correlation_id=task.task_id,
                    )
                )
                self.event_bus.publish(
                    Event(
                        event_type=EventType.TASK_FAILED,
                        source=stage.agent.name,
                        payload={
                            "pipeline_id": self.pipeline_id,
                            "task_id": task.task_id,
                            "stage": idx + 1,
                            "error": result.error,
                        },
                        correlation_id=task.task_id,
                    )
                )
                return PipelineResult(
                    pipeline_id=self.pipeline_id,
                    success=False,
                    stages=stage_results,
                    error=result.error,
                )

            self.event_bus.publish(
                Event(
                    event_type=EventType.TASK_COMPLETED,
                    source=stage.agent.name,
                    payload={
                        "task_id": task.task_id,
                        "pipeline_id": self.pipeline_id,
                        "stage": idx + 1,
                    },
                    correlation_id=task.task_id,
                )
            )

            if pipe_output:
                if stage.output_transform:
                    current_input = stage.output_transform(result)
                else:
                    current_input = {
                        "message": result.output.get("response", ""),
                        "previous_output": result.output,
                        "stage": idx + 1,
                    }

        self._results = stage_results
        self._completed.set()

        self.event_bus.publish(
            Event(
                event_type=EventType.PIPELINE_COMPLETED,
                source=self.name,
                payload={
                    "pipeline_id": self.pipeline_id,
                    "stages_completed": len(stage_results),
                },
            )
        )

        return PipelineResult(
            pipeline_id=self.pipeline_id,
            success=True,
            stages=stage_results,
        )

    async def wait_for_completion(self, timeout: Optional[float] = None) -> bool:
        """Wait for the pipeline to complete."""
        try:
            await asyncio.wait_for(self._completed.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def is_completed(self) -> bool:
        """Check if the pipeline has completed."""
        return self._completed.is_set()


class CollaborativePipeline(Pipeline):
    """Pipeline where agents collaborate on shared context."""

    async def run(
        self,
        initial_input: Dict[str, Any],
        pipe_output: bool = True,
    ) -> PipelineResult:
        """Run collaboratively, passing full history to each stage."""
        shared_context = {
            **initial_input,
            "collaboration_history": [],
        }
        stage_results: List[Dict[str, Any]] = []

        self.event_bus.publish(
            Event(
                event_type=EventType.PIPELINE_STARTED,
                source=self.name,
                payload={
                    "pipeline_id": self.pipeline_id,
                    "stage_count": len(self.stages),
                    "mode": "collaborative",
                },
            )
        )

        for idx, stage in enumerate(self.stages):
            task_input = {
                **shared_context,
                "message": shared_context.get("message", stage.name),
            }

            if stage.input_transform:
                task_input = stage.input_transform(task_input)

            task = TaskRequest(
                title=f"{self.name} - collaborative stage {idx + 1}: {stage.name}",
                description=stage.name,
                input_data=task_input,
            )

            self.event_bus.publish(
                Event(
                    event_type=EventType.TASK_EXECUTION_STARTED,
                    source=stage.agent.name,
                    payload={
                        "task_id": task.task_id,
                        "agent": stage.agent.name,
                        "pipeline_id": self.pipeline_id,
                        "stage": idx + 1,
                        "mode": "collaborative",
                    },
                    correlation_id=task.task_id,
                )
            )

            result = await stage.agent.execute(task)

            stage_output = {
                "stage": idx + 1,
                "name": stage.name,
                "agent": stage.agent.name,
                "success": result.success,
                "output": result.output,
                "error": result.error,
            }
            stage_results.append(stage_output)

            shared_context["collaboration_history"].append(stage_output)

            if result.success and pipe_output:
                response = result.output.get("response", "")
                shared_context["message"] = response
                if stage.output_transform:
                    shared_context.update(stage.output_transform(result))

            if not result.success:
                self.event_bus.publish(
                    Event(
                        event_type=EventType.PIPELINE_STAGE_FAILED,
                        source=self.name,
                        payload={
                            "pipeline_id": self.pipeline_id,
                            "stage": idx + 1,
                            "agent": stage.agent.name,
                            "error": result.error,
                        },
                        correlation_id=task.task_id,
                    )
                )
                self.event_bus.publish(
                    Event(
                        event_type=EventType.TASK_FAILED,
                        source=stage.agent.name,
                        payload={
                            "pipeline_id": self.pipeline_id,
                            "task_id": task.task_id,
                            "stage": idx + 1,
                            "error": result.error,
                        },
                        correlation_id=task.task_id,
                    )
                )
                return PipelineResult(
                    pipeline_id=self.pipeline_id,
                    success=False,
                    stages=stage_results,
                    error=result.error,
                )

            self.event_bus.publish(
                Event(
                    event_type=EventType.TASK_COMPLETED,
                    source=stage.agent.name,
                    payload={
                        "task_id": task.task_id,
                        "pipeline_id": self.pipeline_id,
                        "stage": idx + 1,
                        "mode": "collaborative",
                    },
                    correlation_id=task.task_id,
                )
            )

        self._results = stage_results
        self._completed.set()

        self.event_bus.publish(
            Event(
                event_type=EventType.PIPELINE_COMPLETED,
                source=self.name,
                payload={
                    "pipeline_id": self.pipeline_id,
                    "stages_completed": len(stage_results),
                    "mode": "collaborative",
                },
            )
        )

        return PipelineResult(
            pipeline_id=self.pipeline_id,
            success=True,
            stages=stage_results,
        )


def create_pipeline(
    name: str,
    agents: List[BaseAgent],
    event_bus: Optional[EventBus] = None,
    collaborative: bool = False,
    stage_names: Optional[List[str]] = None,
) -> Pipeline:
    """Create a pipeline from a list of agents.

    Args:
        name: Pipeline name.
        agents: Ordered list of agents to run.
        event_bus: Optional event bus to use.
        collaborative: Whether to use collaborative mode.
        stage_names: Optional names for each stage.

    Returns:
        A Pipeline instance.
    """
    stages = []
    for idx, agent in enumerate(agents):
        stage_name = (
            stage_names[idx]
            if stage_names and idx < len(stage_names)
            else f"stage_{idx + 1}"
        )
        stages.append(PipelineStage(name=stage_name, agent=agent))

    pipeline_cls = CollaborativePipeline if collaborative else Pipeline
    return pipeline_cls(name=name, stages=stages, event_bus=event_bus)
