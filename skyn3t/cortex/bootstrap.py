"""Consolidated lifecycle owner for the autonomy cortex.

The cortex is the loop that takes observed outcomes (failures,
patterns, gaps) and turns them into review-gated proposals that
can mutate config, refile fixes, or surface capability gaps. Five
components participate; each was previously started inline in
``Orchestrator._start_cortex`` with subtly different lifecycle
shapes (sync vs async ``start``, only some have a ``stop``).

``CortexBootstrap`` normalizes that mess behind one ``start`` /
``stop`` / ``status`` surface, gives operators a per-component
status view (handler kinds it owns, subscriptions, last error),
and accepts an env-var kill switch — ``SKYN3T_CORTEX_DISABLE`` —
that skips individual components by name. This is the single
on-switch for the loop the rest of the planning doc refers to.

Components, in start order:

| name             | creates        | handles        | listens to             |
|------------------|----------------|----------------|------------------------|
| gated_tuner      | tuning         | tuning         | SYSTEM_ALERT:tuning_*  |
| feature_suggester| feature        |                | TASK_FAILED, ALERT     |
| curiosity        |                |                | (timer loop)           |
| review_watcher   | studio_debug   |                | (global)               |
| auto_cleanup     |                |                | (timer loop)           |

The proposal-handler installer (``cortex.handlers.install_handlers``)
runs after all components are up so ``studio_debug``, ``feature``,
and ``ingest`` kinds get registered against the live orchestrator.
"""

from __future__ import annotations

import inspect
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from skyn3t.core.events import Event, EventType

if TYPE_CHECKING:
    from skyn3t.core.orchestrator import Orchestrator

logger = logging.getLogger("skyn3t.cortex.bootstrap")

_DISABLE_ENV = "SKYN3T_CORTEX_DISABLE"
_REPO_ROOT_ENV = "SKYN3T_REPO_ROOT"


@dataclass
class _Component:
    """In-memory record for one cortex component.

    The bootstrap is the only thing that holds the live instance —
    everything else reads ``status()`` to see what's wired.
    """

    name: str
    instance: Any
    creates: Tuple[str, ...] = ()
    handles: Tuple[str, ...] = ()
    subscriptions: Tuple[str, ...] = ()
    started: bool = False
    error: Optional[str] = None
    skipped_reason: Optional[str] = None
    construction_kwargs: Dict[str, Any] = field(default_factory=dict)


def _disabled_set() -> set[str]:
    """Parse ``SKYN3T_CORTEX_DISABLE`` into a name set.

    ``*`` disables everything; otherwise a comma-separated list of
    component names. Whitespace and empty entries are ignored.
    """
    raw = os.environ.get(_DISABLE_ENV, "").strip()
    if not raw:
        return set()
    if raw == "*":
        return {"*"}
    return {part.strip() for part in raw.split(",") if part.strip()}


def _resolve_repo_root() -> Path:
    """Find the repo root for AutoCleanup's git operations.

    ``AutoCleanup`` runs ``git`` against ``repo_root``. The default
    ``Path(".")`` is the cwd of the python process, which for the
    studio runner can be a project scaffold — pointing the janitor
    at a scaffold tree would have it deleting unrelated branches.
    Honor ``SKYN3T_REPO_ROOT`` first, then walk upward from this file
    looking for the package marker.
    """
    env = os.environ.get(_REPO_ROOT_ENV, "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_dir():
            return p
    here = Path(__file__).resolve()
    for ancestor in (here.parent, *here.parents):
        if (ancestor / "skyn3t" / "core" / "orchestrator.py").exists():
            return ancestor
    return Path(".").resolve()


class CortexBootstrap:
    """Start/stop/status owner for the autonomy cortex components.

    Construct cheaply (no I/O, no subscriptions). Call ``start()``
    from the orchestrator's boot path to wire up every component;
    call ``stop()`` from the orchestrator's shutdown path.

    Failure in one component is logged and reported via ``status()``
    but does not abort the others — a bad ``AutoCleanup`` should
    not silence ``GatedTuner``.
    """

    def __init__(self, orchestrator: "Orchestrator"):
        self.orchestrator = orchestrator
        self.event_bus = orchestrator.event_bus
        self._components: List[_Component] = []
        self._handlers_installed = False
        self._wired = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._wired:
            return
        self._wired = True
        self._register_default_components()
        for c in self._components:
            if c.skipped_reason is not None:
                self._publish_cortex_decision(
                    action="skip_component",
                    reason=c.skipped_reason,
                    input={"component": c.name, "error": c.error},
                )
                continue
            await self._safe_start(c)
        # Proposal handlers depend on the components being live (so
        # that, e.g., gated_tuner can field its own tuning proposals).
        # Install once, after components.
        await self._install_handlers()
        self._publish_boot_summary()

    async def stop(self) -> None:
        if not self._wired:
            return
        # Reverse order: stop dependents before producers.
        for c in reversed(self._components):
            if not c.started:
                continue
            stop_fn = getattr(c.instance, "stop", None)
            if stop_fn is None:
                continue
            try:
                await self._call_lifecycle(stop_fn)
            except Exception as exc:
                logger.exception("cortex stop failed: %s", c.name)
                c.error = f"stop: {exc}"
        self._wired = False

    # ------------------------------------------------------------------
    # Status (web/app surfaces this via /api/cortex/status)
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        store = self._proposal_store()
        return {
            "running": True,
            "booted": self._wired,
            "components": [self._component_status(c) for c in self._components],
            "proposal_handlers": store.registered_handlers() if store else [],
            "proposal_counts": (store.counts() if store else {}),
            "recent_failures": (store.recent_failures(limit=5) if store else []),
            "warnings": self._warnings(),
        }

    def _component_status(self, c: _Component) -> Dict[str, Any]:
        details: Dict[str, Any] = {}
        getter = getattr(c.instance, "get_status", None)
        if callable(getter):
            try:
                got = getter()
                if isinstance(got, dict):
                    details = got
            except Exception:
                logger.debug("get_status failed for %s", c.name, exc_info=True)
        return {
            "name": c.name,
            "class_name": type(c.instance).__name__,
            "started": c.started,
            "subscriptions": list(c.subscriptions),
            "creates_proposals": list(c.creates),
            "handles_proposals": list(c.handles),
            "details": details,
            "error": c.error,
            "skipped_reason": c.skipped_reason,
        }

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register_default_components(self) -> None:
        """Build the default component list, honoring the disable env var.

        Each entry is constructed inside its own try block so a broken
        constructor (missing optional dep, bad path) downgrades the
        component to ``skipped_reason`` instead of aborting bootstrap.

        When the orchestrator already has a component instance attached
        (``orchestrator._feature_suggester`` etc.) — typically because
        an inline ``_start_cortex`` path constructed it earlier — we
        reuse that instance instead of building a second one. This
        avoids duplicate event-bus subscriptions during the transition
        period where some callers wire the cortex inline and some
        delegate to this bootstrap.
        """
        disabled = _disabled_set()
        all_disabled = "*" in disabled

        repo_root = _resolve_repo_root()
        projects_root = repo_root / "data" / "projects"
        proposals_root = repo_root / "data" / "proposals"

        specs: List[Tuple[str, str, Any, Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]] = [
            (
                "gated_tuner",
                "_gated_tuner",
                lambda: _import_and_build(
                    "skyn3t.cortex.gated_tuner", "GatedTuner",
                    self.event_bus,
                ),
                ("tuning",),
                ("tuning",),
                ("SYSTEM_ALERT:tuning_suggestion",),
            ),
            (
                "feature_suggester",
                "_feature_suggester",
                lambda: _import_and_build(
                    "skyn3t.cortex.feature_suggester", "FeatureSuggester",
                    event_bus=self.event_bus,
                ),
                ("feature",),
                (),
                ("TASK_FAILED", "TASK_FAILED_FINAL", "SYSTEM_ALERT"),
            ),
            (
                "curiosity",
                "_curiosity",
                lambda: _import_and_build(
                    "skyn3t.cortex.curiosity", "CuriosityLoop",
                    orchestrator=self.orchestrator, event_bus=self.event_bus,
                ),
                (),
                (),
                (),
            ),
            (
                "review_watcher",
                "_review_watcher",
                lambda: _import_and_build(
                    "skyn3t.cortex.review_watcher", "ReviewWatcher",
                    self.event_bus,
                ),
                ("studio_debug",),
                (),
                ("*",),  # global subscribe
            ),
            (
                "auto_cleanup",
                "_auto_cleanup",
                lambda: _import_and_build(
                    "skyn3t.cortex.auto_cleanup", "AutoCleanup",
                    event_bus=self.event_bus,
                    projects_root=projects_root,
                    proposals_root=proposals_root,
                    repo_root=repo_root,
                ),
                (),
                (),
                (),
            ),
        ]

        for name, orch_attr, factory, creates, handles, subs in specs:
            if all_disabled or name in disabled:
                self._components.append(_Component(
                    name=name, instance=_DisabledMarker(name),
                    creates=creates, handles=handles, subscriptions=subs,
                    skipped_reason=f"disabled via {_DISABLE_ENV}",
                ))
                continue
            existing = getattr(self.orchestrator, orch_attr, None)
            if existing is not None:
                # Reuse the inline-constructed instance so we don't
                # double-subscribe its event handlers. Idempotency of
                # the component's own ``start()`` (each has a ``_wired``
                # check) handles the case where it was already started
                # by the inline path.
                self._components.append(_Component(
                    name=name, instance=existing,
                    creates=creates, handles=handles, subscriptions=subs,
                ))
                continue
            try:
                instance = factory()
            except Exception as exc:
                logger.exception("cortex construct failed: %s", name)
                self._components.append(_Component(
                    name=name, instance=_DisabledMarker(name),
                    creates=creates, handles=handles, subscriptions=subs,
                    error=f"construct: {exc}",
                    skipped_reason="construct failed",
                ))
                continue
            # Park the freshly-built instance on the orchestrator so
            # later code paths (status endpoints, shutdown handlers,
            # other tests) see it the same way they'd see an inline-
            # constructed one.
            try:
                setattr(self.orchestrator, orch_attr, instance)
            except Exception:
                logger.debug("could not bind %s to orchestrator", orch_attr, exc_info=True)
            self._components.append(_Component(
                name=name, instance=instance,
                creates=creates, handles=handles, subscriptions=subs,
            ))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _safe_start(self, c: _Component) -> None:
        start_fn = getattr(c.instance, "start", None)
        if start_fn is None:
            c.error = "no start() method"
            return
        try:
            await self._call_lifecycle(start_fn)
        except Exception as exc:
            logger.exception("cortex start failed: %s", c.name)
            c.error = f"start: {exc}"
            self._publish_alert("component_start_failed", c.name, str(exc))
            return
        c.started = True

    async def _install_handlers(self) -> None:
        if self._handlers_installed:
            return
        try:
            from skyn3t.cortex.handlers import install_handlers
            install_handlers(self.orchestrator)
            self._handlers_installed = True
        except Exception as exc:
            logger.exception("install_handlers failed")
            self._publish_alert("handlers_install_failed", "handlers", str(exc))

    async def _call_lifecycle(self, fn) -> None:
        """Call ``fn()`` whether sync or async."""
        result = fn()
        if inspect.iscoroutine(result):
            await result

    def _proposal_store(self):
        try:
            from skyn3t.cortex import get_store
            return get_store()
        except Exception:
            logger.debug("proposal store unavailable", exc_info=True)
            return None

    def _warnings(self) -> List[str]:
        out: List[str] = []
        for c in self._components:
            if c.skipped_reason and "disabled" not in c.skipped_reason:
                out.append(f"{c.name}: {c.skipped_reason}")
            elif c.error and not c.started:
                out.append(f"{c.name}: {c.error}")
        if not self._handlers_installed and self._wired:
            out.append("proposal handlers not installed")
        return out

    def _publish_boot_summary(self) -> None:
        started = [c.name for c in self._components if c.started]
        skipped = [c.name for c in self._components if c.skipped_reason]
        failed = [c.name for c in self._components if c.error and not c.started]
        try:
            self.event_bus.publish(Event(
                event_type=EventType.SYSTEM_ALERT,
                source="cortex_bootstrap",
                payload={
                    "kind": "cortex_booted",
                    "started": started,
                    "skipped": skipped,
                    "failed": failed,
                    "handlers_installed": self._handlers_installed,
                },
            ))
        except Exception:
            logger.debug("could not publish boot summary", exc_info=True)

    def _publish_alert(self, kind: str, component: str, message: str) -> None:
        try:
            self.event_bus.publish(Event(
                event_type=EventType.SYSTEM_ALERT,
                source="cortex_bootstrap",
                payload={"kind": kind, "component": component, "error": message},
            ))
        except Exception:
            logger.debug("alert publish failed", exc_info=True)

    def _publish_cortex_decision(
        self, *, action: str, reason: str = "", input: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit a CORTEX_DECISION event for the autonomy audit stream."""
        try:
            from skyn3t.intelligence.cortex_decisions import publish_decision
            publish_decision(
                self.event_bus,
                system="cortex",
                action=action,
                reason=reason,
                input=input,
                source="cortex_bootstrap",
            )
        except Exception:
            logger.debug("cortex decision publish failed", exc_info=True)


class _DisabledMarker:
    """Stand-in instance for disabled / failed-to-construct components.

    Has no ``start``/``stop`` so the safe-start path records "no start()"
    in ``c.error`` and moves on. Keeps ``status()`` shape stable.
    """

    def __init__(self, name: str):
        self.name = name


def _import_and_build(module_name: str, class_name: str, *args, **kwargs) -> Any:
    """Lazy-import and instantiate a cortex component class."""
    import importlib

    mod = importlib.import_module(module_name)
    cls = getattr(mod, class_name)
    return cls(*args, **kwargs)
