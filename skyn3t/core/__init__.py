"""Core modules for SkyN3t orchestrator."""

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import Event, EventBus, EventType

__all__ = [
    "AgentCapability",
    "BaseAgent",
    "TaskRequest",
    "TaskResult",
    "Event",
    "EventBus",
    "EventType",
    "Orchestrator",
    "Pipeline",
    "CollaborativePipeline",
    "PipelineResult",
    "PipelineStage",
    "create_pipeline",
    "SelfHealingManager",
]


def __getattr__(name):
    if name == "Orchestrator":
        from skyn3t.core.orchestrator import Orchestrator
        return Orchestrator
    if name in {"Pipeline", "CollaborativePipeline", "PipelineResult", "PipelineStage", "create_pipeline"}:
        from skyn3t.core import pipeline
        return getattr(pipeline, name)
    if name == "SelfHealingManager":
        from skyn3t.core.self_healing import SelfHealingManager
        return SelfHealingManager
    raise AttributeError(name)
