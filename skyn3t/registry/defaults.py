"""Built-in default agent roster.

Registers a curated set of specialist agents on orchestrator startup so the
swarm has agents to route to immediately. Opt-out via the env var
SKYN3T_AUTO_REGISTER_AGENTS=false.

Each entry is a (class_name_in_skyn3t.agents, kwargs_factory) tuple; the
factory takes the orchestrator and may pull dependencies (event_bus, rag,
memory) off it. Each agent is instantiated, initialised, and registered.
Failures for a single agent are logged and skipped — they do not abort
startup of the others.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from skyn3t.registry.catalog import build_agent_override

logger = logging.getLogger("skyn3t.registry.defaults")

# cortex audit 2026-06-13: GatedTuner writes approved tuning nudges to
# data/config/runtime.json under {"agents": {<name>: {<config overrides>}}}
# (see skyn3t/cortex/gated_tuner.py). Nothing read that file at startup, so
# approved tuning never took effect (write-only dead loop). Load it here and
# layer the per-agent overrides over each agent's base config at construction.
RUNTIME_CONFIG_PATH = Path("data/config/runtime.json")


def _load_runtime_agent_overrides(
    path: Path | str = RUNTIME_CONFIG_PATH,
) -> Dict[str, Dict[str, Any]]:
    """Return {agent_name: {config overrides}} from runtime.json.

    Defensive: a missing or corrupt file yields {} and never raises, so a bad
    tuning write can never abort agent registration.
    """
    try:
        p = Path(path)
        if not p.exists():
            return {}
        loaded = json.loads(p.read_text())
        agents = loaded.get("agents") if isinstance(loaded, dict) else None
        if not isinstance(agents, dict):
            return {}
        return {
            str(name): dict(cfg)
            for name, cfg in agents.items()
            if isinstance(cfg, dict)
        }
    except Exception:
        logger.exception("runtime tuning overrides load failed (ignored)")
        return {}


def _kw_eb(o):  # event_bus only
    return {"event_bus": o.event_bus}


def _kw_rag(o):
    return {"event_bus": o.event_bus, "rag": getattr(o, "_rag", None)}


# (class_name, kwargs_factory). Order is the registration order shown in UIs.
DEFAULT_ROSTER: List[Tuple[str, Callable[[Any], Dict[str, Any]]]] = [
    ("BrainstormAgent", _kw_eb),
    ("ResearchAgent", _kw_eb),
    ("ArchitectAgent", _kw_eb),
    ("CodeAgent", _kw_eb),
    ("WriterAgent", _kw_eb),
    ("DesignerAgent", _kw_eb),
    ("MarketerAgent", _kw_eb),
    ("ReviewerAgent", _kw_eb),
    ("BusinessAnalystAgent", _kw_eb),
    ("FileOpsAgent", _kw_eb),
    ("GitHubExplorerAgent", _kw_eb),
    ("GitHubIngestorAgent", _kw_rag),
    ("ExplorerAgent", _kw_rag),
    ("CodeImproverAgent", _kw_eb),
    ("SchedulerAgent", _kw_eb),
    ("ProjectMemoryAgent", _kw_rag),
    ("DocsIngestorAgent", _kw_rag),
    ("VerifierAgent", _kw_eb),
    ("BuildVerifierAgent", _kw_eb),
]


async def register_default_roster(orchestrator) -> Dict[str, Any]:
    """Register the default roster on the given orchestrator.

    Returns a dict {"registered": [...names...], "skipped": [{"name", "reason"}, ...]}.
    """
    if os.environ.get("SKYN3T_AUTO_REGISTER_AGENTS", "true").lower() in ("0", "false", "no"):
        return {"registered": [], "skipped": [{"name": "*", "reason": "disabled by env"}]}

    mod = importlib.import_module("skyn3t.agents")
    registered: List[str] = []
    skipped: List[Dict[str, str]] = []

    # cortex audit 2026-06-13: approved tuning overrides keyed by agent name
    # (and class name as fallback), layered over base config below.
    runtime_overrides = _load_runtime_agent_overrides()

    import time as _time

    for class_name, kwargs_factory in DEFAULT_ROSTER:
        cls = getattr(mod, class_name, None)
        if cls is None:
            skipped.append({"name": class_name, "reason": "class not found"})
            continue
        _t0 = _time.monotonic()
        _phase = {"t": _t0}

        def _mark_phase(label: str, _phase=_phase, _class=class_name) -> None:
            now = _time.monotonic()
            dt = now - _phase["t"]
            _phase["t"] = now
            if dt > 0.5:
                logger.warning("[boot] roster %s.%s %.1fs", _class, label, dt)

        try:
            kwargs = kwargs_factory(orchestrator)
            # filter to params the constructor accepts
            sig = inspect.signature(cls)
            kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
            agent = cls(**kwargs)
            _mark_phase("construct")
            # Apply persisted overrides BEFORE register so disable/config
            # take effect from the first task.
            try:
                from skyn3t.config.agent_overrides import get_override_store
                store = get_override_store()
                # By class name (e.g. "WriterAgent") and by instance name
                # (preferred — what the user types in the UI).
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
                logger.exception("override apply failed for %s", class_name)
            # cortex audit 2026-06-13: layer approved tuning (runtime.json)
            # over the agent's base config. GatedTuner keys by runtime agent
            # name; fall back to class name. Tuning keys (request_interval,
            # timeout, max_tokens, prompt_suffix, auth_retry, temperature,
            # max_retries) are read straight off agent.config, so merge there.
            try:
                tuning = (
                    runtime_overrides.get(getattr(agent, "name", ""))
                    or runtime_overrides.get(getattr(cls, "__name__", ""))
                    or {}
                )
                if tuning and isinstance(getattr(agent, "config", None), dict):
                    agent.config.update(tuning)
            except Exception:
                logger.exception("runtime tuning apply failed for %s", class_name)
            if hasattr(agent, "initialize"):
                init = agent.initialize()
                if inspect.iscoroutine(init):
                    await init
            _mark_phase("initialize")
            orchestrator.register_agent(agent)
            _mark_phase("register")
            registered.append(agent.name)
            _dt = _time.monotonic() - _t0
            if _dt > 0.5:
                logger.warning("[boot] roster %-24s %.1fs", class_name, _dt)
        except Exception as e:
            logger.exception("failed to register %s", class_name)
            skipped.append({"name": class_name, "reason": str(e)[:200]})

    # Instantiate persisted custom agents
    try:
        from skyn3t.agents.research_agent import ResearchAgent as _BlankBase
        from skyn3t.config.custom_agents import get_custom_store
        BLANK_BASES = {
            # Use ResearchAgent as a flexible blank slate (simple BaseAgent subclass).
            "blank": _BlankBase,
        }
        for spec in get_custom_store().list():
            cname = str(spec.get("name") or "")
            try:
                base_type = spec.get("base_type") or "blank"
                cls = getattr(mod, base_type, None) or BLANK_BASES.get(base_type)
                if cls is None:
                    skipped.append({"name": cname or "?", "reason": f"unknown base_type {base_type}"})
                    continue
                sig = inspect.signature(cls)
                kwargs = {}
                if "event_bus" in sig.parameters:
                    kwargs["event_bus"] = orchestrator.event_bus
                if "rag" in sig.parameters:
                    kwargs["rag"] = getattr(orchestrator, "_rag", None)
                if "name" in sig.parameters:
                    kwargs["name"] = cname
                agent = cls(**kwargs)
                if hasattr(agent, "initialize"):
                    init = agent.initialize()
                    if inspect.iscoroutine(init):
                        await init
                # apply spec as override (system_prompt, model, backend, ...)
                if hasattr(agent, "apply_override"):
                    agent.apply_override(spec)
                orchestrator.register_agent(agent)
                registered.append(agent.name)
            except Exception as e:
                logger.exception("custom agent failed: %s", cname)
                skipped.append({"name": cname or "?", "reason": str(e)[:200]})
    except Exception:
        logger.exception("custom agent loading failed")

    return {"registered": registered, "skipped": skipped}
