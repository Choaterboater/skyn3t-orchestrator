"""Parallel autonomous worker fleet — up to N concurrent learn + build slots.

Composes with :class:`AutonomousCoordinator` for brief queue and daily caps;
does not duplicate proposal or persistence logic. Uses a fleet-local semaphore
so human orchestrator tasks keep their own concurrency budget.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from skyn3t.cortex.autonomous_loop import AutonomousBrief, _reset_daily_counters
from skyn3t.cortex.continuous_improvement import (
    continuous_improvement_enabled,
    learning_kind_for_slot,
    run_fleet_learning_tick,
)

logger = logging.getLogger("skyn3t.cortex.agent_fleet")

SLOT_STATES = frozenset({"idle", "learning", "building", "proofing"})


@dataclass
class FleetSlot:
    """One worker slot in the fleet."""

    slot_id: int
    state: str = "idle"
    current_slug: Optional[str] = None
    current_brief: Optional[str] = None
    learning_kind: Optional[str] = None
    tokens_today: int = 0
    started_at: float = 0.0
    last_completed_at: float = 0.0
    last_error: Optional[str] = None
    _task: Optional[asyncio.Task] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "state": self.state,
            "current_slug": self.current_slug,
            "current_brief": (self.current_brief or "")[:120] or None,
            "learning_kind": self.learning_kind,
            "tokens_today": self.tokens_today,
            "started_at": self.started_at or None,
            "last_completed_at": self.last_completed_at or None,
            "last_error": self.last_error,
        }


def fleet_should_run(settings: Any | None = None) -> bool:
    from skyn3t.config.settings import get_settings

    cfg = settings or get_settings()
    size = int(getattr(cfg, "agent_fleet_size", 0) or 0)
    return size > 0 or continuous_improvement_enabled()


def effective_fleet_size(settings: Any | None = None) -> int:
    from skyn3t.config.settings import get_settings

    cfg = settings or get_settings()
    size = int(getattr(cfg, "agent_fleet_size", 0) or 0)
    if size > 0:
        return max(1, min(size, 64))
    if continuous_improvement_enabled():
        return 3
    return 0


_DEFAULT_BUILD_DAILY_CAP = 3


def effective_studio_concurrency(settings: Any | None = None) -> int:
    """Cap Studio parallel builds separately from fleet slot count."""
    from skyn3t.config.settings import get_settings

    cfg = settings or get_settings()
    fleet = effective_fleet_size(cfg)
    cap = max(1, int(getattr(cfg, "agent_fleet_max_concurrent_builds", 5) or 5))
    if fleet <= 0:
        return cap
    return max(1, min(fleet, cap))


def effective_build_daily_cap(settings: Any | None = None) -> int:
    """Daily build cap; auto-scales to fleet size only when cap is still default."""
    from skyn3t.config.settings import get_settings

    cfg = settings or get_settings()
    cap = max(0, int(getattr(cfg, "autonomous_build_daily_cap", _DEFAULT_BUILD_DAILY_CAP) or 0))
    fleet = int(getattr(cfg, "agent_fleet_size", 0) or 0)
    if fleet > 0 and cap == _DEFAULT_BUILD_DAILY_CAP:
        return max(cap, fleet)
    return cap


def orchestrator_backpressure(orchestrator: Any) -> Optional[str]:
    """Pause fleet builds when the human task queue is saturated."""
    running = len(getattr(orchestrator, "running_tasks", {}) or {})
    max_c = int(getattr(orchestrator, "_max_concurrent", 10) or 10)
    threshold = max(1, int(max_c * 0.75))
    if running >= threshold:
        return f"orchestrator busy ({running}/{max_c})"
    return None


class AgentFleetCoordinator:
    """Manages N parallel autonomous learn + build workers."""

    def __init__(self, orchestrator: Any, event_bus: Any):
        self.orchestrator = orchestrator
        self.event_bus = event_bus
        self._running = False
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._fleet_sem: Optional[asyncio.Semaphore] = None
        self._learning_sem: Optional[asyncio.Semaphore] = None
        self._slots: List[FleetSlot] = []
        self._slot_tokens_date: str = ""
        self._active_builds: int = 0
        self._active_learning: int = 0
        self._learning_parallel: int = 1
        self._status_cache: Dict[str, Any] = {}
        self._status_cache_at: float = 0.0
        self._status_refresh_task: Optional[asyncio.Task] = None

    def _autonomous(self) -> Any:
        return getattr(self.orchestrator, "_autonomous_coordinator", None)

    async def start(self) -> None:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
        if not fleet_should_run(settings):
            return
        if self._running:
            return

        fleet_size = effective_fleet_size(settings)
        learning_parallel = max(1, int(getattr(settings, "agent_fleet_learning", 1) or 1))

        self._fleet_sem = asyncio.Semaphore(fleet_size)
        self._learning_sem = asyncio.Semaphore(min(learning_parallel, fleet_size))
        self._learning_parallel = min(learning_parallel, fleet_size)
        self._slots = [FleetSlot(slot_id=i) for i in range(fleet_size)]
        self._running = True

        # Cap Studio concurrency below fleet slot count to keep the API responsive.
        if getattr(settings, "autonomous_builds", False):
            try:
                from skyn3t.studio.runner import StudioRunner

                studio_cap = effective_studio_concurrency(settings)
                StudioRunner.configure_max_concurrent(studio_cap)
                logger.info(
                    "agent fleet studio concurrency=%d (fleet_size=%d)",
                    studio_cap,
                    fleet_size,
                )
            except Exception:
                logger.debug("studio concurrency configure failed", exc_info=True)

        # Ensure autonomous coordinator is running (learning + queue); fleet owns builds.
        auto = self._autonomous()
        if auto is not None:
            await auto.start()
            if hasattr(auto, "set_fleet_delegates_builds"):
                auto.set_fleet_delegates_builds(True)
            seed = getattr(auto, "seed_startup_briefs", None)
            if callable(seed):
                try:
                    await seed(min_depth=min(3, fleet_size))
                except Exception:
                    logger.debug("startup brief seed failed", exc_info=True)

        self._dispatcher_task = asyncio.create_task(self._dispatcher_loop())
        self._status_cache = self._build_status()
        self._status_cache_at = time.time()
        self._status_refresh_task = asyncio.create_task(self._status_refresh_loop())
        self._publish_alert(
            "agent_fleet_started",
            fleet_size=fleet_size,
            learning_parallel=learning_parallel,
            studio_concurrency=effective_studio_concurrency(settings),
        )
        logger.info("agent fleet started size=%d learning=%d", fleet_size, learning_parallel)

    async def stop(self) -> None:
        self._running = False
        if self._dispatcher_task:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass
            self._dispatcher_task = None

        if self._status_refresh_task:
            self._status_refresh_task.cancel()
            try:
                await self._status_refresh_task
            except asyncio.CancelledError:
                pass
            self._status_refresh_task = None

        for slot in self._slots:
            if slot._task and not slot._task.done():
                slot._task.cancel()
        if self._slots:
            await asyncio.gather(
                *[s._task for s in self._slots if s._task],
                return_exceptions=True,
            )

        auto = self._autonomous()
        if auto is not None and hasattr(auto, "set_fleet_delegates_builds"):
            auto.set_fleet_delegates_builds(False)

        self._slots = []
        self._running = False

    def get_status(self) -> Dict[str, Any]:
        """Return a cached snapshot — safe under heavy Studio load."""
        if self._status_cache:
            return dict(self._status_cache)
        return self._build_status()

    def _build_status(self) -> Dict[str, Any]:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
        auto = self._autonomous()
        auto_status: Dict[str, Any] = {}
        if auto is not None:
            try:
                auto_status = auto.get_status()
            except Exception:
                pass

        self._reset_slot_token_counters()
        return {
            "running": self._running,
            "fleet_size": len(self._slots),
            "configured_size": effective_fleet_size(settings),
            "studio_concurrency_cap": effective_studio_concurrency(settings),
            "active_builds": self._active_builds,
            "active_learning": self._active_learning,
            "daily_builds": auto_status.get("daily_builds", 0),
            "daily_cap": effective_build_daily_cap(settings),
            "daily_spend_usd": auto_status.get("daily_spend_usd", 0.0),
            "queue_depth": auto_status.get("queue_depth", 0),
            "backpressure": orchestrator_backpressure(self.orchestrator),
            "slots": [s.to_dict() for s in self._slots],
            "autonomous_learning": auto_status.get("autonomous_learning", False),
            "autonomous_builds": auto_status.get("autonomous_builds", False),
            "status_cached_at": self._status_cache_at or None,
        }

    async def _status_refresh_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(2)
                self._status_cache = self._build_status()
                self._status_cache_at = time.time()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("fleet status refresh failed", exc_info=True)

    def _reset_slot_token_counters(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self._slot_tokens_date != today:
            self._slot_tokens_date = today
            for slot in self._slots:
                slot.tokens_today = 0

    async def _dispatcher_tick(self) -> int:
        """One fleet dispatch pass — propose briefs and fill idle slots."""
        assigned_count = 0
        auto = self._autonomous()
        if auto is not None and hasattr(auto, "state"):
            _reset_daily_counters(auto.state)
            auto.state.last_tick_at = time.time()
            await auto._maybe_propose_briefs()

        for slot in self._slots:
            if slot.state != "idle":
                continue
            if slot._task and not slot._task.done():
                continue
            if await self._try_assign_slot(slot):
                assigned_count += 1
            elif self._active_learning >= self._learning_parallel:
                break
        if assigned_count:
            depth = auto.get_status().get("queue_depth") if auto else "?"
            logger.info(
                "fleet dispatcher assigned %d slot(s); queue_depth=%s "
                "active_builds=%d active_learning=%d",
                assigned_count,
                depth,
                self._active_builds,
                self._active_learning,
            )
        return assigned_count

    async def _boot_dispatch(self) -> None:
        """Run one dispatch pass immediately on fleet start (no idle stall on boot)."""
        try:
            await self._dispatcher_tick()
        except Exception:
            logger.exception("fleet boot dispatch failed")

    async def _dispatcher_loop(self) -> None:
        from skyn3t.config.settings import get_settings
        from skyn3t.cortex.never_stop import MIN_TICK_INTERVAL_NEVER_STOP, never_stop_enabled

        settings = get_settings()
        interval = max(
            5,
            int(getattr(settings, "agent_fleet_tick_seconds", 30) or 30),
        )
        if never_stop_enabled():
            interval = max(MIN_TICK_INTERVAL_NEVER_STOP, interval)
        elif interval < 30:
            await asyncio.sleep(2)  # brief warmup when not in never-stop mode
        while self._running:
            try:
                await self._dispatcher_tick()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("agent fleet dispatcher tick failed")
                await asyncio.sleep(interval)

    async def _try_assign_slot(self, slot: FleetSlot) -> bool:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
        builds_on = bool(getattr(settings, "autonomous_builds", False))

        if builds_on:
            busy = orchestrator_backpressure(self.orchestrator)
            if busy is not None:
                logger.debug("fleet slot %d skipped build: %s", slot.slot_id, busy)
            else:
                brief = self._dequeue_brief()
                if brief is not None:
                    logger.info(
                        "fleet slot %d picking build source=%s brief=%s",
                        slot.slot_id,
                        brief.source,
                        brief.brief[:100],
                    )
                    slot._task = asyncio.create_task(self._run_build_slot(slot, brief))
                    return True

        # Learning fill when no build assigned (or builds disabled).
        if self._learning_sem is None or self._active_learning >= self._learning_parallel:
            return False
        try:
            await asyncio.wait_for(self._learning_sem.acquire(), timeout=0.01)
        except asyncio.TimeoutError:
            return False

        kind = learning_kind_for_slot(slot.slot_id)
        logger.info("fleet slot %d picking learning kind=%s", slot.slot_id, kind)
        slot._task = asyncio.create_task(self._run_learning_slot(slot, kind))
        return True

    def _dequeue_brief(self) -> Optional[AutonomousBrief]:
        auto = self._autonomous()
        if auto is None:
            return None
        pop = getattr(auto, "pop_highest_priority_brief", None)
        if callable(pop):
            return pop()  # type: ignore
        return None

    async def _run_build_slot(self, slot: FleetSlot, brief: AutonomousBrief) -> None:
        assert self._fleet_sem is not None
        auto = self._autonomous()
        slot.state = "building"
        slot.current_brief = brief.brief
        slot.current_slug = None
        slot.learning_kind = None
        slot.started_at = time.time()
        slot.last_error = None
        self._active_builds += 1

        self._publish_alert(
            "FLEET_SLOT_STARTED",
            slot_id=slot.slot_id,
            mode="building",
            brief=brief.brief[:200],
            source=brief.source,
        )

        await self._fleet_sem.acquire()
        try:
            if auto is None:
                slot.last_error = "autonomous coordinator unavailable"
                return

            start_build = getattr(auto, "start_build_for_brief", None)
            if not callable(start_build):
                slot.last_error = "start_build_for_brief missing"
                return

            result = await start_build(brief, slot_id=slot.slot_id)
            if result.get("ok"):
                slot.current_slug = str(result.get("slug") or "")
                est_tokens = int(result.get("est_tokens") or 0)
                slot.tokens_today += est_tokens
            else:
                slot.last_error = str(result.get("reason") or "build skipped")
                requeue = getattr(auto, "reenqueue_brief", None)
                if callable(requeue):
                    requeue(brief)
        except Exception as exc:
            slot.last_error = str(exc)
            logger.exception("fleet build slot %s failed", slot.slot_id)
            requeue = getattr(auto, "reenqueue_brief", None) if auto else None
            if callable(requeue):
                requeue(brief)
        finally:
            self._fleet_sem.release()
            self._active_builds = max(0, self._active_builds - 1)
            slot.last_completed_at = time.time()
            slot.state = "idle"
            slot.current_brief = None
            self._publish_alert(
                "FLEET_SLOT_COMPLETED",
                slot_id=slot.slot_id,
                mode="building",
                slug=slot.current_slug,
                error=slot.last_error,
                tokens_today=slot.tokens_today,
            )
            slot.current_slug = None

    async def _run_learning_slot(self, slot: FleetSlot, kind: str) -> None:
        slot.state = "learning"
        slot.learning_kind = kind
        slot.current_brief = None
        slot.current_slug = None
        slot.started_at = time.time()
        slot.last_error = None
        self._active_learning += 1

        self._publish_alert(
            "FLEET_SLOT_STARTED",
            slot_id=slot.slot_id,
            mode="learning",
            learning_kind=kind,
        )

        try:
            summary = await run_fleet_learning_tick(
                self.orchestrator,
                self.event_bus,
                kind=kind,
                slot_id=slot.slot_id,
            )
            if not summary.get("ok", True):
                slot.last_error = str(summary.get("reason") or summary.get("error") or "learning failed")
        except Exception as exc:
            slot.last_error = str(exc)
            logger.debug("fleet learning slot %s failed", slot.slot_id, exc_info=True)
        finally:
            if self._learning_sem is not None:
                self._learning_sem.release()
            self._active_learning = max(0, self._active_learning - 1)
            slot.last_completed_at = time.time()
            slot.state = "idle"
            slot.learning_kind = None
            self._publish_alert(
                "FLEET_SLOT_COMPLETED",
                slot_id=slot.slot_id,
                mode="learning",
                learning_kind=kind,
                error=slot.last_error,
            )

    def _publish_alert(self, kind: str, **fields: Any) -> None:
        try:
            from skyn3t.core.events import Event, EventType

            payload: Dict[str, Any] = {"kind": kind, "fleet": True}
            payload.update(fields)
            self.event_bus.publish(
                Event(
                    event_type=EventType.SYSTEM_ALERT,
                    source="agent_fleet",
                    payload=payload,
                )
            )
        except Exception:
            logger.debug("fleet alert publish failed", exc_info=True)
