"""Single-point publisher for ``CORTEX_DECISION`` events.

Three subsystems make autonomous decisions that operators may need
to audit:
- ``CortexBootstrap`` skips a component (disabled or construct-failed).
- ``model_router`` demotes a backend that's been losing.
- ``CodeAgent`` injects ranked fixes into the build prompt.

Centralizing the publish here keeps the payload shape consistent
across producers — the Activity timeline can render every entry the
same way without each subsystem rolling its own conventions.

Payload contract:

    {
        "system": "cortex" | "router" | "recall",
        "action": <short verb, e.g. "demote_backend">,
        "reason": <human-readable reason>,
        "input":  <dict of relevant context>,
    }

The publish call is best-effort: a failing event bus must never
abort the decision it was reporting on. A debug log is emitted on
failure so the silence is at least diagnosable.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from skyn3t.core.events import Event, EventType

logger = logging.getLogger("skyn3t.intelligence.cortex_decisions")

_VALID_SYSTEMS = {"cortex", "router", "recall"}


def publish_decision(
    event_bus: Any,
    *,
    system: str,
    action: str,
    reason: str = "",
    input: Optional[Dict[str, Any]] = None,
    source: str = "",
) -> None:
    """Publish one ``CORTEX_DECISION`` event onto the bus.

    ``system`` must be one of {"cortex","router","recall"}; otherwise
    nothing publishes (we'd rather drop a malformed event than poison
    the audit stream). All other fields are passed through unchanged.
    """
    if event_bus is None:
        return
    sys_name = (system or "").strip().lower()
    if sys_name not in _VALID_SYSTEMS:
        logger.debug("cortex_decision: invalid system %r", system)
        return
    payload: Dict[str, Any] = {
        "system": sys_name,
        "action": str(action or "").strip(),
        "reason": str(reason or "").strip(),
        "input": dict(input or {}),
    }
    try:
        event_bus.publish(Event(
            event_type=EventType.CORTEX_DECISION,
            source=source or sys_name,
            payload=payload,
        ))
    except Exception:
        logger.debug("cortex_decision publish failed", exc_info=True)
