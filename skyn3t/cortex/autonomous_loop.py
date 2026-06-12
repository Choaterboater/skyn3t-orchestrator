"""Autonomous learning schedule + Studio build loop.

When enabled via env flags, SkyN3t can:
  - Schedule periodic GitHub scout runs (learning ingest)
  - Propose micro-briefs from scout, build-pattern gaps, failures, competitive intel
  - Start Studio builds in PROJECTS_DIR without CLI prompts

SkyN3t repo mutations remain approval-gated — autonomous builds only create
projects under PROJECTS_DIR. When ``SKYN3T_AUTONOMOUS_BUILDS=1``, Studio
architect/designer gates are bypassed automatically (see
``auto_approve_enabled()`` in ``settings.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger("skyn3t.cortex.autonomous_loop")

STATE_PATH = Path("data/autonomous_loop_state.json")
_SCOUT_JOB_NAME = "autonomous-repo-scout"


@dataclass
class AutonomousBrief:
    """A queued autonomous Studio brief."""

    brief: str
    template: str = "auto"
    source: str = "unknown"
    trigger: str = ""
    priority: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class LoopState:
    today_date: str = ""
    daily_builds: int = 0
    daily_spend_usd: float = 0.0
    recent_brief_hashes: List[str] = field(default_factory=list)
    queue: List[Dict[str, Any]] = field(default_factory=list)
    last_tick_at: float = 0.0
    last_build_slug: Optional[str] = None
    last_skip_reason: Optional[str] = None
    last_proof_slug: Optional[str] = None
    last_proof_ok: Optional[bool] = None
    last_proof_summary: Optional[str] = None
    last_proof_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "today_date": self.today_date,
            "daily_builds": self.daily_builds,
            "daily_spend_usd": self.daily_spend_usd,
            "recent_brief_hashes": list(self.recent_brief_hashes),
            "queue": list(self.queue),
            "last_tick_at": self.last_tick_at,
            "last_build_slug": self.last_build_slug,
            "last_skip_reason": self.last_skip_reason,
            "last_proof_slug": self.last_proof_slug,
            "last_proof_ok": self.last_proof_ok,
            "last_proof_summary": self.last_proof_summary,
            "last_proof_at": self.last_proof_at,
        }


def _load_state() -> LoopState:
    try:
        if STATE_PATH.exists():
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return LoopState(
                today_date=str(raw.get("today_date") or ""),
                daily_builds=int(raw.get("daily_builds") or 0),
                daily_spend_usd=float(raw.get("daily_spend_usd") or 0.0),
                recent_brief_hashes=list(raw.get("recent_brief_hashes") or []),
                queue=list(raw.get("queue") or []),
                last_tick_at=float(raw.get("last_tick_at") or 0.0),
                last_build_slug=raw.get("last_build_slug"),
                last_skip_reason=raw.get("last_skip_reason"),
                last_proof_slug=raw.get("last_proof_slug"),
                last_proof_ok=raw.get("last_proof_ok"),
                last_proof_summary=raw.get("last_proof_summary"),
                last_proof_at=float(raw.get("last_proof_at") or 0.0),
            )
    except Exception:
        logger.debug("autonomous loop state load failed", exc_info=True)
    return LoopState()


def _save_state(state: LoopState) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    except Exception:
        logger.exception("autonomous loop state save failed")


def _brief_hash(brief: str) -> str:
    normalized = " ".join((brief or "").strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _reset_daily_counters(state: LoopState) -> None:
    today = time.strftime("%Y-%m-%d")
    if state.today_date != today:
        state.today_date = today
        state.daily_builds = 0
        state.daily_spend_usd = 0.0


def _parse_schedule_expr(schedule_expr: str):
    """Resolve a scout cadence expression to its first next_run, or None.

    Delegates to ``SchedulerAgent._parse_schedule`` so the autonomous loop and
    the scheduler agree on which cadence forms (including ``interval:<N><unit>``)
    are valid. Returns ``None`` when the expression cannot be parsed.
    """
    try:
        from skyn3t.agents.scheduler_agent import SchedulerAgent

        return SchedulerAgent._parse_schedule(SchedulerAgent.__new__(SchedulerAgent), schedule_expr)
    except Exception:
        logger.debug("scout schedule parse failed for %r", schedule_expr, exc_info=True)
        return None


async def ensure_autonomous_scout_schedule(orchestrator: Any) -> Dict[str, Any]:
    """Create a recurring repo-scout job when autonomous learning is on."""
    from skyn3t.config.settings import get_settings

    settings = get_settings()
    if not getattr(settings, "autonomous_learning", True):
        return {"scheduled": False, "reason": "autonomous_learning disabled"}

    memory = getattr(orchestrator, "memory_store", None) or getattr(orchestrator, "_memory", None)
    if memory is None:
        return {"scheduled": False, "reason": "memory store unavailable"}

    schedule_expr = str(getattr(settings, "autonomous_scout_schedule", "interval:12h") or "interval:12h")
    try:
        jobs = await memory.list_scheduled_jobs(enabled_only=False)
    except Exception as exc:
        return {"scheduled": False, "reason": f"list jobs failed: {exc}"}

    for job in jobs:
        name = str(job.get("name") or "")
        agent = str(job.get("agent_name") or "")
        if name == _SCOUT_JOB_NAME or (
            agent in {"github_repo_scout", "repo_scout"} and name.startswith("autonomous-")
        ):
            return {"scheduled": True, "job_id": job.get("id"), "existing": True}

    from skyn3t.config.settings import get_settings as _gs

    cfg = _gs()
    scout_config = {
        "cadence": "daily",
        "limit": max(1, min(int(cfg.cortex_scout_default_limit), 4)),
        "queries": list(cfg.cortex_scout_fit_queries or [])[:5],
        "platforms": ["github"],
    }

    # Compute a concrete first next_run so the recurring job actually fires.
    # If the cadence expression is unparseable we must NOT claim it is
    # scheduled — otherwise next_run stays None forever and status lies.
    next_run = _parse_schedule_expr(schedule_expr)
    if next_run is None:
        logger.warning("autonomous scout schedule %r is unparseable; not scheduling", schedule_expr)
        return {"scheduled": False, "reason": f"unparseable schedule: {schedule_expr}"}

    job_id = str(uuid4())
    try:
        await memory.save_scheduled_job(
            job_id=job_id,
            name=_SCOUT_JOB_NAME,
            schedule_expr=schedule_expr,
            agent_name="github_repo_scout",
            prompt=json.dumps(scout_config, sort_keys=True),
            enabled=True,
            next_run=next_run,
            run_count=0,
        )
    except Exception as exc:
        logger.exception("failed to save autonomous scout job")
        return {"scheduled": False, "reason": str(exc)}
    return {
        "scheduled": True,
        "job_id": job_id,
        "existing": False,
        "schedule_expr": schedule_expr,
        "next_run": next_run.isoformat(),
    }


class AutonomousCoordinator:
    """Owns scout schedule + autonomous Studio build loop."""

    def __init__(self, orchestrator: Any, event_bus: Any):
        self.orchestrator = orchestrator
        self.event_bus = event_bus
        self.state = _load_state()
        self._pending: asyncio.Queue[AutonomousBrief] = asyncio.Queue()  # type: ignore
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._wired = False
        self._scout_boot: Dict[str, Any] = {}
        self._fleet_delegates_builds = False
        self._queue_empty_since: Optional[float] = None

    async def start(self) -> None:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
        if getattr(settings, "autonomous_learning", True):
            self._scout_boot = await ensure_autonomous_scout_schedule(self.orchestrator)
            self._publish_alert(
                "autonomous_learning_booted",
                scout=self._scout_boot,
            )
            try:
                from skyn3t.core.openrouter_catalog import schedule_background_sync

                schedule_background_sync()
            except Exception:
                logger.debug("openrouter sync schedule from autonomous loop failed", exc_info=True)

        if not getattr(settings, "autonomous_builds", False):
            return

        if self._running:
            return
        self._running = True
        if not self._wired:
            self._wire_events()
        # Hydrate queue from persisted state
        for item in self.state.queue:
            try:
                brief = AutonomousBrief(
                    brief=str(item.get("brief") or ""),
                    template=str(item.get("template") or "auto"),
                    source=str(item.get("source") or "persisted"),
                    trigger=str(item.get("trigger") or ""),
                    priority=int(item.get("priority") or 0),
                    created_at=float(item.get("created_at") or time.time()),
                )
                if brief.brief.strip():
                    await self._pending.put(brief)
            except Exception:
                continue
        self.state.queue = []
        _save_state(self.state)
        self._task = asyncio.create_task(self._loop())
        self._publish_alert("autonomous_builds_started", scout=self._scout_boot)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def set_fleet_delegates_builds(self, enabled: bool) -> None:
        """When True, the single-build loop only proposes briefs; fleet starts builds."""
        self._fleet_delegates_builds = bool(enabled)

    def pop_highest_priority_brief(self) -> Optional[AutonomousBrief]:
        """Dequeue the highest-priority brief for the agent fleet."""
        items: List[AutonomousBrief] = []
        while not self._pending.empty():
            try:
                items.append(self._pending.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not items:
            return None
        items.sort(key=lambda b: (-b.priority, b.created_at))
        chosen = items[0]
        for rest in items[1:]:
            self._pending.put_nowait(rest)
        self._persist_queue_snapshot()
        return chosen

    def reenqueue_brief(self, brief: AutonomousBrief) -> None:
        """Put a brief back on the queue after a failed fleet start."""
        try:
            self._pending.put_nowait(brief)
            self._persist_queue_snapshot()
        except Exception:
            logger.debug("reenqueue brief failed", exc_info=True)

    async def start_build_for_brief(
        self,
        brief: AutonomousBrief,
        *,
        slot_id: int = 0,
    ) -> Dict[str, Any]:
        """Start a Studio build for *brief* (used by AgentFleetCoordinator)."""
        skip = self._check_build_gates()
        if skip:
            return {"ok": False, "reason": skip}
        try:
            slug, est_tokens = await self._execute_build(brief, slot_id=slot_id)
            return {"ok": True, "slug": slug, "est_tokens": est_tokens}
        except Exception as exc:
            logger.exception("fleet build start failed slot=%s", slot_id)
            return {"ok": False, "reason": str(exc)}

    def get_status(self) -> Dict[str, Any]:
        from skyn3t.config.settings import get_settings
        from skyn3t.cortex.agent_fleet import effective_build_daily_cap

        settings = get_settings()
        return {
            "autonomous_learning": bool(getattr(settings, "autonomous_learning", True)),
            "autonomous_builds": bool(getattr(settings, "autonomous_builds", False)),
            "autonomous_proof_run": bool(getattr(settings, "autonomous_proof_run", True)),
            "autonomous_min_reviewer_score": int(
                getattr(settings, "autonomous_min_reviewer_score", 85)
            ),
            "scout_schedule": self._scout_boot,
            "daily_builds": self.state.daily_builds,
            "daily_spend_usd": self.state.daily_spend_usd,
            "daily_cap": effective_build_daily_cap(settings),
            "fleet_delegates_builds": self._fleet_delegates_builds,
            "daily_budget_usd": float(getattr(settings, "autonomous_build_daily_budget_usd", 5.0)),
            "queue_depth": self._pending.qsize(),
            "last_build_slug": self.state.last_build_slug,
            "last_skip_reason": self.state.last_skip_reason,
            "last_proof_slug": self.state.last_proof_slug,
            "last_proof_ok": self.state.last_proof_ok,
            "last_proof_summary": self.state.last_proof_summary,
            "last_proof_at": self.state.last_proof_at,
            "last_tick_at": self.state.last_tick_at,
            "running": self._running,
        }

    def _wire_events(self) -> None:
        self._wired = True
        try:
            from skyn3t.core.events import EventType

            self.event_bus.subscribe(self._on_system_alert, EventType.SYSTEM_ALERT)
        except Exception:
            logger.exception("autonomous loop event subscribe failed")

    def _on_system_alert(self, event: Any) -> None:
        from skyn3t.config.settings import get_settings

        if not getattr(get_settings(), "autonomous_builds", False):
            return
        payload = getattr(event, "payload", {}) or {}
        kind = str(payload.get("kind") or "")
        if kind == "PROJECT_FAILED":
            try:
                asyncio.create_task(self._enqueue_failure_brief(payload))
            except Exception:
                logger.debug("enqueue failure brief failed", exc_info=True)
        elif kind == "PROJECT_COMPLETED":
            try:
                asyncio.create_task(self._on_project_completed(payload))
            except Exception:
                logger.debug("autonomous proof-run hook failed", exc_info=True)

    async def _on_project_completed(self, payload: Dict[str, Any]) -> None:
        """Fail-closed quality/proof checks after autonomous Studio builds finish."""
        from skyn3t.config.settings import get_settings

        settings = get_settings()
        if not getattr(settings, "autonomous_builds", False):
            return

        slug = str(payload.get("slug") or "").strip()
        if not slug:
            return
        if not payload.get("autonomous") and not slug.startswith("auto-"):
            return

        quality_failure = self._autonomous_quality_failure(payload, settings)
        if quality_failure:
            self._publish_alert(
                "AUTONOMOUS_QUALITY_REJECTED",
                slug=slug,
                stack=payload.get("stack"),
                status=payload.get("status"),
                verdict=payload.get("verdict"),
                reviewer_score=self._payload_reviewer_score(payload),
                summary=quality_failure,
            )
            await self._enqueue_quality_retry_brief(payload, quality_failure, settings)
            return

        if not getattr(settings, "autonomous_proof_run", True):
            return

        from skyn3t.studio.proof_run import run_proof_for_slug

        proof = await run_proof_for_slug(
            slug,
            execution_profile=str(payload.get("execution_profile") or "balanced"),
            strict=True,
        )
        self.state.last_proof_slug = slug
        self.state.last_proof_ok = bool(proof.get("ok"))
        self.state.last_proof_summary = str(proof.get("summary") or "")[:500]
        self.state.last_proof_at = time.time()
        _save_state(self.state)

        if proof.get("ok"):
            self._publish_alert(
                "AUTONOMOUS_PROOF_PASSED",
                slug=slug,
                stack=proof.get("stack"),
                summary=proof.get("summary"),
            )
            return

        failure_msg = str(proof.get("summary") or proof.get("failure_hint") or "proof run failed")
        self._publish_alert(
            "AUTONOMOUS_PROOF_FAILED",
            slug=slug,
            stack=proof.get("stack"),
            summary=failure_msg,
            stderr=(proof.get("stderr") or "")[:800],
        )
        await self._enqueue_failure_brief(
            {
                "slug": slug,
                "error": f"post-build proof failed: {failure_msg}",
                "stack": proof.get("stack") or payload.get("stack") or "web",
                "source": "proof_run",
            }
        )

    @staticmethod
    def _payload_reviewer_score(payload: Dict[str, Any]) -> Optional[float]:
        for key in ("reviewer_score", "score"):
            raw = payload.get(key)
            if isinstance(raw, (int, float)):
                return float(raw)
        quality = payload.get("quality_summary")
        if isinstance(quality, dict):
            raw = quality.get("score")
            if isinstance(raw, (int, float)):
                return float(raw)
        return None

    def _autonomous_quality_failure(
        self,
        payload: Dict[str, Any],
        settings: Any,
    ) -> Optional[str]:
        threshold = int(getattr(settings, "autonomous_min_reviewer_score", 85))
        status = str(payload.get("status") or "").strip().lower()
        verdict = str(payload.get("verdict") or "").strip().lower()
        if not verdict:
            quality = payload.get("quality_summary")
            if isinstance(quality, dict):
                verdict = str(quality.get("verdict") or "").strip().lower()

        score = self._payload_reviewer_score(payload)
        if status == "failed":
            return str(payload.get("message") or payload.get("error") or "project status is failed")
        if status != "done":
            return f"project status is {status or 'unknown'}, not done"
        if verdict != "go":
            return f"reviewer verdict is {verdict or 'missing'}, not go"
        if score is None:
            return "reviewer score is missing"
        if score < threshold:
            return f"reviewer score {score:.0f}/100 is below autonomous floor {threshold}/100"
        return None

    async def _enqueue_quality_retry_brief(
        self,
        payload: Dict[str, Any],
        reason: str,
        settings: Any,
    ) -> None:
        if not getattr(settings, "autonomous_quality_retry", True):
            return
        stack = str(payload.get("stack") or "web").strip() or "web"
        slug = str(payload.get("slug") or payload.get("project_slug") or "previous run").strip()
        original = str(payload.get("brief") or payload.get("title") or "").strip()
        if not original:
            original = "the previous autonomous project brief"
        threshold = int(getattr(settings, "autonomous_min_reviewer_score", 85))
        brief = (
            f"Autonomous quality recovery drill: rebuild {original[:180]} as a polished, "
            f"runnable {stack} product. Previous run {slug} was rejected because {reason}. "
            f"Ship a clean reviewer 'go' result at or above {threshold}/100: implement the core "
            "workflow end-to-end, include runnable packaging/configuration, use real integrations "
            "where the brief asks for them, and do not ship TODOs, placeholders, mock-only demos, "
            "or weak visual states."
        )
        await self._offer_brief(
            AutonomousBrief(
                brief=brief,
                source="quality_gate",
                trigger=reason[:200],
                priority=90,
            )
        )

    async def _enqueue_failure_brief(self, payload: Dict[str, Any]) -> None:
        error = str(payload.get("error") or payload.get("message") or "").strip()
        stack = str(payload.get("stack") or "web").strip()
        if not error:
            return
        brief = (
            f"Autonomous practice build: minimal {stack} app that avoids the failure "
            f"«{error[:120]}». Ship a runnable scaffold with dark theme and one core workflow."
        )
        await self._offer_brief(
            AutonomousBrief(
                brief=brief,
                source="failure_lesson",
                trigger=error[:200],
                priority=80,
            )
        )

    async def enqueue_brief(self, item: AutonomousBrief) -> bool:
        """Public entry for other flywheel components (e.g. continuous improvement)."""
        return await self._offer_brief(item)

    async def seed_startup_briefs(self, *, min_depth: int = 3) -> int:
        """Seed competitive practice briefs when the queue is shallow on boot."""
        from skyn3t.cortex.competitive_intel import competitive_practice_brief

        added = 0
        attempts = 0
        max_attempts = max(min_depth * 4, 8)
        while self._pending.qsize() < min_depth and attempts < max_attempts:
            attempts += 1
            text = competitive_practice_brief()
            if not text:
                break
            ok = await self._offer_brief(
                AutonomousBrief(
                    brief=text,
                    source="competitive_intel",
                    trigger="startup_seed",
                    priority=45,
                )
            )
            if ok:
                added += 1
        if added:
            logger.info("seeded %d startup brief(s); queue_depth=%d", added, self._pending.qsize())
        return added

    @staticmethod
    def _augment_brief_text(text: str) -> str:
        """Apply the owner's standing domain + deployability requirements.

        Single choke point for every autonomous brief (scout, build
        patterns, competitive, never-stop synthetics). Owner directives
        2026-06-11: builds target the networking domain (Aruba / Juniper
        / HPE, compared against the scouted reference repos) and ship as
        FULLY deployed apps with configuration in the frontend — not
        minimal stubs.
        """
        parts = [text.strip()]
        domain = os.environ.get("SKYN3T_AUTONOMOUS_BRIEF_DOMAIN", "").strip()
        if domain and domain.lower()[:24] not in text.lower():
            parts.append(f"Domain focus: {domain}.")
        parts.append(
            "Ship a FULLY DEPLOYED app, not a stub: it must pass build and "
            "boot verification, include a README with a one-command run, and "
            "expose all runtime configuration (API base URL / controller "
            "address, tokens, site or org IDs) in an in-app settings panel "
            "persisted client-side — plus a mock-data mode so every screen "
            "is demonstrable without live credentials."
        )
        return "\n\n".join(parts)

    async def _offer_brief(self, item: AutonomousBrief) -> bool:
        if not item.brief.strip():
            return False
        item.brief = self._augment_brief_text(item.brief)
        h = _brief_hash(item.brief)
        if h in self.state.recent_brief_hashes:
            return False
        self.state.recent_brief_hashes.append(h)
        self.state.recent_brief_hashes = self.state.recent_brief_hashes[-40:]
        await self._pending.put(item)
        self._persist_queue_snapshot()
        self._publish_alert(
            "AUTONOMOUS_BUILD_QUEUED",
            brief=item.brief[:200],
            source=item.source,
            trigger=item.trigger[:120],
        )
        return True

    def _persist_queue_snapshot(self) -> None:
        # Best-effort snapshot for dashboard after queue changes
        try:
            items: List[Dict[str, Any]] = []
            while not self._pending.empty():
                b = self._pending.get_nowait()
                items.append(
                    {
                        "brief": b.brief,
                        "template": b.template,
                        "source": b.source,
                        "trigger": b.trigger,
                        "priority": b.priority,
                        "created_at": b.created_at,
                    }
                )
            for b_dict in items:
                self._pending.put_nowait(
                    AutonomousBrief(
                        brief=b_dict["brief"],
                        template=b_dict["template"],
                        source=b_dict["source"],
                        trigger=b_dict["trigger"],
                        priority=b_dict["priority"],
                        created_at=b_dict["created_at"],
                    )
                )
            self.state.queue = items
            _save_state(self.state)
        except Exception:
            logger.debug("queue snapshot failed", exc_info=True)

    async def _loop(self) -> None:
        from skyn3t.config.settings import get_settings
        from skyn3t.cortex.never_stop import effective_loop_interval

        settings = get_settings()
        interval = effective_loop_interval(
            settings,
            "autonomous_build_interval_seconds",
            900,
        )
        while self._running:
            try:
                _reset_daily_counters(self.state)
                self.state.last_tick_at = time.time()
                await self._maybe_propose_briefs()
                if not self._fleet_delegates_builds:
                    skip = await self._maybe_start_build()
                    if skip:
                        self.state.last_skip_reason = skip
                _save_state(self.state)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("autonomous build loop tick failed")
                await asyncio.sleep(interval)

    def _track_queue_depth(self) -> None:
        if self._pending.qsize() > 0:
            self._queue_empty_since = None
        elif self._queue_empty_since is None:
            self._queue_empty_since = time.time()

    async def replenish_queue_if_stale(
        self,
        settings: Any,
        *,
        empty_seconds: int = 300,
    ) -> int:
        """Inject synthetic practice briefs when the queue has been empty too long."""
        from skyn3t.cortex.agent_fleet import effective_build_daily_cap

        self._track_queue_depth()
        if self._queue_empty_since is None:
            return 0
        if time.time() - self._queue_empty_since < empty_seconds:
            return 0

        daily_cap = effective_build_daily_cap(settings)
        if daily_cap > 0 and self.state.daily_builds >= daily_cap:
            return 0

        injected = 0
        for fn in (
            self._propose_from_build_patterns,
            self._propose_from_competitive,
            self._propose_from_scout,
        ):
            if self._pending.qsize() >= 3:
                break
            try:
                item = await fn()
                if item is not None and await self._offer_brief(item):
                    injected += 1
                    self._queue_empty_since = None
            except Exception:
                logger.debug("replenish brief %s failed", fn.__name__, exc_info=True)
        if injected:
            self._publish_alert(
                "NEVER_STOP_QUEUE_REPLENISHED",
                injected=injected,
                queue_depth=self._pending.qsize(),
            )
        return injected

    async def _maybe_propose_briefs(self) -> None:
        self._track_queue_depth()
        if self._pending.qsize() >= 3:
            return
        for fn in (
            self._propose_from_build_patterns,
            self._propose_from_scout,
            self._propose_from_competitive,
        ):
            try:
                item = await fn()
                if item is not None:
                    await self._offer_brief(item)
            except Exception:
                logger.debug("brief proposal %s failed", fn.__name__, exc_info=True)

    async def _propose_from_build_patterns(self) -> Optional[AutonomousBrief]:
        from skyn3t.intelligence.build_patterns import get_default_scoreboard

        sb = get_default_scoreboard()
        summary = sb.summary()
        if summary.get("stacks_tracked", 0) == 0:
            return None
        stacks = [
            "react_vite",
            "next",
            "fastapi",
            "express",
            "vite",
            "python_fastapi",
        ]
        for stack in stacks:
            worst = sb.worst_shape(stack, min_samples=2)
            best = sb.best_shape(stack, min_samples=2)
            if worst is None or best is None:
                continue
            if worst.success_rate >= 0.5:
                continue
            shape_hint = ", ".join(best.shape[:4]) if best.shape else "standard scaffold files"
            brief = (
                f"Autonomous pattern drill ({stack}): build a small runnable app using the "
                f"winning scaffold shape ({shape_hint}). Avoid shapes that historically fail "
                f"verification on this stack."
            )
            return AutonomousBrief(
                brief=brief,
                source="build_pattern_gap",
                trigger=f"{stack}:{worst.success_rate:.0%} vs {best.success_rate:.0%}",
                priority=60,
            )
        return None

    async def _propose_from_scout(self) -> Optional[AutonomousBrief]:
        scout = getattr(self.orchestrator, "_repo_scout", None)
        if scout is None:
            return None
        last = getattr(scout, "_last_result", None) or {}
        proposals = list(last.get("proposals") or [])
        if not proposals:
            return None
        top = proposals[0]
        repo = str(top.get("repo") or top.get("full_name") or "").strip()
        topic = str(top.get("title") or top.get("summary") or repo or "scout finding").strip()
        if not topic:
            return None
        brief = (
            f"Autonomous scout drill: build a minimal demo app inspired by «{topic}» "
            f"({repo or 'GitHub scout'}). Runnable React or Python scaffold only — "
            "no SkyN3t repo changes."
        )
        return AutonomousBrief(
            brief=brief,
            source="scout_ingest",
            trigger=repo or topic[:80],
            priority=50,
        )

    async def _propose_from_competitive(self) -> Optional[AutonomousBrief]:
        from skyn3t.cortex.competitive_intel import competitive_practice_brief

        brief = competitive_practice_brief()
        if not brief:
            return None
        return AutonomousBrief(
            brief=brief,
            source="competitive_intel",
            trigger="catalog",
            priority=40,
        )

    def _check_build_gates(self) -> Optional[str]:
        from skyn3t.config.settings import get_settings
        from skyn3t.cortex.agent_fleet import effective_build_daily_cap

        settings = get_settings()
        daily_cap = effective_build_daily_cap(settings)
        daily_budget = float(getattr(settings, "autonomous_build_daily_budget_usd", 5.0))

        if self.state.daily_builds >= daily_cap:
            return f"daily cap reached ({daily_cap})"
        if daily_budget > 0 and self.state.daily_spend_usd >= daily_budget:
            return f"daily token budget reached (${daily_budget:.2f})"

        runner = self._get_runner()
        if runner is None:
            return "studio runner not available"

        studio_active = self._count_active_studio(runner)
        max_studio = int(getattr(runner, "MAX_CONCURRENT_PROJECTS", 3))
        if studio_active >= max_studio:
            return f"studio concurrency full ({studio_active}/{max_studio})"

        from skyn3t.cortex.repo_scout import MultiSourceRepoScout

        busy = MultiSourceRepoScout.busy_reason(
            self.orchestrator,
            studio_active=studio_active,
        )
        if busy:
            return busy
        return None

    async def _execute_build(
        self,
        chosen: AutonomousBrief,
        *,
        slot_id: int = 0,
    ) -> tuple[str, int]:
        from skyn3t.config.settings import get_settings
        from skyn3t.intelligence.cheap_smart import auto_apply_cheaper_routing

        settings = get_settings()
        daily_budget = float(getattr(settings, "autonomous_build_daily_budget_usd", 5.0))
        per_build_cap = float(getattr(settings, "max_build_cost_usd", 1.0))

        runner = self._get_runner()
        if runner is None:
            raise RuntimeError("studio runner not available")

        auto_apply_cheaper_routing()

        slug_base = f"auto-{chosen.source}-s{slot_id}-{int(time.time())}"
        max_cost = (
            min(per_build_cap, daily_budget - self.state.daily_spend_usd)
            if daily_budget > 0
            else per_build_cap
        )
        extra = {
            "autonomous": True,
            "source": "autonomous",
            "trigger_source": chosen.source,
            "execution_profile": "balanced",
            "max_cost_usd": max_cost,
            "fleet_slot": slot_id,
            "worktree": True,
            "quality_floor_score": int(getattr(settings, "autonomous_min_reviewer_score", 85)),
            "fail_on_needs_fixes": True,
        }
        self._publish_alert(
            "AUTONOMOUS_BUILD_STARTED",
            brief=chosen.brief[:200],
            source=chosen.source,
            trigger=chosen.trigger[:120],
            fleet_slot=slot_id,
        )
        manifest = await runner.start(
            chosen.template,
            chosen.brief,
            slug=slug_base[:48],
            extra=extra,
            mission_setup={
                "autonomy": "balanced",
                "quality_floor_score": int(
                    getattr(settings, "autonomous_min_reviewer_score", 85)
                ),
            },
        )
        slug = str(manifest.get("slug") or slug_base)
        actual_tokens = self._project_token_total(runner, slug)
        actual_spend = self._estimate_spend_from_tokens(
            actual_tokens,
            reserved_max_cost=max_cost,
        )
        self.state.daily_builds += 1
        self.state.daily_spend_usd += actual_spend
        self.state.last_build_slug = slug
        self.state.last_skip_reason = None
        _save_state(self.state)
        est_tokens = actual_tokens or (int(actual_spend * 250_000) if actual_spend else 50_000)
        logger.info(
            "autonomous build finished slug=%s source=%s slot=%s spend=%.4f tokens=%d",
            slug,
            chosen.source,
            slot_id,
            actual_spend,
            est_tokens,
        )
        return slug, est_tokens

    @staticmethod
    def _project_token_total(runner: Any, slug: str) -> int:
        reader = getattr(runner, "_project_token_total", None)
        if callable(reader):
            try:
                return max(0, int(reader(slug) or 0))
            except Exception:
                logger.debug("runner project token lookup failed", exc_info=True)
        try:
            from skyn3t.observability.token_tracker import get_default_tracker

            row = get_default_tracker().project_summary(slug)
            return max(0, int(row.get("total_tokens") or 0))
        except Exception:
            logger.debug("token tracker project lookup failed", exc_info=True)
            return 0

    @staticmethod
    def _blended_token_rate() -> float:
        """Blended $/token across the OpenRouter build tiers, from LIVE catalog
        pricing. Returns 0.0 when those tiers are free models so free builds
        register ~$0 (not a fabricated flat cost). Falls back to the legacy
        1/250_000 heuristic only when pricing genuinely can't be resolved.

        This fixes the prior over-estimate (a flat ~$0.80/build regardless of
        model), which inflated daily_spend_usd ~10x vs. the real bill and could
        falsely trip the daily budget cap even on free models.
        """
        legacy = 1.0 / 250_000.0
        try:
            from skyn3t.core.model_router import _TIERS
            from skyn3t.core.openrouter_catalog import load_catalog
        except Exception:
            return legacy
        wanted = [
            entry[1]
            for t in ("or_cheap", "or_ui", "or_backend", "or_strong")
            if (entry := _TIERS.get(t)) and entry[0] == "openrouter" and entry[1]
        ]
        if not wanted:
            return legacy
        try:
            models = load_catalog().models or []
        except Exception:
            return legacy
        pricing_by_id = {
            m.get("id"): m.get("pricing")
            for m in models
            if m.get("id") and isinstance(m.get("pricing"), dict)
        }
        best = 0.0
        found = False
        for mid in wanted:
            pr = pricing_by_id.get(mid)
            if not isinstance(pr, dict):
                continue
            found = True
            try:
                p = float(pr.get("prompt") or 0.0)
                c = float(pr.get("completion") or 0.0)
            except (TypeError, ValueError):
                p = c = 0.0
            best = max(best, (p + c) / 2.0)
        return best if found else legacy

    @staticmethod
    def _estimate_spend_from_tokens(
        tokens: int,
        *,
        reserved_max_cost: float,
    ) -> float:
        if reserved_max_cost <= 0:
            return 0.0
        rate = AutonomousCoordinator._blended_token_rate()
        if rate <= 0:
            return 0.0  # free build tiers → real spend ~$0; don't fabricate cost
        if tokens > 0:
            return min(float(reserved_max_cost), max(0.0, tokens * rate))
        return min(float(reserved_max_cost), 0.01)

    async def _maybe_start_build(self) -> Optional[str]:
        gate = self._check_build_gates()
        if gate:
            return gate
        if self._pending.empty():
            return "queue empty"

        chosen = self.pop_highest_priority_brief()
        if chosen is None:
            return "queue empty"

        try:
            await self._execute_build(chosen)
            return None
        except Exception as exc:
            logger.exception("autonomous build start failed")
            await self._pending.put(chosen)
            return f"start failed: {exc}"

    def _get_runner(self) -> Any:
        getter = getattr(self.orchestrator, "get_studio_runner", None)
        if callable(getter):
            return getter()
        return None

    def _count_active_studio(self, runner: Any) -> int:
        try:
            projects = runner.list_projects()
        except Exception:
            return 0
        active = {"running", "queued"}
        return sum(1 for p in projects if str(p.get("status") or "") in active)

    def _publish_alert(self, kind: str, **fields: Any) -> None:
        try:
            from skyn3t.core.events import Event, EventType

            payload: Dict[str, Any] = {"kind": kind, "autonomous": True}
            payload.update(fields)
            self.event_bus.publish(
                Event(
                    event_type=EventType.SYSTEM_ALERT,
                    source="autonomous_loop",
                    payload=payload,
                )
            )
        except Exception:
            logger.debug("autonomous alert publish failed", exc_info=True)
