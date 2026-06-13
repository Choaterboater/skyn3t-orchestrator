"""Never-stop improvement flywheel — composes autonomy loops into one coordinator.

Runs as a background task on orchestrator boot (``SKYN3T_CONTINUOUS_IMPROVEMENT=1``,
default ON). Does not duplicate ``autonomous_loop`` build scheduling — it learns from
outcomes, evolves models, guards quality regressions, and queues competitive drills
via the existing ``AutonomousCoordinator`` queue.

Loops:
  A — Learn from every Studio outcome (scoreboard flush, routing check, proof retries)
  B — Model freshness (6h catalog sync + evolution; per-tick cheaper routing apply)
  C — Competitive scout → micro practice briefs (1/day cap)
  D — Rolling reviewer-score regression guard per stack
  E — Operator visibility (``GET /api/improvement/status``, ``IMPROVEMENT_TICK`` events)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("skyn3t.cortex.continuous_improvement")

METRICS_FILENAME = "improvement_metrics.json"
DEFAULT_TICK_SECONDS = 600
MODEL_EVOLUTION_INTERVAL = 21_600  # 6h — matches model_evolution.EVOLUTION_TTL_SECONDS


def continuous_improvement_enabled() -> bool:
    """True unless ``SKYN3T_CONTINUOUS_IMPROVEMENT=0``."""
    raw = os.environ.get("SKYN3T_CONTINUOUS_IMPROVEMENT", "").strip().lower()
    if raw in ("0", "off", "false", "no"):
        return False
    return True


def _metrics_path(settings: Any | None = None) -> Path:
    if settings is None:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
    return Path(settings.data_dir) / METRICS_FILENAME


@dataclass
class ImprovementMetrics:
    today_date: str = ""
    builds_today: int = 0
    competitive_practice_today: int = 0
    proof_retries_today: int = 0
    last_tick_at: float = 0.0
    last_model_sync_at: float = 0.0
    last_model_evolution_at: float = 0.0
    model_evolutions_total: int = 0
    ticks_total: int = 0
    stack_scores: Dict[str, List[float]] = field(default_factory=dict)
    stack_regression_actions: Dict[str, str] = field(default_factory=dict)
    routing_checks: int = 0
    cheaper_routing_applied: int = 0
    seen_competitive_ingests: List[str] = field(default_factory=list)
    score_trend: Dict[str, float] = field(default_factory=dict)
    # Item 7 — each-build-improves-next feedback metrics.
    # Rolling per-stack first-attempt outcomes (True=passed) for non-retry builds.
    first_attempt_results: Dict[str, List[bool]] = field(default_factory=dict)
    # Rolling per-stack first-attempt success rate (mirrors score_trend shape).
    first_attempt_trend: Dict[str, float] = field(default_factory=dict)
    # Builds where lessons/skills were injected AND the build passed.
    injection_hits: Dict[str, int] = field(default_factory=dict)
    # Builds where lessons/skills were injected (denominator for hit-rate).
    injection_total: Dict[str, int] = field(default_factory=dict)
    # Rolling per-stack injection hit-rate (injection_hits/injection_total).
    injection_hit_rate: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "today_date": self.today_date,
            "builds_today": self.builds_today,
            "competitive_practice_today": self.competitive_practice_today,
            "proof_retries_today": self.proof_retries_today,
            "last_tick_at": self.last_tick_at,
            "last_model_sync_at": self.last_model_sync_at,
            "last_model_evolution_at": self.last_model_evolution_at,
            "model_evolutions_total": self.model_evolutions_total,
            "ticks_total": self.ticks_total,
            "stack_scores": {k: list(v) for k, v in self.stack_scores.items()},
            "stack_regression_actions": dict(self.stack_regression_actions),
            "routing_checks": self.routing_checks,
            "cheaper_routing_applied": self.cheaper_routing_applied,
            "seen_competitive_ingests": list(self.seen_competitive_ingests),
            "score_trend": dict(self.score_trend),
            "first_attempt_results": {
                k: list(v) for k, v in self.first_attempt_results.items()
            },
            "first_attempt_trend": dict(self.first_attempt_trend),
            "injection_hits": dict(self.injection_hits),
            "injection_total": dict(self.injection_total),
            "injection_hit_rate": dict(self.injection_hit_rate),
        }


def _load_metrics(settings: Any | None = None) -> ImprovementMetrics:
    path = _metrics_path(settings)
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                stack_scores = raw.get("stack_scores") or {}
                return ImprovementMetrics(
                    today_date=str(raw.get("today_date") or ""),
                    builds_today=int(raw.get("builds_today") or 0),
                    competitive_practice_today=int(raw.get("competitive_practice_today") or 0),
                    proof_retries_today=int(raw.get("proof_retries_today") or 0),
                    last_tick_at=float(raw.get("last_tick_at") or 0.0),
                    last_model_sync_at=float(raw.get("last_model_sync_at") or 0.0),
                    last_model_evolution_at=float(raw.get("last_model_evolution_at") or 0.0),
                    model_evolutions_total=int(raw.get("model_evolutions_total") or 0),
                    ticks_total=int(raw.get("ticks_total") or 0),
                    stack_scores={
                        str(k): [float(x) for x in v]
                        for k, v in stack_scores.items()
                        if isinstance(v, list)
                    },
                    stack_regression_actions=dict(raw.get("stack_regression_actions") or {}),
                    routing_checks=int(raw.get("routing_checks") or 0),
                    cheaper_routing_applied=int(raw.get("cheaper_routing_applied") or 0),
                    seen_competitive_ingests=list(raw.get("seen_competitive_ingests") or []),
                    score_trend={
                        str(k): float(v)
                        for k, v in (raw.get("score_trend") or {}).items()
                    },
                    first_attempt_results={
                        str(k): [bool(x) for x in v]
                        for k, v in (raw.get("first_attempt_results") or {}).items()
                        if isinstance(v, list)
                    },
                    first_attempt_trend={
                        str(k): float(v)
                        for k, v in (raw.get("first_attempt_trend") or {}).items()
                    },
                    injection_hits={
                        str(k): int(v)
                        for k, v in (raw.get("injection_hits") or {}).items()
                    },
                    injection_total={
                        str(k): int(v)
                        for k, v in (raw.get("injection_total") or {}).items()
                    },
                    injection_hit_rate={
                        str(k): float(v)
                        for k, v in (raw.get("injection_hit_rate") or {}).items()
                    },
                )
    except Exception:
        logger.debug("improvement metrics load failed", exc_info=True)
    return ImprovementMetrics()


def _save_metrics(metrics: ImprovementMetrics, settings: Any | None = None) -> None:
    path = _metrics_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(metrics.to_dict(), indent=2), encoding="utf-8")
    except Exception:
        logger.exception("improvement metrics save failed")


def _reset_daily_counters(metrics: ImprovementMetrics) -> None:
    today = time.strftime("%Y-%m-%d")
    if metrics.today_date != today:
        metrics.today_date = today
        metrics.builds_today = 0
        metrics.competitive_practice_today = 0
        metrics.proof_retries_today = 0


class ContinuousImprovementEngine:
    """Single coordinator for the never-stop improvement flywheel."""

    def __init__(self, orchestrator: Any, event_bus: Any):
        self.orchestrator = orchestrator
        self.event_bus = event_bus
        self.metrics = _load_metrics()
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None
        self._wired = False

    async def start(self) -> None:
        if not continuous_improvement_enabled():
            logger.info("continuous improvement disabled (SKYN3T_CONTINUOUS_IMPROVEMENT=0)")
            return
        if self._running:
            return
        self._running = True
        if not self._wired:
            self._wire_events()
        try:
            from skyn3t.core.model_evolution import set_evolution_event_bus

            set_evolution_event_bus(self.event_bus)
        except Exception:
            logger.debug("model evolution event bus hook failed", exc_info=True)
        self._task = asyncio.create_task(self._loop())
        self._publish_tick("started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        _save_metrics(self.metrics)

    def get_status(self) -> Dict[str, Any]:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
        coord = getattr(self.orchestrator, "_autonomous_coordinator", None)
        auto_status: Dict[str, Any] = {}
        if coord is not None:
            try:
                auto_status = coord.get_status()
            except Exception:
                pass

        evolution: Dict[str, Any] = {}
        try:
            from skyn3t.core.model_evolution import evolution_status

            evolution = evolution_status(settings=settings)
        except Exception:
            pass

        return {
            "enabled": continuous_improvement_enabled(),
            "running": self._running,
            "last_tick_at": self.metrics.last_tick_at,
            "ticks_total": self.metrics.ticks_total,
            "builds_today": self.metrics.builds_today,
            "competitive_practice_today": self.metrics.competitive_practice_today,
            "proof_retries_today": self.metrics.proof_retries_today,
            "daily_competitive_cap": int(
                getattr(settings, "improvement_competitive_practice_daily_cap", 1)
            ),
            "model_evolutions_total": self.metrics.model_evolutions_total,
            "last_model_evolution_at": self.metrics.last_model_evolution_at,
            "last_model_sync_at": self.metrics.last_model_sync_at,
            "cheaper_routing_applied": self.metrics.cheaper_routing_applied,
            "score_trend": dict(self.metrics.score_trend),
            "first_attempt_trend": dict(self.metrics.first_attempt_trend),
            "injection_hit_rate": dict(self.metrics.injection_hit_rate),
            "stack_regression_actions": dict(self.metrics.stack_regression_actions),
            "autonomous_queue_depth": auto_status.get("queue_depth"),
            "autonomous_builds_enabled": auto_status.get("autonomous_builds"),
            "model_evolution": evolution,
            "metrics_path": str(_metrics_path(settings)),
        }

    def _wire_events(self) -> None:
        self._wired = True
        try:
            from skyn3t.core.events import EventType

            self.event_bus.subscribe(self._on_system_alert, EventType.SYSTEM_ALERT)
        except Exception:
            logger.exception("continuous improvement event subscribe failed")

    def _on_system_alert(self, event: Any) -> None:
        payload = getattr(event, "payload", {}) or {}
        kind = str(payload.get("kind") or payload.get("alert_type") or "")
        if kind in {"PROJECT_COMPLETED", "PROJECT_FAILED"}:
            try:
                asyncio.create_task(self._on_studio_outcome(kind, payload))
            except Exception:
                logger.debug("studio outcome handler schedule failed", exc_info=True)
        elif kind == "AUTONOMOUS_PROOF_FAILED":
            try:
                asyncio.create_task(self._on_proof_failed(payload))
            except Exception:
                logger.debug("proof fail handler schedule failed", exc_info=True)
        elif kind == "MODEL_TIER_EVOLVED":
            self._on_model_evolved(payload)

    async def _on_studio_outcome(self, kind: str, payload: Dict[str, Any]) -> None:
        """Loop A + D: learn from every Studio build."""
        from skyn3t.config.settings import get_settings

        settings = get_settings()
        _reset_daily_counters(self.metrics)
        self.metrics.builds_today += 1

        stack = str(payload.get("stack") or "unknown").strip() or "unknown"
        status = str(payload.get("status") or "")
        success = kind == "PROJECT_COMPLETED" and status == "done"

        window = max(3, int(getattr(settings, "improvement_stack_score_window", 8)))

        # Item 7 — first-attempt outcome: a build is a "first attempt" iff its
        # slug carries no '-retry' marker (mirrors runner._maybe_auto_retry which
        # appends '-retry' on auto retries). Append pass/fail to the per-stack
        # rolling window so _tick can compute first-attempt success rate.
        slug = str(payload.get("slug") or "")
        is_first_attempt = slug.count("-retry") == 0
        if is_first_attempt:
            fa_bucket = self.metrics.first_attempt_results.setdefault(stack, [])
            fa_bucket.append(bool(success))
            self.metrics.first_attempt_results[stack] = fa_bucket[-window:]

        # Item 7 — injection hit-rate: count builds that received injected
        # lessons/skills and whether they passed. Source the injected counts from
        # the PROJECT_COMPLETED payload (Owner A publishes injected_skills_count /
        # lessons_count); read defensively so we work before that lands.
        injected_skills_count = self._safe_int(payload.get("injected_skills_count"))
        lessons_count = self._safe_int(payload.get("lessons_count"))
        if (injected_skills_count + lessons_count) > 0:
            self.metrics.injection_total[stack] = (
                self.metrics.injection_total.get(stack, 0) + 1
            )
            if success:
                self.metrics.injection_hits[stack] = (
                    self.metrics.injection_hits.get(stack, 0) + 1
                )

        try:
            from skyn3t.intelligence.build_patterns import get_default_scoreboard

            get_default_scoreboard().flush()
        except Exception:
            logger.debug("scoreboard flush on outcome failed", exc_info=True)

        score = self._extract_reviewer_score(payload)
        if score is not None:
            bucket = self.metrics.stack_scores.setdefault(stack, [])
            bucket.append(float(score))
            self.metrics.stack_scores[stack] = bucket[-window:]
            avg = sum(bucket) / len(bucket)
            self.metrics.score_trend[stack] = round(avg, 2)
            await self._check_score_regression(stack, avg, settings)

        self._check_stack_routing(stack, success=success)
        _save_metrics(self.metrics)

    @staticmethod
    def _safe_int(raw: Any) -> int:
        try:
            if raw is None or isinstance(raw, bool):
                return int(bool(raw))
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    def _extract_reviewer_score(self, payload: Dict[str, Any]) -> Optional[float]:
        for key in ("reviewer_score", "score"):
            raw = payload.get(key)
            if isinstance(raw, (int, float)):
                return float(raw)
        verdict = payload.get("verdict")
        if isinstance(verdict, dict):
            raw = verdict.get("score")
            if isinstance(raw, (int, float)):
                return float(raw)
        return None

    async def _check_score_regression(
        self, stack: str, avg_score: float, settings: Any
    ) -> None:
        """Loop D: rolling reviewer avg below threshold → proposal or tier bump."""
        threshold = float(
            getattr(settings, "improvement_score_regression_threshold", 70.0)
        )
        window = self.metrics.stack_scores.get(stack) or []
        if len(window) < 3:
            return
        if avg_score >= threshold:
            return
        if stack in self.metrics.stack_regression_actions:
            return

        action = "tier_bump"
        try:
            from skyn3t.config.model_routing import get_model_routing_store

            store = get_model_routing_store()
            store.set_many(
                {"code": "or_strong", "code_agent": "or_strong"},
                applied_via="improvement_regression",
            )
        except Exception:
            logger.debug("regression tier bump failed", exc_info=True)
            action = "proposal_only"

        proposal_id: Optional[str] = None
        try:
            from skyn3t.cortex import get_store

            proposal = get_store().create(
                kind="feature",
                title=f"Quality regression on {stack}",
                summary=(
                    f"Rolling reviewer avg {avg_score:.0f} < {threshold:.0f} "
                    f"over {len(window)} builds"
                )[:200],
                detail=(
                    f"_Flywheel regression guard detected declining quality on `{stack}`._\n\n"
                    f"- Rolling reviewer average: **{avg_score:.1f}**\n"
                    f"- Threshold: **{threshold:.0f}**\n"
                    f"- Samples: {len(window)}\n\n"
                    "Auto-action: bumped code routing to `or_strong` for next builds.\n"
                    "Approve to investigate root cause and ship a targeted fix."
                ),
                payload={
                    "action": "quality_regression",
                    "stack": stack,
                    "avg_score": avg_score,
                    "threshold": threshold,
                    "samples": len(window),
                },
                source="continuous_improvement:regression",
                force_requires_approval=True,
            )
            proposal_id = proposal.id
        except Exception:
            logger.debug("regression proposal filing failed", exc_info=True)

        self.metrics.stack_regression_actions[stack] = action
        try:
            from skyn3t.cortex.cursor_improvement import enqueue_regression_task

            enqueue_regression_task(
                stack=stack,
                avg_score=avg_score,
                threshold=threshold,
                samples=len(window),
                settings=settings,
            )
        except Exception:
            logger.debug("cursor regression task enqueue failed", exc_info=True)
        self._publish_tick(
            "score_regression",
            stack=stack,
            avg_score=avg_score,
            threshold=threshold,
            action=action,
            proposal_id=proposal_id,
        )

    def _check_stack_routing(self, stack: str, *, success: bool) -> None:
        """Loop A: note routing health for stack backends (adaptive router learns live)."""
        if not stack or stack == "unknown":
            return
        self.metrics.routing_checks += 1
        try:
            from skyn3t.core.model_router import _adaptive_enabled
            from skyn3t.intelligence.build_patterns import get_default_scoreboard

            if not _adaptive_enabled():
                return
            sb = get_default_scoreboard()
            for backend in ("openrouter", "copilot_cli", "claude_cli", "kimi_cli"):
                rate = sb.backend_rate(stack, backend, min_samples=3)
                if rate is None:
                    continue
                logger.debug(
                    "improvement routing check stack=%s backend=%s rate=%.2f success=%s",
                    stack,
                    backend,
                    rate,
                    success,
                )
        except Exception:
            logger.debug("stack routing check failed", exc_info=True)

    async def _on_proof_failed(self, payload: Dict[str, Any]) -> None:
        """Loop A: bounded proof-run retry brief (delegates to autonomous queue)."""
        from skyn3t.config.settings import get_settings

        settings = get_settings()
        if not getattr(settings, "autonomous_builds", False):
            return
        _reset_daily_counters(self.metrics)
        cap = max(0, int(getattr(settings, "improvement_proof_retry_daily_cap", 2)))
        if self.metrics.proof_retries_today >= cap:
            return

        coord = getattr(self.orchestrator, "_autonomous_coordinator", None)
        if coord is None:
            return

        slug = str(payload.get("slug") or "").strip()
        summary = str(payload.get("summary") or "proof run failed").strip()
        stack = str(payload.get("stack") or "web").strip()
        brief = (
            f"Improvement retry: minimal {stack} app fixing proof failure "
            f"«{summary[:120]}» (after {slug or 'auto build'}). "
            "Runnable scaffold with one core workflow."
        )
        try:
            from skyn3t.cortex.autonomous_loop import AutonomousBrief

            queued = await coord.enqueue_brief(
                AutonomousBrief(
                    brief=brief,
                    source="improvement_proof_retry",
                    trigger=summary[:200],
                    priority=75,
                )
            )
            if queued:
                self.metrics.proof_retries_today += 1
                _save_metrics(self.metrics)
        except Exception:
            logger.debug("proof retry enqueue failed", exc_info=True)

    def _on_model_evolved(self, payload: Dict[str, Any]) -> None:
        count = int(payload.get("count") or len(payload.get("upgrades") or []))
        if count <= 0:
            return
        self.metrics.model_evolutions_total += count
        self.metrics.last_model_evolution_at = time.time()
        _save_metrics(self.metrics)

    async def _loop(self) -> None:
        from skyn3t.config.settings import get_settings
        from skyn3t.cortex.never_stop import effective_loop_interval

        settings = get_settings()
        tick_seconds = effective_loop_interval(
            settings,
            "improvement_tick_seconds",
            DEFAULT_TICK_SECONDS,
        )
        while self._running:
            try:
                _reset_daily_counters(self.metrics)
                await self._tick(settings)
                self.metrics.last_tick_at = time.time()
                self.metrics.ticks_total += 1
                _save_metrics(self.metrics)
                await asyncio.sleep(tick_seconds)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("continuous improvement tick failed")
                await asyncio.sleep(tick_seconds)

    async def _tick(self, settings: Any) -> None:
        """One flywheel revolution — loops B, C, and periodic model sync."""
        cheaper_applied = 0
        evolution_upgrades = 0

        try:
            from skyn3t.intelligence.cheap_smart import auto_apply_cheaper_routing

            rows = auto_apply_cheaper_routing(min_confidence="high")
            cheaper_applied = len(rows)
            self.metrics.cheaper_routing_applied += cheaper_applied
        except Exception:
            logger.debug("cheaper routing auto-apply failed", exc_info=True)

        now = time.time()
        if now - self.metrics.last_model_sync_at >= MODEL_EVOLUTION_INTERVAL:
            try:
                from skyn3t.core.model_evolution import run_evolution
                from skyn3t.core.openrouter_catalog import sync_catalog

                sync_result = await sync_catalog(force=True, settings=settings)
                self.metrics.last_model_sync_at = now
                evo = run_evolution(event_bus=self.event_bus, settings=settings)
                upgrades = list(evo.get("upgrades") or [])
                if upgrades:
                    evolution_upgrades = len(upgrades)
                    self.metrics.model_evolutions_total += evolution_upgrades
                    self.metrics.last_model_evolution_at = now
                logger.info(
                    "improvement model freshness: sync=%s evolutions=%d",
                    sync_result.get("status"),
                    evolution_upgrades,
                )
            except Exception:
                logger.debug("model freshness tick failed", exc_info=True)

        competitive_queued = await self._maybe_queue_competitive_practice(settings)

        # Item 3 — curate the skill library on a cadence (Owner E owns the math
        # and persists the last-curate timestamp inside curate_if_due). Called
        # defensively so a not-yet-landed curate_if_due never breaks the tick.
        self._maybe_curate_skills(settings)

        # Refresh the Learnings Store (the distilled corpus the local micro-LLM
        # and the build-context boost read). Time-gated; never breaks the tick.
        self._maybe_compile_learnings(settings)

        # Item 7 — recompute rolling each-build-improves-next feedback metrics so
        # the dashboard/operators can see whether builds are getting better.
        first_attempt_trend = self._compute_first_attempt_trend()
        injection_hit_rate = self._compute_injection_hit_rate()

        self._publish_tick(
            "tick",
            cheaper_routing_applied=cheaper_applied,
            model_evolutions=evolution_upgrades,
            competitive_practice_queued=competitive_queued,
            builds_today=self.metrics.builds_today,
            score_trend=dict(self.metrics.score_trend),
            first_attempt_trend=first_attempt_trend,
            injection_hit_rate=injection_hit_rate,
        )

    def _compute_first_attempt_trend(self) -> Dict[str, float]:
        """Rolling first-attempt success rate per stack (fraction of passes)."""
        trend: Dict[str, float] = {}
        for stack, results in self.metrics.first_attempt_results.items():
            if not results:
                continue
            trend[stack] = round(sum(1 for r in results if r) / len(results), 3)
        self.metrics.first_attempt_trend = trend
        return dict(trend)

    def _compute_injection_hit_rate(self) -> Dict[str, float]:
        """Rolling injection hit-rate per stack (passed builds / injected builds)."""
        rate: Dict[str, float] = {}
        for stack, total in self.metrics.injection_total.items():
            if total <= 0:
                continue
            hits = self.metrics.injection_hits.get(stack, 0)
            rate[stack] = round(hits / total, 3)
        self.metrics.injection_hit_rate = rate
        return dict(rate)

    def _maybe_compile_learnings(self, settings: Any) -> None:
        """Refresh the Learnings Store corpus on a cadence (default 1h)."""
        try:
            import time as _time

            interval = float(
                getattr(settings, "improvement_learnings_compile_interval_seconds", 3600)
            )
            now = _time.time()
            if now - getattr(self, "_last_learnings_compile", 0.0) < interval:
                return
            from skyn3t.intelligence.learnings_store import get_default_store

            count = get_default_store().compile()
            self._last_learnings_compile = now
            logger.debug("learnings store compiled %d entries", count)
        except Exception:
            logger.debug("learnings compile failed", exc_info=True)

    def _maybe_curate_skills(self, settings: Any) -> None:
        """Item 3 — invoke Owner E's curate_if_due on a cadence (default 24h)."""
        try:
            from skyn3t.intelligence.skill_library import get_default_library

            interval = float(
                getattr(settings, "improvement_skill_curate_interval_seconds", 86_400)
            )
            lib = get_default_library()
            curate_if_due = getattr(lib, "curate_if_due", None)
            if callable(curate_if_due):
                curate_if_due(interval)
        except Exception:
            logger.debug("skill library curate_if_due failed", exc_info=True)

    async def _maybe_queue_competitive_practice(self, settings: Any) -> bool:
        """Loop C: queue one micro practice brief per day from scout competitor ingest."""
        if not getattr(settings, "autonomous_builds", False):
            return False
        cap = max(0, int(getattr(settings, "improvement_competitive_practice_daily_cap", 1)))
        if self.metrics.competitive_practice_today >= cap:
            return False

        coord = getattr(self.orchestrator, "_autonomous_coordinator", None)
        if coord is None:
            return False

        seen: Set[str] = set(self.metrics.seen_competitive_ingests)
        candidate = self._find_new_competitive_ingest(seen)
        if candidate is None:
            return False

        from skyn3t.cortex.autonomous_loop import AutonomousBrief
        from skyn3t.cortex.competitive_intel import (
            build_competitive_adaptation_brief,
            competitive_practice_brief,
            match_competitor,
        )

        repo = str(candidate.get("repo") or "")
        match = match_competitor(repo)
        if match is None:
            return False

        brief_text = competitive_practice_brief()
        if not brief_text:
            pattern = (match.get("patterns") or ["workflow automation"])[0]
            name = str(match.get("name") or repo)
            brief_text = (
                f"Autonomous competitive drill: build a minimal runnable app demonstrating "
                f"«{pattern}» inspired by {name} ({repo}). Ship scaffold/ only."
            )

        queued = await coord.enqueue_brief(
            AutonomousBrief(
                brief=brief_text,
                source="competitive_intel",
                trigger=repo,
                priority=45,
            )
        )
        if not queued:
            return False

        ingest_id = str(candidate.get("proposal_id") or repo)
        seen.add(ingest_id)
        self.metrics.seen_competitive_ingests = list(seen)[-80:]
        self.metrics.competitive_practice_today += 1

        adaptation = build_competitive_adaptation_brief(
            repo,
            description=str(candidate.get("description") or ""),
            ingested_paths=list(candidate.get("paths") or []),
        )
        if adaptation:
            logger.info(
                "competitive practice queued for %s (adaptation brief available)",
                repo,
            )
        try:
            from skyn3t.cortex.cursor_improvement import maybe_enqueue_from_competitive_adaptation

            maybe_enqueue_from_competitive_adaptation(
                repo,
                description=str(candidate.get("description") or ""),
                ingested_paths=list(candidate.get("paths") or []),
                settings=settings,
            )
        except Exception:
            logger.debug("cursor competitive task enqueue failed", exc_info=True)
        return True

    def _find_new_competitive_ingest(self, seen: Set[str]) -> Optional[Dict[str, Any]]:
        try:
            from skyn3t.cortex import get_store
            from skyn3t.cortex.competitive_intel import match_competitor
        except Exception:
            return None

        cutoff = time.time() - 86_400
        for proposal in get_store().list(status="applied"):
            if proposal.kind != "ingest":
                continue
            applied_at = float(proposal.applied_at or proposal.decided_at or 0.0)
            if applied_at < cutoff:
                continue
            if proposal.id in seen:
                continue
            payload = proposal.payload or {}
            repo = str(payload.get("repo") or "").strip()
            if not repo or match_competitor(repo) is None:
                continue
            return {
                "proposal_id": proposal.id,
                "repo": repo,
                "description": payload.get("description") or "",
                "paths": payload.get("ingested_paths") or [],
            }
        return None

    def _publish_tick(self, phase: str, **fields: Any) -> None:
        try:
            from skyn3t.core.events import Event, EventType

            payload: Dict[str, Any] = {
                "kind": "IMPROVEMENT_TICK",
                "phase": phase,
                "continuous_improvement": True,
            }
            payload.update(fields)
            self.event_bus.publish(
                Event(
                    event_type=EventType.SYSTEM_ALERT,
                    source="continuous_improvement",
                    payload=payload,
                )
            )
        except Exception:
            logger.debug("IMPROVEMENT_TICK publish failed", exc_info=True)


# ---------------------------------------------------------------------------
# Fleet slot learning ticks (parallel workers when build queue is empty)
# ---------------------------------------------------------------------------

FLEET_LEARNING_KINDS: tuple[str, ...] = (
    "scout_ingest",
    "rag_refresh",
    "routing_apply",
    "model_evolution",
)


def learning_kind_for_slot(slot_id: int) -> str:
    """Rotate learning tasks across fleet slots."""
    return FLEET_LEARNING_KINDS[slot_id % len(FLEET_LEARNING_KINDS)]


async def run_fleet_learning_tick(
    orchestrator: Any,
    event_bus: Any,
    *,
    kind: str,
    slot_id: int = 0,
) -> Dict[str, Any]:
    """Run one bounded learning task for an agent fleet slot."""
    kind = str(kind or learning_kind_for_slot(slot_id)).strip().lower()
    try:
        if kind == "scout_ingest":
            return await _fleet_tick_scout_ingest(orchestrator)
        if kind == "rag_refresh":
            return await _fleet_tick_rag_refresh(orchestrator)
        if kind == "routing_apply":
            return _fleet_tick_routing_apply()
        if kind == "model_evolution":
            return _fleet_tick_model_evolution(event_bus)
        return {"kind": kind, "ok": False, "reason": "unknown_kind"}
    except Exception as exc:
        logger.debug("fleet learning tick %s failed slot=%s", kind, slot_id, exc_info=True)
        return {"kind": kind, "ok": False, "error": str(exc)}


def _scout_boot_deferred(orchestrator: Any) -> Optional[str]:
    """Skip scout during orchestrator boot so uvicorn finishes startup promptly."""
    from skyn3t.config.settings import get_settings

    settings = get_settings()
    defer = max(0, int(getattr(settings, "cortex_scout_defer_boot_seconds", 120) or 0))
    if defer <= 0:
        return None
    booted_at = float(getattr(orchestrator, "_booted_at", 0.0) or 0.0)
    if booted_at <= 0:
        return None
    elapsed = time.time() - booted_at
    if elapsed < defer:
        return f"boot defer ({int(defer - elapsed)}s remaining)"
    return None


async def _fleet_tick_scout_ingest(orchestrator: Any) -> Dict[str, Any]:
    defer = _scout_boot_deferred(orchestrator)
    if defer:
        return {"kind": "scout_ingest", "ok": True, "deferred": defer}

    scout = getattr(orchestrator, "_repo_scout", None)
    if scout is None:
        return {"kind": "scout_ingest", "ok": False, "reason": "no_repo_scout"}

    start_bg = getattr(scout, "start_background", None)
    if callable(start_bg):
        if getattr(scout, "is_running", False):
            return {"kind": "scout_ingest", "ok": True, "state": "already_running"}
        started = start_bg({})
        return {
            "kind": "scout_ingest",
            "ok": bool(started.get("started") or started.get("ok")),
            "result": _fleet_summarize(started),
            "background": True,
        }

    run = getattr(scout, "run_once", None) or getattr(scout, "ingest_once", None)
    if run is None:
        return {"kind": "scout_ingest", "ok": False, "reason": "scout_no_run_method"}
    result = run()
    if hasattr(result, "__await__"):
        result = await result
    return {"kind": "scout_ingest", "ok": True, "result": _fleet_summarize(result)}


async def _fleet_tick_rag_refresh(orchestrator: Any) -> Dict[str, Any]:
    ingestor = getattr(orchestrator, "_ingestor", None)
    if ingestor is None:
        return {"kind": "rag_refresh", "ok": False, "reason": "no_ingestor"}
    rag = getattr(ingestor, "rag", None)
    if rag is None:
        return {"kind": "rag_refresh", "ok": False, "reason": "no_rag"}
    refresh = getattr(rag, "refresh", None) or getattr(rag, "reindex", None)
    if refresh is not None:
        res = refresh()
        if hasattr(res, "__await__"):
            await res
        return {"kind": "rag_refresh", "ok": True, "action": "refresh"}
    sync = getattr(ingestor, "sync_pending", None)
    if sync is not None:
        res = sync()
        if hasattr(res, "__await__"):
            await res
        return {"kind": "rag_refresh", "ok": True, "action": "sync_pending"}
    return {"kind": "rag_refresh", "ok": True, "action": "noop"}


def _fleet_tick_routing_apply() -> Dict[str, Any]:
    from skyn3t.intelligence.cheap_smart import auto_apply_cheaper_routing

    applied = auto_apply_cheaper_routing()
    return {
        "kind": "routing_apply",
        "ok": True,
        "applied_count": len(applied),
        "applied": applied[:8],
    }


def _fleet_tick_model_evolution(event_bus: Any) -> Dict[str, Any]:
    from skyn3t.core.model_evolution import run_evolution

    summary = run_evolution(event_bus=event_bus)
    upgrades = list(summary.get("upgrades") or [])
    return {
        "kind": "model_evolution",
        "ok": True,
        "upgrades": len(upgrades),
        "enabled": bool(summary.get("enabled")),
    }


def _fleet_summarize(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return {k: result[k] for k in list(result.keys())[:12]}
    return {"value": str(result)[:200]}
