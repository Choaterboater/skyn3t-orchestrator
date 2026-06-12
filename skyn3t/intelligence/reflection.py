"""Agent reflection and self-improvement engine."""

import asyncio
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from skyn3t.core.agent import TaskResult
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.intelligence.error_signatures import (
    signature_for_build_issues,
    signature_for_findings,
    signatures_for_blockers,
)

logger = logging.getLogger("skyn3t.intelligence.reflection")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FailurePattern:
    """A detected failure pattern."""

    pattern_id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    description: str = ""
    regex: str = ""
    affected_agents: List[str] = field(default_factory=list)
    occurrence_count: int = 0
    first_seen: datetime = field(default_factory=_utcnow)
    last_seen: datetime = field(default_factory=_utcnow)
    example_errors: List[str] = field(default_factory=list)
    suggested_fix: Optional[str] = None

    def matches(self, error_message: str) -> bool:
        try:
            return bool(re.search(self.regex, error_message, re.IGNORECASE))
        except re.error:
            return self.regex.lower() in error_message.lower()


@dataclass
class Lesson:
    """A learned lesson from task execution."""

    lesson_id: str = field(default_factory=lambda: str(uuid4()))
    agent_name: Optional[str] = None
    capability: Optional[str] = None
    context: str = ""
    insight: str = ""
    action_taken: str = ""
    outcome: bool = True
    created_at: datetime = field(default_factory=_utcnow)
    relevance_score: float = 1.0


class LessonsLearnedKB:
    """Knowledge base of lessons learned from execution."""

    def __init__(self, max_lessons: int = 1000):
        self._lessons: List[Lesson] = []
        self._max_lessons = max_lessons
        self._by_agent: Dict[str, List[Lesson]] = defaultdict(list)
        self._by_capability: Dict[str, List[Lesson]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def add(self, lesson: Lesson) -> None:
        async with self._lock:
            self._lessons.append(lesson)
            if lesson.agent_name:
                self._by_agent[lesson.agent_name].append(lesson)
            if lesson.capability:
                self._by_capability[lesson.capability].append(lesson)
            if len(self._lessons) > self._max_lessons:
                removed = self._lessons.pop(0)
                if removed.agent_name:
                    self._by_agent[removed.agent_name].remove(removed)
                if removed.capability:
                    self._by_capability[removed.capability].remove(removed)

    async def query(
        self,
        agent_name: Optional[str] = None,
        capability: Optional[str] = None,
        context_query: Optional[str] = None,
        limit: int = 10,
        min_relevance: float = 0.0,
    ) -> List[Lesson]:
        async with self._lock:
            candidates = self._lessons.copy()
            if agent_name:
                candidates = [lesson for lesson in candidates if lesson.agent_name == agent_name]
            if capability:
                candidates = [lesson for lesson in candidates if lesson.capability == capability]
            if context_query:
                candidates = [
                    lesson
                    for lesson in candidates
                    if context_query.lower() in lesson.context.lower()
                    or context_query.lower() in lesson.insight.lower()
                ]
            candidates = [lesson for lesson in candidates if lesson.relevance_score >= min_relevance]
            candidates.sort(key=lambda lesson: lesson.relevance_score, reverse=True)
            return candidates[:limit]

    async def update_relevance(self, lesson_id: str, delta: float) -> None:
        async with self._lock:
            for lesson in self._lessons:
                if lesson.lesson_id == lesson_id:
                    lesson.relevance_score = max(0.0, min(1.0, lesson.relevance_score + delta))
                    break

    def export(self) -> List[Dict[str, Any]]:
        return [
            {
                "lesson_id": lesson.lesson_id,
                "agent": lesson.agent_name,
                "capability": lesson.capability,
                "insight": lesson.insight,
                "action": lesson.action_taken,
                "outcome": lesson.outcome,
                "relevance": lesson.relevance_score,
                "created_at": lesson.created_at.isoformat(),
            }
            for lesson in self._lessons
        ]


class FailurePatternAnalyzer:
    """Analyzes errors to identify recurring failure patterns."""

    DEFAULT_PATTERNS: List[FailurePattern] = [
        FailurePattern(
            name="timeout",
            regex=r"timeout|timed out|deadline exceeded",
            description="Task exceeded time limit",
            suggested_fix="Increase timeout or split into smaller subtasks",
        ),
        FailurePattern(
            name="rate_limit",
            regex=r"rate.limit|too many requests|429|throttled",
            description="API rate limit hit",
            suggested_fix="Add exponential backoff or use a different provider",
        ),
        FailurePattern(
            name="auth_error",
            regex=r"unauthorized|authentication|invalid.*key|401|403",
            description="Authentication or authorization failure",
            suggested_fix="Check API keys and permissions",
        ),
        FailurePattern(
            name="context_length",
            regex=r"context.*length|token.*limit|maximum.*length",
            description="Input exceeded model context window",
            suggested_fix="Truncate input or use a model with larger context",
        ),
        FailurePattern(
            name="syntax_error",
            regex=r"syntax.error|parse.error|invalid.json|unexpected token",
            description="Generated output has syntax errors",
            suggested_fix="Add stricter output format instructions to prompt",
        ),
        FailurePattern(
            name="hallucination",
            regex=r"hallucinat|made.up|does not exist|incorrect.*fact",
            description="Agent produced factually incorrect output",
            suggested_fix="Add retrieval-augmented generation or fact-checking step",
        ),
    ]

    def __init__(self, custom_patterns: Optional[List[FailurePattern]] = None):
        self.patterns = (custom_patterns or []) + self.DEFAULT_PATTERNS
        self._pattern_hits: Dict[str, int] = defaultdict(int)
        self._agent_errors: Dict[str, List[str]] = defaultdict(list)

    def analyze(self, agent_name: str, error: str) -> List[FailurePattern]:
        """Analyze an error and return matching patterns."""
        matched = []
        self._agent_errors[agent_name].append(error)
        for pattern in self.patterns:
            if pattern.matches(error):
                pattern.occurrence_count += 1
                pattern.last_seen = _utcnow()
                pattern.affected_agents = list(set(pattern.affected_agents + [agent_name]))
                if len(pattern.example_errors) < 5:
                    pattern.example_errors.append(error[:500])
                self._pattern_hits[pattern.name] += 1
                matched.append(pattern)
        return matched

    def get_top_patterns(self, n: int = 5) -> List[Tuple[str, int]]:
        """Return most frequent failure patterns."""
        sorted_patterns = sorted(
            self._pattern_hits.items(), key=lambda x: x[1], reverse=True
        )
        return sorted_patterns[:n]

    def get_agent_patterns(self, agent_name: str) -> List[FailurePattern]:
        """Get patterns affecting a specific agent."""
        return [p for p in self.patterns if agent_name in p.affected_agents]

    def add_pattern(self, pattern: FailurePattern) -> None:
        self.patterns.append(pattern)


class PromptSuggestionEngine:
    """Suggests prompt improvements based on failure analysis."""

    SUGGESTION_RULES: List[Tuple[Set[str], str]] = [
        (
            {"timeout"},
            "Break the task into smaller chunks or increase the max execution time.",
        ),
        (
            {"rate_limit"},
            "Add retry logic with jitter, or temporarily switch to a backup provider.",
        ),
        (
            {"auth_error"},
            "Verify API credentials and rotate keys if necessary.",
        ),
        (
            {"context_length"},
            "Summarize the input before sending, or use a model with a larger context window.",
        ),
        (
            {"syntax_error"},
            "Add explicit output format instructions (e.g., 'Respond with valid JSON only').",
        ),
        (
            {"hallucination"},
            "Attach relevant documents as context and instruct the agent to cite sources.",
        ),
    ]

    def suggest(
        self, patterns: List[FailurePattern], current_prompt: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Generate suggestions based on detected failure patterns."""
        suggestions = []
        pattern_names = {p.name for p in patterns}

        for triggers, advice in self.SUGGESTION_RULES:
            if triggers & pattern_names:
                suggestions.append(
                    {
                        "type": "prompt",
                        "issue": ", ".join(triggers & pattern_names),
                        "advice": advice,
                        "current_prompt_preview": (current_prompt or "")[:200],
                    }
                )

        # Add specific prompt patches
        if "syntax_error" in pattern_names and current_prompt:
            suggestions.append(
                {
                    "type": "patch",
                    "patch": "Append: 'Your response must be valid, parseable JSON. Do not include markdown formatting.'",
                }
            )
        if "hallucination" in pattern_names and current_prompt:
            suggestions.append(
                {
                    "type": "patch",
                    "patch": "Append: 'Base your answer strictly on the provided context. Cite specific sources.'",
                }
            )

        return suggestions


class AutoTuner:
    """Automatically adjusts agent parameters based on performance feedback."""

    def __init__(self, config_store: Optional[Dict[str, Dict[str, Any]]] = None):
        self._configs: Dict[str, Dict[str, Any]] = config_store or {}
        self._adjustments: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    def get_config(self, agent_name: str) -> Dict[str, Any]:
        return self._configs.get(agent_name, {}).copy()

    def set_config(self, agent_name: str, config: Dict[str, Any]) -> None:
        self._configs[agent_name] = config.copy()

    def tune(
        self,
        agent_name: str,
        success_rate: float,
        avg_latency_ms: float,
        error_patterns: List[FailurePattern],
    ) -> Dict[str, Any]:
        """Suggest config adjustments for an agent."""
        config = self._configs.get(agent_name, {})
        adjustments = []

        # Latency tuning
        if avg_latency_ms > 10000:
            adjustments.append(
                {
                    "parameter": "timeout",
                    "old": config.get("timeout", 30),
                    "new": config.get("timeout", 30) + 10,
                    "reason": "High average latency detected",
                }
            )
        elif avg_latency_ms < 1000:
            adjustments.append(
                {
                    "parameter": "timeout",
                    "old": config.get("timeout", 30),
                    "new": max(5, config.get("timeout", 30) - 5),
                    "reason": "Low latency allows tighter timeout",
                }
            )

        # Success rate tuning
        if success_rate < 0.7:
            adjustments.append(
                {
                    "parameter": "temperature",
                    "old": config.get("temperature", 0.7),
                    "new": max(0.1, config.get("temperature", 0.7) - 0.1),
                    "reason": "Low success rate; reducing randomness",
                }
            )
            adjustments.append(
                {
                    "parameter": "max_retries",
                    "old": config.get("max_retries", 3),
                    "new": config.get("max_retries", 3) + 1,
                    "reason": "Low success rate; increasing retries",
                }
            )
        elif success_rate > 0.95:
            adjustments.append(
                {
                    "parameter": "temperature",
                    "old": config.get("temperature", 0.7),
                    "new": min(1.0, config.get("temperature", 0.7) + 0.05),
                    "reason": "High success rate; can afford more creativity",
                }
            )

        # Error-pattern-specific tuning
        pattern_names = {p.name for p in error_patterns}
        if "rate_limit" in pattern_names:
            adjustments.append(
                {
                    "parameter": "request_interval",
                    "old": config.get("request_interval", 0),
                    "new": config.get("request_interval", 0) + 0.5,
                    "reason": "Rate limiting detected; throttling requests",
                }
            )
        if "context_length" in pattern_names:
            adjustments.append(
                {
                    "parameter": "max_tokens",
                    "old": config.get("max_tokens", 4096),
                    "new": config.get("max_tokens", 4096) + 1024,
                    "reason": "Context length errors; increasing token budget",
                }
            )

        self._adjustments[agent_name].extend(adjustments)
        return {"agent": agent_name, "adjustments": adjustments}

    def apply_adjustments(self, agent_name: str) -> Dict[str, Any]:
        """Apply pending adjustments to an agent's config."""
        config = self._configs.get(agent_name, {})
        for adj in self._adjustments.get(agent_name, []):
            config[adj["parameter"]] = adj["new"]
        self._adjustments[agent_name] = []
        self._configs[agent_name] = config
        return config.copy()

    def get_history(self, agent_name: str) -> List[Dict[str, Any]]:
        return self._adjustments[agent_name].copy()


class ReflectionEngine:
    """Main reflection engine that analyzes outcomes and drives self-improvement."""

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        lessons_kb: Optional[LessonsLearnedKB] = None,
        pattern_analyzer: Optional[FailurePatternAnalyzer] = None,
        prompt_engine: Optional[PromptSuggestionEngine] = None,
        auto_tuner: Optional[AutoTuner] = None,
    ):
        self.event_bus = event_bus
        self.lessons = lessons_kb or LessonsLearnedKB()
        self.patterns = pattern_analyzer or FailurePatternAnalyzer()
        self.prompts = prompt_engine or PromptSuggestionEngine()
        self.tuner = auto_tuner or AutoTuner()
        self._reflection_queue: asyncio.Queue[TaskResult] = asyncio.Queue()
        self._running = False
        self._reflection_task: Optional[asyncio.Task] = None
        self._callbacks: List[Callable[[Dict[str, Any]], None]] = []

        if event_bus:
            event_bus.subscribe(self._on_task_completed, EventType.TASK_COMPLETED)
            event_bus.subscribe(self._on_task_failed, EventType.TASK_FAILED)

    def register_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self._callbacks.append(callback)

    async def start(self) -> None:
        self._running = True
        self._reflection_task = asyncio.create_task(self._reflection_loop())

    async def stop(self) -> None:
        self._running = False
        if self._reflection_task:
            self._reflection_task.cancel()
            try:
                await self._reflection_task
            except asyncio.CancelledError:
                pass

    async def _reflection_loop(self) -> None:
        while self._running:
            try:
                result = await asyncio.wait_for(self._reflection_queue.get(), timeout=1.0)
                await self._reflect(result)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Reflection loop error")
                await asyncio.sleep(1)

    async def _reflect(self, result: TaskResult) -> None:
        agent_name = result.metadata.get("agent_name", "unknown")
        capability = result.metadata.get("capability", "")
        error = result.error

        reflection: Dict[str, Any] = {
            "task_id": result.task_id,
            "agent": agent_name,
            "success": result.success,
            "timestamp": _utcnow().isoformat(),
        }

        if not result.success and error:
            matched = self.patterns.analyze(agent_name, error)
            suggestions = self.prompts.suggest(matched)
            reflection["patterns"] = [p.name for p in matched]
            reflection["suggestions"] = suggestions

            lesson = Lesson(
                agent_name=agent_name,
                capability=capability,
                context=f"Task {result.task_id} failed",
                insight=f"Detected patterns: {', '.join(p.name for p in matched)}",
                action_taken=str(suggestions),
                outcome=False,
            )
            await self.lessons.add(lesson)
        else:
            lesson = Lesson(
                agent_name=agent_name,
                capability=capability,
                context=f"Task {result.task_id} succeeded",
                insight="Standard execution path",
                action_taken="None",
                outcome=True,
            )
            await self.lessons.add(lesson)

        # Notify callbacks
        for cb in self._callbacks:
            try:
                cb(reflection)
            except Exception:
                logger.exception("Reflection callback error")

        if self.event_bus:
            self.event_bus.publish(
                Event(
                    event_type=EventType.KNOWLEDGE_UPDATED,
                    source="reflection",
                    payload=reflection,
                )
            )

    def _on_task_completed(self, event: Event) -> None:
        payload = event.payload or {}
        if not isinstance(payload, dict):
            logger.warning("Skipping task_completed event with non-dict payload")
            return
        task_id = payload.get("task_id")
        if not task_id:
            logger.warning("Skipping task_completed event missing task_id")
            return
        result = TaskResult(
            task_id=task_id,
            success=True,
            output=payload.get("output", {}),
            execution_time_ms=payload.get("execution_time_ms", 0.0),
            metadata={"agent_name": event.source},
        )
        self._reflection_queue.put_nowait(result)

    def _on_task_failed(self, event: Event) -> None:
        payload = event.payload or {}
        if not isinstance(payload, dict):
            logger.warning("Skipping task_failed event with non-dict payload")
            return
        task_id = payload.get("task_id")
        if not task_id:
            logger.warning("Skipping task_failed event missing task_id")
            return
        # Only learn from TERMINAL failures. Intermediate failures
        # (retries are still available, fallback agent is about to be
        # tried, etc) shouldn't be treated as ground-truth "this agent
        # failed" — they pollute the lessons store with noise that a
        # successful retry would have erased. We treat an event as
        # terminal when retry_count >= max_retries, OR when neither
        # field is present (older publishers we shouldn't assume are
        # retryable). When `terminal: True` is explicitly set, honor it.
        retry_count = payload.get("retry_count")
        max_retries = payload.get("max_retries")
        is_terminal = payload.get("terminal")
        if is_terminal is None and retry_count is not None and max_retries is not None:
            try:
                is_terminal = int(retry_count) >= int(max_retries)
            except (TypeError, ValueError):
                is_terminal = True  # malformed → assume terminal
        if is_terminal is False:
            # Mid-retry — skip. The next attempt may succeed and the
            # reflection store should reflect outcome, not transient state.
            return
        result = TaskResult(
            task_id=task_id,
            success=False,
            error=payload.get("error", "unknown"),
            execution_time_ms=payload.get("execution_time_ms", 0.0),
            metadata={"agent_name": event.source},
        )
        self._reflection_queue.put_nowait(result)

    async def reflect_on_agent(
        self,
        agent_name: str,
        registry_lookup: Callable[[str], Optional[Any]],
    ) -> Dict[str, Any]:
        """Perform deep reflection on a specific agent's performance."""
        record = registry_lookup(agent_name)
        agent_patterns = self.patterns.get_agent_patterns(agent_name)
        lessons = await self.lessons.query(agent_name=agent_name, limit=20)

        report = {
            "agent": agent_name,
            "patterns": [
                {
                    "name": p.name,
                    "count": p.occurrence_count,
                    "last_seen": p.last_seen.isoformat(),
                    "suggested_fix": p.suggested_fix,
                }
                for p in agent_patterns
            ],
            "lessons": [
                {
                    "insight": lesson.insight,
                    "action": lesson.action_taken,
                    "outcome": lesson.outcome,
                    "relevance": lesson.relevance_score,
                }
                for lesson in lessons
            ],
            "tuning_suggestions": [],
        }

        if record:
            tuning = self.tuner.tune(
                agent_name,
                success_rate=getattr(record, "success_rate", 0.5),
                avg_latency_ms=getattr(record, "average_latency_ms", 0.0),
                error_patterns=agent_patterns,
            )
            report["tuning_suggestions"] = tuning.get("adjustments", [])

        return report

    def get_summary(self) -> Dict[str, Any]:
        return {
            "top_failure_patterns": self.patterns.get_top_patterns(),
            "lessons_count": len(self.lessons._lessons),
            "agents_with_adjustments": list(self.tuner._adjustments.keys()),
        }


# ---------------------------------------------------------------------------
# ReflectionPlannerHook — planner-facing reflective-retry directive
# ---------------------------------------------------------------------------
#
# Today the retry "lesson" is hand-rolled inline in studio/runner.py
# (_maybe_auto_retry): it builds a lesson_block / debate_note / escalation_note
# by string-concatenation and re-launches `start("auto", ...)` blindly. That
# logic reasons about the failure ad-hoc and can't feed the planner anything
# structured.
#
# build_retry_directive() centralizes that reasoning. It is PURE and SYNC — no
# I/O, no LLM calls, no event bus — so the planner (studio/planner.py) and the
# runner can both consult it deterministically. It reuses the existing failure
# vocabulary (FailurePatternAnalyzer + PromptSuggestionEngine) and the stable
# error-signature derivation (error_signatures.*) so the retry biases the next
# plan/prompt toward WHY the prior attempt failed instead of a blind re-run.
#
# Flag: SKYN3T_REFLECTIVE_RETRY (default ON). Consumers check it; when there is
# no failure context the directive is a graceful no-op (augmented_brief == brief).


# Canonical model_router tier names (see core/model_router.py _TIERS).
_TIER_CHEAP = "cheap"
_TIER_BALANCED = "balanced"
_TIER_STRONG = "strong"


def reflective_retry_enabled() -> bool:
    """SKYN3T_REFLECTIVE_RETRY gate (default ON).

    Consumers (planner/runner) call this to decide whether to apply a
    directive. build_retry_directive itself is always safe to call; this
    just lets callers degrade to legacy blind-retry behavior if the flag
    is explicitly turned off.
    """
    raw = os.getenv("SKYN3T_REFLECTIVE_RETRY")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


@dataclass
class RetryDirective:
    """Structured guidance for re-planning after a failed build attempt.

    Pure data — produced by build_retry_directive() and consumed by
    studio/planner.plan_pipeline() (to bias agent selection + inject prompt
    patches) and studio/runner (to escalate tiers / avoid backends).

    Fields:
        augmented_brief:   original brief + an actionable "prior attempt
                           failed because X" constraint block. Equals the
                           input brief verbatim when there is no failure
                           context (graceful no-op).
        prompt_patches:    short imperative instructions to append to stage
                           prompts (derived from PromptSuggestionEngine).
        forced_stage_tier: {stage_name: tier} overrides ('cheap'/'balanced'/
                           'strong') — e.g. escalate 'code' to 'strong' on a
                           stub/entrypoint failure. Never forces expensive
                           tiers without a concrete justification.
        avoid_backends:    backends to prefer NOT re-using (the prior backend
                           that produced the failing scaffold), so the auto
                           chain falls through to a fresh perspective.
        rationale:         human-readable one-liner(s) explaining the choices.
        signatures:        stable error signatures (error_signatures.*) so the
                           experience index / routing can correlate retries.
    """

    augmented_brief: str
    prompt_patches: List[str] = field(default_factory=list)
    forced_stage_tier: Dict[str, str] = field(default_factory=dict)
    avoid_backends: List[str] = field(default_factory=list)
    rationale: str = ""
    signatures: List[str] = field(default_factory=list)


def _dedupe_preserve(items: List[str]) -> List[str]:
    """Order-preserving de-dup; drops empties. Pure helper."""
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        if not item:
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _is_stub_failure(blob: str) -> bool:
    """Heuristic mirror of runner._maybe_auto_retry's stub/entrypoint gate."""
    low = blob.lower()
    return any(
        tok in low
        for tok in (
            "stub",
            "entrypoint",
            "code generation failed",
            "skyn3t-backfill",
            "export default null",
            "generation failed",
        )
    )


def build_retry_directive(
    *,
    brief: str,
    error: Optional[str] = None,
    build_hint: Optional[str] = None,
    blockers: Optional[List[Dict[str, Any]]] = None,
    prior_backend: Optional[str] = None,
    failed_stages: Optional[List[str]] = None,
) -> RetryDirective:
    """Reason about WHY a build attempt failed and emit a structured retry
    directive the planner/runner can apply (instead of blind re-run).

    PURE + SYNC: no I/O, no LLM calls, no event bus. Safe to call from the
    planner. Idempotent — same inputs always yield an equal directive.

    Graceful no-op: when there is no usable failure context (no error,
    build_hint, blockers, prior_backend, or failed_stages), returns a
    directive whose augmented_brief == brief, with empty patches/signatures.

    Args mirror the ReflectionPlannerHook contract:
        brief:          the original brief to augment.
        error:          terminal error summary (manifest['error'] / next_action).
        build_hint:     compact build-log tail (manifest['_retry_hint']).
        blockers:       reviewer/contract findings (list of dicts with
                        'category'/'severity'/'file' etc).
        prior_backend:  backend that produced the failing scaffold.
        failed_stages:  stage names that failed in the prior attempt.
    """
    base_brief = brief or ""
    blockers = blockers or []
    failed_stages = [s for s in (failed_stages or []) if s]
    prior_backend = (prior_backend or "").strip()
    error = (error or "").strip()
    build_hint = (build_hint or "").strip()

    has_context = bool(error or build_hint or blockers or prior_backend or failed_stages)
    if not has_context:
        # No failure to reason about — pure pass-through (graceful no-op).
        return RetryDirective(augmented_brief=base_brief)

    # --- 1. Stable error signatures (error_signatures.*) ------------------
    signatures: List[str] = []
    if blockers:
        signatures.extend(signatures_for_blockers(blockers))
        # signature_for_findings prefers severity=='blocker' and returns the
        # single dominant signature — keep it too so the experience index has
        # a canonical "the one that mattered" entry alongside the per-blocker
        # bucket list.
        dominant = signature_for_findings(blockers, source="reviewer")
        if dominant:
            signatures.append(dominant)
    if build_hint:
        # Build-log tail: feed it as a single pseudo-issue dict so we reuse the
        # build-error classifier without I/O.
        build_sig = signature_for_build_issues([{"error_message": build_hint}])
        if build_sig:
            signatures.append(build_sig)
    signatures = _dedupe_preserve(signatures)

    # --- 2. Failure patterns + prompt patches ------------------------------
    # Reuse the existing detectors. A fresh analyzer instance keeps this pure
    # (no shared mutable state across calls / no occurrence_count leakage into
    # the module-level DEFAULT_PATTERNS singletons would be wrong — but
    # FailurePatternAnalyzer reuses DEFAULT_PATTERNS by reference, so we pass
    # COPIES to avoid mutating shared state across retries).
    analyzer = FailurePatternAnalyzer(
        custom_patterns=[
            FailurePattern(
                name=p.name,
                regex=p.regex,
                description=p.description,
                suggested_fix=p.suggested_fix,
            )
            for p in FailurePatternAnalyzer.DEFAULT_PATTERNS
        ]
    )
    # The copies above are PREPENDED, so they win; but analyzer still appends
    # the shared DEFAULT_PATTERNS. To guarantee we never touch the singletons,
    # restrict matching to just our copies.
    analyzer.patterns = analyzer.patterns[: len(FailurePatternAnalyzer.DEFAULT_PATTERNS)]

    error_blob = " ".join(
        part
        for part in (
            error,
            build_hint,
            " ".join(
                str(b.get("message") or b.get("detail") or b.get("category") or "")
                for b in blockers
                if isinstance(b, dict)
            ),
        )
        if part
    ).strip()

    matched_patterns: List[FailurePattern] = []
    if error_blob:
        matched_patterns = analyzer.analyze("retry", error_blob)

    suggestion_engine = PromptSuggestionEngine()
    suggestions = suggestion_engine.suggest(matched_patterns, current_prompt=base_brief)
    prompt_patches: List[str] = []
    for s in suggestions:
        if s.get("type") == "patch" and s.get("patch"):
            prompt_patches.append(str(s["patch"]))
        elif s.get("advice"):
            prompt_patches.append(str(s["advice"]))

    # --- 3. Tier escalation (forced_stage_tier) ----------------------------
    forced_stage_tier: Dict[str, str] = {}
    pattern_names = {p.name for p in matched_patterns}
    stub_failure = _is_stub_failure(f"{error} {build_hint}")
    rationale_bits: List[str] = []

    if stub_failure:
        # Entrypoint/core stub → the code stage needs a stronger model.
        forced_stage_tier["code"] = _TIER_STRONG
        rationale_bits.append("prior attempt shipped an entrypoint/core stub; escalating code→strong")
    if "syntax_error" in pattern_names:
        # Syntax errors mean the generator botched output — escalate code a
        # notch (balanced) rather than all the way to strong (cost-aware).
        forced_stage_tier.setdefault("code", _TIER_BALANCED)
        rationale_bits.append("syntax errors in prior output; escalating code→balanced")
    if "hallucination" in pattern_names:
        forced_stage_tier.setdefault("reviewer", _TIER_BALANCED)
        rationale_bits.append("hallucination signals; escalating reviewer→balanced")
    if "context_length" in pattern_names:
        # Context overflow won't be fixed by a stronger model — leave tiers,
        # the prompt patch (summarize/larger context) handles it.
        rationale_bits.append("context-length overflow; relying on prompt patch not tier bump")
    # rate_limit / auth / timeout are environmental — never escalate tier for
    # those (would just burn budget). The prompt patches already advise on them.

    # --- 4. Backends to avoid ---------------------------------------------
    avoid_backends: List[str] = []
    if prior_backend:
        avoid_backends.append(prior_backend)
        rationale_bits.append(
            f"prior backend '{prior_backend}' produced the failing scaffold; "
            f"prefer a different model for a fresh perspective"
        )
    avoid_backends = _dedupe_preserve(avoid_backends)

    # --- 5. Augmented brief -----------------------------------------------
    constraint_lines: List[str] = ["Prior attempt failed. Use this as a hard constraint for the retry:"]
    if build_hint:
        constraint_lines.append(f"- Build verification failed with:\n{build_hint}")
        constraint_lines.append(
            "  Fix the specific error above. Keep the same stack and file "
            "structure unless the error is fundamentally unfixable in this ecosystem."
        )
    if failed_stages:
        constraint_lines.append(f"- Stages that failed: {', '.join(failed_stages)}.")
    if error and not build_hint:
        constraint_lines.append(f"- Error: {error[:500]}")
    if stub_failure:
        constraint_lines.append(
            "- The entrypoint (App/main/page) or a core file shipped as a "
            "generation-failure stub. The retry MUST generate a complete, "
            "runnable entrypoint that imports and renders the real components "
            "— no placeholders, no 'export default null', no 'Generation failed' JSX."
        )
    if prior_backend:
        constraint_lines.append(
            f"- The prior scaffold was generated by '{prior_backend}'. Prefer a "
            f"DIFFERENT model so a second perspective gets a shot at the problem."
        )
    if blockers:
        labels = _dedupe_preserve(
            [
                str(b.get("category") or b.get("rule") or b.get("kind") or "")
                for b in blockers
                if isinstance(b, dict)
            ]
        )
        if labels:
            constraint_lines.append(f"- Reviewer blockers to resolve: {', '.join(labels)}.")
    for patch in prompt_patches:
        constraint_lines.append(f"- {patch}")

    constraint_block = "\n".join(constraint_lines)
    augmented_brief = (base_brief.rstrip() + "\n\n" + constraint_block) if base_brief else constraint_block

    rationale = "; ".join(rationale_bits) if rationale_bits else "reflective retry: applied prior-failure constraints to brief"

    return RetryDirective(
        augmented_brief=augmented_brief,
        prompt_patches=_dedupe_preserve(prompt_patches),
        forced_stage_tier=forced_stage_tier,
        avoid_backends=avoid_backends,
        rationale=rationale,
        signatures=signatures,
    )
