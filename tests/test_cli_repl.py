from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest
from rich.text import Text

import skyn3t.cli.repl as repl


def test_read_one_uses_single_line_prompt_mode():
    calls = []

    class FakeSession:
        def prompt(self, label, **kwargs):
            calls.append((label, kwargs))
            return "hey"

    result = repl._read_one(FakeSession())

    assert result == "hey"
    assert calls == [
        ("> ", {"multiline": False}),
    ]


def test_wait_for_repl_future_repaints_while_pending():
    state = repl.ReplState()
    future: concurrent.futures.Future[str] = concurrent.futures.Future()
    paints = []

    def _resolve() -> None:
        time.sleep(0.02)
        state.render_version += 1
        time.sleep(0.02)
        future.set_result("done")

    thread = threading.Thread(target=_resolve)
    thread.start()
    try:
        result = repl._wait_for_repl_future(
            future,
            state=state,
            paint=lambda: paints.append("tick"),
            timeout=1,
            tick=0.01,
        )
    finally:
        thread.join()

    assert result == "done"
    assert paints


def test_wait_for_repl_future_skips_repaint_without_state_change():
    state = repl.ReplState()
    future: concurrent.futures.Future[str] = concurrent.futures.Future()
    paints = []

    def _resolve() -> None:
        time.sleep(0.03)
        future.set_result("done")

    thread = threading.Thread(target=_resolve)
    thread.start()
    try:
        result = repl._wait_for_repl_future(
            future,
            state=state,
            paint=lambda: paints.append("tick"),
            timeout=1,
            tick=0.01,
        )
    finally:
        thread.join()

    assert result == "done"
    assert paints == []


def test_render_layout_uses_stacked_sections_without_activity():
    state = repl.ReplState()
    layout = repl._render_layout(state)

    assert [child.name for child in layout.children] == ["header", "transcript"]


def test_render_layout_uses_sidebar_for_activity_when_wide(monkeypatch):
    state = repl.ReplState()
    state.activity.append(Text("• llm_exchange"))
    monkeypatch.setattr(repl, "_terminal_width", lambda: 140)
    layout = repl._render_layout(state)

    assert [child.name for child in layout.children] == ["header", "body"]
    assert [child.name for child in layout["body"].children] == ["transcript", "activity"]


def test_render_layout_stacks_activity_when_narrow(monkeypatch):
    state = repl.ReplState()
    state.activity.append(Text("• task"))
    monkeypatch.setattr(repl, "_terminal_width", lambda: 100)
    layout = repl._render_layout(state)

    assert [child.name for child in layout.children] == ["header", "transcript", "activity"]


def test_format_event_filters_llm_exchange():
    line = repl._format_event(
        {
            "kind": "convo",
            "event_type": "LLM_EXCHANGE",
            "from": "openrouter",
            "label": "openrouter · openai/gpt-4.1",
        }
    )

    assert line is None


def test_format_event_uses_compact_swarm_payload():
    line = repl._format_event(
        {
            "kind": "task",
            "event_type": "TASK_STARTED",
            "from": "writer",
            "label": "Draft README",
        }
    )

    assert line is not None
    assert "writer" in line.plain
    assert "started" in line.plain
    assert "Draft README" in line.plain


def test_looks_like_project_request_detects_builder_prompt():
    assert repl._looks_like_project_request("build me a habit tracker with streaks") is True
    assert repl._looks_like_project_request("create a dashboard for my team") is True
    assert repl._looks_like_project_request("hey there") is False
    assert repl._looks_like_project_request("make me laugh") is False


def test_run_plain_prompt_routes_builder_request_to_project(monkeypatch):
    state = repl.ReplState()
    calls = {}
    paints = []

    monkeypatch.setattr(
        repl,
        "_run_project_command",
        lambda _state, rest, *, paint, prompt_reader: calls.setdefault("rest", rest),
    )

    repl._run_plain_prompt(
        state,
        "build me a habit tracker with streaks",
        paint=lambda: paints.append("paint"),
        prompt_reader=lambda _index, _question: "",
        loop=None,  # type: ignore[arg-type]
    )

    assert calls["rest"] == "build me a habit tracker with streaks"
    assert any("routing this into a project build" in str(item) for item in state.transcript)
    assert paints == ["paint"]


def test_watch_project_in_repl_skips_repaint_when_nothing_changes(monkeypatch):
    state = repl.ReplState()
    paints = []

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code
            self.text = str(payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.text)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.project_reads = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path):
            assert path == "/api/studio/projects/demo"
            self.project_reads += 1
            if self.project_reads == 1:
                return FakeResponse(
                    {
                        "status": "running",
                        "history": [
                            {
                                "event": "PROJECT_STAGE_STARTED",
                                "stage": "brainstorm",
                                "message": "Collecting project intent",
                            }
                        ],
                    }
                )
            if self.project_reads == 2:
                return FakeResponse(
                    {
                        "status": "running",
                        "history": [
                            {
                                "event": "PROJECT_STAGE_STARTED",
                                "stage": "brainstorm",
                                "message": "Collecting project intent",
                            }
                        ],
                    }
                )
            return FakeResponse(
                {
                    "status": "done",
                    "next_action": "Artifacts ready",
                    "history": [
                        {
                            "event": "PROJECT_STAGE_STARTED",
                            "stage": "brainstorm",
                            "message": "Collecting project intent",
                        },
                        {
                            "event": "PROJECT_COMPLETED",
                            "message": "Artifacts ready",
                        },
                    ],
                }
            )

    monkeypatch.setattr(repl.httpx, "Client", FakeClient)
    monkeypatch.setattr(repl.time, "sleep", lambda _seconds: None)

    repl._watch_project_in_repl(
        state,
        "demo",
        paint=lambda: paints.append("paint"),
        prompt_reader=lambda _index, _question: "",
    )

    assert paints == ["paint", "paint", "paint"]


def test_parse_project_command_extracts_template_and_repo_target(tmp_path, monkeypatch):
    repo_root = tmp_path / "demo-repo"
    repo_root.mkdir()
    resolved_target = {
        "local_path": str(repo_root.resolve()),
        "focus_file": "src/app.py",
    }

    monkeypatch.setattr(repl, "resolve_repo_target", lambda payload: resolved_target)

    parsed = repl._parse_project_command(
        f'--audience builders --autonomy confirm_first --repo-path "{repo_root}" '
        '--focus-file src/app.py marketing :: Build a launch site'
    )

    assert parsed["template"] == "marketing"
    assert parsed["brief"] == "Build a launch site"
    assert parsed["audience"] == "builders"
    assert parsed["autonomy"] == "confirm_first"
    assert parsed["repo_target"]["local_path"] == str(repo_root.resolve())
    assert parsed["repo_target"]["focus_file"] == "src/app.py"


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
async def test_submit_prompt_falls_back_to_direct_exec_when_result_route_missing(monkeypatch):
    state = repl.ReplState()
    state.active_agent = "writer"
    monkeypatch.setattr(repl, "_agent_line", lambda name, text: f"{name}: {text}")

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.posts = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, path, json=None, timeout=None):
            self.posts.append((path, json, timeout))
            if path == "/api/orchestrator/submit":
                return type(
                    "Resp",
                    (),
                    {
                        "status_code": 200,
                        "json": lambda self: {"task_id": "task-123"},
                    },
                )()
            if path == "/api/tasks/task-123/cancel":
                return type("Resp", (), {"status_code": 200, "json": lambda self: {"ok": True}})()
            if path == "/api/agents/writer/exec":
                return type(
                    "Resp",
                    (),
                    {
                        "raise_for_status": lambda self: None,
                        "json": lambda self: {"output": "hey back"},
                    },
                )()
            raise AssertionError(path)

        async def get(self, path):
            if path == "/api/tasks/task-123/result":
                return type("Resp", (), {"status_code": 404})()
            if path == "/api/agents":
                return type(
                    "Resp",
                    (),
                    {
                        "json": lambda self: {
                            "agents": [{"name": "writer", "status": "idle"}]
                        }
                    },
                )()
            raise AssertionError(path)

    monkeypatch.setattr(repl.httpx, "AsyncClient", FakeAsyncClient)

    await repl._submit_prompt(state, "hey")

    assert any("falling back to direct agent reply" in str(item) for item in state.transcript)
    assert any("writer" in str(item) and "hey back" in str(item) for item in state.transcript)
    assert state.last_failed_prompt is None


@pytest.mark.asyncio
async def test_submit_prompt_uses_raw_llm_chat_when_no_agent_selected(monkeypatch):
    state = repl.ReplState()
    state.active_agent = None
    state.active_backend = "copilot_cli"
    monkeypatch.setattr(repl, "_agent_line", lambda name, text: f"{name}: {text}")

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, path, json=None, timeout=None):
            assert path == "/api/llm/complete"
            assert json["prompt"] == "hey"
            assert json["backend"] == "copilot_cli"
            return type(
                "Resp",
                (),
                {
                    "raise_for_status": lambda self: None,
                    "json": lambda self: {
                        "response": "hey back",
                        "backend": "copilot_cli",
                    },
                },
            )()

    monkeypatch.setattr(repl.httpx, "AsyncClient", FakeAsyncClient)

    await repl._submit_prompt(state, "hey")

    assert any("copilot_cli: hey back" == str(item) for item in state.transcript)
    assert state.last_failed_prompt is None


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


def test_run_project_command_watches_and_submits_clarifications(monkeypatch):
    state = repl.ReplState()
    answers_sent = {}

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code
            self.text = str(payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.text)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.project_reads = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json):
            if path == "/api/studio/start":
                return FakeResponse(
                    {
                        "accepted": True,
                        "slug": "demo",
                        "next_action": "Brainstorm queued",
                        "repo_target": {"local_path": "", "focus_file": ""},
                    }
                )
            if path == "/api/studio/projects/demo/clarify":
                answers_sent["answers"] = json["answers"]
                return FakeResponse({"ok": True})
            raise AssertionError(path)

        def get(self, path):
            assert path == "/api/studio/projects/demo"
            self.project_reads += 1
            if self.project_reads == 1:
                return FakeResponse(
                    {
                        "status": "awaiting_clarification",
                        "history": [
                            {
                                "event": "PROJECT_STAGE_STARTED",
                                "stage": "brainstorm",
                                "message": "Collecting project intent",
                            }
                        ],
                        "clarification": {
                            "questions": [
                                "Who is this for?",
                                "What workflow matters most?",
                            ]
                        },
                    }
                )
            return FakeResponse(
                {
                    "status": "done",
                    "next_action": "Artifacts ready",
                    "history": [
                        {
                            "event": "PROJECT_STAGE_STARTED",
                            "stage": "brainstorm",
                            "message": "Collecting project intent",
                        },
                        {
                            "event": "PROJECT_COMPLETED",
                            "message": "Artifacts ready",
                        },
                    ],
                }
            )

    monkeypatch.setattr(repl.httpx, "Client", FakeClient)
    monkeypatch.setattr(repl.time, "sleep", lambda _seconds: None)

    prompts = []

    def _prompt_reader(index, question):
        prompts.append((index, question))
        return f"answer {index}"

    repl._run_project_command(
        state,
        "Build a service dashboard",
        paint=lambda: None,
        prompt_reader=_prompt_reader,
    )

    assert answers_sent["answers"] == ["answer 1", "answer 2"]
    assert prompts == [
        (1, "Who is this for?"),
        (2, "What workflow matters most?"),
    ]
    assert any("project queued: demo" in str(item) for item in state.transcript)
    assert any("clarifications sent. Resuming build" in str(item) for item in state.transcript)
    assert getattr(state.transcript[-1], "title", None) == "Project finished"
