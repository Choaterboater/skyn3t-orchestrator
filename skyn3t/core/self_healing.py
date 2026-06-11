"""Self-healing system for agents."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.memory.tuner import apply_adjustments_to_config

if TYPE_CHECKING:
    from skyn3t.core.orchestrator import Orchestrator

logger = logging.getLogger("skyn3t.core.self_healing")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class HealingAction:
    """A healing action to be performed."""

    agent_name: str
    action_type: str
    reason: str
    timestamp: datetime = field(default_factory=_utcnow)
    attempts: int = 0
    max_attempts: int = 3
    resolved: bool = False


class SelfHealingManager:
    """Manages self-healing of agents."""

    HEALING_ACTIONS: Dict[str, List[str]] = {
        "default": ["restart", "reset_queue", "isolate"],
        "timeout": ["restart", "increase_timeout", "isolate"],
        "error_rate": ["restart", "throttle", "isolate"],
        "memory": ["restart", "clear_cache", "isolate"],
    }

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self._orchestrator: Optional["Orchestrator"] = None
        self._lazy_healing_queue: Optional[asyncio.Queue[HealingAction]] = None
        self.healing_history: List[HealingAction] = []
        self._running = False
        self._healing_task: Optional[asyncio.Task] = None
        self._healing_handlers: Dict[str, Callable[[HealingAction], Any]] = {}
        self._isolated_agents: set[str] = set()

        # Register default handlers
        self.register_healing_handler("restart", self._handle_restart)
        self.register_healing_handler("reset_queue", self._handle_reset_queue)
        self.register_healing_handler("isolate", self._handle_isolate)
        self.register_healing_handler("throttle", self._handle_throttle)
        self.register_healing_handler("clear_cache", self._handle_clear_cache)
        self.register_healing_handler("increase_timeout", self._handle_increase_timeout)

    def set_orchestrator(self, orchestrator: "Orchestrator") -> None:
        """Attach the live orchestrator so healing actions can mutate agents."""
        self._orchestrator = orchestrator

    def register_healing_handler(
        self, action_type: str, handler: Callable[[HealingAction], Any]
    ) -> None:
        """Register a healing action handler."""
        self._healing_handlers[action_type] = handler

    @property
    def healing_queue(self) -> "asyncio.Queue[HealingAction]":
        if self._lazy_healing_queue is None:
            self._lazy_healing_queue = asyncio.Queue()
        return self._lazy_healing_queue

    def request_healing(self, agent_name: str, reason: str = "error_threshold") -> None:
        """Request healing for an agent (safe from sync or async context)."""
        action = HealingAction(
            agent_name=agent_name,
            action_type="default",
            reason=reason,
        )
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(self.healing_queue.put_nowait, action)
        except RuntimeError:
            self.healing_queue.put_nowait(action)

    async def start(self) -> None:
        """Start the healing manager."""
        self._running = True
        self._healing_task = asyncio.create_task(self._healing_loop())

    async def stop(self) -> None:
        """Stop the healing manager."""
        self._running = False
        # Wake the loop without waiting for the next 1s timeout.
        try:
            self.healing_queue.put_nowait(None)  # type: ignore[arg-type]
        except Exception:
            pass
        if self._healing_task:
            self._healing_task.cancel()
            try:
                await self._healing_task
            except asyncio.CancelledError:
                pass

    async def _healing_loop(self) -> None:
        """Main healing loop."""
        while self._running:
            try:
                action = await self.healing_queue.get()
                if action is None or not self._running:
                    break
                await self._perform_healing(action)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Healing loop error: {e}")
                await asyncio.sleep(1)

    async def _perform_healing(self, action: HealingAction) -> None:
        """Perform healing actions."""
        self.event_bus.publish(
            Event(
                event_type=EventType.SELF_HEAL_TRIGGERED,
                source="self_healing",
                payload={
                    "agent": action.agent_name,
                    "reason": action.reason,
                    "action_type": action.action_type,
                    "attempt": action.attempts + 1,
                },
            )
        )

        action_types = self.HEALING_ACTIONS.get(action.reason, self.HEALING_ACTIONS["default"])

        for healing_type in action_types:
            if action.attempts >= action.max_attempts:
                break

            action.attempts += 1
            handler = self._healing_handlers.get(healing_type)

            if handler:
                try:
                    await asyncio.wait_for(
                        self._run_handler(handler, action), timeout=30.0
                    )
                    action.resolved = True
                    break
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"Healing action {healing_type} failed: {e}")
                    continue

        self.healing_history.append(action)

    async def _run_handler(
        self, handler: Callable[[HealingAction], Any], action: HealingAction
    ) -> None:
        """Run a healing handler, supporting both sync and async."""
        result = handler(action)
        if asyncio.iscoroutine(result):
            await result

    def _agent(self, action: HealingAction):
        if self._orchestrator is None:
            return None
        return self._orchestrator.agents.get(action.agent_name)

    async def _handle_restart(self, action: HealingAction) -> None:
        """Restart the agent's task processor."""
        agent = self._agent(action)
        if agent is None:
            logger.warning("[SelfHeal] restart skipped — agent %s not found", action.agent_name)
            return
        logger.info("[SelfHeal] restarting agent: %s", action.agent_name)
        self._isolated_agents.discard(action.agent_name)
        try:
            await agent.shutdown()
            await agent.initialize()
            await agent.start()
            agent.status = "idle"
        except Exception:
            logger.exception("[SelfHeal] restart failed for %s", action.agent_name)
            raise

    async def _handle_reset_queue(self, action: HealingAction) -> None:
        """Drop queued tasks for a stuck agent."""
        agent = self._agent(action)
        if agent is None:
            return
        logger.info("[SelfHeal] resetting queue for agent: %s", action.agent_name)
        queue = getattr(agent, "_task_queue", None)
        if queue is None:
            return
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _handle_isolate(self, action: HealingAction) -> None:
        """Stop routing new tasks to the agent until a restart heal clears it."""
        agent = self._agent(action)
        if agent is None:
            return
        logger.info("[SelfHeal] isolating agent: %s", action.agent_name)
        agent._enabled = False
        self._isolated_agents.add(action.agent_name)

    async def _handle_throttle(self, action: HealingAction) -> None:
        """Apply a safe request-interval bump via live agent config."""
        agent = self._agent(action)
        if agent is None:
            return
        logger.info("[SelfHeal] throttling agent: %s", action.agent_name)
        agent.config = apply_adjustments_to_config(
            dict(agent.config or {}),
            [{"parameter": "request_interval", "reason": action.reason}],
        )

    async def _handle_clear_cache(self, action: HealingAction) -> None:
        """Clear ephemeral agent caches without touching persisted memory."""
        agent = self._agent(action)
        if agent is None:
            return
        logger.info("[SelfHeal] clearing cache for agent: %s", action.agent_name)
        for key in ("cache", "response_cache", "last_output"):
            agent.metadata.pop(key, None)
        if hasattr(agent, "_results"):
            agent._results.clear()

    async def _handle_increase_timeout(self, action: HealingAction) -> None:
        """Increase the agent timeout within tuner safety bounds."""
        agent = self._agent(action)
        if agent is None:
            return
        logger.info("[SelfHeal] increasing timeout for agent: %s", action.agent_name)
        agent.config = apply_adjustments_to_config(
            dict(agent.config or {}),
            [{"parameter": "timeout", "reason": action.reason}],
        )

    def get_healing_history(self) -> List[Dict[str, Any]]:
        """Get healing history."""
        return [
            {
                "agent": h.agent_name,
                "action_type": h.action_type,
                "reason": h.reason,
                "timestamp": h.timestamp.isoformat(),
                "attempts": h.attempts,
                "resolved": h.resolved,
            }
            for h in self.healing_history
        ]
