"""Lazy agent registry for the Project Studio.

Resolves agent class names (strings, as referenced in templates) to
instantiated :class:`BaseAgent` objects.  Imports are performed at call
time so the studio package can be imported even when the specialist
agents have not yet been authored.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any


def get_agent(name: str, *, event_bus: Any = None, rag: Any = None, **kw: Any) -> Any:
    """Resolve ``name`` to an agent class and return an instance.

    Parameters
    ----------
    name:
        The class name as referenced in a :class:`StageSpec.agent`.
    event_bus, rag:
        Common collaborators forwarded only when the constructor accepts
        them (we introspect the signature to avoid passing unsupported
        kwargs).
    **kw:
        Additional keyword arguments forwarded to the constructor.
    """
    mod = importlib.import_module("skyn3t.agents")
    cls = getattr(mod, name, None)
    if cls is None:
        raise KeyError(f"agent {name} not found")

    sig = inspect.signature(cls)
    kwargs: dict = {}
    if "event_bus" in sig.parameters:
        kwargs["event_bus"] = event_bus
    if "rag" in sig.parameters:
        kwargs["rag"] = rag
    kwargs.update(kw)
    return cls(**kwargs)
