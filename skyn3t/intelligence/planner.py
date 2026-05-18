"""Strategic planning with goal decomposition, progress tracking, and adaptive execution."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Awaitable, Callable, Dict, List, Optional
from uuid import uuid4

from skyn3t.core.agent import BaseAgent, TaskRequest
from skyn3t.core.events import Event, EventBus, EventType

logger = logging.getLogger("skyn3t.intelligence.planner")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MilestoneStatus(Enum):
    PENDING = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    BLOCKED = auto()
    SKIPPED = auto()


class PlanStatus(Enum):
    DRAFT = auto()
    ACTIVE = auto()
    PAUSED = auto()
    COMPLETED = auto()
    FAILED = auto()
    ADAPTED = auto()


@dataclass
class Milestone:
    """A milestone within a strategic plan."""

    milestone_id: str = field(default_factory=lambda: str(uuid4()))
    title: str = ""
    description: str = ""
    status: MilestoneStatus = MilestoneStatus.PENDING
    priority: int = 0
    dependencies: List[str] = field(default_factory=list)
    assigned_agents: List[str] = field(default_factory=list)
    required_capabilities: List[str] = field(default_factory=list)
    estimated_duration_minutes: Optional[int] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    task_ids: List[str] = field(default_factory=list)
    result_summary: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        return self.status == MilestoneStatus.PENDING

    @property
    def duration_seconds(self) -> Optional[float]:
        if not self.started_at:
            return None
        end = self.completed_at or _utcnow()
        return (end - self.started_at).total_seconds()


@dataclass
class Plan:
    """A strategic plan composed of milestones."""

    plan_id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    goal: str = ""
    status: PlanStatus = PlanStatus.DRAFT
    milestones: List[Milestone] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    context: Dict[str, Any] = field(default_factory=dict)
    adaptation_count: int = 0
    max_adaptations: int = 5

    @property
    def progress(self) -> float:
        if not self.milestones:
            return 0.0
        completed = sum(1 for m in self.milestones if m.status == MilestoneStatus.COMPLETED)
        return completed / len(self.milestones)

    @property
    def is_done(self) -> bool:
        return self.status in (PlanStatus.COMPLETED, PlanStatus.FAILED)

    def get_ready_milestones(self) -> List[Milestone]:
        completed_ids = {
            m.milestone_id for m in self.milestones if m.status == MilestoneStatus.COMPLETED
        }
        return [
            m
            for m in self.milestones
            if m.status == MilestoneStatus.PENDING
            and all(d in completed_ids for d in m.dependencies)
        ]

    def get_blocked_milestones(self) -> List[Milestone]:
        failed_or_blocked = {
            m.milestone_id
            for m in self.milestones
            if m.status in (MilestoneStatus.BLOCKED, MilestoneStatus.SKIPPED)
        }
        return [
            m
            for m in self.milestones
            if m.status == MilestoneStatus.PENDING
            and any(d in failed_or_blocked for d in m.dependencies)
        ]


class ProgressTracker:
    """Tracks progress of active plans and milestones."""

    def __init__(self, event_bus: Optional[EventBus] = None):
        self._plans: Dict[str, Plan] = {}
        self._event_bus = event_bus

    def register_plan(self, plan: Plan) -> None:
        self._plans[plan.plan_id] = plan

    def update_milestone(
        self, plan_id: str, milestone_id: str, status: MilestoneStatus, **kwargs
    ) -> bool:
        plan = self._plans.get(plan_id)
        if not plan:
            return False
        for m in plan.milestones:
            if m.milestone_id == milestone_id:
                old_status = m.status
                m.status = status
                if status == MilestoneStatus.IN_PROGRESS and not m.started_at:
                    m.started_at = _utcnow()
                if status == MilestoneStatus.COMPLETED and not m.completed_at:
                    m.completed_at = _utcnow()
                for k, v in kwargs.items():
                    setattr(m, k, v)

                if self._event_bus:
                    self._event_bus.publish(
                        Event(
                            event_type=EventType.SYSTEM_ALERT,
                            source="planner",
                            payload={
                                "plan_id": plan_id,
                                "milestone_id": milestone_id,
                                "old_status": old_status.name,
                                "new_status": status.name,
                                "plan_progress": plan.progress,
                            },
                        )
                    )
                return True
        return False

    def get_plan_summary(self, plan_id: str) -> Optional[Dict[str, Any]]:
        plan = self._plans.get(plan_id)
        if not plan:
            return None
        return {
            "plan_id": plan.plan_id,
            "name": plan.name,
            "status": plan.status.name,
            "progress": plan.progress,
            "milestones": [
                {
                    "id": m.milestone_id,
                    "title": m.title,
                    "status": m.status.name,
                    "priority": m.priority,
                    "duration_seconds": m.duration_seconds,
                }
                for m in plan.milestones
            ],
            "ready": [m.title for m in plan.get_ready_milestones()],
            "blocked": [m.title for m in plan.get_blocked_milestones()],
        }

    def get_all_summaries(self) -> List[Dict[str, Any]]:
        summaries: List[Dict[str, Any]] = []
        for plan_id in self._plans:
            summary = self.get_plan_summary(plan_id)
            if summary is not None:
                summaries.append(summary)
        return summaries


class ResourceAllocator:
    """Allocates agents to milestones based on availability and capability."""

    def __init__(self, event_bus: Optional[EventBus] = None):
        self._allocations: Dict[str, Dict[str, Any]] = {}
        self._event_bus = event_bus

    def allocate(
        self,
        milestone: Milestone,
        available_agents: Dict[str, BaseAgent],
        selector_fn: Optional[Callable[[List[BaseAgent], str], Optional[BaseAgent]]] = None,
    ) -> Optional[str]:
        """Allocate the best agent for a milestone. Returns agent name or None."""
        candidates = []
        for agent in available_agents.values():
            if agent.status in ("idle", "busy"):
                caps = {c.name for c in agent.capabilities}
                if any(req in caps for req in milestone.required_capabilities):
                    candidates.append(agent)

        if not candidates:
            return None

        if selector_fn:
            chosen = selector_fn(candidates, milestone.required_capabilities[0])
            if chosen:
                return chosen.name

        # Default: least loaded capable agent
        chosen = min(candidates, key=lambda a: a._task_queue.qsize())
        self._allocations[milestone.milestone_id] = {
            "agent": chosen.name,
            "allocated_at": _utcnow().isoformat(),
        }
        return chosen.name

    def release(self, milestone_id: str) -> None:
        self._allocations.pop(milestone_id, None)

    def get_allocations(self) -> Dict[str, Dict[str, Any]]:
        return self._allocations.copy()


class PriorityManager:
    """Manages dynamic priority across plans and tasks."""

    def __init__(self, base_priority: int = 0):
        self._base = base_priority
        self._boosts: Dict[str, int] = {}
        self._deadlines: Dict[str, datetime] = {}

    def set_deadline(self, plan_id: str, deadline: datetime) -> None:
        self._deadlines[plan_id] = deadline

    def boost(self, plan_id: str, amount: int = 1) -> None:
        self._boosts[plan_id] = self._boosts.get(plan_id, 0) + amount

    def compute_effective_priority(
        self, plan_id: str, milestone_priority: int
    ) -> int:
        boost = self._boosts.get(plan_id, 0)
        deadline_urgency = 0
        if plan_id in self._deadlines:
            remaining = (self._deadlines[plan_id] - _utcnow()).total_seconds()
            if remaining < 300:
                deadline_urgency = 10
            elif remaining < 900:
                deadline_urgency = 5
            elif remaining < 3600:
                deadline_urgency = 2
        return self._base + milestone_priority + boost + deadline_urgency

    def get_urgent_plans(self, threshold: int = 8) -> List[str]:
        return [
            pid
            for pid in self._boosts
            if self.compute_effective_priority(pid, 0) >= threshold
        ]


class Planner:
    """Strategic planner for multi-step goal execution."""

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        progress_tracker: Optional[ProgressTracker] = None,
        resource_allocator: Optional[ResourceAllocator] = None,
        priority_manager: Optional[PriorityManager] = None,
        consciousness: Any = None,
    ):
        self.event_bus = event_bus
        self.progress = progress_tracker or ProgressTracker(event_bus)
        self.allocator = resource_allocator or ResourceAllocator(event_bus)
        self.priorities = priority_manager or PriorityManager()
        self._consciousness = consciousness
        self._plans: Dict[str, Plan] = {}
        self._running = False
        self._planner_task: Optional[asyncio.Task] = None
        self._task_executor: Optional[Callable[[TaskRequest, Optional[str]], Awaitable[str]]] = None
        # Per-task completion signals. Populated when a milestone task is
        # submitted; resolved by _on_task_completed/_on_task_failed listeners.
        # Without this the planner used to mark milestones COMPLETED as soon as
        # submit_task returned a task id, regardless of actual outcome.
        self._task_outcomes: Dict[str, "tuple[asyncio.Event, dict]"] = {}
        if event_bus is not None:
            event_bus.subscribe(self._on_task_completed, EventType.TASK_COMPLETED)
            event_bus.subscribe(self._on_task_failed, EventType.TASK_FAILED_FINAL)
            event_bus.subscribe(self._on_task_failed, EventType.TASK_FAILED)

    def _on_task_completed(self, event: Event) -> None:
        task_id = event.payload.get("task_id")
        if not task_id:
            return
        slot = self._task_outcomes.get(task_id)
        if slot is None:
            return
        signal, state = slot
        state["success"] = True
        state["error"] = None
        signal.set()

    def _on_task_failed(self, event: Event) -> None:
        task_id = event.payload.get("task_id")
        if not task_id:
            return
        slot = self._task_outcomes.get(task_id)
        if slot is None:
            return
        signal, state = slot
        # TASK_FAILED can fire while fallback is still attempting; only treat
        # TASK_FAILED_FINAL (or a TASK_FAILED with no further fallback) as terminal.
        # We rely on the orchestrator publishing TASK_FAILED_FINAL when retries
        # are exhausted; intermediate TASK_FAILED events will be superseded by
        # a later TASK_COMPLETED if fallback succeeds.
        if event.event_type == EventType.TASK_FAILED_FINAL:
            state["success"] = False
            state["error"] = event.payload.get("error", "task failed")
            signal.set()

    def set_task_executor(
        self, executor: Callable[[TaskRequest, Optional[str]], Awaitable[str]]
    ) -> None:
        """Set the function used to execute tasks (e.g., orchestrator.submit_task)."""
        self._task_executor = executor

    async def start(self) -> None:
        self._running = True
        self._planner_task = asyncio.create_task(self._planning_loop())

    async def stop(self) -> None:
        self._running = False
        if self._planner_task:
            self._planner_task.cancel()
            try:
                await self._planner_task
            except asyncio.CancelledError:
                pass

    async def _planning_loop(self) -> None:
        while self._running:
            try:
                for plan in list(self._plans.values()):
                    if plan.is_done:
                        continue
                    await self._execute_plan_step(plan)
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Planning loop error")
                await asyncio.sleep(5)

    async def _execute_plan_step(self, plan: Plan) -> None:
        if plan.status == PlanStatus.DRAFT:
            plan.status = PlanStatus.ACTIVE
            plan.started_at = _utcnow()

        ready = plan.get_ready_milestones()
        if not ready and not any(
            m.status == MilestoneStatus.IN_PROGRESS for m in plan.milestones
        ):
            # Check if all done or all blocked
            if all(
                m.status in (MilestoneStatus.COMPLETED, MilestoneStatus.SKIPPED)
                for m in plan.milestones
            ):
                plan.status = PlanStatus.COMPLETED
                plan.completed_at = _utcnow()
            elif plan.get_blocked_milestones():
                plan.status = PlanStatus.FAILED
                plan.completed_at = _utcnow()
            return

        # Sort by priority
        ready.sort(key=lambda m: self.priorities.compute_effective_priority(plan.plan_id, m.priority), reverse=True)

        for milestone in ready[:3]:  # Max 3 parallel milestones
            self.progress.update_milestone(plan.plan_id, milestone.milestone_id, MilestoneStatus.IN_PROGRESS)

            if self._task_executor:
                task = TaskRequest(
                    title=milestone.title,
                    description=milestone.description,
                    input_data=milestone.metadata.get("input_data", {}),
                    priority=self.priorities.compute_effective_priority(plan.plan_id, milestone.priority),
                )
                # Async fire-and-forget; in real usage you'd track completion via events
                asyncio.create_task(self._execute_milestone_task(plan, milestone, task))

    async def _execute_milestone_task(self, plan: Plan, milestone: Milestone, task: TaskRequest) -> None:
        if not self._task_executor:
            return
        try:
            # Inject collective consciousness context if available
            if self._consciousness and milestone.assigned_agents:
                agent_name = milestone.assigned_agents[0]
                capability = milestone.required_capabilities[0] if milestone.required_capabilities else None
                ctx = await self._consciousness.get_relevant_context(
                    agent_name=agent_name,
                    task_description=task.description or task.title,
                    capability=capability,
                )
                task.input_data["planner_context"] = ctx
                task.input_data["plan_id"] = plan.plan_id
                task.input_data["milestone_id"] = milestone.milestone_id

            # Register outcome slot before submitting so we can't miss a fast
            # completion event that fires before we await the signal.
            signal = asyncio.Event()
            state: Dict[str, Any] = {"success": None, "error": None}
            self._task_outcomes[task.task_id] = (signal, state)
            try:
                submitted_id = await self._task_executor(
                    task,
                    milestone.assigned_agents[0] if milestone.assigned_agents else None,
                )
                # If the executor renamed the task (e.g. via decomposition) we
                # need to track the new id instead.
                if submitted_id and submitted_id != task.task_id:
                    self._task_outcomes.pop(task.task_id, None)
                    self._task_outcomes[submitted_id] = (signal, state)
                    milestone.task_ids.append(submitted_id)
                else:
                    milestone.task_ids.append(task.task_id)

                # Wait for actual completion (or hard timeout from milestone metadata).
                timeout = float(
                    milestone.metadata.get("task_timeout_seconds", 600.0)
                )
                try:
                    await asyncio.wait_for(signal.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    state["success"] = False
                    state["error"] = f"milestone task timed out after {int(timeout)}s"
            finally:
                # Drop the slot from both possible keys.
                self._task_outcomes.pop(task.task_id, None)

            if state["success"]:
                self.progress.update_milestone(
                    plan.plan_id, milestone.milestone_id, MilestoneStatus.COMPLETED
                )
            else:
                err = state["error"] or "task failed"
                milestone.metadata["last_error"] = err
                self.progress.update_milestone(
                    plan.plan_id, milestone.milestone_id, MilestoneStatus.BLOCKED
                )
                await self._adapt_plan(plan, milestone, err)
        except Exception as e:
            milestone.metadata["last_error"] = str(e)
            self.progress.update_milestone(
                plan.plan_id, milestone.milestone_id, MilestoneStatus.BLOCKED
            )
            await self._adapt_plan(plan, milestone, str(e))

    async def _adapt_plan(self, plan: Plan, failed_milestone: Milestone, reason: str) -> None:
        if plan.adaptation_count >= plan.max_adaptations:
            plan.status = PlanStatus.FAILED
            return

        plan.adaptation_count += 1
        plan.status = PlanStatus.ADAPTED

        # Try reassigning agents for blocked milestone
        failed_milestone.status = MilestoneStatus.PENDING
        failed_milestone.metadata["adaptation_reason"] = reason
        failed_milestone.metadata["adaptation_attempt"] = plan.adaptation_count

        # Unblock dependents that might have alternate paths
        for m in plan.milestones:
            if failed_milestone.milestone_id in m.dependencies:
                if m.metadata.get("allow_skip_on_block"):
                    m.dependencies = [d for d in m.dependencies if d != failed_milestone.milestone_id]

        if self.event_bus:
            self.event_bus.publish(
                Event(
                    event_type=EventType.SYSTEM_ALERT,
                    source="planner",
                    payload={
                        "plan_id": plan.plan_id,
                        "milestone_id": failed_milestone.milestone_id,
                        "action": "adapted",
                        "reason": reason,
                        "adaptation_count": plan.adaptation_count,
                    },
                )
            )

    def create_plan(
        self,
        name: str,
        goal: str,
        milestones: Optional[List[Milestone]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Plan:
        plan = Plan(
            name=name,
            goal=goal,
            milestones=milestones or [],
            context=context or {},
        )
        self._plans[plan.plan_id] = plan
        self.progress.register_plan(plan)
        return plan

    def decompose_goal_to_plan(
        self,
        name: str,
        goal: str,
        steps: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> Plan:
        """Create a plan from a list of step descriptions."""
        milestones = []
        id_map: Dict[int, str] = {}

        for idx, step in enumerate(steps):
            m = Milestone(
                title=step.get("title", f"Step {idx + 1}"),
                description=step.get("description", ""),
                priority=step.get("priority", 0),
                required_capabilities=step.get("capabilities", []),
                estimated_duration_minutes=step.get("estimated_minutes"),
                metadata=step.get("metadata", {}),
            )
            id_map[idx] = m.milestone_id
            milestones.append(m)

        for idx, step in enumerate(steps):
            deps = step.get("dependencies", [])
            milestones[idx].dependencies = [id_map[d] for d in deps if d in id_map]

        return self.create_plan(name, goal, milestones, context)

    def get_plan(self, plan_id: str) -> Optional[Plan]:
        return self._plans.get(plan_id)

    def get_all_plans(self) -> List[Plan]:
        return list(self._plans.values())

    def cancel_plan(self, plan_id: str) -> bool:
        plan = self._plans.get(plan_id)
        if not plan or plan.is_done:
            return False
        plan.status = PlanStatus.FAILED
        for m in plan.milestones:
            if m.status in (MilestoneStatus.PENDING, MilestoneStatus.IN_PROGRESS):
                m.status = MilestoneStatus.SKIPPED
        return True
