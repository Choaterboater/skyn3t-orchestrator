"""Meta-Agent — the swarm's autonomous cortex.

This agent watches the entire system, identifies improvement opportunities,
generates hypotheses, and executes self-improvement actions. It is the
"brain thinking about itself."

It does NOT replace human direction — it amplifies it by automatically
handling the repetitive optimization work.
"""

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.memory.consciousness import CollectiveConsciousness
from skyn3t.memory.store import MemoryStore

logger = logging.getLogger("skyn3t.memory.meta_agent")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

# Threshold-based proposal-filing config.
# Each kind of proposal has a 4-hour dedup cooldown so the cortex isn't
# spammed with identical findings.
PROPOSAL_DEDUP_SECONDS = 4 * 3600

# Window over which to evaluate the "agent failed >3 times in last hour" rule.
AGENT_FAILURE_WINDOW_SECONDS = 3600
AGENT_FAILURE_THRESHOLD = 3

# Window over which we accumulate per-project retry counts (tracked by slug,
# observed via TASK_COMPLETED/FAILED events whose payloads carry a "slug" or
# whose task titles include one). Threshold is "retried >2 times".
PROJECT_RETRY_THRESHOLD = 2

# Rolling-window of review scores; if the mean of the last N drops below this,
# file a proposal.
REVIEW_SCORE_WINDOW = 5
REVIEW_SCORE_FLOOR = 50.0


class MetaAgent:
    """Autonomous meta-agent for system self-improvement.

    Runs on a configurable loop (default every 60s) and can:
    - Detect underperforming agents and suggest fallback chain updates
    - Detect unhandled capabilities and suggest agent creation
    - Detect queue backlog and suggest concurrency adjustments
    - Detect repeated failure patterns and suggest pattern additions
    - Trigger self-healing for unhealthy agents
    - Spawn improvement tasks for other agents to execute
    """

    def __init__(
        self,
        event_bus: EventBus,
        memory_store: Optional[MemoryStore] = None,
        consciousness: Optional[CollectiveConsciousness] = None,
        interval_seconds: int = 60,
        enabled: bool = True,
    ):
        self.event_bus = event_bus
        self._memory = memory_store
        self._consciousness = consciousness
        self._interval = interval_seconds
        self._enabled = enabled
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Action history
        self._actions: List[Dict[str, Any]] = []
        self._max_actions = 100

        # Observation window
        self._observation_window: List[Dict[str, Any]] = []
        self._max_window = 50

        # ── Threshold-watch state ─────────────────────────────────────
        # Per-agent timestamp deque of recent failures.
        self._agent_failures: Dict[str, Deque[float]] = {}
        # Per-project retry counts (slug → max retry observed).
        self._project_retries: Dict[str, int] = {}
        # Rolling review scores (most recent first cap at REVIEW_SCORE_WINDOW).
        self._review_scores: Deque[float] = deque(maxlen=REVIEW_SCORE_WINDOW)
        # Per-signature dedup map for proposal filing (sig → last-fired ts).
        self._proposal_last_filed: Dict[str, float] = {}

        # Subscribe to the events that drive threshold detection. These are
        # all best-effort: handler is wrapped so a malformed event never
        # raises into the bus.
        try:
            self.event_bus.subscribe(self._on_event_safely)
        except Exception:
            logger.exception("meta_agent: event subscription failed")

    async def start(self) -> None:
        """Start the meta-agent observation loop."""
        if not self._enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._publish("meta_agent_started", {"interval": self._interval})

    async def stop(self) -> None:
        """Stop the meta-agent."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Main observation-think-act loop."""
        while self._running:
            try:
                await self._observe()
                hypotheses = await self._think()
                for hypothesis in hypotheses:
                    await self._act(hypothesis)
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._publish("meta_agent_error", {"error": str(e)})
                await asyncio.sleep(self._interval)

    # ------------------------------------------------------------------
    # Observe
    # ------------------------------------------------------------------

    async def _observe(self) -> None:
        """Collect system observations."""
        observation = {
            "timestamp": _utcnow().isoformat(),
            "agent_count": 0,
            "task_queue_depth": 0,
            "failure_patterns": [],
            "agent_health": {},
        }

        # We don't have direct access to orchestrator state here,
        # so we rely on events in the consciousness working memory
        # and the persistent memory store.
        if self._consciousness:
            status = await self._consciousness.get_status()
            observation["consciousness"] = status

        if self._memory:
            try:
                stats = await self._memory.get_stats()
                observation["memory_stats"] = stats
            except Exception:
                pass

        self._observation_window.append(observation)
        if len(self._observation_window) > self._max_window:
            self._observation_window = self._observation_window[-self._max_window:]

        # Evaluate threshold-based proposal triggers each tick.
        try:
            self._check_thresholds()
        except Exception:
            logger.exception("meta_agent: _check_thresholds failed")

    # ------------------------------------------------------------------
    # Event-driven counters for threshold detection
    # ------------------------------------------------------------------

    def _on_event_safely(self, event: Event) -> None:
        """Defensive wrapper — event subscribers must never raise."""
        try:
            self._on_event(event)
        except Exception:
            logger.exception("meta_agent: _on_event failed")

    def _on_event(self, event: Event) -> None:
        etype = getattr(event, "event_type", None)
        payload = getattr(event, "payload", {}) or {}
        source = getattr(event, "source", "")
        now = time.time()

        # 1) Track agent failures for the failure-rate threshold.
        if etype in (EventType.TASK_FAILED, EventType.TASK_FAILED_FINAL):
            agent_name = payload.get("agent") or source or "unknown"
            dq = self._agent_failures.setdefault(agent_name, deque())
            dq.append(now)
            # Trim old entries outside the window.
            cutoff = now - AGENT_FAILURE_WINDOW_SECONDS
            while dq and dq[0] < cutoff:
                dq.popleft()

        # 2) Track per-project retries. Retry count rides along the event
        # payload; the project slug may be in payload["slug"] (set by Studio
        # runner) or the input_data.
        retry_count = payload.get("retry_count")
        if isinstance(retry_count, int) and retry_count > 0:
            slug = (
                payload.get("slug")
                or payload.get("project_slug")
                or (payload.get("input_data") or {}).get("slug")
            )
            if slug:
                prev = self._project_retries.get(slug, 0)
                if retry_count > prev:
                    self._project_retries[slug] = retry_count

        # 3) Capture reviewer scores from TASK_COMPLETED events whose
        # payload includes the reviewer's score.
        if etype == EventType.TASK_COMPLETED:
            output = payload.get("output") or {}
            if isinstance(output, dict):
                score = output.get("score")
                # Reviewer publishes a numeric 0-100 score.
                if isinstance(score, (int, float)) and 0 <= score <= 100:
                    # Heuristic: only count it as a review score if the source
                    # agent looks like a reviewer (or output carries a verdict).
                    if "verdict" in output or "review" in str(source).lower():
                        self._review_scores.append(float(score))

    # ------------------------------------------------------------------
    # Threshold-based proposal filing
    # ------------------------------------------------------------------

    def _check_thresholds(self) -> None:
        """Evaluate threshold rules and file feature proposals when crossed."""
        # Rule A: agent failed >AGENT_FAILURE_THRESHOLD in last hour.
        now = time.time()
        cutoff = now - AGENT_FAILURE_WINDOW_SECONDS
        # Drop dedup entries older than 24h so _proposal_last_filed doesn't
        # grow forever as new signatures are minted.
        dedup_cutoff = now - 86400
        stale_sigs = [s for s, ts in self._proposal_last_filed.items() if ts < dedup_cutoff]
        for s in stale_sigs:
            self._proposal_last_filed.pop(s, None)
        for agent_name, dq in list(self._agent_failures.items()):
            # Trim old entries first.
            while dq and dq[0] < cutoff:
                dq.popleft()
            # Drop empty deques so _agent_failures doesn't accumulate
            # entries for agents that haven't failed recently.
            if not dq:
                self._agent_failures.pop(agent_name, None)
                continue
            if len(dq) > AGENT_FAILURE_THRESHOLD:
                self._file_threshold_proposal(
                    signature=f"agent_failures:{agent_name}",
                    title=f"Investigate {agent_name} repeated failures",
                    summary=(
                        f"Agent '{agent_name}' has failed {len(dq)} times in the "
                        f"last hour (threshold {AGENT_FAILURE_THRESHOLD})."
                    ),
                    detail=(
                        f"_MetaAgent threshold trip._\n\n"
                        f"- agent: `{agent_name}`\n"
                        f"- recent failures (1h): **{len(dq)}**\n"
                        f"- window: {AGENT_FAILURE_WINDOW_SECONDS}s\n\n"
                        f"Investigate root cause: bad model, broken backend, "
                        f"prompt regression, or a recurring input pattern."
                    ),
                    payload={
                        "kind": "agent_repeated_failures",
                        "agent": agent_name,
                        "count": len(dq),
                    },
                )

        # Rule B: project retried >PROJECT_RETRY_THRESHOLD times.
        for slug, retries in list(self._project_retries.items()):
            if retries > PROJECT_RETRY_THRESHOLD:
                self._file_threshold_proposal(
                    signature=f"project_retries:{slug}",
                    title=f"Pattern: {slug} needs structural rethink",
                    summary=(
                        f"Project '{slug}' has been retried {retries} times "
                        f"(threshold {PROJECT_RETRY_THRESHOLD}). The brief or "
                        f"plan likely has a structural problem."
                    ),
                    detail=(
                        f"_MetaAgent threshold trip._\n\n"
                        f"- project: `{slug}`\n"
                        f"- retries: **{retries}**\n\n"
                        f"Repeated retries usually signal that the brief is "
                        f"ambiguous, the chosen pipeline is mis-fit, or an "
                        f"earlier stage is producing artifacts the next "
                        f"stage can't consume. Worth a manual review."
                    ),
                    payload={
                        "kind": "project_repeated_retries",
                        "slug": slug,
                        "retries": retries,
                    },
                )

        # Rule C: avg review score across last N projects < floor.
        if len(self._review_scores) >= REVIEW_SCORE_WINDOW:
            avg = sum(self._review_scores) / len(self._review_scores)
            if avg < REVIEW_SCORE_FLOOR:
                self._file_threshold_proposal(
                    signature="review_score_low",
                    title="Reviewer scoring trending low — investigate brief quality",
                    summary=(
                        f"Average review score over the last "
                        f"{REVIEW_SCORE_WINDOW} projects is {avg:.1f} "
                        f"(floor {REVIEW_SCORE_FLOOR:.0f})."
                    ),
                    detail=(
                        f"_MetaAgent threshold trip._\n\n"
                        f"- window: last {REVIEW_SCORE_WINDOW} reviews\n"
                        f"- average score: **{avg:.1f}/100**\n"
                        f"- recent scores: {list(self._review_scores)}\n\n"
                        f"Either the briefs are getting harder, the brief "
                        f"quality has dropped, or the reviewer's heuristic "
                        f"weights need recalibration."
                    ),
                    payload={
                        "kind": "review_score_trending_low",
                        "avg": avg,
                        "samples": list(self._review_scores),
                    },
                )

        # Rule D: build-pattern shape bias detected. When a particular
        # scaffold shape has accumulated meaningfully better success
        # data than another shape for the same stack, surface it as a
        # Cortex proposal so the operator sees what the system has
        # learned. Closes the feedback loop user-side.
        self._check_build_pattern_biases()

    def _check_build_pattern_biases(self) -> None:
        """Scan BuildPatternScoreboard for clear shape biases per stack.

        For each stack with at least one shape at ≥75% success (min 5
        samples) AND another shape at ≤40% success (min 3 samples), file
        a single proposal describing the contrast. Dedup signature is
        per-stack so repeated runs don't spam the queue.
        """
        try:
            from skyn3t.intelligence.build_patterns import get_default_scoreboard
            sb = get_default_scoreboard()
        except Exception:
            return
        try:
            # The scoreboard's _stats is intentionally private; tap it
            # under the lock to enumerate stacks.
            with sb._lock:
                stacks = list(sb._stats.keys())
        except Exception:
            return
        for stack in stacks:
            try:
                all_stats = sb.all_stats_for(stack)
            except Exception:
                continue
            if len(all_stats) < 2:
                continue
            winners = [
                s for s in all_stats
                if (s.success + s.failure) >= 5 and s.success_rate >= 0.75
            ]
            losers = [
                s for s in all_stats
                if (s.success + s.failure) >= 3 and s.success_rate <= 0.40
            ]
            if not winners or not losers:
                continue
            # Pick the best winner and the worst loser for the contrast.
            winners.sort(key=lambda s: s.success_rate, reverse=True)
            losers.sort(key=lambda s: s.success_rate)
            best = winners[0]
            worst = losers[0]
            # Find files in winner that aren't in loser — those are the
            # likely-load-bearing additions.
            winner_set = set(best.shape)
            loser_set = set(worst.shape)
            distinguishing = sorted(winner_set - loser_set)
            self._file_threshold_proposal(
                signature=f"build_pattern_bias:{stack}",
                title=f"Build pattern: prefer winning shape for {stack}",
                summary=(
                    f"On {stack} scaffolds, one shape is at "
                    f"{best.success_rate:.0%} success ({best.success}/{best.success + best.failure}); "
                    f"another is at {worst.success_rate:.0%} ({worst.success}/{worst.success + worst.failure}). "
                    f"Adopt the winning shape as the template default."
                ),
                detail=(
                    f"_MetaAgent build-pattern scan._\n\n"
                    f"**Winning shape ({best.success_rate:.0%} success "
                    f"on {best.success + best.failure} graded builds):**\n"
                    + "\n".join(f"- `{p}`" for p in best.shape)
                    + (
                        f"\n\n**Losing shape ({worst.success_rate:.0%} success "
                        f"on {worst.success + worst.failure} graded builds):**\n"
                    )
                    + "\n".join(f"- `{p}`" for p in worst.shape)
                    + (
                        f"\n\n**Distinguishing files in the winner:**\n"
                        if distinguishing
                        else "\n\n_(no extra files in the winner — same paths, different content?)_"
                    )
                    + ("\n".join(f"- `{p}`" for p in distinguishing) if distinguishing else "")
                ),
                payload={
                    "kind": "build_pattern_bias",
                    "stack": stack,
                    "winner_shape": list(best.shape),
                    "winner_success_rate": best.success_rate,
                    "winner_samples": best.success + best.failure,
                    "loser_shape": list(worst.shape),
                    "loser_success_rate": worst.success_rate,
                    "loser_samples": worst.success + worst.failure,
                    "distinguishing_files": distinguishing,
                },
            )

    def _file_threshold_proposal(
        self,
        *,
        signature: str,
        title: str,
        summary: str,
        detail: str,
        payload: Dict[str, Any],
    ) -> None:
        """File a 'feature' proposal with a per-signature dedup cooldown."""
        now = time.time()
        last = self._proposal_last_filed.get(signature, 0.0)
        if now - last < PROPOSAL_DEDUP_SECONDS:
            return
        try:
            from skyn3t.cortex import get_store
            get_store().create(
                kind="feature",
                title=title,
                summary=summary,
                detail=detail,
                payload=payload,
                source="meta_agent:thresholds",
            )
            self._proposal_last_filed[signature] = now
            self._actions.append({
                "type": "threshold_proposal_filed",
                "signature": signature,
                "title": title,
                "timestamp": _utcnow().isoformat(),
                "result": "filed",
            })
            if len(self._actions) > self._max_actions:
                self._actions = self._actions[-self._max_actions:]
        except Exception:
            logger.exception(
                "meta_agent: failed to file threshold proposal %s", signature
            )

    # ------------------------------------------------------------------
    # Think
    # ------------------------------------------------------------------

    async def _think(self) -> List[Dict[str, Any]]:
        """Generate improvement hypotheses from observations."""
        hypotheses: List[Dict[str, Any]] = []

        if not self._observation_window:
            return hypotheses

        latest = self._observation_window[-1]
        memory_stats = latest.get("memory_stats", {})

        # Hypothesis 1: Low success rate → suggest fallback chain review
        success_rate = memory_stats.get("success_rate", 1.0)
        if success_rate < 0.7:
            hypotheses.append({
                "type": "suggest_fallback_review",
                "confidence": 0.8,
                "reason": f"System success rate is {success_rate:.0%}",
                "action": "Review fallback chains for weakest capability",
            })

        # Hypothesis 2: High failure count → suggest pattern detection
        total_failed = memory_stats.get("total_failed", 0)
        if total_failed > 5:
            hypotheses.append({
                "type": "suggest_pattern_analysis",
                "confidence": 0.7,
                "reason": f"{total_failed} tasks have failed",
                "action": "Analyze recent failures for new patterns",
            })

        # Hypothesis 3: No agents registered but tasks submitted
        agent_count = memory_stats.get("agents", 0)
        task_count = memory_stats.get("tasks", 0)
        if agent_count == 0 and task_count > 0:
            hypotheses.append({
                "type": "suggest_agent_registration",
                "confidence": 0.9,
                "reason": "Tasks exist but no agents are registered",
                "action": "Prompt user to register agents",
            })

        # Hypothesis 4: Many tasks, few agents → suggest scaling
        if agent_count > 0 and task_count > 0:
            ratio = task_count / agent_count
            if ratio > 20:
                hypotheses.append({
                    "type": "suggest_scale_up",
                    "confidence": 0.6,
                    "reason": f"High task-to-agent ratio ({ratio:.0f}:1)",
                    "action": "Consider adding more agents or increasing concurrency",
                })

        # Hypothesis 5: Consciousness has many insights → suggest RAG sync
        consciousness = latest.get("consciousness", {})
        if consciousness.get("total_insights", 0) > 10:
            hypotheses.append({
                "type": "suggest_rag_sync",
                "confidence": 0.75,
                "reason": f"{consciousness['total_insights']} insights waiting to be persisted",
                "action": "Sync working memory insights to RAG",
            })

        return hypotheses

    # ------------------------------------------------------------------
    # Act
    # ------------------------------------------------------------------

    async def _act(self, hypothesis: Dict[str, Any]) -> None:
        """Execute an improvement action."""
        action_type = hypothesis["type"]
        action_record = {
            "type": action_type,
            "confidence": hypothesis.get("confidence", 0.5),
            "reason": hypothesis.get("reason", ""),
            "timestamp": _utcnow().isoformat(),
            "result": "pending",
        }

        if action_type == "suggest_fallback_review":
            # Publish a system alert that can be picked up by dashboard/API
            self._publish("suggest_fallback_review", {
                "reason": hypothesis["reason"],
                "recommendation": "Review and update fallback chains for capabilities with low success rates",
            })
            action_record["result"] = "alert_published"

        elif action_type == "suggest_pattern_analysis":
            self._publish("suggest_pattern_analysis", {
                "reason": hypothesis["reason"],
                "recommendation": "Run reflection deep-dive on agents with recent failures",
            })
            action_record["result"] = "alert_published"

        elif action_type == "suggest_agent_registration":
            self._publish("suggest_agent_registration", {
                "reason": hypothesis["reason"],
                "recommendation": "Register LLM CLI agents (claude, kimi, copilot) to handle pending tasks",
            })
            action_record["result"] = "alert_published"

        elif action_type == "suggest_scale_up":
            self._publish("suggest_scale_up", {
                "reason": hypothesis["reason"],
                "recommendation": "Add more agent instances or increase max_concurrent_tasks",
            })
            action_record["result"] = "alert_published"

        elif action_type == "suggest_rag_sync":
            # If consciousness is available, we could trigger an explicit sync
            if self._consciousness:
                insights = await self._consciousness.get_insights(limit=50)
                self._publish("rag_sync_triggered", {
                    "insight_count": len(insights),
                    "reason": hypothesis["reason"],
                })
            action_record["result"] = "sync_triggered"

        self._actions.append(action_record)
        if len(self._actions) > self._max_actions:
            self._actions = self._actions[-self._max_actions:]

        # Persist action to memory
        if self._memory:
            await self._memory.save_log(
                level="INFO",
                source="meta_agent",
                message=f"Meta-agent action: {action_type}",
                meta=action_record,
            )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _publish(self, alert_type: str, payload: Dict[str, Any]) -> None:
        """Publish a system alert event."""
        self.event_bus.publish(
            Event(
                event_type=EventType.SYSTEM_ALERT,
                source="meta_agent",
                payload={"alert_type": alert_type, **payload},
            )
        )

    def get_status(self) -> Dict[str, Any]:
        """Get meta-agent status."""
        return {
            "enabled": self._enabled,
            "running": self._running,
            "interval_seconds": self._interval,
            "observations_collected": len(self._observation_window),
            "actions_taken": len(self._actions),
            "recent_actions": self._actions[-10:],
        }

    def get_observations(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent observations."""
        return self._observation_window[-limit:]

    def pause(self) -> None:
        """Pause the meta-agent."""
        self._enabled = False

    def resume(self) -> None:
        """Resume the meta-agent."""
        self._enabled = True
