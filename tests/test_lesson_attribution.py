"""Tests for skyn3t.intelligence.lesson_attribution.LessonScoreboard."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from skyn3t.intelligence.lesson_attribution import LessonScoreboard, LessonStats

# ---------------------------------------------------------------------------
# LessonStats math
# ---------------------------------------------------------------------------


def test_score_is_zero_with_no_signal():
    s = LessonStats(lesson_id="x")
    assert s.score == 0.0
    assert s.total == 0


def test_score_one_when_all_helpful():
    s = LessonStats(lesson_id="x", helpful=5)
    assert s.score == 1.0


def test_score_minus_one_when_all_hurt():
    s = LessonStats(lesson_id="x", hurt=5)
    assert s.score == -1.0


def test_score_ignores_neutral():
    s = LessonStats(lesson_id="x", helpful=2, hurt=1, neutral=99)
    # Neutral doesn't move the score in either direction.
    assert s.score == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_record_and_outcome_persist_across_restarts(tmp_path):
    path = tmp_path / "scores.json"
    sb = LessonScoreboard(store_path=path, flush_every=1)
    sb.record_injection("task-1", ["lesson-A", "lesson-B"])
    sb.record_outcome("task-1", success=True)
    del sb
    sb2 = LessonScoreboard(store_path=path)
    a = sb2.get_stats("lesson-A")
    b = sb2.get_stats("lesson-B")
    assert a is not None and a.helpful == 1 and a.hurt == 0
    assert b is not None and b.helpful == 1 and b.hurt == 0


def test_record_outcome_credits_all_injected_lessons(tmp_path):
    sb = LessonScoreboard(store_path=tmp_path / "scores.json", flush_every=999)
    sb.record_injection("task-1", ["a", "b"])
    sb.record_outcome("task-1", success=False)
    assert sb.get_stats("a").hurt == 1
    assert sb.get_stats("b").hurt == 1
    assert sb.get_stats("a").helpful == 0


def test_record_outcome_unknown_task_is_safe(tmp_path):
    sb = LessonScoreboard(store_path=tmp_path / "scores.json")
    # No record_injection beforehand — outcome should be a no-op, not raise.
    sb.record_outcome("never-seen", success=True)


def test_atomic_flush_uses_tmp_then_replace(tmp_path):
    path = tmp_path / "scores.json"
    sb = LessonScoreboard(store_path=path, flush_every=1)
    sb.record_injection("t1", ["x"])
    sb.record_outcome("t1", success=True)
    assert path.exists()
    data = json.loads(path.read_text())
    assert "x" in data
    assert data["x"]["helpful"] == 1


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_filter_keeps_unscored_lessons(tmp_path):
    sb = LessonScoreboard(store_path=tmp_path / "s.json")
    kept = sb.filter_lessons([("a", "text-a"), ("b", "text-b")])
    assert len(kept) == 2


def test_filter_keeps_under_min_samples(tmp_path):
    sb = LessonScoreboard(
        store_path=tmp_path / "s.json", min_samples_for_filter=3
    )
    # Hurt once — only 1 sample, below min_samples_for_filter=3 → keep.
    sb.record_injection("t1", ["a"])
    sb.record_outcome("t1", success=False)
    kept = sb.filter_lessons([("a", "text")])
    assert len(kept) == 1


def test_filter_drops_lessons_with_sustained_negative_score(tmp_path):
    sb = LessonScoreboard(
        store_path=tmp_path / "s.json",
        min_score=-0.34,
        min_samples_for_filter=3,
    )
    for i in range(3):
        sb.record_injection(f"t{i}", ["a"])
        sb.record_outcome(f"t{i}", success=False)
    kept = sb.filter_lessons([("a", "text-a"), ("b", "text-b")])
    ids = [lid for lid, _ in kept]
    assert "a" not in ids
    assert "b" in ids  # unscored lesson is kept


def test_filter_keeps_balanced_lessons(tmp_path):
    sb = LessonScoreboard(
        store_path=tmp_path / "s.json",
        min_score=-0.34,
        min_samples_for_filter=3,
    )
    for i in range(2):
        sb.record_injection(f"t{i}", ["a"])
        sb.record_outcome(f"t{i}", success=True)
    for i in range(2, 4):
        sb.record_injection(f"t{i}", ["a"])
        sb.record_outcome(f"t{i}", success=False)
    kept = sb.filter_lessons([("a", "text")])
    assert len(kept) == 1


# ---------------------------------------------------------------------------
# Top-N + summary
# ---------------------------------------------------------------------------


def test_top_helpful_ordered_by_score(tmp_path):
    sb = LessonScoreboard(store_path=tmp_path / "s.json", flush_every=999)
    sb.record_injection("t1", ["a", "b", "c"])
    sb.record_outcome("t1", success=True)
    sb.record_injection("t2", ["a"])
    sb.record_outcome("t2", success=True)
    sb.record_injection("t3", ["b"])
    sb.record_outcome("t3", success=False)
    sb.record_injection("t4", ["c"])
    sb.record_outcome("t4", success=False)
    sb.record_injection("t5", ["c"])
    sb.record_outcome("t5", success=False)
    top = sb.top_helpful(limit=3)
    ids = [s.lesson_id for s in top]
    assert ids[0] == "a"  # highest score


def test_summary_counts_demoted_and_helpful(tmp_path):
    sb = LessonScoreboard(
        store_path=tmp_path / "s.json",
        min_score=-0.34,
        min_samples_for_filter=3,
    )
    for i in range(5):
        sb.record_injection(f"win-{i}", ["winner"])
        sb.record_outcome(f"win-{i}", success=True)
    for i in range(5):
        sb.record_injection(f"lose-{i}", ["loser"])
        sb.record_outcome(f"lose-{i}", success=False)
    s = sb.summary()
    assert s["total_lessons_tracked"] == 2
    assert s["net_helpful_lessons"] >= 1
    assert s["demoted_lessons"] >= 1


# ---------------------------------------------------------------------------
# Integration: LearningLoop._inject uses the scoreboard end-to-end
# ---------------------------------------------------------------------------


class _StubRAG:
    def __init__(self, documents: List[Dict[str, Any]]):
        self._documents = documents

    async def query(self, query: str, n_results: int = 5, filter_dict=None) -> Dict[str, Any]:
        return {
            "query": query,
            "documents": self._documents[:n_results],
            "context": "",
            "document_count": min(n_results, len(self._documents)),
        }


class _StubTaskRequest:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.input_data: Dict[str, Any] = {}


class _StubEvent:
    def __init__(self, payload: Dict[str, Any], source: str = "test"):
        self.payload = payload
        self.source = source


@pytest.mark.asyncio
async def test_learning_loop_injects_only_non_demoted_lessons(tmp_path):
    from skyn3t.intelligence.learning_loop import LearningLoop

    sb = LessonScoreboard(
        store_path=tmp_path / "s.json",
        min_score=-0.34,
        min_samples_for_filter=3,
    )
    # Demote lesson "bad" with 3 failures, keep "good" unscored.
    for i in range(3):
        sb.record_injection(f"prev-{i}", ["bad"])
        sb.record_outcome(f"prev-{i}", success=False)

    rag = _StubRAG([
        {"id": "bad", "content": "bad lesson body", "metadata": {}},
        {"id": "good", "content": "good lesson body", "metadata": {}},
    ])
    loop = LearningLoop(event_bus=None, rag=rag, scoreboard=sb)

    task = _StubTaskRequest(task_id="next-task")
    event = _StubEvent({
        "title": "build a thing",
        "task": task,
        "task_id": task.task_id,
    })
    await loop._inject(event)

    assert "lessons" in task.input_data
    assert "good lesson body" in task.input_data["lessons"]
    assert "bad lesson body" not in task.input_data["lessons"]


@pytest.mark.asyncio
async def test_learning_loop_records_injection_for_attribution(tmp_path):
    from skyn3t.intelligence.learning_loop import LearningLoop

    sb = LessonScoreboard(store_path=tmp_path / "s.json", flush_every=999)
    rag = _StubRAG([
        {"id": "lesson-x", "content": "x body", "metadata": {}},
    ])
    loop = LearningLoop(event_bus=None, rag=rag, scoreboard=sb)

    task = _StubTaskRequest(task_id="task-99")
    event = _StubEvent({"title": "do thing", "task": task, "task_id": "task-99"})
    await loop._inject(event)

    # Now simulate a successful outcome via the completion handler.
    completion = _StubEvent({"task_id": "task-99"})
    loop._credit_outcome(completion, success=True)
    sb.flush()
    assert sb.get_stats("lesson-x").helpful == 1
