"""Agent reflection and self-improvement engine."""

import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from skyn3t.core.agent import TaskResult
from skyn3t.core.events import Event, EventBus, EventType

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
