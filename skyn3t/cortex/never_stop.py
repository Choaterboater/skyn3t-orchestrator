"""Process-level never-stop watchdog — restarts dead autonomy tasks and refills queues.

When ``SKYN3T_NEVER_STOP=1`` (default ON when continuous improvement is on), a
background loop every ~30s verifies that the improvement engine, autonomous
coordinator, and agent fleet dispatcher tasks are alive. Dead tasks are restarted
and ``NEVER_STOP_RECOVERED`` alerts are published on the event bus.

Also replenishes the autonomous brief queue when it stays empty for longer than
``SKYN3T_NEVER_STOP_QUEUE_EMPTY_SECONDS`` (default 300s).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from skyn3t.cortex.continuous_improvement import continuous_improvement_enabled

logger = logging.getLogger("skyn3t.cortex.never_stop")

WATCHDOG_CHECK_SECONDS = 30
DEFAULT_QUEUE_EMPTY_SECONDS = 300
RECOVERY_ALERT = "NEVER_STOP_RECOVERED"
MIN_TICK_INTERVAL_NEVER_STOP = 30


def never_stop_enabled() -> bool:
    """True unless ``SKYN3T_NEVER_STOP=0``; defaults to continuous-improvement flag."""
    raw = os.environ.get("SKYN3T_NEVER_STOP", "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return False
    if raw in ("1", "on", "true", "yes"):
        return True
    return continuous_improvement_enabled()


def effective_loop_interval(
    settings: Any,
    attr: str,
    default: int,
    *,
    floor_idle: int = 60,
) -> int:
    """Return loop interval; min 30s when never-stop is on."""
    interval = max(1, int(getattr(settings, attr, default) or default))
    if never_stop_enabled():
        return max(MIN_TICK_INTERVAL_NEVER_STOP, interval)
    return max(floor_idle, interval)


class NeverStopWatchdog:
    """Monitors autonomy background tasks and restarts them when they die."""

    def __init__(self, orchestrator: Any, event_bus: Any):
        self.orchestrator = orchestrator
        self.event_bus = event_bus
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None
        self._started_at = time.time()
        self._last_recovery_at: Optional[float] = None
        self._recoveries_total = 0

    async def start(self) -> None:
        if not never_stop_enabled():
            logger.info("never-stop watchdog disabled (SKYN3T_NEVER_STOP=0)")
            return
        if self._running:
            return
        self._running = True
        self._started_at = time.time()
        self._task = asyncio.create_task(self._loop())
        logger.info("never-stop watchdog started (check every %ss)", WATCHDOG_CHECK_SECONDS)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def get_status(self) -> Dict[str, Any]:
        return {
            "never_stop": never_stop_enabled(),
            "watchdog_running": self._running,
            "last_recovery_at": self._last_recovery_at,
            "uptime_seconds": int(time.time() - self._started_at) if self._running else 0,
            "recoveries_total": self._recoveries_total,
        }

    async def _loop(self) -> None:
        while self._running:
            try:
                if getattr(self.orchestrator, "_running", True):
                    await self._check_and_recover()
                    await self._maybe_replenish_queue()
                await asyncio.sleep(WATCHDOG_CHECK_SECONDS)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("never-stop watchdog tick failed")
                await asyncio.sleep(WATCHDOG_CHECK_SECONDS)

    def _monitored_components(self) -> List[Tuple[str, str]]:
        return [
            ("continuous_improvement", "_continuous_improvement"),
            ("autonomous_loop", "_autonomous_coordinator"),
            ("agent_fleet", "_agent_fleet_coordinator"),
        ]

    def _should_run(self, name: str) -> bool:
        from skyn3t.config.settings import get_settings
        from skyn3t.cortex.agent_fleet import fleet_should_run

        settings = get_settings()
        if name == "continuous_improvement":
            return continuous_improvement_enabled()
        if name == "autonomous_loop":
            # Build loop background task only exists when autonomous builds are on.
            return bool(getattr(settings, "autonomous_builds", False))
        if name == "agent_fleet":
            return fleet_should_run(settings)
        return False

    def _component_task(self, inst: Any, name: str) -> Optional[asyncio.Task[Any]]:
        if name == "agent_fleet":
            return getattr(inst, "_dispatcher_task", None)
        return getattr(inst, "_task", None)

    def _needs_recovery(self, name: str, inst: Any) -> bool:
        if not self._should_run(name):
            return False
        task = self._component_task(inst, name)
        if task is not None and task.done():
            exc = task.exception() if not task.cancelled() else None
            if exc:
                logger.warning(
                    "never-stop: %s task died: %s: %s",
                    name,
                    type(exc).__name__,
                    exc,
                )
            else:
                logger.warning("never-stop: %s task ended unexpectedly", name)
            return True
        if getattr(inst, "_running", False):
            return False
        # Component should be running but _running is false — cold restart.
        return True

    async def _check_and_recover(self) -> None:
        if not getattr(self.orchestrator, "_running", True):
            return
        for name, attr in self._monitored_components():
            inst = getattr(self.orchestrator, attr, None)
            if inst is None:
                continue
            if not self._needs_recovery(name, inst):
                continue
            await self._restart_component(name, inst)

    async def _restart_component(self, name: str, inst: Any) -> None:
        reason = "task_died"
        try:
            stop_fn = getattr(inst, "stop", None)
            if callable(stop_fn) and getattr(inst, "_running", False):
                # A component's stop() does ``await self._task`` and only swallows
                # CancelledError. When the loop died with a non-CancelledError
                # exception (e.g. ImportError above the inner try), awaiting that
                # finished task re-raises it — which would abort recovery before
                # start() ever runs and leave _running stuck True, looping the
                # same failure forever. Swallow ALL exceptions from stop() here.
                try:
                    result = stop_fn()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.debug(
                        "never-stop: %s stop() raised during recovery (ignored)",
                        name,
                        exc_info=True,
                    )
                reason = "task_died_after_stop"

            # Clear any finished task reference and reset _running so start()
            # actually restarts the loop (start() short-circuits while _running
            # is True, and stop() may have re-raised before nulling the task).
            self._clear_dead_task(inst, name)
            setattr(inst, "_running", False)

            start_fn = getattr(inst, "start", None)
            if not callable(start_fn):
                logger.error("never-stop: %s has no start()", name)
                return

            result = start_fn()
            if asyncio.iscoroutine(result):
                await result

            self._last_recovery_at = time.time()
            self._recoveries_total += 1
            self._publish_recovery(name, reason=reason)
            logger.info("never-stop recovered component=%s reason=%s", name, reason)
        except Exception as exc:
            logger.exception("never-stop recovery failed for %s", name)
            self._publish_recovery(name, reason=f"recovery_failed:{exc}")

    def _clear_dead_task(self, inst: Any, name: str) -> None:
        """Null out a finished task reference so start() recreates it."""
        attr = "_dispatcher_task" if name == "agent_fleet" else "_task"
        task = getattr(inst, attr, None)
        if task is not None and task.done():
            setattr(inst, attr, None)

    async def _maybe_replenish_queue(self) -> None:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
        if not getattr(settings, "autonomous_builds", False):
            return

        coord = getattr(self.orchestrator, "_autonomous_coordinator", None)
        if coord is None:
            return

        replenish = getattr(coord, "replenish_queue_if_stale", None)
        if not callable(replenish):
            return

        empty_seconds = max(
            60,
            int(
                getattr(settings, "never_stop_queue_empty_seconds", DEFAULT_QUEUE_EMPTY_SECONDS)
                or DEFAULT_QUEUE_EMPTY_SECONDS
            ),
        )
        try:
            injected = await replenish(settings, empty_seconds=empty_seconds)
            if injected:
                logger.info("never-stop replenished queue with %d synthetic brief(s)", injected)
        except Exception:
            logger.debug("never-stop queue replenish failed", exc_info=True)

    def _publish_recovery(self, component: str, *, reason: str = "task_died") -> None:
        try:
            from skyn3t.core.events import Event, EventType

            self.event_bus.publish(
                Event(
                    event_type=EventType.SYSTEM_ALERT,
                    source="never_stop",
                    payload={
                        "kind": RECOVERY_ALERT,
                        "component": component,
                        "reason": reason,
                        "recoveries_total": self._recoveries_total,
                        "last_recovery_at": self._last_recovery_at,
                    },
                )
            )
        except Exception:
            logger.debug("NEVER_STOP_RECOVERED publish failed", exc_info=True)
