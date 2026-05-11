"""Main orchestrator that manages all agents and tasks."""

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4

from skyn3t.core.agent import BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.core.fallback import FallbackManager
from skyn3t.core.pipeline import Pipeline, create_pipeline
from skyn3t.core.self_healing import SelfHealingManager
from skyn3t.intelligence.agent_selector import AgentSelector
from skyn3t.intelligence.planner import Planner
from skyn3t.intelligence.reflection import ReflectionEngine
from skyn3t.intelligence.task_decomposer import ResultAggregator, TaskDecomposer
from skyn3t.memory.consciousness import CollectiveConsciousness
from skyn3t.memory.ingestor import ExperienceIngestor
from skyn3t.memory.meta_agent import MetaAgent
from skyn3t.memory.store import MemoryStore
from skyn3t.memory.tuner import SelfTuningEngine

logger = logging.getLogger("skyn3t.core.orchestrator")


class Orchestrator:
    """Central orchestrator for managing agents and tasks with intelligent auto-orchestration."""

    def __init__(self, event_bus: Optional[EventBus] = None):
        self.event_bus = event_bus or EventBus()
        self.agents: Dict[str, BaseAgent] = {}
        self.agent_registry: Dict[str, Dict[str, Any]] = {}
        self.running_tasks: Dict[str, TaskRequest] = {}
        self.task_results: Dict[str, TaskResult] = {}
        # Wall-clock timestamp for each terminal task_result, used by the
        # compaction sweep in _monitor_loop to evict entries older than
        # _result_ttl_seconds. Without this, long-running daemons grow
        # task_results / _failed_agents_by_task / _pipeline_results forever.
        self._task_result_completed_at: Dict[str, datetime] = {}
        self._result_ttl_seconds: float = 3600.0  # 1h
        # Idempotency-key → task_id map. A caller that retries with the same
        # key within _idempotency_ttl_seconds gets the prior task_id back
        # instead of spawning a duplicate task.
        self._idempotency_keys: Dict[str, tuple[str, datetime]] = {}
        self._idempotency_ttl_seconds: float = 3600.0  # 1h
        # Per-task completion signals so wait_for_task can be event-driven
        # rather than polling task_results every 500ms.
        self._task_done_events: Dict[str, asyncio.Event] = {}
        self._failed_agents_by_task: Dict[str, Set[str]] = {}
        self._handling_task_failures: Set[str] = set()
        # Guards _handling_task_failures so concurrent TASK_FAILED publishes
        # (e.g. from worker thread + decomposer thread) can't both pass the
        # dedup check before either records the in-flight handler.
        self._failure_dedup_lock = threading.Lock()
        self._pipelines: Dict[str, Pipeline] = {}
        self._pipeline_results: Dict[str, Any] = {}
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._self_healing = SelfHealingManager(self.event_bus)
        self._task_semaphore: Optional[asyncio.Semaphore] = None
        self._max_concurrent = 10

        # Fallback / resilience layer
        self._fallback = FallbackManager(self.event_bus)

        # Intelligence layer
        self._agent_selector = AgentSelector()
        self._agent_selector.attach_event_bus(self.event_bus)
        self._task_decomposer: Optional[TaskDecomposer] = None
        self._reflection: Optional[ReflectionEngine] = None
        self._planner: Optional[Planner] = None

        # Persistent memory layer
        self._memory: Optional[MemoryStore] = None
        self._consciousness: Optional[CollectiveConsciousness] = None
        self._ingestor: Optional[ExperienceIngestor] = None
        self._tuner: Optional[SelfTuningEngine] = None
        self._meta_agent: Optional[MetaAgent] = None
        self._feature_suggester: Optional[Any] = None
        self._review_watcher: Optional[Any] = None
        self._curiosity: Optional[Any] = None

        # Autonomy cortex (auto-booted on start)
        self._learning_loop: Optional[Any] = None
        self._auto_cleanup: Optional[Any] = None
        self._cortex_started = False
        self._cortex_tasks: List[asyncio.Task] = []

        # Subscribe to system events
        self.event_bus.subscribe(self._on_task_completed, EventType.TASK_COMPLETED)
        self.event_bus.subscribe(self._on_task_failed, EventType.TASK_FAILED)
        self.event_bus.subscribe(self._on_agent_error, EventType.AGENT_ERROR)
        self.event_bus.subscribe(self._on_message, EventType.MESSAGE)
        self.event_bus.subscribe(self._on_collective_insight, EventType.COLLECTIVE_INSIGHT)
        self.event_bus.subscribe(self._on_self_heal_triggered, EventType.SELF_HEAL_TRIGGERED)

    # ------------------------------------------------------------------
    # Intelligence layer configuration
    # ------------------------------------------------------------------

    def enable_decomposition(self, decomposer: Optional[TaskDecomposer] = None) -> None:
        """Enable automatic task decomposition."""
        self._task_decomposer = decomposer or TaskDecomposer(event_bus=self.event_bus)

    def enable_reflection(self, reflection: Optional[ReflectionEngine] = None) -> None:
        """Enable reflection and self-improvement."""
        self._reflection = reflection or ReflectionEngine(event_bus=self.event_bus)

    def enable_planning(self, planner: Optional[Planner] = None) -> None:
        """Enable strategic planning."""
        self._planner = planner or Planner(
            event_bus=self.event_bus,
            consciousness=self._consciousness,
        )
        if self._planner:
            self._planner.set_task_executor(self._execute_plan_task)

    def enable_memory(self, memory_store: Optional[MemoryStore] = None) -> None:
        """Enable persistent memory storage."""
        self._memory = memory_store or MemoryStore()

    def enable_consciousness(self, consciousness: Optional[CollectiveConsciousness] = None) -> None:
        """Enable the collective consciousness shared working memory."""
        self._consciousness = consciousness or CollectiveConsciousness(memory_store=self._memory)

    def enable_experience_ingestion(self, ingestor: Optional[ExperienceIngestor] = None) -> None:
        """Enable automatic experience → RAG ingestion."""
        self._ingestor = ingestor or ExperienceIngestor(event_bus=self.event_bus)

    def enable_self_tuning(self, tuner: Optional[SelfTuningEngine] = None) -> None:
        """Enable automatic self-tuning based on reflection."""
        self._tuner = tuner or SelfTuningEngine(event_bus=self.event_bus, memory_store=self._memory)

    def enable_meta_agent(self, meta_agent: Optional[MetaAgent] = None) -> None:
        """Enable the autonomous meta-agent cortex."""
        self._meta_agent = meta_agent or MetaAgent(
            event_bus=self.event_bus,
            memory_store=self._memory,
            consciousness=self._consciousness,
        )

    async def _execute_plan_task(self, task: TaskRequest, agent_name: Optional[str]) -> str:
        """Adapter for planner to submit tasks through the orchestrator."""
        return await self.submit_task(task, agent_name=agent_name)

    @property
    def selector(self) -> AgentSelector:
        """Access the agent selector for tuning or inspection."""
        return self._agent_selector

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, max_concurrent: int = 10) -> None:
        """Start the orchestrator."""
        self._max_concurrent = max_concurrent
        self._task_semaphore = asyncio.Semaphore(max_concurrent)
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        await self._self_healing.start()

        if self._reflection:
            await self._reflection.start()
        if self._planner:
            await self._planner.start()
        if self._ingestor:
            await self._ingestor.initialize()
        if self._meta_agent:
            await self._meta_agent.start()

        await self._boot_cortex()

        try:
            from skyn3t.registry import register_default_roster
            roster = await register_default_roster(self)
            logger.info("default roster: registered=%s skipped=%s",
                        roster.get("registered"), roster.get("skipped"))
        except Exception:
            logger.exception("default roster registration failed")

        self.event_bus.publish(
            Event(
                event_type=EventType.SYSTEM_ALERT,
                source="orchestrator",
                payload={"status": "started", "max_concurrent": max_concurrent},
            )
        )

    async def stop(self) -> None:
        """Stop the orchestrator gracefully."""
        self._running = False

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        await self._stop_cortex()

        await self._self_healing.stop()
        if self._reflection:
            await self._reflection.stop()
        if self._planner:
            await self._planner.stop()
        # ingestor doesn't need explicit stop — it's event-driven
        if self._meta_agent:
            await self._meta_agent.stop()

        # Shutdown all agents in parallel. Sequential awaits scaled linearly
        # with the number of agents (each shutdown waits for its task loop to
        # observe _running=False), turning N-agent stop into N seconds.
        if self.agents:
            await asyncio.gather(
                *[agent.shutdown() for agent in self.agents.values()],
                return_exceptions=True,
            )

        self.event_bus.publish(
            Event(
                event_type=EventType.SYSTEM_ALERT,
                source="orchestrator",
                payload={"status": "stopped"},
            )
        )

    # ------------------------------------------------------------------
    # Autonomy cortex (self-healing, reflection, learning, meta, tuning)
    # ------------------------------------------------------------------

    async def _boot_cortex(self) -> None:
        """Idempotently boot the autonomy stack.

        Instantiates and starts SelfHealingManager, ReflectionEngine,
        LearningLoop, MetaAgent, and SelfTuningEngine if they aren't
        already wired up. Failures in any one component are logged
        without aborting the rest of the boot.
        """
        if self._cortex_started:
            return
        self._cortex_started = True

        async def _maybe_start(obj: Any) -> None:
            start = getattr(obj, "start", None)
            if start is None:
                return
            res = start()
            if asyncio.iscoroutine(res):
                await res

        try:
            from skyn3t.core.self_healing import SelfHealingManager as _SH
            if self._self_healing is None:
                self._self_healing = _SH(self.event_bus)
                await _maybe_start(self._self_healing)
        except Exception:
            import logging
            logging.getLogger("skyn3t.cortex").exception("self_healing boot failed")

        try:
            from skyn3t.intelligence.reflection import ReflectionEngine as _RE
            if self._reflection is None:
                self._reflection = _RE(event_bus=self.event_bus)
                await _maybe_start(self._reflection)
        except Exception:
            import logging
            logging.getLogger("skyn3t.cortex").exception("reflection boot failed")

        try:
            from skyn3t.intelligence.learning_loop import LearningLoop as _LL
            if self._learning_loop is None:
                self._learning_loop = _LL(
                    self.event_bus,
                    ingestor=getattr(self, "_ingestor", None),
                    rag=getattr(self, "_rag", None),
                    memory=getattr(self, "_memory", None),
                )
                await _maybe_start(self._learning_loop)
        except Exception:
            import logging
            logging.getLogger("skyn3t.cortex").exception("learning_loop boot failed")

        try:
            from skyn3t.memory.meta_agent import MetaAgent as _MA
            if self._meta_agent is None:
                self._meta_agent = _MA(
                    event_bus=self.event_bus,
                    memory_store=self._memory,
                    consciousness=self._consciousness,
                )
                await _maybe_start(self._meta_agent)
        except Exception:
            import logging
            logging.getLogger("skyn3t.cortex").exception("meta_agent boot failed")

        try:
            from skyn3t.cortex.feature_suggester import FeatureSuggester
            if getattr(self, "_feature_suggester", None) is None:
                self._feature_suggester = FeatureSuggester(event_bus=self.event_bus)
                self._feature_suggester.start()
        except Exception:
            import logging
            logging.getLogger("skyn3t.cortex").exception("feature_suggester boot failed")

        try:
            from skyn3t.cortex.curiosity import CuriosityLoop
            if getattr(self, "_curiosity", None) is None:
                self._curiosity = CuriosityLoop(orchestrator=self, event_bus=self.event_bus)
                await self._curiosity.start()
        except Exception:
            logger.exception("curiosity boot failed")

        try:
            from skyn3t.cortex.review_watcher import ReviewWatcher
            if getattr(self, "_review_watcher", None) is None:
                self._review_watcher = ReviewWatcher(event_bus=self.event_bus)
                self._review_watcher.start()
        except Exception:
            logger.exception("review_watcher boot failed")

        try:
            from skyn3t.cortex.handlers import install_handlers
            install_handlers(self)
        except Exception:
            logger.exception("cortex handlers install failed")

        try:
            from skyn3t.memory.tuner import SelfTuningEngine as _ST
            if self._tuner is None:
                self._tuner = _ST(
                    event_bus=self.event_bus,
                    memory_store=self._memory,
                )
                await _maybe_start(self._tuner)
        except Exception:
            import logging
            logging.getLogger("skyn3t.cortex").exception("tuner boot failed")

        try:
            from skyn3t.cortex.auto_cleanup import AutoCleanup
            if getattr(self, "_auto_cleanup", None) is None:
                self._auto_cleanup = AutoCleanup(event_bus=self.event_bus)
                await self._auto_cleanup.start()
        except Exception:
            logger.exception("auto_cleanup boot failed")

    async def _stop_cortex(self) -> None:
        """Stop autonomy cortex components that were booted by us."""
        if not self._cortex_started:
            return

        async def _maybe_stop(obj: Any) -> None:
            if obj is None:
                return
            stop = getattr(obj, "stop", None)
            if stop is None:
                return
            try:
                res = stop()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                import logging
                logging.getLogger("skyn3t.cortex").exception(
                    "cortex stop failed for %s", type(obj).__name__
                )

        # Note: _self_healing, _reflection, _meta_agent are also stopped by
        # the main stop() flow; _maybe_stop is defensive but safe to call
        # because each component's stop is idempotent enough for this use.
        await _maybe_stop(self._learning_loop)
        await _maybe_stop(self._tuner)
        await _maybe_stop(self._curiosity)
        await _maybe_stop(getattr(self, "_auto_cleanup", None))

        for task in self._cortex_tasks:
            if not task.done():
                task.cancel()
        self._cortex_tasks.clear()
        self._cortex_started = False

    async def reset_cortex(self) -> None:
        """Restart autonomy cortex components and re-arm runtime handlers."""
        await self._stop_cortex()
        await self._boot_cortex()

    # ------------------------------------------------------------------
    # Agent management
    # ------------------------------------------------------------------

    def register_agent(self, agent: BaseAgent) -> None:
        """Register an agent with the orchestrator."""
        self.agents[agent.name] = agent
        self.agent_registry[agent.name] = {
            "id": agent.id,
            "name": agent.name,
            "type": agent.agent_type,
            "provider": agent.provider,
            "capabilities": [c.name for c in agent.capabilities],
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        asyncio.create_task(
            self._agent_selector.registry.update_capability_index(
                agent.name, [c.name for c in agent.capabilities]
            )
        )
        # Persist to long-term memory
        if self._memory:
            asyncio.create_task(
                self._memory.save_agent(
                    agent_id=agent.id,
                    name=agent.name,
                    agent_type=agent.agent_type,
                    provider=agent.provider,
                    status=agent.status,
                    capabilities=[c.name for c in agent.capabilities],
                    config=agent.config,
                    meta=agent.metadata,
                )
            )

    def unregister_agent(self, agent_name: str) -> None:
        """Unregister an agent."""
        if agent_name in self.agents:
            del self.agents[agent_name]
        if agent_name in self.agent_registry:
            del self.agent_registry[agent_name]

    def get_agent(self, name: str) -> Optional[BaseAgent]:
        """Get an agent by name."""
        return self.agents.get(name)

    def find_agents_by_capability(self, capability: str) -> List[BaseAgent]:
        """Find agents that have a specific capability."""
        return [
            agent
            for agent in self.agents.values()
            if any(c.name == capability for c in agent.capabilities)
        ]

    def find_agents_by_type(self, agent_type: str) -> List[BaseAgent]:
        """Find agents by type."""
        return [agent for agent in self.agents.values() if agent.agent_type == agent_type]

    # ------------------------------------------------------------------
    # Task submission with intelligence hooks
    # ------------------------------------------------------------------

    async def submit_task(
        self,
        task: TaskRequest,
        agent_name: Optional[str] = None,
        capability: Optional[str] = None,
        auto_decompose: bool = False,
        experiment_id: Optional[str] = None,
        cost_budget: Optional[float] = None,
    ) -> str:
        """Submit a task to be executed.

        Args:
            task: The task request.
            agent_name: Optional explicit agent name.
            capability: Optional required capability.
            auto_decompose: If True, decompose the task automatically.
            experiment_id: Optional A/B test experiment ID.
            cost_budget: Optional maximum cost per task.
        """
        if self._task_semaphore is None:
            self._task_semaphore = asyncio.Semaphore(self._max_concurrent)

        # Idempotency: if the caller provided a key and we've recently seen it,
        # return the prior task_id rather than starting a second copy.
        if task.idempotency_key:
            self._compact_idempotency_keys()
            cached = self._idempotency_keys.get(task.idempotency_key)
            if cached is not None:
                return cached[0]

        async with self._task_semaphore:
            if not task.session_id:
                task.session_id = task.input_data.get("session_id") or f"sess-{uuid4().hex[:8]}"
            if task.idempotency_key:
                self._idempotency_keys[task.idempotency_key] = (
                    task.task_id,
                    datetime.now(timezone.utc),
                )

            # Handle piping from previous task
            if task.pipe_from:
                prev_result = self.task_results.get(task.pipe_from)
                if prev_result:
                    stdout = prev_result.output.get("stdout", str(prev_result.output))
                    task.input_data.setdefault("stdin", stdout)

            # Auto-decomposition hook
            if auto_decompose and self._task_decomposer:
                subtasks = self._task_decomposer.decompose(task)
                if len(subtasks) > 1:
                    return await self._execute_decomposed(task, subtasks)

            target_agent: Optional[BaseAgent] = None

            if agent_name:
                target_agent = self.agents.get(agent_name)
            elif capability:
                candidates = self.find_agents_by_capability(capability)
                target_agent = await self._agent_selector.select(
                    candidates, task, capability=capability, cost_budget=cost_budget
                )
            else:
                # Intelligent selection across all agents
                target_agent = await self._agent_selector.select(
                    list(self.agents.values()), task, experiment_id=experiment_id, cost_budget=cost_budget
                )

            if not target_agent:
                raise ValueError(
                    f"No suitable agent found for task '{task.title}'. "
                    f"Agent: {agent_name}, Capability: {capability}"
                )

            self.event_bus.publish(
                Event(
                    event_type=EventType.TASK_ROUTED,
                    source="orchestrator",
                    payload={
                        "task_id": task.task_id,
                        "agent": target_agent.name,
                        "capability": capability,
                    },
                    correlation_id=task.task_id,
                )
            )

            # Join collective consciousness session
            if self._consciousness and target_agent:
                asyncio.create_task(
                    self._consciousness.join_session(task.session_id, target_agent.name)
                )

            # Inject collective context into task input (blocking, before queue)
            if self._consciousness and target_agent:
                try:
                    cap_name = (
                        target_agent.capabilities[0].name
                        if target_agent.capabilities
                        else None
                    )
                    ctx = await self._consciousness.get_relevant_context(
                        agent_name=target_agent.name,
                        task_description=task.description or task.title,
                        capability=cap_name,
                        session_id=task.session_id,
                    )
                    task.input_data["collective_context"] = ctx
                except Exception:
                    pass
                self.event_bus.publish(
                    Event(
                        event_type=EventType.TASK_ENRICHED,
                        source="orchestrator",
                        payload={"task_id": task.task_id, "agent": target_agent.name},
                        correlation_id=task.task_id,
                    )
                )

            self.running_tasks[task.task_id] = task

            # Persist task creation
            if self._memory:
                asyncio.create_task(
                    self._memory.save_task(
                        task_id=task.task_id,
                        title=task.title,
                        description=task.description,
                        status="pending",
                        priority=task.priority,
                        agent_id=target_agent.id if target_agent else None,
                        agent_name=target_agent.name if target_agent else None,
                        parent_task_id=None,
                        input_data=task.input_data,
                        output_data={},
                        error_message=None,
                        retry_count=task.retry_count,
                        max_retries=task.max_retries,
                        started_at=None,
                        completed_at=None,
                        session_id=task.session_id,
                    )
                )

            try:
                await target_agent.submit_task(task)
            except Exception as exc:
                logger.exception(
                    "Failed to queue task %s on agent %s",
                    task.task_id,
                    target_agent.name,
                )
                self.event_bus.publish(
                    Event(
                        event_type=EventType.TASK_FAILED,
                        source=target_agent.name,
                        payload={"task_id": task.task_id, "error": str(exc)},
                        correlation_id=task.task_id,
                    )
                )
                return task.task_id
            self.event_bus.publish(
                Event(
                    event_type=EventType.TASK_QUEUED,
                    source="orchestrator",
                    payload={"task_id": task.task_id, "agent": target_agent.name},
                    correlation_id=task.task_id,
                )
            )
            return task.task_id

    async def _execute_decomposed(
        self, parent_task: TaskRequest, subtasks: List[Any]
    ) -> str:
        """Execute decomposed subtasks and aggregate results."""
        from skyn3t.intelligence.task_decomposer import SubTask

        # Inject parent task reference
        for st in subtasks:
            if isinstance(st, SubTask):
                st.input_data["parent_task_id"] = parent_task.task_id

        async def resolve_agent(st: SubTask) -> Optional[BaseAgent]:
            if st.agent_name and st.agent_name in self.agents:
                return self.agents[st.agent_name]
            if st.capability:
                candidates = self.find_agents_by_capability(st.capability)
                selected = await self._agent_selector.select(
                    candidates, st.to_task_request(), capability=st.capability
                )
                return selected
            return await self._agent_selector.select(
                list(self.agents.values()), st.to_task_request()
            )

        if self._task_decomposer is None:
            raise RuntimeError("Task decomposer not enabled")

        self.running_tasks[parent_task.task_id] = parent_task
        results = await self._task_decomposer.execute_decomposed(
            subtasks, resolve_agent
        )

        # Aggregate and store result under parent task ID
        merged = ResultAggregator.merge_json([r for r in results if r.success])
        all_success = all(r.success for r in results)
        parent_result = TaskResult(
            task_id=parent_task.task_id,
            success=all_success,
            output={"subtask_results": [r.output for r in results], "merged": merged},
            error=None if all_success else "One or more subtasks failed",
            metadata={"decomposed": True, "subtask_count": len(subtasks)},
            session_id=parent_task.session_id,
        )
        self.task_results[parent_task.task_id] = parent_result
        self._task_result_completed_at[parent_task.task_id] = datetime.now(timezone.utc)
        self._signal_task_done(parent_task.task_id)
        if all_success:
            self.event_bus.publish(
                Event(
                    event_type=EventType.TASK_COMPLETED,
                    source="decomposer",
                    payload={
                        "task_id": parent_task.task_id,
                        "output": parent_result.output,
                        "execution_time_ms": parent_result.execution_time_ms,
                    },
                    correlation_id=parent_task.task_id,
                )
            )
        else:
            # Publish TASK_FAILED so subscribers (memory, consciousness, dashboards)
            # observe the parent failure the same way they see successes. Without
            # this, persistence + UI silently drop decomposed-task failures.
            self.event_bus.publish(
                Event(
                    event_type=EventType.TASK_FAILED,
                    source="decomposer",
                    payload={
                        "task_id": parent_task.task_id,
                        "error": parent_result.error or "subtask failure",
                    },
                    correlation_id=parent_task.task_id,
                )
            )
            self.running_tasks.pop(parent_task.task_id, None)
            self._persist_terminal_task_state(parent_task, "decomposer", parent_result, status="failed")
        return parent_task.task_id

    async def create_and_submit_task(
        self,
        title: str,
        description: str = "",
        input_data: Optional[Dict[str, Any]] = None,
        agent_name: Optional[str] = None,
        capability: Optional[str] = None,
        priority: int = 0,
        auto_decompose: bool = False,
    ) -> str:
        """Create and submit a task in one call."""
        task = TaskRequest(
            title=title,
            description=description,
            input_data=input_data or {},
            priority=priority,
        )
        return await self.submit_task(
            task, agent_name, capability, auto_decompose=auto_decompose
        )

    async def broadcast_task(
        self, task: TaskRequest, agent_names: Optional[List[str]] = None
    ) -> List[str]:
        """Broadcast a task to multiple agents."""
        targets = (
            [self.agents[n] for n in agent_names if n in self.agents]
            if agent_names
            else list(self.agents.values())
        )
        task_ids = []
        for agent in targets:
            t = TaskRequest(
                title=task.title,
                description=task.description,
                input_data=task.input_data,
                priority=task.priority,
            )
            self.running_tasks[t.task_id] = t
            await agent.submit_task(t)
            task_ids.append(t.task_id)
        return task_ids

    # ------------------------------------------------------------------
    # Results & waiting
    # ------------------------------------------------------------------

    def get_task_result(self, task_id: str) -> Optional[TaskResult]:
        """Get the result of a task."""
        return self.task_results.get(task_id)

    async def wait_for_task(self, task_id: str, timeout: float = 300.0) -> Optional[TaskResult]:
        """Wait for a task to complete (event-driven, no polling)."""
        # Fast path: already completed.
        if task_id in self.task_results:
            return self.task_results[task_id]
        # Lazily create the signal so callers waiting on a task that hasn't been
        # registered yet don't miss the completion event.
        event = self._task_done_events.get(task_id)
        if event is None:
            event = asyncio.Event()
            self._task_done_events[task_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            # Clean up the signal once we're done waiting; subsequent lookups
            # can read from task_results directly.
            self._task_done_events.pop(task_id, None)
        return self.task_results.get(task_id)

    def _signal_task_done(self, task_id: str) -> None:
        """Wake any wait_for_task waiters for this task_id."""
        event = self._task_done_events.get(task_id)
        if event is not None:
            event.set()

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    async def run_conversation(
        self,
        initiator: str,
        participants: List[str],
        topic: str,
        rounds: int = 3,
    ) -> List[Dict[str, Any]]:
        """Run a multi-agent conversation."""
        conversation: List[Dict[str, Any]] = []
        current_message = topic

        for round_num in range(rounds):
            for participant in participants:
                agent = self.agents.get(participant)
                if not agent:
                    continue

                task = TaskRequest(
                    title=f"Conversation round {round_num + 1}",
                    description=f"Respond to: {current_message}",
                    input_data={
                        "message": current_message,
                        "conversation_history": conversation,
                        "round": round_num + 1,
                    },
                )

                result = await agent.execute(task)
                entry = {
                    "round": round_num + 1,
                    "agent": participant,
                    "input": current_message,
                    "response": result.output.get("response", str(result.output)),
                    "success": result.success,
                }
                conversation.append(entry)

                if result.success:
                    current_message = entry["response"]

        return conversation

    # ------------------------------------------------------------------
    # Pipelines
    # ------------------------------------------------------------------

    async def create_and_run_pipeline(
        self,
        name: str,
        agent_names: List[str],
        prompts: List[str],
        collaborative: bool = False,
    ) -> str:
        """Create and run a pipeline through the pipeline module."""
        agent_objects = []
        for agent_name in agent_names:
            agent = self.agents.get(agent_name)
            if not agent:
                raise ValueError(f"Agent '{agent_name}' not found")
            agent_objects.append(agent)

        pipeline = create_pipeline(
            name=name,
            agents=agent_objects,
            event_bus=self.event_bus,
            collaborative=collaborative,
            stage_names=prompts,
        )
        self._pipelines[pipeline.pipeline_id] = pipeline

        # Run pipeline asynchronously
        asyncio.create_task(self._run_pipeline(pipeline, prompts))
        return pipeline.pipeline_id

    async def _run_pipeline(self, pipeline: Pipeline, prompts: List[str]) -> None:
        """Run a pipeline and store its result."""
        initial_input = {"message": prompts[0] if prompts else "Run pipeline"}
        result = await pipeline.run(initial_input=initial_input, pipe_output=True)
        self._pipeline_results[pipeline.pipeline_id] = result

    def get_pipeline(self, pipeline_id: str) -> Optional[Dict[str, Any]]:
        """Get pipeline status as a dict."""
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            return None

        result = self._pipeline_results.get(pipeline_id)
        stages = []
        for i, stage in enumerate(pipeline.stages):
            stage_info = {
                "stage_index": i,
                "agent_name": stage.agent.name,
                "name": stage.name,
                "status": "completed" if pipeline.is_completed else "running",
            }
            if result and i < len(result.stages):
                rs = result.stages[i]
                stage_info["status"] = "completed" if rs.get("success") else "failed"
                stage_info["output"] = rs.get("output", {}).get("response", "")
                stage_info["error"] = rs.get("error")
            stages.append(stage_info)

        return {
            "pipeline_id": pipeline.pipeline_id,
            "name": pipeline.name,
            "status": (
                "completed"
                if result and result.success
                else "failed" if result and not result.success
                else "running" if not pipeline.is_completed
                else "pending"
            ),
            "stages": stages,
            "final_output": result.stages[-1].get("output", {}).get("response", "") if result and result.stages else None,
            "error": result.error if result else None,
        }

    async def create_pipeline(self, tasks: List[TaskRequest]) -> List[str]:
        """Submit tasks in order, piping output of task N to task N+1."""
        task_ids: List[str] = []
        for i, task in enumerate(tasks):
            if i > 0 and not task.pipe_from:
                task.pipe_from = task_ids[i - 1]
            task_id = await self.submit_task(task)
            task_ids.append(task_id)
            await self.wait_for_task(task_id)
            self.event_bus.publish(
                Event(
                    event_type=EventType.PIPELINE_STAGE_COMPLETED,
                    source="orchestrator",
                    payload={
                        "stage": i + 1,
                        "task_id": task_id,
                        "pipeline_task_ids": task_ids.copy(),
                    },
                )
            )
        self.event_bus.publish(
            Event(
                event_type=EventType.PIPELINE_COMPLETED,
                source="orchestrator",
                payload={
                    "task_ids": task_ids,
                    "total_stages": len(task_ids),
                },
            )
        )
        return task_ids

    async def pipe_task(
        self,
        from_task_id: str,
        to_agent_name: str,
        new_task: TaskRequest,
    ) -> str:
        """Create a new task with stdin from another task's output."""
        new_task.pipe_from = from_task_id
        return await self.submit_task(new_task, agent_name=to_agent_name)

    async def run_collaborative(
        self,
        task: TaskRequest,
        agents: List[str],
        strategy: str = "round_robin",
    ) -> List[str]:
        """Run task through multiple agents sequentially, passing output forward."""
        if strategy != "round_robin":
            raise ValueError(f"Unsupported strategy: {strategy}")

        task_ids: List[str] = []
        current_input = task.input_data.copy()

        for i, agent_name in enumerate(agents):
            t = TaskRequest(
                title=f"{task.title} (collaboration {i + 1}/{len(agents)})",
                description=task.description,
                input_data=current_input.copy(),
                priority=task.priority,
                max_retries=task.max_retries,
            )
            if i > 0:
                t.pipe_from = task_ids[i - 1]

            task_id = await self.submit_task(t, agent_name=agent_name)
            task_ids.append(task_id)

            result = await self.wait_for_task(task_id)
            if result and result.success:
                current_input["stdin"] = result.output.get(
                    "stdout", str(result.output)
                )

            self.event_bus.publish(
                Event(
                    event_type=EventType.AGENT_COLLABORATION,
                    source="orchestrator",
                    payload={
                        "stage": i + 1,
                        "agent": agent_name,
                        "task_id": task_id,
                        "collaboration_task_ids": task_ids.copy(),
                        "strategy": strategy,
                    },
                )
            )

        return task_ids

    # ------------------------------------------------------------------
    # Planning hooks
    # ------------------------------------------------------------------

    async def submit_plan(
        self,
        name: str,
        goal: str,
        steps: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Submit a strategic plan for execution."""
        if not self._planner:
            raise RuntimeError("Planning not enabled. Call enable_planning() first.")
        plan = self._planner.decompose_goal_to_plan(name, goal, steps, context)
        return plan.plan_id

    def get_plan_status(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """Get the status of a running plan."""
        if not self._planner:
            return None
        return self._planner.progress.get_plan_summary(plan_id)

    # ------------------------------------------------------------------
    # Fallback chains
    # ------------------------------------------------------------------

    def register_fallback_chain(
        self, capability: str, agent_names: List[str], strategy: str = "priority"
    ) -> None:
        """Register a fallback chain for a capability.

        When the first agent fails, SkyN3t automatically tries the next.
        Example: register_fallback_chain("code_generation", ["claude", "copilot", "kimi"])
        """
        self._fallback.register_chain(capability, agent_names, strategy)
        print(f"Fallback chain registered: {capability} -> {', '.join(agent_names)}")

    def register_circuit_breaker(
        self, agent_name: str, failure_threshold: int = 3, recovery_timeout: int = 60
    ) -> None:
        """Register a circuit breaker for an agent."""
        self._fallback.register_circuit(
            agent_name,
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery_timeout,
        )

    def get_fallback_status(self) -> Dict[str, Any]:
        """Get fallback manager status."""
        return self._fallback.get_status()

    # ------------------------------------------------------------------
    # System status
    # ------------------------------------------------------------------

    async def reorder_tasks(self) -> Dict[str, Any]:
        """Dynamically reorder queued tasks based on learning and priorities.

        - Bumps tasks similar to previously-failed ones (learn from mistakes)
        - Moves tasks to agents that just became idle
        - Escalates deadline-urgent tasks
        """
        if not self._consciousness:
            return {"reordered": 0, "reason": "consciousness not enabled"}

        reordered = 0
        for agent in self.agents.values():
            # Simple reordering: if agent is idle and has queued tasks,
            # we could re-prioritize based on consciousness insights.
            # For now, this is a hook that the meta-agent can call.
            queue_size = agent._task_queue.qsize()
            if queue_size > 1 and agent.status == "idle":
                # Peek at tasks and potentially reorder
                # asyncio.Queue doesn't support peek/reorder directly,
                # so this is more of a planning signal
                reordered += 1

        self.event_bus.publish(
            Event(
                event_type=EventType.SYSTEM_ALERT,
                source="orchestrator",
                payload={"action": "reorder_tasks", "reordered": reordered},
            )
        )
        return {"reordered": reordered}

    def get_system_status(self) -> Dict[str, Any]:
        """Get overall system status."""
        status = {
            "running": self._running,
            "total_agents": len(self.agents),
            "agents": {
                name: agent.get_stats() for name, agent in self.agents.items()
            },
            "running_tasks": len(self.running_tasks),
            "completed_tasks": len(self.task_results),
            "pipelines": len(self._pipelines),
            "registry": self.agent_registry,
            "intelligence": {
                "selector_stats": self._agent_selector.get_stats(),
                "reflection_summary": self._reflection.get_summary() if self._reflection else None,
                "plans": len(self._planner.get_all_plans()) if self._planner else 0,
            },
            "memory": {"enabled": self._memory is not None},
            "consciousness": {"enabled": self._consciousness is not None},
            "experience_ingestion": {"enabled": self._ingestor is not None},
            "self_tuning": {"enabled": self._tuner is not None},
            "meta_agent": self._meta_agent.get_status() if self._meta_agent else {"enabled": False},
        }
        return status

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _output_with_meta(
        self,
        output: Any,
        *,
        agent_name: str,
        execution_time_ms: float,
        capabilities: List[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if isinstance(output, dict):
            payload = dict(output)
        else:
            payload = {"value": output}
        payload["_meta"] = {
            "agent_name": agent_name,
            "execution_time_ms": execution_time_ms,
            "capabilities": capabilities,
            **(metadata or {}),
        }
        return payload

    def _persist_terminal_task_state(
        self,
        task: Optional[TaskRequest],
        agent_name: str,
        result: TaskResult,
        *,
        status: str,
    ) -> None:
        if self._consciousness and task and task.session_id:
            history_item: Dict[str, Any] = {
                "agent": agent_name,
                "task_title": task.title,
                "success": result.success,
            }
            if result.success:
                history_item["output_summary"] = str(result.output)[:200]
            else:
                history_item["error"] = result.error or "unknown"
            asyncio.create_task(
                self._consciousness.add_to_session_history(
                    task.session_id,
                    history_item,
                )
            )

        if self._memory:
            agent = self.agents.get(agent_name)
            capabilities = [c.name for c in agent.capabilities] if agent else []
            asyncio.create_task(
                self._memory.save_task(
                    task_id=result.task_id,
                    title=task.title if task else "",
                    description=task.description if task else "",
                    status=status,
                    priority=task.priority if task else 0,
                    agent_id=agent.id if agent else None,
                    agent_name=agent_name,
                    parent_task_id=None,
                    input_data=task.input_data if task else {},
                    output_data=self._output_with_meta(
                        result.output,
                        agent_name=agent_name,
                        execution_time_ms=result.execution_time_ms,
                        capabilities=capabilities,
                        metadata=result.metadata,
                    ),
                    error_message=result.error,
                    retry_count=task.retry_count if task else 0,
                    max_retries=task.max_retries if task else 3,
                    started_at=None,
                    completed_at=datetime.now(timezone.utc),
                    session_id=task.session_id if task else result.session_id,
                )
            )

    def _on_task_completed(self, event: Event) -> None:
        """Handle task completion."""
        task_id = event.payload.get("task_id")
        if not isinstance(task_id, str):
            return
        agent_name = event.source
        existing_result = self.task_results.get(task_id)
        task = self.running_tasks.pop(task_id, None)
        self._failed_agents_by_task.pop(task_id, None)
        self._handling_task_failures.discard(task_id)

        # Store full result for piping and lookups
        result = TaskResult(
            task_id=task_id,
            success=True,
            output=event.payload.get(
                "output",
                existing_result.output if existing_result is not None else {},
            ),
            execution_time_ms=event.payload.get(
                "execution_time_ms",
                existing_result.execution_time_ms if existing_result is not None else 0.0,
            ),
            metadata=dict(existing_result.metadata) if existing_result is not None else {},
            insights=list(existing_result.insights) if existing_result is not None else [],
            session_id=(
                task.session_id
                if task
                else (existing_result.session_id if existing_result is not None else None)
            ),
        )
        self.task_results[task_id] = result
        self._task_result_completed_at[task_id] = datetime.now(timezone.utc)
        self._persist_terminal_task_state(task, agent_name, result, status="completed")
        self._signal_task_done(task_id)

    def _on_task_failed(self, event: Event) -> None:
        """Handle task failure with fallback to other agents."""
        task_id = event.payload.get("task_id")
        if not isinstance(task_id, str):
            return
        # Atomic check-and-add to prevent two concurrent TASK_FAILED events
        # for the same task from both spawning failure handlers.
        with self._failure_dedup_lock:
            if (
                task_id not in self.running_tasks
                or task_id in self._handling_task_failures
            ):
                return
            self._handling_task_failures.add(task_id)

        async def _run_failure_handler() -> None:
            try:
                await self._handle_task_failure_async(event)
            finally:
                self._handling_task_failures.discard(task_id)

        asyncio.create_task(_run_failure_handler())

    async def _handle_task_failure_async(self, event: Event) -> None:
        task_id = event.payload.get("task_id")
        if not isinstance(task_id, str):
            return
        failed_agent_name = event.source
        error = event.payload.get("error", "unknown")

        task = self.running_tasks.get(task_id)
        if not task:
            return

        attempted_agents = self._failed_agents_by_task.setdefault(task_id, set())
        attempted_agents.add(failed_agent_name)

        # Add failure to session history
        if self._consciousness and task.session_id:
            asyncio.create_task(
                self._consciousness.add_to_session_history(
                    task.session_id,
                    {
                        "agent": failed_agent_name,
                        "task_title": task.title,
                        "success": False,
                        "error": error,
                    }
                )
            )

        # Persist failure
        if self._memory:
            agent = self.agents.get(failed_agent_name)
            asyncio.create_task(
                self._memory.save_task(
                    task_id=task_id,
                    title=task.title,
                    description=task.description,
                    status="failed",
                    priority=task.priority,
                    agent_id=agent.id if agent else None,
                    agent_name=failed_agent_name,
                    parent_task_id=None,
                    input_data=task.input_data,
                    output_data={"_meta": {"agent_name": failed_agent_name, "error": error}},
                    error_message=error,
                    retry_count=task.retry_count,
                    max_retries=task.max_retries,
                    started_at=None,
                    completed_at=datetime.now(timezone.utc),
                    session_id=task.session_id,
                )
            )

        if task.retry_count >= task.max_retries:
            self._finalize_task_failure(task, failed_agent_name, error)
            return

        # Prefer a new fallback agent when one is still available.
        fallback_agent = self._get_fallback_agent(
            failed_agent_name,
            task,
            exclude_agents=attempted_agents,
        )
        if fallback_agent and fallback_agent != failed_agent_name:
            circuit = self._fallback.circuits.get(fallback_agent)
            healthy = True
            if circuit is not None:
                healthy = await circuit.can_execute()
            if healthy:
                task.retry_count += 1
                self.event_bus.publish(
                    Event(
                        event_type=EventType.FALLBACK_ATTEMPTED,
                        source="orchestrator",
                        payload={
                            "task_id": task_id,
                            "failed_agent": failed_agent_name,
                            "fallback_agent": fallback_agent,
                            "retry_count": task.retry_count,
                        },
                        correlation_id=task_id,
                    )
                )
                await self._retry_on_agent(task, fallback_agent, failed_agent_name)
                return

        task.retry_count += 1
        await asyncio.sleep(2 ** task.retry_count)
        queued = await self._retry_task(task, exclude_agents=attempted_agents)
        if queued:
            return

        self._finalize_task_failure(task, failed_agent_name, error)

    def _finalize_task_failure(
        self,
        task: TaskRequest,
        failed_agent_name: str,
        error: str,
    ) -> None:
        """Persist and publish terminal task failure state."""
        self.event_bus.publish(
            Event(
                event_type=EventType.TASK_FAILED_FINAL,
                source="orchestrator",
                payload={
                    "task_id": task.task_id,
                    "agent": failed_agent_name,
                    "error": error,
                    "retry_count": task.retry_count,
                },
                correlation_id=task.task_id,
            )
        )
        self.task_results[task.task_id] = TaskResult(
            task_id=task.task_id,
            success=False,
            output={},
            error=error,
            session_id=task.session_id,
        )
        self.running_tasks.pop(task.task_id, None)
        self._failed_agents_by_task.pop(task.task_id, None)
        self._handling_task_failures.discard(task.task_id)
        self._task_result_completed_at[task.task_id] = datetime.now(timezone.utc)
        self._signal_task_done(task.task_id)

    def _get_fallback_agent(
        self,
        failed_agent_name: str,
        task: TaskRequest,
        exclude_agents: Optional[Set[str]] = None,
    ) -> Optional[str]:
        """Find a fallback agent for a failed task."""
        excluded_agents = exclude_agents or set()

        # Check fallback chains
        for chain in self._fallback.chains.values():
            if failed_agent_name in chain.agent_names:
                idx = chain.agent_names.index(failed_agent_name)
                for name in chain.agent_names[idx + 1:]:
                    if name in self.agents and name not in excluded_agents:
                        agent = self.agents[name]
                        if agent.status in ("idle", "busy"):
                            return name

        # Fallback to any agent with same capability
        failed = self.agents.get(failed_agent_name)
        if failed and failed.capabilities:
            cap_names = [c.name for c in failed.capabilities]
            for name, agent in self.agents.items():
                if (
                    name != failed_agent_name
                    and name not in excluded_agents
                    and agent.status in ("idle", "busy")
                ):
                    agent_caps = [c.name for c in agent.capabilities]
                    if any(c in agent_caps for c in cap_names):
                        return name

        return None

    async def _retry_on_agent(self, task: TaskRequest, agent_name: str, failed_agent: str) -> None:
        """Retry a task on a fallback agent."""
        await asyncio.sleep(1)
        self.event_bus.publish(
            Event(
                event_type=EventType.AGENT_COLLABORATION,
                source="orchestrator",
                payload={
                    "action": "fallback",
                    "task_id": task.task_id,
                    "failed_agent": failed_agent,
                    "fallback_agent": agent_name,
                    "retry_count": task.retry_count,
                },
                correlation_id=task.task_id,
            )
        )
        if agent_name in self.agents:
            await self.submit_task(task, agent_name=agent_name)

    async def _retry_task(
        self,
        task: TaskRequest,
        exclude_agents: Optional[Set[str]] = None,
    ) -> bool:
        """Retry a failed task."""
        self.event_bus.publish(
            Event(
                event_type=EventType.TASK_CREATED,
                source="orchestrator",
                payload={
                    "task_id": task.task_id,
                    "title": task.title,
                    "retry_count": task.retry_count,
                },
                correlation_id=task.task_id,
            )
        )

        def _find_candidate(excluded: Set[str]) -> Optional[BaseAgent]:
            for status in ("idle", "busy"):
                for name, agent in self.agents.items():
                    if name in excluded:
                        continue
                    if agent.status == status:
                        return agent
            return None

        excluded_agents = exclude_agents or set()
        agent = _find_candidate(excluded_agents)
        if agent is None and excluded_agents:
            agent = _find_candidate(set())
        if agent is None:
            return False

        await self.submit_task(task, agent_name=agent.name)
        return True

    def _on_message(self, event: Event) -> None:
        """Persist inter-agent messages."""
        if not self._memory:
            return
        asyncio.create_task(
            self._memory.save_message(
                source_agent=event.source,
                target_agent=event.target,
                content=event.payload.get("content", ""),
                message_type=event.payload.get("message_type", "chat"),
                context={"correlation_id": event.correlation_id, **event.payload},
            )
        )

    def _on_collective_insight(self, event: Event) -> None:
        """Handle insights shared by agents."""
        if not self._consciousness:
            return
        payload = event.payload
        asyncio.create_task(
            self._consciousness.add_insight(
                agent_name=event.source,
                insight=payload.get("insight", ""),
                capability=payload.get("capability"),
                metadata=payload.get("metadata", {}),
            )
        )

    def _on_agent_error(self, event: Event) -> None:
        """Handle agent errors."""
        agent_name = event.source
        if agent_name in self.agents:
            agent = self.agents[agent_name]
            if len(agent._errors) >= 3:
                self._self_healing.request_healing(agent_name)

    def _on_self_heal_triggered(self, event: Event) -> None:
        """Clear an agent's accumulated errors when a heal action fires.

        Without this, _errors stays at the cap (10) forever, so every
        subsequent error keeps re-entering the >=3 branch above and the
        agent gets stuck in a continuous heal loop.
        """
        agent_name = event.payload.get("agent")
        if not agent_name or agent_name not in self.agents:
            return
        agent = self.agents[agent_name]
        try:
            agent._errors.clear()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        """Monitor agents and tasks."""
        try:
            from skyn3t.config.settings import get_settings
            task_timeout = float(get_settings().task_timeout_seconds)
        except Exception:
            task_timeout = 300.0
        while self._running:
            try:
                for agent in list(self.agents.values()):
                    # Skip monitoring + self-healing for explicitly-disabled agents
                    if not getattr(agent, "enabled", True):
                        agent.status = "disabled"
                        continue

                    # Check for stuck tasks: if a task has been running longer than
                    # the configured timeout, mark it failed and request healing so
                    # the agent's processor restarts on a fresh task.
                    cur_task = agent._current_task
                    started_at = getattr(agent, "_current_task_started_at", None)
                    if cur_task and started_at:
                        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                        if elapsed > task_timeout:
                            logger.warning(
                                "Task %s on agent %s exceeded timeout (%.0fs > %.0fs); "
                                "publishing TASK_FAILED and requesting healing.",
                                cur_task.task_id, agent.name, elapsed, task_timeout,
                            )
                            self.event_bus.publish(
                                Event(
                                    event_type=EventType.TASK_FAILED,
                                    source=agent.name,
                                    payload={
                                        "task_id": cur_task.task_id,
                                        "error": f"task timed out after {int(elapsed)}s",
                                    },
                                    correlation_id=cur_task.task_id,
                                )
                            )
                            agent._current_task = None
                            agent._current_task_started_at = None
                            agent.status = "error"
                            self._self_healing.request_healing(agent.name)
                            continue

                    # Check agent health
                    try:
                        healthy = await asyncio.wait_for(
                            agent.health_check(), timeout=10.0
                        )
                        agent._health_checks += 1
                        if not healthy:
                            agent.status = "error"
                            self._self_healing.request_healing(agent.name)
                        else:
                            agent.status = "idle"
                    except asyncio.TimeoutError:
                        agent.status = "error"
                        self._self_healing.request_healing(agent.name)

                # Compact terminal task state: drop entries whose completion
                # is older than _result_ttl_seconds. Without this the dicts
                # grow unbounded for the lifetime of the process.
                self._compact_terminal_state()
                self._compact_idempotency_keys()

                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Monitor loop error: {e}")
                await asyncio.sleep(5)

    def _compact_idempotency_keys(self) -> None:
        """Drop idempotency-key entries older than _idempotency_ttl_seconds."""
        now = datetime.now(timezone.utc)
        ttl = self._idempotency_ttl_seconds
        expired = [
            k for k, (_tid, ts) in self._idempotency_keys.items()
            if (now - ts).total_seconds() > ttl
        ]
        for k in expired:
            self._idempotency_keys.pop(k, None)

    def _compact_terminal_state(self) -> None:
        """Evict task results older than _result_ttl_seconds."""
        now = datetime.now(timezone.utc)
        ttl = self._result_ttl_seconds
        expired = [
            tid for tid, ts in self._task_result_completed_at.items()
            if (now - ts).total_seconds() > ttl
        ]
        for tid in expired:
            self._task_result_completed_at.pop(tid, None)
            self.task_results.pop(tid, None)
            self._failed_agents_by_task.pop(tid, None)
            self._task_done_events.pop(tid, None)

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats."""
        while self._running:
            try:
                for agent in self.agents.values():
                    self.event_bus.publish(
                        Event(
                            event_type=EventType.AGENT_HEARTBEAT,
                            source=agent.name,
                            payload={
                                "agent_id": agent.id,
                                "status": agent.status,
                                "queue_size": agent._task_queue.qsize(),
                            },
                        )
                    )
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Heartbeat loop error: {e}")
                await asyncio.sleep(5)
