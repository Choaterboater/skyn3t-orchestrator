"""Tests for Studio approval CLI helpers and commands."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
from typer.testing import CliRunner

import skyn3t.cli.studio_approval as studio_approval
from skyn3t.cli.main import app

runner = CliRunner()


def test_resolve_approval_choice_approve():
    calls = []

    class FakeClient:
        def post(self, path, json=None):
            calls.append((path, json))
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"ok": True},
            )

    message = studio_approval.resolve_approval_choice(
        FakeClient(),
        "demo",
        original="# Architecture\n\n## Overview\n",
        choice="approve",
    )
    assert message == "Approved — build resuming."
    assert calls == [("/api/studio/projects/demo/approve", None)]


def test_resolve_approval_choice_approve_with_edits():
    calls = []

    class FakeClient:
        def post(self, path, json=None):
            calls.append((path, json))
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"ok": True},
            )

    message = studio_approval.resolve_approval_choice(
        FakeClient(),
        "demo",
        original="old",
        choice="e",
        edited="new",
    )
    assert "edits" in message
    assert calls[0][0].endswith("/approve-with-edits")
    assert calls[0][1] == {"content": "new"}


def test_cli_studio_approve_command(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path):
            assert path == "/api/studio/projects/demo-gate"
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"status": "awaiting_approval"},
            )

        def post(self, path, json=None):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"ok": True},
            )

    monkeypatch.setattr(
        studio_approval,
        "fetch_approval_document",
        lambda client, slug: "# Architecture\n\n## Overview\nDone.\n",
    )
    monkeypatch.setattr("skyn3t.cli.main._client", lambda: FakeClient())

    result = runner.invoke(app, ["studio", "approve", "demo-gate"])
    assert result.exit_code == 0
    assert "Approved" in result.stdout


def test_approval_renderables_include_panel():
    project = {
        "awaiting_approval_for": {"stage": "architect", "agent": "architect"},
    }
    renderables = studio_approval.approval_renderables(project, "# Architecture\n")
    assert len(renderables) == 2
    assert "Approval required" in str(renderables[0].title)


def test_run_interactive_approval_uses_emit(monkeypatch):
    emitted = []

    class FakeClient:
        def get(self, path, params=None):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                text="# Architecture\n",
            )

        def post(self, path, json=None):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"ok": True},
            )

    monkeypatch.setattr(
        studio_approval,
        "fetch_approval_document",
        lambda client, slug: "# Architecture\n",
    )
    message = studio_approval.run_interactive_approval(
        console=SimpleNamespace(print=lambda *a, **k: None),
        client=FakeClient(),
        slug="demo",
        project={"awaiting_approval_for": {"stage": "architect", "agent": "architect"}},
        prompt_choice=lambda: "a",
        prompt_feedback=lambda: "",
        edit_text=lambda text: text,
        emit=emitted.append,
    )
    assert message == "Approved — build resuming."
    assert len(emitted) == 2


def test_cli_studio_reject_command(monkeypatch):
    calls = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"status": "awaiting_approval"},
            )

        def post(self, path, json=None):
            calls.append((path, json))
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"ok": True},
            )

    monkeypatch.setattr("skyn3t.cli.main._client", lambda: FakeClient())

    result = runner.invoke(
        app,
        ["studio", "reject", "demo-gate", "Use SQLite instead of Postgres"],
    )
    assert result.exit_code == 0
    assert calls[0][1] == {"feedback": "Use SQLite instead of Postgres"}
