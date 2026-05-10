"""Agent-to-agent (A2A) messaging system.

Provides a typed AgentMessage and a MessageBus that rides on top of the
existing EventBus. Every send and receive emits a structured event so the
dashboard can render the swarm's conversation in real time.
"""

from __future__ import annotations

import asyncio
import time
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, MutableMapping, Optional, Union

from skyn3t.core.events import Event, EventBus, EventType


@dataclass
class AgentMessage:
    """A message sent from one agent to another (or broadcast)."""

    from_agent: str
    to_agent: str  # specific name, or "*" for broadcast
    kind: str  # "request" | "response" | "info" | "share" | "ask"
    content: str
    payload: Dict[str, Any] = field(default_factory=dict)
    correlation_id: Optional[str] = None
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {**self.__dict__}


# Type for push-style subscriber handlers. Both sync and async callables
# are accepted — the bus awaits coroutines if returned.
MessageHandler = Callable[[AgentMessage], Union[None, Awaitable[None]]]


class MessageBus:
    """Routes AgentMessages between agents and mirrors them on the EventBus.

    Each agent has a lazily-created inbox queue. Sends publish an
    AGENT_MESSAGE_SENT event; deliveries publish AGENT_MESSAGE_RECEIVED.
    Broadcast messages (``to_agent="*"``) are fanned out to every known
    inbox except the sender's.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self._inboxes: Dict[str, "asyncio.Queue[AgentMessage]"] = {}
        self._handlers: Dict[str, List[MessageHandler]] = {}

    # ------------------------------------------------------------------
    # Inbox management
    # ------------------------------------------------------------------
    def _inbox(self, agent_name: str) -> "asyncio.Queue[AgentMessage]":
        q = self._inboxes.get(agent_name)
        if q is None:
            q = asyncio.Queue()
            self._inboxes[agent_name] = q
        return q

    def known_agents(self) -> List[str]:
        """Return the names of agents that have an inbox or subscription."""
        names = set(self._inboxes.keys()) | set(self._handlers.keys())
        return sorted(names)

    # ------------------------------------------------------------------
    # Subscriptions (push style)
    # ------------------------------------------------------------------
    def subscribe(self, agent_name: str, handler: MessageHandler) -> None:
        """Register a push-style handler invoked on every message for ``agent_name``."""
        self._handlers.setdefault(agent_name, []).append(handler)
        # Ensure inbox exists so broadcasts still find this agent.
        self._inbox(agent_name)

    def unsubscribe(self, agent_name: str, handler: MessageHandler) -> None:
        handlers = self._handlers.get(agent_name)
        if not handlers:
            return
        self._handlers[agent_name] = [h for h in handlers if h is not handler]

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------
    async def send(self, msg: AgentMessage) -> None:
        """Publish ``msg`` on the event bus and route it to recipient inboxes."""
        # Mirror the send on the event bus first so observers see it even if
        # delivery later raises.
        self.event_bus.publish(
            Event(
                event_type=EventType.AGENT_MESSAGE_SENT,
                source=msg.from_agent,
                target=msg.to_agent,
                payload=msg.to_dict(),
                correlation_id=msg.correlation_id,
            )
        )

        if msg.to_agent == "*":
            recipients = [name for name in self.known_agents() if name != msg.from_agent]
        else:
            recipients = [msg.to_agent]

        for recipient in recipients:
            await self._deliver(recipient, msg)

    async def _deliver(self, recipient: str, msg: AgentMessage) -> None:
        inbox = self._inbox(recipient)
        await inbox.put(msg)

        self.event_bus.publish(
            Event(
                event_type=EventType.AGENT_MESSAGE_RECEIVED,
                source=msg.from_agent,
                target=recipient,
                payload={**msg.to_dict(), "delivered_to": recipient},
                correlation_id=msg.correlation_id,
            )
        )

        for handler in list(self._handlers.get(recipient, [])):
            try:
                result = handler(msg)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 — handlers must not break delivery
                # Subscribers shouldn't be able to crash the bus.
                pass

    async def recv(
        self, agent_name: str, timeout: Optional[float] = None
    ) -> Optional[AgentMessage]:
        """Pull the next message for ``agent_name``.

        If ``timeout`` is None, blocks until a message is available. If
        ``timeout`` elapses, returns ``None``.
        """
        inbox = self._inbox(agent_name)
        if timeout is None:
            return await inbox.get()
        try:
            return await asyncio.wait_for(inbox.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None


# ----------------------------------------------------------------------
# Default-bus singleton (keyed by EventBus identity)
# ----------------------------------------------------------------------
# Use a WeakKeyDictionary so an EventBus that goes out of scope can be GC'd
# along with its MessageBus. The previous int-keyed dict stored stale entries
# whose id() could be reused for a different EventBus, returning the wrong bus.
_default_buses: "MutableMapping[EventBus, MessageBus]" = weakref.WeakKeyDictionary()


def get_default_bus(event_bus: EventBus) -> MessageBus:
    """Return a process-wide MessageBus tied to ``event_bus``.

    The same EventBus instance always yields the same MessageBus so that
    every agent sharing that event bus also shares its messaging fabric.
    """
    bus = _default_buses.get(event_bus)
    if bus is None:
        bus = MessageBus(event_bus)
        _default_buses[event_bus] = bus
    return bus
