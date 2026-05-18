"""Tests for the top-level SkyN3t CLI entry flow."""

from types import SimpleNamespace

import httpx
from typer.testing import CliRunner

import skyn3t.cli.main as cli_main
from skyn3t.cli.main import app

runner = CliRunner()


def test_cli_no_args_shows_getting_started_panel():
    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "SkyN3t Getting Started" in result.stdout
    assert "skyn3t repl" in result.stdout


def test_cli_project_starts_studio_with_auto_template(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json):
            calls["path"] = path
            calls["json"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "accepted": True,
                    "slug": "demo-123",
                    "title": "Auto-planned",
                    "next_action": "Queued — waiting for a worker slot.",
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["project", "build a habit tracker"])

    assert result.exit_code == 0
    assert calls["path"] == "/api/studio/start"
    assert calls["json"] == {
        "template": "auto",
        "brief": "build a habit tracker",
        "mission_setup": {"audience": "", "autonomy": "move_fast"},
        "repo_target": {"local_path": "", "focus_file": ""},
    }
    assert "demo-123" in result.stdout
    assert "Next: Queued" in result.stdout
    assert "Mode: Move fast" in result.stdout
    assert "Repo: Current SkyN3t workspace" in result.stdout


def test_cli_project_sends_custom_mission_setup(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json):
            calls["path"] = path
            calls["json"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "accepted": True,
                    "slug": "demo-456",
                    "title": "Auto-planned",
                    "next_action": "Queued — waiting for a worker slot.",
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(
        app,
        [
            "project",
            "--audience",
            "leaders",
            "--autonomy",
            "confirm_first",
            "build a launch plan",
        ],
    )

    assert result.exit_code == 0
    assert calls["json"]["mission_setup"] == {
        "audience": "leaders",
        "autonomy": "confirm_first",
    }
    assert calls["json"]["repo_target"] == {"local_path": "", "focus_file": ""}
    assert "Audience: Decision-makers" in result.stdout
    assert "Mode: Confirm first" in result.stdout


def test_cli_project_sends_repo_target(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json):
            calls["path"] = path
            calls["json"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "accepted": True,
                    "slug": "demo-789",
                    "title": "Targeted fix",
                    "next_action": "Queued — waiting for a worker slot.",
                    "repo_target": {
                        "local_path": "/tmp/customer-portal",
                        "focus_file": "src/login.tsx",
                    },
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())
    monkeypatch.setattr(
        cli_main,
        "resolve_repo_target",
        lambda value: {
            "local_path": "/tmp/customer-portal",
            "focus_file": "src/login.tsx",
        },
    )

    result = runner.invoke(
        app,
        [
            "project",
            "--repo-path",
            "../customer-portal",
            "--focus-file",
            "src/login.tsx",
            "fix the login form",
        ],
    )

    assert result.exit_code == 0
    assert calls["json"]["repo_target"] == {
        "local_path": "/tmp/customer-portal",
        "focus_file": "src/login.tsx",
    }
    assert "Repo: /tmp/customer-portal" in result.stdout
    assert "Focus file: src/login.tsx" in result.stdout


def test_cli_project_rejects_focus_file_without_repo_path(monkeypatch):
    called = {"client": False}

    class FakeClient:
        def __enter__(self):
            called["client"] = True
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(
        app,
        ["project", "--focus-file", "src/login.tsx", "fix the login form"],
    )

    assert result.exit_code == 1
    assert "focus file requires a repo path" in result.stdout
    assert called["client"] is False


def test_cli_agent_add_maps_anthropic_alias_to_claude(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json):
            calls["path"] = path
            calls["json"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"status": "registered"},
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["agent", "add", "helper", "--provider", "anthropic"])

    assert result.exit_code == 0
    assert calls["path"] == "/api/agents"
    assert calls["json"]["provider"] == "claude"
    assert "Provider: claude" in result.stdout


def test_cli_proposal_list_requests_system_origin(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            calls["path"] = path
            calls["params"] = params
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "proposals": [
                        {
                            "id": "prop-123",
                            "kind": "feature",
                            "origin": "system",
                            "title": "Tune planner",
                            "summary": "Reduce repeated failures",
                            "created_at": 1_700_000_000.0,
                        }
                    ]
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["proposal", "list"])

    assert result.exit_code == 0
    assert calls["path"] == "/api/proposals"
    assert calls["params"] == {"status": "pending", "origin": "system"}
    assert "Tune planner" in result.stdout
    assert "system" in result.stdout


def test_cli_proposal_list_falls_back_to_local_store(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())
    monkeypatch.setattr(
        cli_main,
        "_load_local_proposals",
        lambda **kwargs: [
            {
                "id": "prop-local",
                "kind": "tuning",
                "origin": "system",
                "title": "Tune reviewer",
                "summary": "Fallback proposal",
                "created_at": 1_700_000_100.0,
            }
        ],
    )

    result = runner.invoke(app, ["proposal", "list"])

    assert result.exit_code == 0
    assert "Server unavailable — showing local proposal files only." in result.stdout
    assert "Tune reviewer" in result.stdout


def test_cli_proposal_approve_posts_to_server(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path):
            calls["path"] = path
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"ok": True, "applied": True, "result": {"snapshot": "snap-1"}},
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["proposal", "approve", "prop-123"])

    assert result.exit_code == 0
    assert calls["path"] == "/api/proposals/prop-123/approve"
    assert "Proposal approved" in result.stdout


def test_cli_proposal_reject_posts_reason(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json):
            calls["path"] = path
            calls["json"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"ok": True},
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(
        app,
        ["proposal", "reject", "prop-123", "--reason", "not safe enough"],
    )

    assert result.exit_code == 0
    assert calls["path"] == "/api/proposals/prop-123/reject"
    assert calls["json"] == {"reason": "not safe enough"}
    assert "Proposal rejected" in result.stdout
