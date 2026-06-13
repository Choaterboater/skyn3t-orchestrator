"""Lesson attribution scoreboard.

The LearningLoop injects lessons into incoming tasks. Without attribution,
every lesson stays visible forever and the loop never learns which lessons
actually help vs. add noise.

This module tracks (task_id → injected lesson ids) at routing time, and on
task completion credits success/failure to each injected lesson. Lessons
with a net-negative score get demoted (filtered out at injection time);
lessons that consistently help get pinned and floated to the top.

The scoreboard is persisted to disk so it survives restarts, but is
cheap enough to keep entirely in memory between writes (writes batch at
every Nth update).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("skyn3t.intelligence.lesson_attribution")


@dataclass
class LessonStats:
    """Running tally for one lesson (keyed by lesson embedding id)."""

    lesson_id: str
    helpful: int = 0
    hurt: int = 0
    neutral: int = 0
    last_seen_at: float = field(default_factory=time.time)

    @property
    def total(self) -> int:
        return self.helpful + self.hurt + self.neutral

    @property
    def score(self) -> float:
        """Net helpfulness, in [-1, 1]. 0 means no signal (or balanced)."""
        denom = max(1, self.helpful + self.hurt)
        return (self.helpful - self.hurt) / denom

    def to_dict(self) -> Dict[str, object]:
        return {
            "lesson_id": self.lesson_id,
            "helpful": self.helpful,
            "hurt": self.hurt,
            "neutral": self.neutral,
            "last_seen_at": self.last_seen_at,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "LessonStats":
        return cls(
            lesson_id=str(data.get("lesson_id", "")),
            helpful=int(data.get("helpful", 0)),
            hurt=int(data.get("hurt", 0)),
            neutral=int(data.get("neutral", 0)),
            last_seen_at=float(data.get("last_seen_at", time.time())),
        )


class LessonScoreboard:
    """Per-lesson stats + injection-time filter.

    Thread-safe (cortex handlers + the learning loop subscriber both call us
    from event-bus callbacks which can fire on different threads).
    """

    # Default score threshold below which a lesson is suppressed at injection.
    # Lessons start at score=0 (no signal) so we keep -0.34 as the cutoff —
    # anything that's been hurt more than helped at least 2:1 over 3+ events
    # gets dropped. Tunable via constructor.
    DEFAULT_MIN_SCORE = -0.34
    DEFAULT_MIN_SAMPLES = 3

    def __init__(
        self,
        store_path: Optional[Path] = None,
        *,
        min_score: float = DEFAULT_MIN_SCORE,
        min_samples_for_filter: int = DEFAULT_MIN_SAMPLES,
        flush_every: int = 16,
    ):
        self.store_path = Path(store_path) if store_path else Path("data/lesson_scores.json")
        self.min_score = float(min_score)
        self.min_samples_for_filter = int(min_samples_for_filter)
        self._flush_every = int(flush_every)
        self._unflushed = 0
        self._lock = threading.Lock()
        self._stats: Dict[str, LessonStats] = self._load()
        # task_id → list of lesson ids that were injected into that task
        self._inflight: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> Dict[str, LessonStats]:
        try:
            if self.store_path.exists():
                raw = json.loads(self.store_path.read_text())
                if isinstance(raw, dict):
                    return {
                        str(k): LessonStats.from_dict(v)
                        for k, v in raw.items()
                        if isinstance(v, dict)
                    }
        except Exception:
            logger.exception("lesson scoreboard load failed")
        return {}

    def _flush_locked(self) -> None:
        """Persist current state. Caller holds the lock."""
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
            payload = {k: v.to_dict() for k, v in self._stats.items()}
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.store_path)
            self._unflushed = 0
        except Exception:
            logger.exception("lesson scoreboard flush failed")

    def flush(self) -> None:
        """Force-persist now. Useful in tests and shutdown paths."""
        with self._lock:
            self._flush_locked()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_injection(self, task_id: str, lesson_ids: Iterable[str]) -> None:
        """Remember that ``lesson_ids`` were injected into ``task_id``."""
        ids = [lid for lid in lesson_ids if lid]
        if not ids:
            return
        with self._lock:
            self._inflight[task_id] = list(ids)
            for lid in ids:
                stats = self._stats.get(lid)
                if stats is None:
                    stats = LessonStats(lesson_id=lid)
                    self._stats[lid] = stats
                stats.last_seen_at = time.time()

    def record_outcome(
        self,
        task_id: str,
        *,
        success: Optional[bool] = None,
        neutral: bool = False,
    ) -> None:
        """Credit/debit injected lessons based on the task outcome.

        ``neutral=True`` counts the outcome without nudging the
        helpful/hurt ratio — useful for tasks that completed but
        didn't exercise the lesson (skipped, no-op, etc). Without
        this path the ``LessonStats.neutral`` field is write-only.
        """
        if not neutral and success is None:
            raise ValueError("record_outcome: pass success= or neutral=True")
        with self._lock:
            ids = self._inflight.pop(task_id, [])
            if not ids:
                return
            for lid in ids:
                stats = self._stats.get(lid)
                if stats is None:
                    stats = LessonStats(lesson_id=lid)
                    self._stats[lid] = stats
                if neutral:
                    stats.neutral += 1
                elif success:
                    stats.helpful += 1
                else:
                    stats.hurt += 1
                stats.last_seen_at = time.time()
            self._unflushed += len(ids)
            if self._unflushed >= self._flush_every:
                self._flush_locked()

    def record_feedback(self, lesson_id: str, *, helpful: bool) -> None:
        """Direct user feedback on a lesson (post-ship, post-review, etc).

        This is separate from task outcome attribution: it lets a human
        tell the system that a particular lesson helped or hurt even when
        the automated task outcome is unavailable or disagrees with the
        human verdict.
        """
        if not lesson_id:
            return
        with self._lock:
            stats = self._stats.get(lesson_id)
            if stats is None:
                stats = LessonStats(lesson_id=lesson_id)
                self._stats[lesson_id] = stats
            if helpful:
                stats.helpful += 1
            else:
                stats.hurt += 1
            stats.last_seen_at = time.time()
            self._unflushed += 1
            if self._unflushed >= self._flush_every:
                self._flush_locked()

    # ------------------------------------------------------------------
    # Filtering at injection time
    # ------------------------------------------------------------------

    def filter_lessons(
        self,
        candidates: List[Tuple[str, str]],
    ) -> List[Tuple[str, str]]:
        """Drop lessons that have a sustained negative score.

        ``candidates`` is a list of ``(lesson_id, lesson_text)`` pairs (in
        the order RAG returned them). Returns the same list with demoted
        lessons removed. Lessons that haven't accumulated ``min_samples_for_filter``
        signals are kept (no signal = give them a chance).
        """
        with self._lock:
            kept: List[Tuple[str, str]] = []
            for lid, text in candidates:
                stats = self._stats.get(lid)
                if stats is None:
                    kept.append((lid, text))
                    continue
                samples = stats.helpful + stats.hurt
                if samples < self.min_samples_for_filter:
                    kept.append((lid, text))
                    continue
                if stats.score < self.min_score:
                    continue
                kept.append((lid, text))
            return kept

    # ------------------------------------------------------------------
    # Introspection (for the dashboard / debugging)
    # ------------------------------------------------------------------

    def get_stats(self, lesson_id: str) -> Optional[LessonStats]:
        with self._lock:
            stats = self._stats.get(lesson_id)
            if stats is None:
                return None
            # Return a copy so callers can't mutate our internal state.
            return LessonStats.from_dict(stats.to_dict())

    def top_helpful(self, limit: int = 10) -> List[LessonStats]:
        with self._lock:
            return sorted(
                (LessonStats.from_dict(s.to_dict()) for s in self._stats.values()),
                key=lambda s: (s.score, s.helpful),
                reverse=True,
            )[:limit]

    def top_hurtful(self, limit: int = 10) -> List[LessonStats]:
        with self._lock:
            return sorted(
                (LessonStats.from_dict(s.to_dict()) for s in self._stats.values() if s.hurt > 0),
                key=lambda s: s.score,
            )[:limit]

    def summary(self) -> Dict:
        with self._lock:
            total = len(self._stats)
            helpful = sum(1 for s in self._stats.values() if s.score > 0.1)
            hurt = sum(1 for s in self._stats.values() if s.score < self.min_score)
            inflight = len(self._inflight)
        return {
            "total_lessons_tracked": total,
            "net_helpful_lessons": helpful,
            "demoted_lessons": hurt,
            "inflight_tasks": inflight,
        }
