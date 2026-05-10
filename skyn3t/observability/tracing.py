"""OpenTelemetry-style tracing for SkyN3t (zero external dependencies)."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
class SpanStatus(str, Enum):
    """Span lifecycle status."""

    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass
class TraceSpan:
    """A single span in a trace."""

    id: str = field(default_factory=lambda: str(uuid4()))
    trace_id: str = field(default_factory=lambda: str(uuid4()))
    parent_id: Optional[str] = None
    name: str = "unnamed"
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: Optional[datetime] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    status: SpanStatus = SpanStatus.UNKNOWN
    events: List[Dict[str, Any]] = field(default_factory=list)
    children: List["TraceSpan"] = field(default_factory=list)
    depth: int = 0

    @property
    def duration_ms(self) -> Optional[float]:
        """Return span duration in milliseconds."""
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time).total_seconds() * 1000

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        """Add a timed event to the span."""
        self.events.append(
            {
                "name": name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "attributes": attributes or {},
            }
        )

    def set_status(self, status: SpanStatus, message: Optional[str] = None) -> None:
        """Set the span status."""
        self.status = status
        if message:
            self.attributes["status_message"] = message

    def finish(self, status: Optional[SpanStatus] = None, message: Optional[str] = None) -> None:
        """Finish the span."""
        self.end_time = datetime.now(timezone.utc)
        if status:
            self.set_status(status, message)
        elif self.status == SpanStatus.UNKNOWN:
            self.set_status(SpanStatus.OK)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize span to dict."""
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms,
            "attributes": self.attributes,
            "status": self.status.value,
            "events": self.events,
            "children": [child.to_dict() for child in self.children],
        }


# ---------------------------------------------------------------------------
# Context propagation
# ---------------------------------------------------------------------------
_current_span: ContextVar[Optional[TraceSpan]] = ContextVar("_current_span", default=None)
_current_trace_id: ContextVar[Optional[str]] = ContextVar("_current_trace_id", default=None)


class TraceContext:
    """Propagates trace context across async boundaries and tasks."""

    def __init__(
        self,
        trace_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        baggage: Optional[Dict[str, Any]] = None,
    ):
        self.trace_id = trace_id or str(uuid4())
        self.parent_span_id = parent_span_id
        self.baggage: Dict[str, Any] = baggage or {}

    def inject(self) -> Dict[str, str]:
        """Serialize context for propagation (e.g., HTTP headers)."""
        return {
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id or "",
            "baggage": str(self.baggage),
        }

    @classmethod
    def extract(cls, carrier: Dict[str, str]) -> "TraceContext":
        """Deserialize context from carrier."""
        return cls(
            trace_id=carrier.get("trace_id") or str(uuid4()),
            parent_span_id=carrier.get("parent_span_id") or None,
        )

    def attach(self) -> None:
        """Attach this context to the current execution context."""
        _current_trace_id.set(self.trace_id)

    @classmethod
    def current(cls) -> "TraceContext":
        """Get the current trace context."""
        trace_id = _current_trace_id.get()
        parent_span = _current_span.get()
        return cls(
            trace_id=trace_id or str(uuid4()),
            parent_span_id=parent_span.id if parent_span else None,
        )


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------
class Tracer:
    """Manages span hierarchy and lifecycle."""

    def __init__(self, name: str = "skyn3t"):
        self.name = name
        self._spans: deque[TraceSpan] = deque(maxlen=10000)
        self._finished: deque[TraceSpan] = deque(maxlen=1000)

    def start_span(
        self,
        name: str,
        context: Optional[TraceContext] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> TraceSpan:
        """Start a new span."""
        ctx = context or TraceContext.current()
        parent = _current_span.get()

        span = TraceSpan(
            trace_id=ctx.trace_id,
            parent_id=parent.id if parent else ctx.parent_span_id,
            name=name,
            attributes=attributes or {},
            depth=(parent.depth + 1) if parent else 0,
        )

        if parent:
            parent.children.append(span)

        self._spans.append(span)
        _current_span.set(span)
        _current_trace_id.set(ctx.trace_id)
        return span

    def end_span(
        self,
        span: TraceSpan,
        status: Optional[SpanStatus] = None,
        message: Optional[str] = None,
    ) -> None:
        """End a span and restore the parent as current."""
        span.finish(status, message)
        self._finished.append(span)

        # Pop this span if it's the current one
        current = _current_span.get()
        if current and current.id == span.id:
            # Restore parent
            _current_span.set(
                next(
                    (s for s in reversed(self._spans) if s.id == span.parent_id),
                    None,
                )
            )

    @asynccontextmanager
    async def span(
        self,
        name: str,
        context: Optional[TraceContext] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ):
        """Async context manager for creating spans."""
        span = self.start_span(name, context=context, attributes=attributes)
        try:
            yield span
            if span.status == SpanStatus.UNKNOWN:
                span.set_status(SpanStatus.OK)
        except asyncio.CancelledError:
            span.set_status(SpanStatus.CANCELLED, "cancelled")
            raise
        except Exception as e:
            span.set_status(SpanStatus.ERROR, str(e))
            raise
        finally:
            self.end_span(span)

    def get_current_span(self) -> Optional[TraceSpan]:
        """Return the current span from context vars."""
        return _current_span.get()

    def get_recent_spans(self, limit: int = 100) -> List[TraceSpan]:
        """Return recently finished spans."""
        return list(self._finished)[-limit:]

    def get_trace(self, trace_id: str) -> List[TraceSpan]:
        """Return all spans for a given trace."""
        return [s for s in self._spans if s.trace_id == trace_id]

    def clear(self) -> None:
        """Clear all stored spans."""
        self._spans.clear()
        self._finished.clear()


# ---------------------------------------------------------------------------
# Global tracer singleton
# ---------------------------------------------------------------------------
_tracer: Optional[Tracer] = None


def get_tracer(name: str = "skyn3t") -> Tracer:
    """Get the global tracer instance."""
    global _tracer
    if _tracer is None:
        _tracer = Tracer(name=name)
    return _tracer


def set_tracer(tracer: Tracer) -> None:
    """Set the global tracer (useful for testing)."""
    global _tracer
    _tracer = tracer


# ---------------------------------------------------------------------------
# Console exporter
# ---------------------------------------------------------------------------
class ConsoleExporter:
    """Prints traces to stdout in a readable format."""

    def __init__(self, tracer: Optional[Tracer] = None):
        self.tracer = tracer or get_tracer()

    def export(self, span: TraceSpan) -> None:
        """Export a single span to the console."""
        depth = span.depth if span.depth else self._depth(span)
        prefix = "  " * depth
        duration = span.duration_ms
        dur_str = f"{duration:.2f}ms" if duration is not None else "incomplete"
        status_icon = "✓" if span.status == SpanStatus.OK else "✗" if span.status == SpanStatus.ERROR else "?"
        print(
            f"{prefix}[{status_icon}] {span.name}  ({dur_str})  trace={span.trace_id[:8]}…"
        )
        for event in span.events:
            print(f"{prefix}  ▸ {event['name']} @ {event['timestamp']}")

    def export_recent(self, limit: int = 20) -> None:
        """Export the most recently finished spans."""
        for span in self.tracer.get_recent_spans(limit):
            self.export(span)

    def _depth(self, span: TraceSpan) -> int:
        """Compute nesting depth of a span."""
        depth = 0
        current = span
        all_spans = list(self.tracer._spans)
        while current.parent_id:
            parent = next((s for s in all_spans if s.id == current.parent_id), None)
            if parent is None:
                break
            depth += 1
            current = parent
        return depth


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------
def trace_task(name: str, attributes: Optional[Dict[str, Any]] = None):
    """Decorator to auto-trace a function / coroutine.

    Usage::

        @trace_task("execute_task")
        async def execute(task): ...
    """

    def decorator(func):
        async def async_wrapper(*args, **kwargs):
            tracer = get_tracer()
            async with tracer.span(name, attributes=attributes) as span:
                span.add_event("started", {"args": str(args), "kwargs": str(kwargs)})
                return await func(*args, **kwargs)

        def sync_wrapper(*args, **kwargs):
            tracer = get_tracer()
            span = tracer.start_span(name, attributes=attributes)
            try:
                span.add_event("started", {"args": str(args), "kwargs": str(kwargs)})
                return func(*args, **kwargs)
            except Exception as e:
                span.set_status(SpanStatus.ERROR, str(e))
                raise
            finally:
                tracer.end_span(span)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
