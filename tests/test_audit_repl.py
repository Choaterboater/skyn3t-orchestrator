"""Regression tests for the audit fixes in skyn3t/cli/repl.py.

Covers two confirmed bugs:

1. "approve with edits" used to call the blocking ``edit_markdown_in_editor``
   ($EDITOR via subprocess.run) directly on the background asyncio loop,
   freezing the ws/poll/studio-sync listeners. It must now run off-loop via
   ``asyncio.to_thread`` (i.e. on a worker thread, not the loop thread).

2. ``/reject FEEDBACK...`` used to consume the first feedback word as a project
   slug, so multi-word reject feedback POSTed to a nonexistent slug (404) and
   the active project was never rejected.
"""

from __future__ import annotations

import asyncio
import threading

import skyn3t.cli.repl as repl


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal stand-in for httpx.Client used as a context manager."""

    def __init__(self, project_payload=None, **_kwargs):
        self._project_payload = project_payload or {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, _path):
        return _FakeResponse(self._project_payload)


# ---------------------------------------------------------------------------
# Bug 2: /reject slug-vs-feedback parsing
# ---------------------------------------------------------------------------


def _run_reject(state, rest):
    return asyncio.run(repl._cmd_studio_reject(state, rest))


def test_reject_multiword_feedback_targets_active_slug(monkeypatch):
    state = repl.ReplState(studio_slug="demo-build")
    captured = {}

    def _fake_submit(_client, slug, feedback):
        captured["slug"] = slug
        captured["feedback"] = feedback

    monkeypatch.setattr(repl.httpx, "Client", _FakeClient)
    monkeypatch.setattr(repl, "submit_reject", _fake_submit)

    _run_reject(state, "use SQLite only")

    # The whole input is feedback against the active slug — the first word is
    # NOT swallowed as a slug (which previously caused a 404).
    assert captured["slug"] == "demo-build"
    assert captured["feedback"] == "use SQLite only"


def test_reject_explicit_matching_slug_is_consumed(monkeypatch):
    state = repl.ReplState(studio_slug="demo-build")
    captured = {}

    monkeypatch.setattr(repl.httpx, "Client", _FakeClient)
    monkeypatch.setattr(
        repl,
        "submit_reject",
        lambda _c, slug, feedback: captured.update(slug=slug, feedback=feedback),
    )

    _run_reject(state, "demo-build use SQLite only")

    assert captured["slug"] == "demo-build"
    assert captured["feedback"] == "use SQLite only"


def test_reject_single_word_feedback_targets_active_slug(monkeypatch):
    state = repl.ReplState(studio_slug="demo-build")
    captured = {}

    monkeypatch.setattr(repl.httpx, "Client", _FakeClient)
    monkeypatch.setattr(
        repl,
        "submit_reject",
        lambda _c, slug, feedback: captured.update(slug=slug, feedback=feedback),
    )

    _run_reject(state, "slow")

    assert captured["slug"] == "demo-build"
    assert captured["feedback"] == "slow"


def test_reject_without_feedback_shows_usage(monkeypatch):
    state = repl.ReplState(studio_slug="demo-build")
    called = {"submit": False}

    monkeypatch.setattr(repl.httpx, "Client", _FakeClient)
    monkeypatch.setattr(
        repl,
        "submit_reject",
        lambda *_a, **_k: called.__setitem__("submit", True),
    )

    _run_reject(state, "")

    assert called["submit"] is False
    assert any("usage" in t.plain for t in state.transcript if hasattr(t, "plain"))


# ---------------------------------------------------------------------------
# Bug 1: editor runs off the event loop
# ---------------------------------------------------------------------------


def test_approve_with_edits_runs_editor_off_loop(monkeypatch):
    state = repl.ReplState(studio_slug="demo-build")
    editor_thread = {}

    project = {"status": "awaiting_approval"}
    monkeypatch.setattr(
        repl.httpx,
        "Client",
        lambda *a, **k: _FakeClient(project_payload=project),
    )
    monkeypatch.setattr(repl, "fetch_approval_document", lambda *_a, **_k: "doc")
    monkeypatch.setattr(
        repl,
        "resolve_approval_choice",
        lambda *a, **k: "approved",
    )

    def _fake_editor(text):
        editor_thread["thread"] = threading.current_thread()
        return text + "\nedited"

    monkeypatch.setattr(repl, "edit_markdown_in_editor", _fake_editor)

    async def _drive():
        # Capture the loop's own thread, then run the approval coroutine.
        loop_thread = threading.current_thread()
        result = await repl._handle_studio_approval_plain(state, "approve with edits")
        return loop_thread, result

    loop_thread, handled = asyncio.run(_drive())

    assert handled is True
    # The blocking editor must have run on a different (worker) thread than the
    # event loop — proving it was dispatched via asyncio.to_thread.
    assert editor_thread["thread"] is not loop_thread
