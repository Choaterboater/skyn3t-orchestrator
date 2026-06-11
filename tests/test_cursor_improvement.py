"""Tests for Cursor improvement task queue."""

from __future__ import annotations

import json

import pytest

from skyn3t.config.settings import get_settings
from skyn3t.cortex.cursor_improvement import (
    CURSOR_TASKS_FILENAME,
    enqueue_regression_task,
    enqueue_task,
    load_tasks,
    peek_next_task,
    pop_highest_priority_task,
)


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_enqueue_and_pop_priority_order(tmp_path):
    enqueue_task(priority=10, brief="Low priority task", source="test")
    enqueue_task(priority=90, brief="High priority task", source="test")

    nxt = peek_next_task()
    assert nxt is not None
    assert nxt["brief"] == "High priority task"

    popped = pop_highest_priority_task()
    assert popped is not None
    assert popped["brief"] == "High priority task"

    remaining = load_tasks()
    assert len(remaining["tasks"]) == 1
    assert remaining["tasks"][0]["brief"] == "Low priority task"


def test_enqueue_dedupes_identical_brief():
    assert enqueue_task(priority=50, brief="Same task", source="a") is True
    assert enqueue_task(priority=99, brief="Same task", source="b") is False
    assert len(load_tasks()["tasks"]) == 1


def test_regression_task_writes_file(tmp_path):
    ok = enqueue_regression_task(
        stack="react_vite",
        avg_score=62.0,
        threshold=70.0,
        samples=5,
    )
    assert ok is True
    path = tmp_path / "data" / CURSOR_TASKS_FILENAME
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tasks"][0]["source"] == "continuous_improvement:regression"
    assert "react_vite" in data["tasks"][0]["brief"]
