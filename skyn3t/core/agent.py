"""Base agent implementation."""

import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional
from uuid import uuid4

from skyn3t.core.event_context import current_event_correlation_id, merge_event_payload
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.observability.metrics import get_collector
from skyn3t.observability.tracing import SpanStatus, get_tracer
from skyn3t.prompt_compression import compress_prompt_context

if TYPE_CHECKING:
    from skyn3t.core.messaging import AgentMessage

logger = logging.getLogger("skyn3t.core.agent")


@dataclass
class AgentCapability:
    """A capability that an agent can offer."""

    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    required_config: List[str] = field(default_factory=list)


@dataclass
class TaskRequest:
    """A request for an agent to perform a task."""

    task_id: str = field(default_factory=lambda: str(uuid4()))
    title: str = ""
    description: str = ""
    input_data: Dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    max_retries: int = 3
    retry_count: int = 0
    callback: Optional[Callable[[Dict[str, Any]], None]] = None
    pipe_from: Optional[str] = None
    pipe_to: Optional[str] = None
    session_id: Optional[str] = None
    context_id: Optional[str] = None
    required_memory: List[str] = field(default_factory=list)
    # Optional caller-supplied key for deduping retried submissions. If set,
    # the orchestrator will return the prior task_id when it sees the same
    # key within the dedup TTL instead of starting a duplicate task.
    idempotency_key: Optional[str] = None


@dataclass
class TaskResult:
    """Result of a task execution."""

    task_id: str
    success: bool
    output: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None
    insights: List[str] = field(default_factory=list)


class BaseAgent(ABC):
    """Base class for all agents in the system."""

    def __init__(
        self,
        name: str,
        agent_type: str,
        provider: str,
        event_bus: EventBus,
        config: Optional[Dict[str, Any]] = None,
        role: Optional[str] = None,
        reports_to: Optional[str] = None,
    ):
        self.id = str(uuid4())
        self.name = name
        self.agent_type = agent_type
        self.provider = provider
        self.event_bus = event_bus
        self.config = config or {}
        self.role = role
        self.reports_to = reports_to
        self.lifecycle = "manual"  # "manual" | "auto" — auto agents may be terminated
        self.last_active_at: Optional[datetime] = None
        self.capabilities: List[AgentCapability] = []
        self.status = "idle"
        self.metadata: Dict[str, Any] = {}
        self._lazy_task_queue: Optional[asyncio.Queue[TaskRequest]] = None
        self._current_task: Optional[TaskRequest] = None
        self._current_task_started_at: Optional[datetime] = None
        self._running = False
        self._task_processor: Optional[asyncio.Task] = None
        self._health_checks: int = 0
        self._errors: List[Dict[str, Any]] = []
        self._max_errors = 10
        self.last_output: str = ""
        # Bounded LRU. Without a cap, an agent that has run for weeks holds
        # every result it has ever produced in memory.
        self._results: "OrderedDict[str, TaskResult]" = OrderedDict()
        self._results_max = 200
        self._enabled: bool = True
        self._llm = None

    @property
    def _task_queue(self) -> "asyncio.Queue[TaskRequest]":
        if self._lazy_task_queue is None:
            try:
                from skyn3t.config.settings import get_settings
                maxsize = int(get_settings().max_queue_depth or 0)
            except Exception:
                maxsize = 0
            self._lazy_task_queue = asyncio.Queue(maxsize=maxsize)
        return self._lazy_task_queue

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the agent. Called before starting."""
        pass

    def get_stdout(self) -> str:
        """Get the last stdout output for piping."""
        return self.last_output

    @abstractmethod
    async def execute(
        self, task: TaskRequest, stdin_data: Optional[str] = None
    ) -> TaskResult:
        """Execute a task. Must be implemented by subclasses."""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the agent is healthy."""
        pass

    async def shutdown(self) -> None:
        """Shutdown the agent gracefully."""
        self._running = False
        # Wake the processor immediately so it observes _running=False
        # without waiting for a queue item or timeout.
        try:
            self._task_queue.put_nowait(None)  # type: ignore[arg-type]
        except Exception:
            pass
        if self._task_processor:
            self._task_processor.cancel()
            try:
                await self._task_processor
            except asyncio.CancelledError:
                pass
        self.status = "offline"
        self.event_bus.publish(
            Event(
                event_type=EventType.AGENT_UNREGISTERED,
                source=self.name,
                payload={"agent_id": self.id, "agent_type": self.agent_type},
            )
        )

    def add_capability(self, capability: AgentCapability) -> None:
        """Add a capability to this agent."""
        self.capabilities.append(capability)

    async def submit_task(self, task: TaskRequest) -> None:
        """Submit a task to this agent's queue."""
        if not getattr(self, "_enabled", True):
            # Disabled agents drop tasks; emit a failure event so the UI sees it.
            self.event_bus.publish(
                Event(
                    event_type=EventType.TASK_FAILED,
                    source=self.name,
                    payload={
                        "task_id": task.task_id,
                        "error": f"agent '{self.name}' is disabled",
                    },
                    correlation_id=task.task_id,
                )
            )
            return
        # Backpressure: if the queue has a maxsize and is full, reject rather
        # than buffer (which can OOM under a flood). The orchestrator's
        # fallback path can pick a different agent.
        q = self._task_queue
        if q.maxsize and q.full():
            self.event_bus.publish(
                Event(
                    event_type=EventType.QUEUE_BACKPRESSURE_REJECT,
                    source=self.name,
                    payload={
                        "task_id": task.task_id,
                        "queue_size": q.qsize(),
                        "queue_max": q.maxsize,
                    },
                    correlation_id=task.task_id,
                )
            )
            self.event_bus.publish(
                Event(
                    event_type=EventType.TASK_FAILED,
                    source=self.name,
                    payload={
                        "task_id": task.task_id,
                        "error": (
                            f"agent '{self.name}' queue full "
                            f"({q.qsize()}/{q.maxsize})"
                        ),
                    },
                    correlation_id=task.task_id,
                )
            )
            return
        await q.put(task)
        collector = get_collector()
        collector.record_task_submitted(self.name, self.agent_type)
        collector.set_queue_depth(self.name, self.agent_type, self._task_queue.qsize())
        self.event_bus.publish(
            Event(
                event_type=EventType.TASK_CREATED,
                source=self.name,
                payload={
                    "task_id": task.task_id,
                    "title": task.title,
                    "priority": task.priority,
                },
                correlation_id=task.task_id,
            )
        )

    async def start(self) -> None:
        """Start the agent's task processing loop."""
        self._running = True
        try:
            await self.initialize()
            self._task_processor = asyncio.create_task(self._process_tasks())
        except Exception as exc:
            self._running = False
            self._task_processor = None
            self.status = "offline"
            self._record_error(str(exc), {"phase": "start"})
            raise
        self.status = "idle"
        self.event_bus.publish(
            Event(
                event_type=EventType.AGENT_REGISTERED,
                source=self.name,
                payload={
                    "agent_id": self.id,
                    "agent_type": self.agent_type,
                    "provider": self.provider,
                    "capabilities": [c.name for c in self.capabilities],
                },
            )
        )

    async def _process_tasks(self) -> None:
        """Main task processing loop."""
        collector = get_collector()
        tracer = get_tracer()
        while self._running:
            try:
                # Block until a task arrives (or shutdown sentinel `None` is enqueued).
                # Using bare get() avoids the per-second wait_for cancellation that
                # races with put_nowait and silently drops messages.
                task = await self._task_queue.get()
                if task is None or not self._running:
                    break
                self._current_task = task
                self._current_task_started_at = datetime.now(timezone.utc)
                self.status = "busy"
                collector.set_active_tasks(self.name, 1)
                collector.set_queue_depth(self.name, self.agent_type, self._task_queue.qsize())

                self.event_bus.publish(
                    Event(
                        event_type=EventType.TASK_STARTED,
                        source=self.name,
                        payload={"task_id": task.task_id, "title": task.title},
                        correlation_id=task.task_id,
                    )
                )

                start_time = datetime.now(timezone.utc)
                async with tracer.span(
                    "agent.execute_task",
                    attributes={
                        "agent_name": self.name,
                        "agent_type": self.agent_type,
                        "task_id": task.task_id,
                        "task_title": task.title,
                    },
                ) as span:
                    try:
                        self.event_bus.publish(
                            Event(
                                event_type=EventType.TASK_EXECUTION_STARTED,
                                source=self.name,
                                payload={"task_id": task.task_id, "agent": self.name},
                                correlation_id=task.task_id,
                            )
                        )
                        stdin_data = task.input_data.get("stdin")
                        sig = inspect.signature(self.execute)
                        if "stdin_data" in sig.parameters:
                            result = await self.execute(task, stdin_data=stdin_data)
                        else:
                            result = await self.execute(task)
                        execution_time_sec = (
                            datetime.now(timezone.utc) - start_time
                        ).total_seconds()
                        result.execution_time_ms = execution_time_sec * 1000

                        if result.success:
                            span.set_status(SpanStatus.OK)
                            self.last_output = str(
                                result.output.get("stdout", result.output)
                            )
                            self._results[task.task_id] = result
                            # Evict oldest entries when over the cap.
                            while len(self._results) > self._results_max:
                                self._results.popitem(last=False)
                            collector.record_task_completed(
                                self.name, self.agent_type, execution_time_sec
                            )
                            self.event_bus.publish(
                                Event(
                                    event_type=EventType.TASK_COMPLETED,
                                    source=self.name,
                                    payload={
                                        "task_id": task.task_id,
                                        "execution_time_ms": result.execution_time_ms,
                                        "output_summary": str(result.output)[:200],
                                        "output": result.output,
                                    },
                                    correlation_id=task.task_id,
                                )
                            )
                        else:
                            span.set_status(SpanStatus.ERROR, result.error)
                            collector.record_task_failed(
                                self.name, self.agent_type, reason="execution_error"
                            )
                            self.event_bus.publish(
                                Event(
                                    event_type=EventType.TASK_FAILED,
                                    source=self.name,
                                    payload={
                                        "task_id": task.task_id,
                                        "error": result.error,
                                        # ACTUAL attempts so far (was
                                        # incorrectly publishing the
                                        # configured ceiling, which
                                        # made every failure look
                                        # like a max-retries
                                        # exhaustion to downstream
                                        # telemetry).
                                        "retry_count": getattr(task, "retry_count", 0),
                                        "max_retries": task.max_retries,
                                    },
                                    correlation_id=task.task_id,
                                )
                            )

                        if task.callback:
                            try:
                                cb_arg = result.output if result.success else {"error": result.error}
                                cb_ret = task.callback(cb_arg)
                                # If the callback is async, await it; otherwise the
                                # coroutine would just leak as an unawaited object.
                                if inspect.isawaitable(cb_ret):
                                    await cb_ret
                            except Exception as e:
                                logger.exception(
                                    "Callback error for task %s on agent %s",
                                    task.task_id,
                                    self.name,
                                )
                                result.metadata["callback_failed"] = True
                                result.metadata["callback_error"] = str(e)
                                self._record_error(
                                    str(e),
                                    {"task_id": task.task_id, "phase": "callback"},
                                )

                    except Exception as e:
                        execution_time_sec = (
                            datetime.now(timezone.utc) - start_time
                        ).total_seconds()
                        span.set_status(SpanStatus.ERROR, str(e))
                        collector.record_task_failed(
                            self.name, self.agent_type, reason="exception"
                        )
                        self._record_error(str(e), {"task_id": task.task_id})
                        self.event_bus.publish(
                            Event(
                                event_type=EventType.TASK_FAILED,
                                source=self.name,
                                payload={
                                    "task_id": task.task_id,
                                    "error": str(e),
                                },
                                correlation_id=task.task_id,
                            )
                        )

                self._current_task = None
                self._current_task_started_at = None
                self.status = "idle"
                collector.set_active_tasks(self.name, 0)
                collector.set_queue_depth(self.name, self.agent_type, self._task_queue.qsize())

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._record_error(str(e))
                await asyncio.sleep(1)

    def _record_error(self, error: str, context: Optional[Dict[str, Any]] = None) -> None:
        """Record an error for self-healing analysis."""
        self._errors.append(
            {
                "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "context": context or {},
            }
        )
        if len(self._errors) > self._max_errors:
            self._errors = self._errors[-self._max_errors :]

        self.event_bus.publish(
            Event(
                event_type=EventType.AGENT_ERROR,
                source=self.name,
                payload={"error": error, "context": context},
            )
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get agent statistics."""
        return {
            "id": self.id,
            "name": self.name,
            "type": self.agent_type,
            "provider": self.provider,
            # Raw override values only. UIs that need the routed values
            # should use get_config_view()["effective_*"].
            "backend": self.config.get("backend"),
            "model": self.config.get("model"),
            "status": self.status,
            "capabilities": [c.name for c in self.capabilities],
            "current_task": self._current_task.task_id if self._current_task else None,
            "queue_size": self._task_queue.qsize(),
            "health_checks": self._health_checks,
            "recent_errors": len(self._errors),
            "metadata": self.metadata,
            "role": self.role,
            "reports_to": self.reports_to,
            "lifecycle": self.lifecycle,
            "last_active_at": self.last_active_at.isoformat() if self.last_active_at else None,
        }

    async def send_message(
        self,
        to: str,
        content: str,
        kind: str = "info",
        payload: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
    ) -> None:
        """Send an A2A message to another agent (or "*" for broadcast).

        Routes through the shared MessageBus so the recipient gets it on
        their inbox and the dashboard sees AGENT_MESSAGE_SENT/RECEIVED
        events. ``kind`` is one of "request" | "response" | "info" |
        "share" | "ask".
        """
        from skyn3t.core.messaging import AgentMessage, get_default_bus

        msg = AgentMessage(
            from_agent=self.name,
            to_agent=to,
            kind=kind,
            content=content,
            payload=merge_event_payload(payload),
            correlation_id=current_event_correlation_id(correlation_id),
        )
        bus = get_default_bus(self.event_bus)
        await bus.send(msg)

    async def request(
        self,
        to_agent: str,
        content: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> Optional["AgentMessage"]:
        """Send a request to another agent and await a response.

        Returns the response ``AgentMessage`` or ``None`` on timeout.
        """
        from skyn3t.core.messaging import get_default_bus

        bus = get_default_bus(self.event_bus)
        return await bus.request(
            from_agent=self.name,
            to_agent=to_agent,
            content=content,
            payload=merge_event_payload(payload),
            timeout=timeout,
        )

    def on_message(self, msg: "AgentMessage") -> Optional["AgentMessage"]:
        """Handle an incoming A2A message.

        Subclasses may override this to respond to ``request`` messages.
        The default implementation returns ``None`` (no auto-response).
        If a non-None message is returned, it is sent back as a response
        via the MessageBus.
        """
        return None

    def resolve_artifact_dir(self, raw: Any) -> "Path":
        """Centralized artifact-dir resolution to stop leaks into the repo root.

        Agents used to do `Path(data.get("artifact_dir") or ".")` which silently
        wrote to CWD when called outside a Studio pipeline (Chat, direct API
        exec, scripts). That was the root cause of stray architecture.md /
        brainstorm.md / tech_stack.json files accumulating in the repo root.

        Resolution order:
          1. Caller-provided path (Studio pipeline path) — validated to
             reject repo-root, CWD, and other dangerous locations.
          2. Per-agent scratch under <projects_dir>/_agent_scratch/<agent>/<ts>/
             when no path provided or validation fails. Keeps files in the
             configured projects root, never the repo root.
        """
        import time as _t
        from pathlib import Path as _PP

        # Whitelist: if a `projects_dir` is configured, paths inside
        # that tree are always allowed even if the tree happens to
        # live inside the SkyN3t repo (common dev setup). Without this
        # whitelist, a developer running `projects_dir=./data/projects`
        # sees every caller-supplied path silently fall back to the
        # scratch dir.
        projects_root: Optional["Path"] = None
        try:
            from skyn3t.config import get_settings as _get_settings
            cfg = _get_settings()
            pd = getattr(cfg, "projects_dir", None)
            if pd:
                projects_root = _PP(str(pd)).expanduser().resolve()
        except Exception:
            projects_root = None

        def _is_dangerous(path: "Path") -> bool:
            """Reject paths that look like the repo root, CWD, or system dirs."""
            try:
                resolved = path.resolve()
                # Configured projects root is always safe, even if it's
                # inside the repo.
                if projects_root is not None:
                    try:
                        resolved.relative_to(projects_root)
                        return False
                    except ValueError:
                        pass
                cwd = _PP.cwd().resolve()
                # Never write to CWD if CWD is the SkyN3t repo root
                if resolved == cwd:
                    return True
                # Never write to the repo root OR any subdirectory of it.
                # Walk up the directory tree looking for SkyN3t repo markers.
                for parent in [resolved, *resolved.parents]:
                    if (parent / "skyn3t" / "core" / "agent.py").exists():
                        return True
                    # Alternative marker combo for robustness
                    if (parent / ".git").exists() and (parent / "AGENTS.md").exists():
                        return True
                # Never write to common system dirs
                for bad in ("/", "/tmp", "/var", "/usr", "/home", "~"):
                    if resolved == _PP(bad).expanduser().resolve():
                        return True
            except Exception:
                return True
            return False

        if raw:
            candidate = _PP(str(raw)).expanduser()
            if not _is_dangerous(candidate):
                return candidate
            logger.warning(
                "resolve_artifact_dir: rejecting dangerous path %s for agent %s; "
                "falling back to scratch.",
                candidate, self.name,
            )
        try:
            from skyn3t.config.settings import get_settings
            base = _PP(str(get_settings().projects_dir)).expanduser()
        except Exception:
            # Last resort if settings can't load — write to user's home,
            # NOT to cwd. This is the anti-leak invariant.
            base = _PP("~/.skyn3t/scratch").expanduser()
        scratch = base / "_agent_scratch" / self.name / str(int(_t.time()))
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch

    def load_skills_for_prompt(
        self,
        *,
        tags: List[str],
        limit: int = 4,
        max_chars_per_skill: int = 1200,
    ) -> str:
        """Return a system-prompt-ready block of learned skills for the
        agent. Pulls top-scored skills matching any of ``tags``, dedup
        by skill name, truncates each to ``max_chars_per_skill``.

        Returns "" if no skills found or the library is unavailable —
        callers should always be safe to append the result directly.
        """
        try:
            from skyn3t.intelligence.skill_library import get_default_library
            lib = get_default_library()
        except Exception:
            return ""
        seen: set[str] = set()
        lines: List[str] = []
        for tag in tags:
            if not tag:
                continue
            try:
                hits = lib.find(tag=tag, min_score=0.0, limit=limit)
            except Exception:
                continue
            for s in hits:
                if s.name in seen:
                    continue
                seen.add(s.name)
                body = (s.body or "").strip()
                if not body:
                    continue
                body = compress_prompt_context(body, max_chars=max_chars_per_skill)
                lines.append(f"### Skill: {s.name}\n{body}")
                if len(lines) >= limit:
                    break
            if len(lines) >= limit:
                break
        if not lines:
            return ""
        return (
            "\n\nLearned skills — apply when relevant:\n\n"
            + "\n\n".join(lines)
        )

    async def think(self, line: str) -> None:
        """Stream a "thinking" line to the dashboard."""
        self.event_bus.publish(
            Event(
                event_type=EventType.AGENT_THOUGHT,
                source=self.name,
                payload=merge_event_payload({"line": line, "agent": self.name}),
                correlation_id=current_event_correlation_id(),
            )
        )

    async def share_learning(
        self, lesson: str, scope: str = "global", **meta: Any
    ) -> None:
        """Record a lesson learned by this agent for the swarm."""
        self.event_bus.publish(
            Event(
                event_type=EventType.AGENT_LEARNING,
                source=self.name,
                payload=merge_event_payload({"lesson": lesson, "scope": scope, **meta}),
                correlation_id=current_event_correlation_id(),
            )
        )

    # ─────────────────────────────────────────────────────────────────
    # Per-agent live config / override surface (used by the dashboard)
    # ─────────────────────────────────────────────────────────────────

    def _effective_llm_view(self) -> Dict[str, Any]:
        """Best-effort view of the backend/model this agent is routed to."""
        raw_backend = self.config.get("backend")
        raw_model = self.config.get("model")
        if raw_backend:
            return {
                "effective_backend": raw_backend,
                "effective_model": raw_model,
                "effective_source": "config",
                "effective_stage": None,
                "effective_tier": None,
                "effective_policy_source": None,
            }
        try:
            from skyn3t.core.model_router import describe_stage_route, has_stage_policy

            for stage_name in (self.name, self.agent_type):
                if not stage_name:
                    continue
                if has_stage_policy(stage_name):
                    route = describe_stage_route(stage_name)
                    return {
                        "effective_backend": route.get("backend"),
                        "effective_model": raw_model if raw_model is not None else route.get("model"),
                        "effective_source": "policy",
                        "effective_stage": route.get("stage"),
                        "effective_tier": route.get("tier"),
                        "effective_policy_source": route.get("source"),
                    }
        except Exception:
            logger.debug("effective llm view failed for %s", self.name, exc_info=True)
        return {
            "effective_backend": raw_backend,
            "effective_model": raw_model,
            "effective_source": "config" if (raw_backend or raw_model) else None,
            "effective_stage": None,
            "effective_tier": None,
            "effective_policy_source": None,
        }

    def get_config_view(self) -> Dict[str, Any]:
        """Snapshot of the live, editable config exposed to UIs."""
        effective = self._effective_llm_view()
        return {
            "name": self.name,
            "agent_type": self.agent_type,
            "provider": self.provider,
            "enabled": getattr(self, "_enabled", True),
            "role": self.role,
            "reports_to": self.reports_to,
            "lifecycle": self.lifecycle,
            "capabilities": [c.name for c in self.capabilities],
            **effective,
            "config": {
                "backend": self.config.get("backend"),
                "model": self.config.get("model"),
                "system_prompt": self.config.get("system_prompt"),
                "temperature": self.config.get("temperature"),
                "max_tokens": self.config.get("max_tokens"),
            },
        }

    def apply_override(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a config patch live. Re-binds llm client if backend/model changed.

        Defensive: never raises. Bad/unknown keys are logged and skipped so a
        malformed override never aborts agent registration.
        """
        changed: List[str] = []
        try:
            patch = dict(patch or {})

            known_keys = {
                "provider",
                "enabled",
                "role",
                "reports_to",
                "lifecycle",
                "capabilities",
                "backend",
                "model",
                "system_prompt",
                "temperature",
                "max_tokens",
            }

            # role: empty string is valid; None → skip
            if "role" in patch:
                try:
                    val = patch["role"]
                    if val is None:
                        logger.debug("apply_override: role is None, skipping")
                    else:
                        self.role = str(val) if not isinstance(val, str) else val
                        changed.append("role")
                except Exception:
                    logger.exception("apply_override: failed to set role")

            # reports_to: empty string means "no manager"; None → skip
            if "reports_to" in patch:
                try:
                    val = patch["reports_to"]
                    if val is None:
                        logger.debug("apply_override: reports_to is None, skipping")
                    else:
                        self.reports_to = str(val) if not isinstance(val, str) else val
                        changed.append("reports_to")
                except Exception:
                    logger.exception("apply_override: failed to set reports_to")

            # lifecycle: must be "manual" or "auto"
            if "lifecycle" in patch:
                try:
                    val = patch["lifecycle"]
                    if val in ("manual", "auto"):
                        self.lifecycle = val
                        changed.append("lifecycle")
                    else:
                        logger.debug("apply_override: lifecycle must be manual|auto, got %r", val)
                except Exception:
                    logger.exception("apply_override: failed to set lifecycle")

            # provider: skip if empty/None
            if "provider" in patch:
                try:
                    val = patch["provider"]
                    if val is None or (isinstance(val, str) and not val):
                        pass  # skip empty/None
                    else:
                        self.provider = str(val)
                        changed.append("provider")
                except Exception:
                    logger.exception("apply_override: failed to set provider")

            # enabled: must be bool (or coercible). None / wrong-type → skip with debug.
            if "enabled" in patch:
                try:
                    val = patch["enabled"]
                    if isinstance(val, bool):
                        self._enabled = val
                        changed.append("enabled")
                    elif val is None:
                        logger.debug("apply_override: enabled is None, skipping")
                    else:
                        logger.debug(
                            "apply_override: enabled has unsupported type %s, skipping",
                            type(val).__name__,
                        )
                except Exception:
                    logger.exception("apply_override: failed to set enabled")

            # capabilities: list of strings OR list of dicts
            if "capabilities" in patch:
                try:
                    caps = patch["capabilities"]
                    if isinstance(caps, list):
                        existing_by_name = {c.name: c for c in self.capabilities}
                        new_caps: List[AgentCapability] = []
                        for entry in caps:
                            if entry is None:
                                continue
                            if isinstance(entry, str):
                                if not entry:
                                    continue
                                if entry in existing_by_name:
                                    new_caps.append(existing_by_name[entry])
                                else:
                                    new_caps.append(
                                        AgentCapability(
                                            name=entry,
                                            description=entry.replace("_", " "),
                                            parameters={},
                                        )
                                    )
                            elif isinstance(entry, dict):
                                try:
                                    nm = entry.get("name")
                                    if not nm:
                                        logger.debug(
                                            "apply_override: capability dict missing 'name', skipping: %r",
                                            entry,
                                        )
                                        continue
                                    if nm in existing_by_name and len(entry) == 1:
                                        new_caps.append(existing_by_name[nm])
                                    else:
                                        new_caps.append(
                                            AgentCapability(
                                                name=str(nm),
                                                description=str(
                                                    entry.get(
                                                        "description",
                                                        str(nm).replace("_", " "),
                                                    )
                                                ),
                                                parameters=entry.get("parameters") or {},
                                                required_config=entry.get(
                                                    "required_config"
                                                )
                                                or [],
                                            )
                                        )
                                except Exception:
                                    logger.exception(
                                        "apply_override: bad capability dict %r", entry
                                    )
                            else:
                                logger.debug(
                                    "apply_override: ignoring capability of type %s: %r",
                                    type(entry).__name__,
                                    entry,
                                )
                        self.capabilities = new_caps
                        changed.append("capabilities")
                    elif caps is None:
                        logger.debug("apply_override: capabilities is None, skipping")
                    else:
                        logger.debug(
                            "apply_override: capabilities not a list (got %s), skipping",
                            type(caps).__name__,
                        )
                except Exception:
                    logger.exception("apply_override: failed to set capabilities")

            # backend: skip if None, else stringify
            if "backend" in patch:
                try:
                    val = patch["backend"]
                    if val is None:
                        logger.debug("apply_override: backend is None, skipping")
                    else:
                        self.config["backend"] = str(val) if not isinstance(val, str) else val
                        changed.append("backend")
                except Exception:
                    logger.exception("apply_override: failed to set backend")

            # model: skip if None, else stringify
            if "model" in patch:
                try:
                    val = patch["model"]
                    if val is None:
                        logger.debug("apply_override: model is None, skipping")
                    else:
                        self.config["model"] = str(val) if not isinstance(val, str) else val
                        changed.append("model")
                except Exception:
                    logger.exception("apply_override: failed to set model")

            # system_prompt: empty string is valid; None → skip
            if "system_prompt" in patch:
                try:
                    val = patch["system_prompt"]
                    if val is None:
                        logger.debug("apply_override: system_prompt is None, skipping")
                    else:
                        self.config["system_prompt"] = str(val) if not isinstance(val, str) else val
                        changed.append("system_prompt")
                except Exception:
                    logger.exception("apply_override: failed to set system_prompt")

            # temperature: coerce to float; None → skip
            if "temperature" in patch:
                try:
                    val = patch["temperature"]
                    if val is None:
                        logger.debug("apply_override: temperature is None, skipping")
                    else:
                        self.config["temperature"] = float(val)
                        changed.append("temperature")
                except (TypeError, ValueError):
                    logger.debug(
                        "apply_override: temperature %r not coercible to float, skipping",
                        patch.get("temperature"),
                    )
                except Exception:
                    logger.exception("apply_override: failed to set temperature")

            # max_tokens: coerce to int; None → skip
            if "max_tokens" in patch:
                try:
                    val = patch["max_tokens"]
                    if val is None:
                        logger.debug("apply_override: max_tokens is None, skipping")
                    else:
                        self.config["max_tokens"] = int(val)
                        changed.append("max_tokens")
                except (TypeError, ValueError):
                    logger.debug(
                        "apply_override: max_tokens %r not coercible to int, skipping",
                        patch.get("max_tokens"),
                    )
                except Exception:
                    logger.exception("apply_override: failed to set max_tokens")

            # Any unknown keys: debug log only.
            for k in patch.keys():
                if k not in known_keys:
                    logger.debug("apply_override: ignoring unknown key %s", k)

            # Invalidate cached llm if any backend-affecting key changed.
            try:
                if any(k in changed for k in ("backend", "model")):
                    if hasattr(self, "_llm"):
                        self._llm = None
            except Exception:
                logger.exception("apply_override: failed to invalidate llm cache")
        except Exception:
            # Never let apply_override raise — registration must continue.
            logger.exception("apply_override: unexpected failure on agent %s", getattr(self, "name", "?"))

        try:
            view = self.get_config_view()
        except Exception:
            logger.exception("apply_override: get_config_view failed for %s", getattr(self, "name", "?"))
            view = {"name": getattr(self, "name", ""), "enabled": getattr(self, "_enabled", True)}
        return {"changed": changed, "config_view": view}

    def clear_override(self, keys: List[str]) -> Dict[str, Any]:
        """Clear supported config override keys live."""
        changed: List[str] = []
        try:
            clearable = {"backend", "model", "system_prompt", "temperature", "max_tokens"}
            for key in keys:
                if key not in clearable:
                    logger.debug("clear_override: ignoring unsupported key %s", key)
                    continue
                if key in self.config:
                    self.config.pop(key, None)
                    changed.append(key)
            if any(k in changed for k in ("backend", "model")) and hasattr(self, "_llm"):
                self._llm = None
        except Exception:
            logger.exception("clear_override: unexpected failure on agent %s", getattr(self, "name", "?"))
        try:
            view = self.get_config_view()
        except Exception:
            logger.exception("clear_override: get_config_view failed for %s", getattr(self, "name", "?"))
            view = {"name": getattr(self, "name", ""), "enabled": getattr(self, "_enabled", True)}
        return {"changed": changed, "config_view": view}

    @property
    def enabled(self) -> bool:
        return getattr(self, "_enabled", True)

    @property
    def llm(self):
        """Lazy LLMClient bound to this agent's overrides.

        Resolution order (first non-None wins):
          1. Explicit config (per-agent ``backend`` / ``model`` in
             ``data/agent_overrides.json`` or set via the dashboard).
          2. Model-routing policy by stage name (``cheap``/``strong``
             tier from ``core/model_router.py``).
          3. LLMClient's own auto-discovery default.

        Operators wanting to flip every cheap stage to balanced can
        point ``SKYN3T_MODEL_ROUTING`` at a JSON file; see
        ``core/model_router.py``.
        """
        if getattr(self, "_llm", None) is None:
            backend = self.config.get("backend")
            model = self.config.get("model")
            backend_is_policy = False
            # Layer in the routing policy when the agent didn't set
            # an explicit backend. Identify the stage by agent name
            # — agents are named brainstorm / research / architect /
            # designer / code_agent / reviewer in the runner.
            if not backend:
                try:
                    from skyn3t.core.model_router import describe_stage_route, has_stage_policy
                    for stage_name in (self.name, self.agent_type):
                        if not stage_name or not has_stage_policy(stage_name):
                            continue
                        route = describe_stage_route(stage_name)
                        backend = route.get("backend")
                        backend_is_policy = bool(backend)
                        if model is None:
                            model = route.get("model")
                        break
                except Exception:
                    logger.debug(
                        "model router lookup failed for %s",
                        self.name, exc_info=True,
                    )
            try:
                from skyn3t.adapters import LLMClient
                self._llm = LLMClient(
                    default_model=model,
                    backend=backend,
                    event_bus=self.event_bus,
                    caller_name=self.name,
                    backend_is_policy=backend_is_policy,
                )
            except Exception:
                self._llm = None
        return self._llm

    def get_llm(self):
        """Backward-compat accessor for ``self.llm``."""
        return self.llm

    async def llm_complete(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        max_tokens: int = 4000,
        temperature: float = 0.4,
        timeout: Optional[float] = 60.0,
        retries: int = 1,
        fallback: str = "",
    ) -> str:
        """Run an LLM completion with shared retry+timeout policy.

        Most agents had a near-identical block:

            client = self.get_llm() or LLMClient(...)
            try:
                out = await client.complete(prompt, ...)
                if out and "[deterministic-stub]" not in out:
                    return out
            except Exception:
                pass
            return fallback

        That copy lived in ~10 files, each with a slightly different timeout
        / retry / stub-detection policy. This helper centralizes it so an
        agent that just wants "give me a real LLM response, else fall back"
        can call ``await self.llm_complete(prompt, system=..., fallback=...)``.

        On any failure (no client, exception, deterministic stub, empty,
        timeout) returns ``fallback``. ``retries`` covers transient errors
        only — a deterministic-stub response short-circuits without retry
        because retrying won't change the outcome.
        """
        client = self.get_llm()
        if client is None:
            try:
                from skyn3t.adapters import LLMClient
                client = LLMClient(
                    default_model=self.config.get("model"),
                    backend=self.config.get("backend"),
                    event_bus=self.event_bus,
                    caller_name=self.name,
                )
            except Exception:
                return fallback

        last_exc: Optional[Exception] = None
        for attempt in range(max(1, retries + 1)):
            try:
                if timeout is not None:
                    out = await asyncio.wait_for(
                        client.complete(
                            prompt,
                            system=system,
                            max_tokens=max_tokens,
                            temperature=temperature,
                        ),
                        timeout=timeout,
                    )
                else:
                    out = await client.complete(
                        prompt,
                        system=system,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                if not out or "[deterministic-stub]" in out:
                    return fallback
                return str(out)
            except asyncio.TimeoutError as exc:
                last_exc = exc
            except Exception as exc:
                last_exc = exc
            # transient error — sleep with mild backoff before retrying
            if attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 4))
        if last_exc is not None:
            logger.debug("llm_complete failed after retries: %s", last_exc)
        return fallback
