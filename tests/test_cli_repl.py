from __future__ import annotations

import pytest

import skyn3t.cli.repl as repl


@pytest.mark.asyncio
async def test_cmd_resume_requeues_last_interrupted_prompt(monkeypatch):
    state = repl.ReplState()
    state.last_interrupted_prompt = "resume this task"
    called = {}

    monkeypatch.setattr(
        repl,
        "_queue_prompt_submission",
        lambda _state, prompt: called.setdefault("prompt", prompt),
    )

    await repl._cmd_resume(state)

    assert called["prompt"] == "resume this task"


@pytest.mark.asyncio
async def test_cmd_retry_requeues_last_failed_prompt(monkeypatch):
    state = repl.ReplState()
    state.last_failed_prompt = "retry this task"
    called = {}

    monkeypatch.setattr(
        repl,
        "_queue_prompt_submission",
        lambda _state, prompt: called.setdefault("prompt", prompt),
    )

    await repl._cmd_retry(state)

    assert called["prompt"] == "retry this task"


@pytest.mark.asyncio
async def test_cmd_tasks_renders_running_tasks(monkeypatch):
    state = repl.ReplState()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, path):
            assert path == "/api/swarm/snapshot"
            return type(
                "Resp",
                (),
                {
                    "json": lambda self: {
                        "running_tasks": [
                            {
                                "task_id": "task-123",
                                "agent": "writer",
                                "title": "Fix latency",
                                "session_id": "sess-1",
                            }
                        ]
                    }
                },
            )()

    monkeypatch.setattr(repl.httpx, "AsyncClient", FakeAsyncClient)

    await repl._cmd_tasks(state)

    table = state.transcript[-1]
    assert getattr(table, "title", None) == "Running tasks"


@pytest.mark.asyncio
async def test_cmd_memory_renders_layer_summary(monkeypatch):
    state = repl.ReplState()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, path, params=None):
            assert path == "/api/memory/layers"
            assert params == {"limit": 5}
            return type(
                "Resp",
                (),
                {
                    "json": lambda self: {
                        "layers": {
                            "session": {"active_sessions": 1},
                            "operator": {"insight_count": 2, "skill_summary": {"total": 3}},
                            "project": {"tasks": 4, "knowledge_documents": 5, "success_rate": 0.75},
                        }
                    }
                },
            )()

    monkeypatch.setattr(repl.httpx, "AsyncClient", FakeAsyncClient)

    await repl._cmd_memory(state, "")

    table = state.transcript[-1]
    assert getattr(table, "title", None) == "memory layers"


@pytest.mark.asyncio
async def test_cmd_memory_session_renders_recent_activity(monkeypatch):
    state = repl.ReplState()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, path, params=None):
            assert path == "/api/memory/sessions/sess-1"
            return type(
                "Resp",
                (),
                {
                    "json": lambda self: {
                        "session_id": "sess-1",
                        "context": {"participants": ["writer"], "history": [{"event": "x"}]},
                        "recent_activity": [{"type": "task", "title": "Ship dashboard"}],
                    }
                },
            )()

    monkeypatch.setattr(repl.httpx, "AsyncClient", FakeAsyncClient)

    await repl._cmd_memory(state, "sess-1")

    assert getattr(state.transcript[-2], "title", None) == "session memory"
    assert getattr(state.transcript[-1], "title", None) == "recent activity"


@pytest.mark.asyncio
async def test_cmd_memory_drafts_renders_pending_review_items(monkeypatch):
    state = repl.ReplState()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, path, params=None):
            assert path == "/api/memory/drafts"
            assert params == {"limit": 5}
            return type(
                "Resp",
                (),
                {
                    "json": lambda self: {
                        "drafts": [
                            {
                                "id": "draft-1",
                                "title": "Lesson draft",
                                "doc_type": "lesson",
                                "meta": {"memory_layer": "operator"},
                            }
                        ]
                    }
                },
            )()

    monkeypatch.setattr(repl.httpx, "AsyncClient", FakeAsyncClient)

    await repl._cmd_memory(state, "drafts")

    table = state.transcript[-1]
    assert getattr(table, "title", None) == "memory drafts"


@pytest.mark.asyncio
async def test_cmd_memory_evals_renders_assets(monkeypatch):
    state = repl.ReplState()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, path, params=None):
            assert path == "/api/memory/evaluations"
            assert params == {"status": "approved", "limit": 5}
            return type(
                "Resp",
                (),
                {
                    "json": lambda self: {
                        "evaluations": [
                            {
                                "id": "eval-1",
                                "review_status": "approved",
                                "lane": "fit",
                                "language": "python",
                                "signals": ["cortex", "autonomy"],
                            }
                        ]
                    }
                },
            )()

    monkeypatch.setattr(repl.httpx, "AsyncClient", FakeAsyncClient)

    await repl._cmd_memory(state, "evals approved")

    table = state.transcript[-1]
    assert getattr(table, "title", None) == "evaluation assets"


@pytest.mark.asyncio
async def test_cmd_memory_export_eval_renders_panel(monkeypatch):
    state = repl.ReplState()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, path, params=None):
            assert path == "/api/memory/evaluations/eval-1/export"
            assert params == {"format": "jsonl"}
            return type(
                "Resp",
                (),
                {
                    "json": lambda self: (_ for _ in ()).throw(ValueError("not json")),
                    "text": '{"kind":"evaluation_asset","evaluation":{"id":"eval-1"}}\n',
                },
            )()

    monkeypatch.setattr(repl.httpx, "AsyncClient", FakeAsyncClient)

    await repl._cmd_memory(state, "export-eval eval-1 jsonl")

    panel = state.transcript[-1]
    assert getattr(panel, "title", None) == "evaluation export"
