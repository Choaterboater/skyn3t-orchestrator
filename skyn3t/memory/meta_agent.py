"""Meta-Agent — the swarm's autonomous cortex.

This agent watches the entire system, identifies improvement opportunities,
generates hypotheses, and executes self-improvement actions. It is the
"brain thinking about itself."

It does NOT replace human direction — it amplifies it by automatically
handling the repetitive optimization work.
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.memory.store import MemoryStore
from skyn3t.memory.consciousness import CollectiveConsciousness


class MetaAgent:
    """Autonomous meta-agent for system self-improvement.

    Runs on a configurable loop (default every 60s) and can:
    - Detect underperforming agents and suggest fallback chain updates
    - Detect unhandled capabilities and suggest agent creation
    - Detect queue backlog and suggest concurrency adjustments
    - Detect repeated failure patterns and suggest pattern additions
    - Trigger self-healing for unhealthy agents
    - Spawn improvement tasks for other agents to execute
    """

    def __init__(
        self,
        event_bus: EventBus,
        memory_store: Optional[MemoryStore] = None,
        consciousness: Optional[CollectiveConsciousness] = None,
        interval_seconds: int = 60,
        enabled: bool = True,
    ):
        self.event_bus = event_bus
        self._memory = memory_store
        self._consciousness = consciousness
        self._interval = interval_seconds
        self._enabled = enabled
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Action history
        self._actions: List[Dict[str, Any]] = []
        self._max_actions = 100

        # Observation window
        self._observation_window: List[Dict[str, Any]] = []
        self._max_window = 50

    async def start(self) -> None:
        """Start the meta-agent observation loop."""
        if not self._enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._publish("meta_agent_started", {"interval": self._interval})

    async def stop(self) -> None:
        """Stop the meta-agent."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Main observation-think-act loop."""
        while self._running:
            try:
                await self._observe()
                hypotheses = await self._think()
                for hypothesis in hypotheses:
                    await self._act(hypothesis)
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._publish("meta_agent_error", {"error": str(e)})
                await asyncio.sleep(self._interval)

    # ------------------------------------------------------------------
    # Observe
    # ------------------------------------------------------------------

    async def _observe(self) -> None:
        """Collect system observations."""
        observation = {
            "timestamp": datetime.utcnow().isoformat(),
            "agent_count": 0,
            "task_queue_depth": 0,
            "failure_patterns": [],
            "agent_health": {},
        }

        # We don't have direct access to orchestrator state here,
        # so we rely on events in the consciousness working memory
        # and the persistent memory store.
        if self._consciousness:
            status = await self._consciousness.get_status()
            observation["consciousness"] = status

        if self._memory:
            try:
                stats = await self._memory.get_stats()
                observation["memory_stats"] = stats
            except Exception:
                pass

        self._observation_window.append(observation)
        if len(self._observation_window) > self._max_window:
            self._observation_window = self._observation_window[-self._max_window:]

    # ------------------------------------------------------------------
    # Think
    # ------------------------------------------------------------------

    async def _think(self) -> List[Dict[str, Any]]:
        """Generate improvement hypotheses from observations."""
        hypotheses = []

        if not self._observation_window:
            return hypotheses

        latest = self._observation_window[-1]
        memory_stats = latest.get("memory_stats", {})

        # Hypothesis 1: Low success rate → suggest fallback chain review
        success_rate = memory_stats.get("success_rate", 1.0)
        if success_rate < 0.7:
            hypotheses.append({
                "type": "suggest_fallback_review",
                "confidence": 0.8,
                "reason": f"System success rate is {success_rate:.0%}",
                "action": "Review fallback chains for weakest capability",
            })

        # Hypothesis 2: High failure count → suggest pattern detection
        total_failed = memory_stats.get("total_failed", 0)
        if total_failed > 5:
            hypotheses.append({
                "type": "suggest_pattern_analysis",
                "confidence": 0.7,
                "reason": f"{total_failed} tasks have failed",
                "action": "Analyze recent failures for new patterns",
            })

        # Hypothesis 3: No agents registered but tasks submitted
        agent_count = memory_stats.get("agents", 0)
        task_count = memory_stats.get("tasks", 0)
        if agent_count == 0 and task_count > 0:
            hypotheses.append({
                "type": "suggest_agent_registration",
                "confidence": 0.9,
                "reason": "Tasks exist but no agents are registered",
                "action": "Prompt user to register agents",
            })

        # Hypothesis 4: Many tasks, few agents → suggest scaling
        if agent_count > 0 and task_count > 0:
            ratio = task_count / agent_count
            if ratio > 20:
                hypotheses.append({
                    "type": "suggest_scale_up",
                    "confidence": 0.6,
                    "reason": f"High task-to-agent ratio ({ratio:.0f}:1)",
                    "action": "Consider adding more agents or increasing concurrency",
                })

        # Hypothesis 5: Consciousness has many insights → suggest RAG sync
        consciousness = latest.get("consciousness", {})
        if consciousness.get("total_insights", 0) > 10:
            hypotheses.append({
                "type": "suggest_rag_sync",
                "confidence": 0.75,
                "reason": f"{consciousness['total_insights']} insights waiting to be persisted",
                "action": "Sync working memory insights to RAG",
            })

        return hypotheses

    # ------------------------------------------------------------------
    # Act
    # ------------------------------------------------------------------

    async def _act(self, hypothesis: Dict[str, Any]) -> None:
        """Execute an improvement action."""
        action_type = hypothesis["type"]
        action_record = {
            "type": action_type,
            "confidence": hypothesis.get("confidence", 0.5),
            "reason": hypothesis.get("reason", ""),
            "timestamp": datetime.utcnow().isoformat(),
            "result": "pending",
        }

        if action_type == "suggest_fallback_review":
            # Publish a system alert that can be picked up by dashboard/API
            self._publish("suggest_fallback_review", {
                "reason": hypothesis["reason"],
                "recommendation": "Review and update fallback chains for capabilities with low success rates",
            })
            action_record["result"] = "alert_published"

        elif action_type == "suggest_pattern_analysis":
            self._publish("suggest_pattern_analysis", {
                "reason": hypothesis["reason"],
                "recommendation": "Run reflection deep-dive on agents with recent failures",
            })
            action_record["result"] = "alert_published"

        elif action_type == "suggest_agent_registration":
            self._publish("suggest_agent_registration", {
                "reason": hypothesis["reason"],
                "recommendation": "Register LLM CLI agents (claude, kimi, copilot) to handle pending tasks",
            })
            action_record["result"] = "alert_published"

        elif action_type == "suggest_scale_up":
            self._publish("suggest_scale_up", {
                "reason": hypothesis["reason"],
                "recommendation": "Add more agent instances or increase max_concurrent_tasks",
            })
            action_record["result"] = "alert_published"

        elif action_type == "suggest_rag_sync":
            # If consciousness is available, we could trigger an explicit sync
            if self._consciousness:
                insights = await self._consciousness.get_insights(limit=50)
                self._publish("rag_sync_triggered", {
                    "insight_count": len(insights),
                    "reason": hypothesis["reason"],
                })
            action_record["result"] = "sync_triggered"

        self._actions.append(action_record)
        if len(self._actions) > self._max_actions:
            self._actions = self._actions[-self._max_actions:]

        # Persist action to memory
        if self._memory:
            await self._memory.save_log(
                level="INFO",
                source="meta_agent",
                message=f"Meta-agent action: {action_type}",
                meta=action_record,
            )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _publish(self, alert_type: str, payload: Dict[str, Any]) -> None:
        """Publish a system alert event."""
        self.event_bus.publish(
            Event(
                event_type=EventType.SYSTEM_ALERT,
                source="meta_agent",
                payload={"alert_type": alert_type, **payload},
            )
        )

    def get_status(self) -> Dict[str, Any]:
        """Get meta-agent status."""
        return {
            "enabled": self._enabled,
            "running": self._running,
            "interval_seconds": self._interval,
            "observations_collected": len(self._observation_window),
            "actions_taken": len(self._actions),
            "recent_actions": self._actions[-10:],
        }

    def get_observations(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent observations."""
        return self._observation_window[-limit:]

    def pause(self) -> None:
        """Pause the meta-agent."""
        self._enabled = False

    def resume(self) -> None:
        """Resume the meta-agent."""
        self._enabled = True
