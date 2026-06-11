"""Phase 2 — Owner C: lesson attribution wiring.

Covers the generic routed-task lesson loop:
  * inject_for_task populates task.input_data['lessons'] for a real task.
  * the phantom-attribution gate: no task (or no input_data sink) => the
    LessonScoreboard is NOT touched (no corrupting record_injection).
  * idempotency: the orchestrator's direct await + the fire-and-forget
    _on_routed path cannot double-inject the same task.
  * record_injection only persists with a real task attached, and outcomes
    credit back to the injected lesson ids.

All tests use an in-memory EventBus, a fake RAG, and a LessonScoreboard
backed by tmp_path — they never touch data/.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List

from skyn3t.intelligence.learning_loop import LearningLoop
from skyn3t.intelligence.lesson_attribution import LessonScoreboard


@dataclass
class FakeTask:
    """Minimal stand-in for TaskRequest (task_id/title/description/input_data)."""

    task_id: str = "task-1"
    title: str = "Build a react vite app"
    description: str = "Scaffold a vite + react project"
    input_data: Dict[str, Any] = field(default_factory=dict)


class FakeRAG:
    """Returns a fixed set of lesson documents and counts queries."""

    def __init__(self, documents: List[Dict[str, Any]]):
        self._documents = documents
        self.calls = 0
        self.last_n_results = None

    async def query(self, text: str, n_results: int = 3):
        self.calls += 1
        self.last_n_results = n_results
        await asyncio.sleep(0)  # force a real await/yield to expose races
        return {"documents": list(self._documents)}


def _docs():
    return [
        {"id": "lesson-a", "content": "Pin vite to v5 to avoid build breakage."},
        {"id": "lesson-b", "content": "Use the react-ts template for typed scaffolds."},
    ]


def _make_loop(tmp_path, rag):
    sb = LessonScoreboard(store_path=tmp_path / "lesson_scores.json")
    # No event bus wiring needed for direct inject_for_task tests; pass a dummy.
    loop = LearningLoop(event_bus=object(), rag=rag, scoreboard=sb)
    return loop, sb


def test_inject_for_task_populates_lessons_and_records(tmp_path):
    rag = FakeRAG(_docs())
    loop, sb = _make_loop(tmp_path, rag)
    task = FakeTask()

    injected = asyncio.run(
        loop.inject_for_task(task, title=task.title, task_id=task.task_id)
    )

    assert injected, "expected lessons to be injected"
    assert task.input_data["lessons"] == [
        "Pin vite to v5 to avoid build breakage.",
        "Use the react-ts template for typed scaffolds.",
    ]
    # record_injection should have remembered the lesson ids inflight for this task.
    assert sb._inflight["task-1"] == ["lesson-a", "lesson-b"]


def test_phantom_attribution_gate_no_task(tmp_path):
    """No task attached => never call record_injection (no scoreboard corruption)."""
    rag = FakeRAG(_docs())
    loop, sb = _make_loop(tmp_path, rag)

    injected = asyncio.run(loop.inject_for_task(None, title="orphan query", task_id="ghost"))

    assert injected == []
    # The phantom path must NOT have recorded anything inflight, and must NOT
    # have created phantom LessonStats entries that would later be miscredited.
    assert sb._inflight == {}
    assert sb._stats == {}


def test_phantom_attribution_gate_task_without_input_data(tmp_path):
    rag = FakeRAG(_docs())
    loop, sb = _make_loop(tmp_path, rag)

    class NoSink:
        task_id = "t-2"
        title = "x"

    injected = asyncio.run(loop.inject_for_task(NoSink(), title="x", task_id="t-2"))

    assert injected == []
    assert sb._inflight == {}
    assert sb._stats == {}


def test_idempotent_no_double_injection_under_race(tmp_path):
    """Concurrent direct call + _on_routed-style call must inject exactly once."""
    rag = FakeRAG(_docs())
    loop, sb = _make_loop(tmp_path, rag)
    task = FakeTask(task_id="task-race")

    async def _race():
        # Both coroutines race through the rag.query await window concurrently,
        # mirroring orchestrator.submit_task (direct await) vs _on_routed
        # (fire-and-forget create_task).
        return await asyncio.gather(
            loop.inject_for_task(task, title=task.title, task_id=task.task_id),
            loop.inject_for_task(task, title=task.title, task_id=task.task_id),
        )

    asyncio.run(_race())

    # Exactly one injection — lessons appended once, not duplicated.
    assert task.input_data["lessons"] == [
        "Pin vite to v5 to avoid build breakage.",
        "Use the react-ts template for typed scaffolds.",
    ]
    assert sb._inflight["task-race"] == ["lesson-a", "lesson-b"]


def test_outcome_credits_injected_lessons(tmp_path):
    rag = FakeRAG(_docs())
    loop, sb = _make_loop(tmp_path, rag)
    task = FakeTask(task_id="task-outcome")

    asyncio.run(loop.inject_for_task(task, title=task.title, task_id=task.task_id))
    sb.record_outcome("task-outcome", success=True)

    a = sb.get_stats("lesson-a")
    b = sb.get_stats("lesson-b")
    assert a is not None and a.helpful == 1 and a.hurt == 0
    assert b is not None and b.helpful == 1 and b.hurt == 0
    # inflight cleared once the outcome is credited.
    assert "task-outcome" not in sb._inflight


def test_no_lessons_keeps_input_data_clean(tmp_path):
    """When RAG returns nothing usable, do not create an empty lessons key."""
    rag = FakeRAG([])
    loop, sb = _make_loop(tmp_path, rag)
    task = FakeTask(task_id="task-empty")

    injected = asyncio.run(
        loop.inject_for_task(task, title=task.title, task_id=task.task_id)
    )

    assert injected == []
    assert "lessons" not in task.input_data
    assert sb._inflight == {}
