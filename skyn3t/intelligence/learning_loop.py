from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skyn3t.intelligence.learning_loop")


@dataclass
class Lesson:
    lesson: str
    capability: Optional[str] = None
    agent: Optional[str] = None
    outcome: str = "success"   # "success" | "failure"
    tags: List[str] = field(default_factory=list)
    embedding_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)


class LearningLoop:
    """Continuous learn-and-reuse loop wired to the event bus + RAG + MemoryStore.

    - Listens for TASK_COMPLETED / TASK_FAILED.
    - Builds a Lesson dataclass from the event payload.
    - Persists via ExperienceIngestor.ingest_lesson if provided.
    - Subscribes to TASK_ROUTED and injects matching lessons into task.input_data['lessons'].
    """

    def __init__(self, event_bus, *, ingestor=None, rag=None, memory=None,
                 max_inject=3, scoreboard=None):
        self.event_bus = event_bus
        self.ingestor = ingestor
        self.rag = rag
        self.memory = memory
        self.max_inject = max_inject
        # LessonScoreboard for outcome-attributed lesson filtering. Lazy-create
        # so the loop works without it (back-compat for older callers).
        if scoreboard is None:
            try:
                from skyn3t.intelligence.lesson_attribution import LessonScoreboard
                scoreboard = LessonScoreboard()
            except Exception:
                scoreboard = None
        self.scoreboard = scoreboard
        self._wired = False

    def start(self) -> None:
        if self._wired:
            return
        self._wired = True
        try:
            from skyn3t.core.events import EventType
            self.event_bus.subscribe(self._on_completed, EventType.TASK_COMPLETED)
            self.event_bus.subscribe(self._on_failed, EventType.TASK_FAILED)
            # TASK_ROUTED handler runs late; injection is best-effort
            if hasattr(EventType, "TASK_ROUTED"):
                self.event_bus.subscribe(self._on_routed, EventType.TASK_ROUTED)
        except Exception:
            logger.exception("LearningLoop wiring failed")

    # --- handlers ---
    def _on_completed(self, event):
        self._capture(event, outcome="success")
        self._credit_outcome(event, success=True)

    def _on_failed(self, event):
        self._capture(event, outcome="failure")
        self._credit_outcome(event, success=False)

    def _credit_outcome(self, event, *, success: bool) -> None:
        """Credit or debit the lessons that were injected into this task."""
        if self.scoreboard is None:
            return
        try:
            task_id = (event.payload or {}).get("task_id")
            if not task_id:
                return
            self.scoreboard.record_outcome(task_id, success=success)
        except Exception:
            logger.exception("LearningLoop._credit_outcome failed")

    def _capture(self, event, outcome: str) -> None:
        try:
            payload = event.payload or {}
            lesson_text = self._summarize(payload, outcome)
            lesson = Lesson(
                lesson=lesson_text,
                capability=payload.get("capability"),
                agent=payload.get("agent") or event.source,
                outcome=outcome,
                tags=[outcome] + ([payload["capability"]] if payload.get("capability") else []),
            )
            t = asyncio.create_task(self._persist(lesson))
            t.add_done_callback(self._log_done)
            self._publish_learning(lesson)
        except Exception:
            logger.exception("LearningLoop._capture failed")

    def _on_routed(self, event):
        try:
            t = asyncio.create_task(self._inject(event))
            t.add_done_callback(self._log_done)
        except Exception:
            logger.exception("LearningLoop._on_routed failed")

    # --- workers ---
    async def _persist(self, lesson: Lesson) -> None:
        if self.ingestor is None:
            return
        try:
            res = await self.ingestor.ingest_lesson(
                content=lesson.lesson,
                metadata={
                    "agent": lesson.agent, "capability": lesson.capability,
                    "outcome": lesson.outcome, "tags": lesson.tags,
                },
            )
            if isinstance(res, str):
                lesson.embedding_id = res
        except Exception:
            logger.exception("ingest_lesson failed")

    async def _inject(self, event) -> None:
        if self.rag is None:
            return
        payload = event.payload or {}
        task = payload.get("task")  # orchestrator may attach the TaskRequest reference
        task_id = payload.get("task_id") or (getattr(task, "task_id", None) if task else None)
        capability = payload.get("capability")
        title = payload.get("title") or payload.get("description") or capability or ""
        if not title:
            return
        # RAGEngine.query returns a dict {documents: [...], ...}; each document
        # has id/content/metadata. The old code passed `top_k` (wrong kwarg)
        # which silently no-op'd injection.
        try:
            result = await self.rag.query(title, n_results=self.max_inject)
        except Exception:
            logger.exception("rag.query during inject failed")
            return
        if isinstance(result, dict):
            documents = result.get("documents") or []
        elif isinstance(result, list):
            documents = result
        else:
            documents = []
        candidates = [
            (str(d.get("id") or ""), str(d.get("content") or ""))
            for d in documents
            if d.get("content")
        ]
        # Apply scoreboard-based filtering — lessons with a sustained negative
        # outcome score get dropped before they reach the agent.
        if self.scoreboard is not None:
            candidates = self.scoreboard.filter_lessons(candidates)
        if not candidates:
            return
        # Record which lesson ids went into this task so we can attribute the
        # eventual outcome back to them.
        if self.scoreboard is not None and task_id:
            self.scoreboard.record_injection(task_id, [lid for lid, _ in candidates if lid])
        if task is not None and hasattr(task, "input_data"):
            task.input_data.setdefault("lessons", []).extend(text for _, text in candidates)

    # --- helpers ---
    def _summarize(self, payload: Dict[str, Any], outcome: str) -> str:
        agent = payload.get("agent", "unknown")
        cap = payload.get("capability", "")
        title = payload.get("title") or payload.get("description") or "task"
        if outcome == "success":
            return f"[{agent}] succeeded at {cap}: {title}"
        err = payload.get("error", "unknown error")
        return f"[{agent}] failed {cap}: {title} — {err}"

    def _publish_learning(self, lesson: Lesson) -> None:
        try:
            from skyn3t.core.events import Event, EventType
            et = getattr(EventType, "AGENT_LEARNING", None)
            if et is None:
                return
            self.event_bus.publish(Event(event_type=et, source=lesson.agent or "learning_loop",
                                         payload={"lesson": lesson.lesson, "outcome": lesson.outcome,
                                                  "capability": lesson.capability, "tags": lesson.tags}))
        except Exception:
            logger.exception("publish learning failed")

    @staticmethod
    def _log_done(fut):
        exc = fut.exception()
        if exc:
            logger.error("LearningLoop bg task error: %s", exc, exc_info=exc)
