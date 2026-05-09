"""Automatic task decomposition with pattern matching, dependency graphs, and parallel execution."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from skyn3t.core.agent import BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import Event, EventBus, EventType


@dataclass
class SubTask:
    """A subtask within a decomposed task."""

    subtask_id: str = field(default_factory=lambda: str(uuid4()))
    title: str = ""
    description: str = ""
    capability: Optional[str] = None
    agent_name: Optional[str] = None
    input_data: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    priority: int = 0
    parallel_group: Optional[str] = None
    result: Optional[TaskResult] = None
    status: str = "pending"
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_task_request(self) -> TaskRequest:
        return TaskRequest(
            task_id=self.subtask_id,
            title=self.title,
            description=self.description,
            input_data=self.input_data,
            priority=self.priority,
        )


class DependencyGraph:
    """Directed acyclic graph for subtask dependencies."""

    def __init__(self, subtasks: List[SubTask]):
        self.subtasks = {s.subtask_id: s for s in subtasks}
        self._graph: Dict[str, Set[str]] = {s.subtask_id: set(s.dependencies) for s in subtasks}
        self._completed: Set[str] = set()
        self._failed: Set[str] = set()

    def ready(self) -> List[SubTask]:
        """Return subtasks whose dependencies are all satisfied."""
        ready = []
        for sid, deps in self._graph.items():
            if sid in self._completed or sid in self._failed:
                continue
            if deps <= self._completed:
                ready.append(self.subtasks[sid])
        return ready

    def mark_completed(self, subtask_id: str) -> None:
        self._completed.add(subtask_id)
        if subtask_id in self.subtasks:
            self.subtasks[subtask_id].status = "completed"
            self.subtasks[subtask_id].completed_at = datetime.utcnow()

    def mark_failed(self, subtask_id: str) -> None:
        self._failed.add(subtask_id)
        if subtask_id in self.subtasks:
            self.subtasks[subtask_id].status = "failed"

    def is_done(self) -> bool:
        return all(
            sid in self._completed or sid in self._failed for sid in self._graph
        )

    def all_succeeded(self) -> bool:
        return self._completed == set(self._graph.keys())

    def get_parallel_groups(self) -> Dict[str, List[SubTask]]:
        """Group subtasks by parallel_group for batch execution."""
        groups: Dict[str, List[SubTask]] = {}
        for st in self.subtasks.values():
            pg = st.parallel_group or st.subtask_id
            groups.setdefault(pg, []).append(st)
        return groups


@dataclass
class DecompositionTemplate:
    """Template for decomposing common task patterns."""

    name: str
    pattern: str  # Keyword or regex pattern to match
    subtasks: List[Dict[str, Any]]  # Template subtask definitions
    description: str = ""


class TaskDecomposer:
    """Decomposes complex tasks into executable subtasks."""

    DEFAULT_TEMPLATES: List[DecompositionTemplate] = [
        DecompositionTemplate(
            name="web_app",
            pattern="write a web app",
            description="Full-stack web application development",
            subtasks=[
                {
                    "title": "Design API & Data Model",
                    "capability": "design",
                    "description": "Design the API schema and data models",
                    "priority": 1,
                    "parallel_group": "design",
                },
                {
                    "title": "Design UI/UX",
                    "capability": "design",
                    "description": "Design user interface wireframes and UX flow",
                    "priority": 1,
                    "parallel_group": "design",
                },
                {
                    "title": "Implement Backend",
                    "capability": "backend",
                    "description": "Implement server-side logic and API endpoints",
                    "priority": 2,
                    "dependencies": [],
                },
                {
                    "title": "Implement Frontend",
                    "capability": "frontend",
                    "description": "Implement client-side UI and interactions",
                    "priority": 2,
                    "dependencies": [],
                },
                {
                    "title": "Write Tests",
                    "capability": "testing",
                    "description": "Write unit, integration, and e2e tests",
                    "priority": 3,
                    "dependencies": [2, 3],
                },
                {
                    "title": "Integration & Review",
                    "capability": "review",
                    "description": "Integrate frontend/backend and perform code review",
                    "priority": 4,
                    "dependencies": [3, 4, 5],
                },
            ],
        ),
        DecompositionTemplate(
            name="data_pipeline",
            pattern="build a data pipeline",
            description="ETL / data processing pipeline",
            subtasks=[
                {
                    "title": "Source Analysis",
                    "capability": "data_analysis",
                    "description": "Analyze data sources and schemas",
                    "priority": 1,
                },
                {
                    "title": "Data Extraction",
                    "capability": "data_engineering",
                    "description": "Extract data from sources",
                    "priority": 2,
                    "dependencies": [0],
                    "parallel_group": "extract",
                },
                {
                    "title": "Data Transformation",
                    "capability": "data_engineering",
                    "description": "Clean and transform data",
                    "priority": 3,
                    "dependencies": [1],
                },
                {
                    "title": "Data Loading",
                    "capability": "data_engineering",
                    "description": "Load transformed data into destination",
                    "priority": 4,
                    "dependencies": [2],
                },
                {
                    "title": "Quality Checks",
                    "capability": "data_analysis",
                    "description": "Validate data quality and integrity",
                    "priority": 5,
                    "dependencies": [3],
                },
            ],
        ),
        DecompositionTemplate(
            name="documentation",
            pattern="write documentation",
            description="Generate project documentation",
            subtasks=[
                {
                    "title": "Analyze Codebase",
                    "capability": "code_analysis",
                    "description": "Analyze codebase structure and APIs",
                    "priority": 1,
                },
                {
                    "title": "Write API Docs",
                    "capability": "technical_writing",
                    "description": "Generate API reference documentation",
                    "priority": 2,
                    "dependencies": [0],
                    "parallel_group": "docs",
                },
                {
                    "title": "Write User Guide",
                    "capability": "technical_writing",
                    "description": "Write user-facing guides and tutorials",
                    "priority": 2,
                    "dependencies": [0],
                    "parallel_group": "docs",
                },
                {
                    "title": "Write README & Setup",
                    "capability": "technical_writing",
                    "description": "Write README and setup instructions",
                    "priority": 3,
                    "dependencies": [0],
                    "parallel_group": "docs",
                },
            ],
        ),
        DecompositionTemplate(
            name="bug_fix",
            pattern="fix bug",
            description="Systematic bug fixing workflow",
            subtasks=[
                {
                    "title": "Reproduce Bug",
                    "capability": "debugging",
                    "description": "Create a reproduction case for the bug",
                    "priority": 1,
                },
                {
                    "title": "Root Cause Analysis",
                    "capability": "debugging",
                    "description": "Identify the root cause of the bug",
                    "priority": 2,
                    "dependencies": [0],
                },
                {
                    "title": "Implement Fix",
                    "capability": "coding",
                    "description": "Write and apply the fix",
                    "priority": 3,
                    "dependencies": [1],
                },
                {
                    "title": "Regression Tests",
                    "capability": "testing",
                    "description": "Add tests to prevent regression",
                    "priority": 4,
                    "dependencies": [2],
                },
            ],
        ),
    ]

    def __init__(
        self,
        templates: Optional[List[DecompositionTemplate]] = None,
        event_bus: Optional[EventBus] = None,
    ):
        self.templates = {t.name: t for t in (templates or self.DEFAULT_TEMPLATES)}
        self.event_bus = event_bus
        self._custom_decomposers: List[Callable[[str], Optional[List[SubTask]]]] = []

    def register_custom_decomposer(
        self, decomposer: Callable[[str], Optional[List[SubTask]]]
    ) -> None:
        self._custom_decomposers.append(decomposer)

    def add_template(self, template: DecompositionTemplate) -> None:
        self.templates[template.name] = template

    def match_template(self, task_title: str) -> Optional[DecompositionTemplate]:
        """Match a task title against known patterns."""
        title_lower = task_title.lower()
        for template in self.templates.values():
            if template.pattern.lower() in title_lower:
                return template
        return None

    def decompose(
        self,
        task: TaskRequest,
        template: Optional[DecompositionTemplate] = None,
    ) -> List[SubTask]:
        """Decompose a task into subtasks."""
        # Try custom decomposers first
        for custom in self._custom_decomposers:
            result = custom(task.title)
            if result:
                return result

        matched = template or self.match_template(task.title)
        if not matched:
            # No template matched; return single subtask
            return [
                SubTask(
                    title=task.title,
                    description=task.description,
                    input_data=task.input_data,
                    priority=task.priority,
                )
            ]

        subtasks: List[SubTask] = []
        id_map: Dict[int, str] = {}

        for idx, spec in enumerate(matched.subtasks):
            st = SubTask(
                title=spec.get("title", f"Subtask {idx + 1}"),
                description=spec.get("description", ""),
                capability=spec.get("capability"),
                agent_name=spec.get("agent_name"),
                input_data={**task.input_data, **spec.get("input_data", {})},
                priority=spec.get("priority", 0),
                parallel_group=spec.get("parallel_group"),
            )
            # Inherit description from parent if empty
            if not st.description and task.description:
                st.description = f"[{matched.name}] {task.description}"
            id_map[idx] = st.subtask_id
            subtasks.append(st)

        # Resolve dependency indices to actual IDs
        for idx, spec in enumerate(matched.subtasks):
            deps = spec.get("dependencies", [])
            subtasks[idx].dependencies = [id_map[d] for d in deps if d in id_map]

        return subtasks

    async def execute_decomposed(
        self,
        subtasks: List[SubTask],
        agent_resolver: Callable[[SubTask], Awaitable[Optional[BaseAgent]]],
        max_parallel: int = 5,
    ) -> List[TaskResult]:
        """Execute decomposed subtasks respecting dependencies."""
        graph = DependencyGraph(subtasks)
        semaphore = asyncio.Semaphore(max_parallel)
        results: List[TaskResult] = []

        async def run_subtask(st: SubTask) -> TaskResult:
            async with semaphore:
                st.status = "running"
                st.started_at = datetime.utcnow()

                if self.event_bus:
                    self.event_bus.publish(
                        Event(
                            event_type=EventType.TASK_STARTED,
                            source="decomposer",
                            payload={
                                "subtask_id": st.subtask_id,
                                "title": st.title,
                                "parent_task": st.input_data.get("parent_task_id"),
                            },
                        )
                    )

                agent = await agent_resolver(st)
                if not agent:
                    result = TaskResult(
                        task_id=st.subtask_id,
                        success=False,
                        error="No suitable agent found",
                    )
                    graph.mark_failed(st.subtask_id)
                    st.result = result
                    if self.event_bus:
                        self.event_bus.publish(
                            Event(
                                event_type=EventType.TASK_FAILED,
                                source="decomposer",
                                payload={
                                    "subtask_id": st.subtask_id,
                                    "task_id": st.subtask_id,
                                    "success": False,
                                    "error": "No suitable agent found",
                                },
                            )
                        )
                    return result

                task_req = st.to_task_request()
                result = await agent.execute(task_req)
                st.result = result

                if result.success:
                    graph.mark_completed(st.subtask_id)
                else:
                    graph.mark_failed(st.subtask_id)

                if self.event_bus:
                    self.event_bus.publish(
                        Event(
                            event_type=(
                                EventType.TASK_COMPLETED
                                if result.success
                                else EventType.TASK_FAILED
                            ),
                            source="decomposer",
                            payload={
                                "subtask_id": st.subtask_id,
                                "success": result.success,
                                "agent": agent.name,
                            },
                        )
                    )

                return result

        pending_tasks: Dict[str, asyncio.Task] = {}

        while not graph.is_done():
            ready = graph.ready()
            for st in ready:
                if st.subtask_id not in pending_tasks:
                    pending_tasks[st.subtask_id] = asyncio.create_task(run_subtask(st))

            if not pending_tasks:
                break

            done, _ = await asyncio.wait(
                list(pending_tasks.values()), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                result = await task
                results.append(result)
                # Remove completed from pending map
                for sid, t in list(pending_tasks.items()):
                    if t == task:
                        del pending_tasks[sid]
                        break

        return results


class ResultAggregator:
    """Aggregates results from multiple subtasks into a unified output."""

    @staticmethod
    def concat_outputs(results: List[TaskResult], key: str = "response") -> str:
        """Concatenate a specific key from all result outputs."""
        parts = []
        for r in results:
            val = r.output.get(key, str(r.output))
            if val:
                parts.append(str(val))
        return "\n\n".join(parts)

    @staticmethod
    def merge_json(results: List[TaskResult]) -> Dict[str, Any]:
        """Merge JSON outputs from results."""
        merged: Dict[str, Any] = {}
        for r in results:
            if isinstance(r.output, dict):
                for k, v in r.output.items():
                    if k not in merged:
                        merged[k] = v
                    elif isinstance(merged[k], list) and isinstance(v, list):
                        merged[k].extend(v)
                    elif isinstance(merged[k], dict) and isinstance(v, dict):
                        merged[k].update(v)
        return merged

    @staticmethod
    def vote(results: List[TaskResult]) -> Tuple[Any, int]:
        """Simple majority vote across result outputs."""
        from collections import Counter

        outputs = [str(r.output.get("response", r.output)) for r in results if r.success]
        if not outputs:
            return None, 0
        counter = Counter(outputs)
        winner, count = counter.most_common(1)[0]
        return winner, count

    @staticmethod
    def best_by_score(
        results: List[TaskResult], score_fn: Callable[[TaskResult], float]
    ) -> Optional[TaskResult]:
        """Select the best result by a custom scoring function."""
        valid = [r for r in results if r.success]
        if not valid:
            return None
        return max(valid, key=score_fn)
