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

from skyn3t.registry.catalog import build_agent_override


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
    agent = cls(**kwargs)

    # Apply persisted per-agent overrides (backend/model/system_prompt/etc.)
    # so the LLM client this agent constructs uses the user's configured
    # routing — not vanilla defaults.
    try:
        from skyn3t.config.agent_overrides import get_override_store
        store = get_override_store()
        cls_patch = store.get(getattr(cls, "__name__", "")) or {}
        name_patch = store.get(getattr(agent, "name", "")) or {}
        merged = build_agent_override(
            class_name=getattr(cls, "__name__", ""),
            runtime_name=getattr(agent, "name", ""),
            class_patch=cls_patch,
            name_patch=name_patch,
        )
        if merged and hasattr(agent, "apply_override"):
            agent.apply_override(merged)
    except Exception:
        import logging
        logging.getLogger("skyn3t.studio.registry").exception(
            "could not apply overrides to %s", name)
    return agent
