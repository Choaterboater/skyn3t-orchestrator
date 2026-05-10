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

    def __init__(self, event_bus, *, ingestor=None, rag=None, memory=None, max_inject=3):
        self.event_bus = event_bus
        self.ingestor = ingestor
        self.rag = rag
        self.memory = memory
        self.max_inject = max_inject
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

    def _on_failed(self, event):
        self._capture(event, outcome="failure")

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
        capability = payload.get("capability")
        title = payload.get("title") or payload.get("description") or capability or ""
        if not title:
            return
        try:
            hits = await self.rag.query(title, top_k=self.max_inject)
        except Exception:
            logger.exception("rag.query during inject failed")
            return
        lessons = [h.get("content") for h in hits if h.get("content")]
        if not lessons:
            return
        if task is not None and hasattr(task, "input_data"):
            task.input_data.setdefault("lessons", []).extend(lessons)

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
