"""Crash recovery system."""

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from skyn3t.core.events import EventBus
from skyn3t.persistence.checkpoint import CheckpointManager

_logger = logging.getLogger("skyn3t.memory.recovery")


class RecoveryManager:
    """Manages crash recovery from checkpoints."""

    def __init__(
        self,
        checkpoint_manager: CheckpointManager,
        event_bus: EventBus,
    ):
        self.checkpoint_manager = checkpoint_manager
        self.event_bus = event_bus
        self.recovery_log: List[Dict[str, Any]] = []

    async def recover(
        self,
        orchestrator: Any,
    ) -> Dict[str, Any]:
        """Recover system state from the latest checkpoint."""
        checkpoint = self.checkpoint_manager.load_latest()

        if not checkpoint:
            return {
                "recovered": False,
                "reason": "No checkpoint found",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        recovery_report: Dict[str, Any] = {
            "recovered": True,
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_timestamp": checkpoint.timestamp,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agents_recovered": 0,
            "tasks_recovered": 0,
            "pipelines_recovered": 0,
            "errors": [],
        }

        # Restore agent states
        for agent_state in checkpoint.agent_states:
            try:
                # Re-register agent with saved state
                agent_state_copy = dict(agent_state)
                agent_name = agent_state_copy.get("name")
                if agent_name and agent_name not in orchestrator.agents:
                    # Agent needs to be recreated - log for manual intervention
                    recovery_report["errors"].append(
                        f"Agent '{agent_name}' not available for recovery"
                    )
                elif agent_name:
                    agent = orchestrator.agents[agent_name]
                    # Restore metadata
                    if "metadata" in agent_state_copy:
                        agent.metadata.update(agent_state_copy["metadata"])
                    recovery_report["agents_recovered"] += 1
            except Exception as e:
                recovery_report["errors"].append(str(e))

        # Restore task states
        if checkpoint.task_states:
            _logger.warning(
                "Task state recovery is not fully implemented; %d task states will be counted but not restored.",
                len(checkpoint.task_states),
            )
        for task_state in checkpoint.task_states:
            try:
                recovery_report["tasks_recovered"] += 1
            except Exception as e:
                recovery_report["errors"].append(str(e))

        # Restore pipeline states
        if checkpoint.pipeline_states:
            _logger.warning(
                "Pipeline state recovery is not fully implemented; %d pipeline states will be counted but not restored.",
                len(checkpoint.pipeline_states),
            )
        for pipeline_state in checkpoint.pipeline_states:
            try:
                recovery_report["pipelines_recovered"] += 1
            except Exception as e:
                recovery_report["errors"].append(str(e))

        self.recovery_log.append(recovery_report)

        # Publish recovery event
        from skyn3t.core.events import Event, EventType

        self.event_bus.publish(
            Event(
                event_type=EventType.SYSTEM_ALERT,
                source="recovery_manager",
                payload={
                    "action": "recovery_completed",
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "agents_recovered": recovery_report["agents_recovered"],
                    "tasks_recovered": recovery_report["tasks_recovered"],
                },
            )
        )

        return recovery_report

    def create_checkpoint(self, orchestrator: Any) -> Optional[str]:
        """Persist the current orchestrator state and return the checkpoint id.

        Best-effort: any exception is logged and ``None`` is returned so a
        checkpoint failure does not crash the monitor loop or shutdown path.
        """
        try:
            agent_states = []
            for name, agent in getattr(orchestrator, "agents", {}).items():
                try:
                    state: Dict[str, Any] = {"name": name}
                    if hasattr(agent, "metadata"):
                        state["metadata"] = dict(agent.metadata)
                    if hasattr(agent, "get_stats"):
                        state["stats"] = agent.get_stats()
                    agent_states.append(state)
                except Exception:
                    _logger.debug("failed to serialize agent state for %s", name, exc_info=True)

            task_states = []
            for task_id, task in getattr(orchestrator, "running_tasks", {}).items():
                try:
                    task_states.append(
                        {
                            "task_id": task_id,
                            "task": asdict(task) if hasattr(task, "__dataclass_fields__") else dict(getattr(task, "__dict__", {})),
                        }
                    )
                except Exception:
                    _logger.debug("failed to serialize task state for %s", task_id, exc_info=True)

            pipeline_states = []
            for pipeline_id, pipeline in getattr(orchestrator, "_pipelines", {}).items():
                try:
                    pipeline_states.append(
                        {
                            "pipeline_id": pipeline_id,
                            "pipeline": dict(getattr(pipeline, "__dict__", {})),
                        }
                    )
                except Exception:
                    _logger.debug("failed to serialize pipeline state for %s", pipeline_id, exc_info=True)

            return self.checkpoint_manager.save(
                agent_states=agent_states,
                task_states=task_states,
                pipeline_states=pipeline_states,
            )
        except Exception:
            _logger.exception("recovery checkpoint creation failed")
            return None

    def get_recovery_log(self) -> List[Dict[str, Any]]:
        """Get recovery history."""
        return self.recovery_log
