"""Async-safe event context helpers.

These helpers let higher-level orchestrators attach contextual metadata
like a Studio project slug or stage name to nested agent events without
threading those fields through every agent method signature.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional

_EVENT_CONTEXT: ContextVar[Dict[str, Any]] = ContextVar(
    "skyn3t_event_context", default={}
)

_CONTEXT_KEYS = (
    "project_slug",
    "project_stage",
    "project_template",
    "task_id",
)


def current_event_context() -> Dict[str, Any]:
    """Return a copy of the currently active event context."""
    return dict(_EVENT_CONTEXT.get() or {})


def current_event_correlation_id(default: Optional[str] = None) -> Optional[str]:
    """Return the active correlation id or a sensible default."""
    context = current_event_context()
    return (
        context.get("correlation_id")
        or context.get("task_id")
        or default
    )


def merge_event_payload(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge the active context into an event payload."""
    merged = dict(payload or {})
    context = current_event_context()
    for key in _CONTEXT_KEYS:
        value = context.get(key)
        if value is not None and key not in merged:
            merged[key] = value
    return merged


@contextmanager
def push_event_context(**context: Any) -> Iterator[None]:
    """Temporarily extend the active event context for nested awaits/tasks."""
    current = current_event_context()
    merged = dict(current)
    for key, value in context.items():
        if value is not None:
            merged[key] = value
    token = _EVENT_CONTEXT.set(merged)
    try:
        yield
    finally:
        _EVENT_CONTEXT.reset(token)
