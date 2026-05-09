"""Event system for inter-agent communication."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional
from uuid import UUID, uuid4

logger = logging.getLogger("skyn3t.events")


class EventType(Enum):
    """Types of events in the system."""

    TASK_CREATED = auto()
    TASK_STARTED = auto()
    TASK_COMPLETED = auto()
    TASK_FAILED = auto()
    TASK_CANCELLED = auto()
    TASK_ROUTED = auto()
    TASK_ENRICHED = auto()
    TASK_QUEUED = auto()
    TASK_EXECUTION_STARTED = auto()
    TASK_FAILED_FINAL = auto()
    QUEUE_BACKPRESSURE_REJECT = auto()
    FALLBACK_ATTEMPTED = auto()
    FALLBACK_SUCCEEDED = auto()
    FALLBACK_EXHAUSTED = auto()
    PIPELINE_STAGE_FAILED = auto()
    AGENT_REGISTERED = auto()
    AGENT_UNREGISTERED = auto()
    AGENT_HEARTBEAT = auto()
    AGENT_ERROR = auto()
    MESSAGE = auto()
    KNOWLEDGE_UPDATED = auto()
    SYSTEM_ALERT = auto()
    SELF_HEAL_TRIGGERED = auto()
    GITHUB_EVENT = auto()
    PIPELINE_STARTED = auto()
    PIPELINE_COMPLETED = auto()
    PIPELINE_STAGE_COMPLETED = auto()
    AGENT_COLLABORATION = auto()
    COLLECTIVE_INSIGHT = auto()
    AGENT_MESSAGE_SENT = auto()
    AGENT_MESSAGE_RECEIVED = auto()
    AGENT_THOUGHT = auto()
    AGENT_LEARNING = auto()
    RAG_QUERY_STARTED = auto()
    RAG_RETRIEVED = auto()
    RAG_CRITIQUED = auto()
    RAG_REQUERY = auto()
    INGEST_STARTED = auto()
    INGEST_PROGRESS = auto()
    INGEST_COMPLETE = auto()


@dataclass
class Event:
    """An event in the system."""

    event_type: EventType
    source: str
    payload: Dict[str, Any]
    event_id: UUID = field(default_factory=uuid4)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    target: Optional[str] = None
    correlation_id: Optional[str] = None
    priority: int = 0  # Higher = more urgent

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "event_type": self.event_type.name,
            "source": self.source,
            "target": self.target,
            "timestamp": self.timestamp.isoformat(),
            "payload": self.payload,
            "correlation_id": self.correlation_id,
            "priority": self.priority,
        }


class EventBus:
    """In-memory event bus for publish/subscribe communication."""

    def __init__(self):
        self._subscribers: Dict[EventType, List[Callable[[Event], None]]] = {
            et: [] for et in EventType
        }
        self._global_subscribers: List[Callable[[Event], None]] = []
        self._history: List[Event] = []
        self._max_history = 1000

    def subscribe(
        self,
        callback: Callable[[Event], None],
        event_type: Optional[EventType] = None,
    ) -> None:
        """Subscribe to events."""
        if event_type:
            self._subscribers[event_type].append(callback)
        else:
            self._global_subscribers.append(callback)

    def unsubscribe(
        self,
        callback: Callable[[Event], None],
        event_type: Optional[EventType] = None,
    ) -> None:
        """Unsubscribe from events."""
        if event_type:
            self._subscribers[event_type] = [
                cb for cb in self._subscribers[event_type] if cb != callback
            ]
        else:
            self._global_subscribers = [cb for cb in self._global_subscribers if cb != callback]

    def publish(self, event: Event) -> None:
        """Publish an event to all subscribers."""
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

        # Notify type-specific subscribers
        for callback in self._subscribers.get(event.event_type, []):
            try:
                callback(event)
            except Exception as e:
                logger.exception("Error in event subscriber: %s", e)

        # Notify global subscribers
        for callback in self._global_subscribers:
            try:
                callback(event)
            except Exception as e:
                logger.exception("Error in global subscriber: %s", e)

    def get_history(
        self,
        event_type: Optional[EventType] = None,
        source: Optional[str] = None,
        limit: int = 100,
    ) -> List[Event]:
        """Get event history with optional filtering."""
        events = self._history
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        if source:
            events = [e for e in events if e.source == source]
        return events[-limit:]

    def clear_history(self) -> None:
        """Clear event history."""
        self._history.clear()
