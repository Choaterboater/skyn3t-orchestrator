"""Base agent implementation."""

import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from skyn3t.core.event_context import current_event_correlation_id, merge_event_payload
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.observability.metrics import get_collector
from skyn3t.observability.tracing import SpanStatus, get_tracer

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
    ):
        self.id = str(uuid4())
        self.name = name
        self.agent_type = agent_type
        self.provider = provider
        self.event_bus = event_bus
        self.config = config or {}
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
                                        "retry_count": task.max_retries,
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
            "status": self.status,
            "capabilities": [c.name for c in self.capabilities],
            "current_task": self._current_task.task_id if self._current_task else None,
            "queue_size": self._task_queue.qsize(),
            "health_checks": self._health_checks,
            "recent_errors": len(self._errors),
            "metadata": self.metadata,
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

    def get_config_view(self) -> Dict[str, Any]:
        """Snapshot of the live, editable config exposed to UIs."""
        return {
            "name": self.name,
            "agent_type": self.agent_type,
            "provider": self.provider,
            "enabled": getattr(self, "_enabled", True),
            "capabilities": [c.name for c in self.capabilities],
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
                "capabilities",
                "backend",
                "model",
                "system_prompt",
                "temperature",
                "max_tokens",
            }

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

    @property
    def enabled(self) -> bool:
        return getattr(self, "_enabled", True)

    @property
    def llm(self):
        """Lazy LLMClient bound to this agent's overrides."""
        if getattr(self, "_llm", None) is None:
            try:
                from skyn3t.adapters import LLMClient
                self._llm = LLMClient(
                    default_model=self.config.get("model"),
                    backend=self.config.get("backend"),
                    event_bus=self.event_bus,
                    caller_name=self.name,
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
        max_tokens: int = 1500,
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
