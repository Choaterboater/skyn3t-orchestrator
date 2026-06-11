"""Tests for the top-level SkyN3t CLI entry flow."""

import io
import zipfile
from types import SimpleNamespace

import httpx
from typer.testing import CliRunner

import skyn3t.cli.doctor as cli_doctor
import skyn3t.cli.main as cli_main
import skyn3t.config.model_routing as routing_config
import skyn3t.core.models as core_models
from skyn3t.cli.main import app
from skyn3t.config.model_routing import ModelRoutingStore

runner = CliRunner()


def test_cli_no_args_shows_getting_started_panel():
    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "SkyN3t Getting Started" in result.stdout
    assert "skyn3t repl" in result.stdout


def test_cli_no_args_launches_repl_when_interactive_server_is_up(monkeypatch):
    called = {"repl": False}

    monkeypatch.setattr(cli_main, "_interactive_cli_ready", lambda: True)
    monkeypatch.setattr(cli_main, "_server_is_reachable", lambda: True)

    import skyn3t.cli.repl as cli_repl

    monkeypatch.setattr(cli_repl, "run", lambda: called.__setitem__("repl", True))

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert called["repl"] is True


def test_apply_install_wizard_choice_writes_env_and_routing(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    store = ModelRoutingStore(tmp_path / "model_routing.json")
    monkeypatch.setattr(routing_config, "_store", store)

    result = cli_main._apply_install_wizard_choice(
        "copilot_cli",
        apply_routing_profile=True,
        env_path=env_path,
    )

    assert "SKYN3T_LLM_BACKEND=copilot_cli" in env_path.read_text(encoding="utf-8")
    assert result["routing_applied"] is True
    assert store.entries()["code"]["tier"] == "ui"
    assert store.entries()["reviewer"]["tier"] == "balanced"


def test_apply_studio_quality_wizard_writes_openrouter_and_policies(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    store = ModelRoutingStore(tmp_path / "model_routing.json")
    monkeypatch.setattr(routing_config, "_store", store)

    result = cli_main._apply_install_wizard_choice(
        "openrouter",
        apply_routing_profile=True,
        env_path=env_path,
        openrouter_api_key="sk-or-test",
        enable_quality_env=True,
    )

    text = env_path.read_text(encoding="utf-8")
    assert "SKYN3T_LLM_BACKEND=openrouter" in text
    assert "OPENROUTER_API_KEY=sk-or-test" in text
    assert "SKYN3T_AUTO_RETRY=1" in text
    assert "SKYN3T_EXECUTION_BACKEND=auto" in text
    assert result["routing_applied"] is True
    assert store.entries()["code"]["tier"] == "or_strong"
    assert store.entries()["reviewer"]["tier"] == "or_strong"
    assert store.entries()["designer"]["tier"] == "or_ui"


def test_cli_init_runs_setup_wizard_when_interactive(monkeypatch, tmp_path):
    called = {"wizard": False}

    async def _fake_init_db():
        return None

    fake_settings = SimpleNamespace(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        vector_db_path=str(tmp_path / "vectors"),
        ensure_directories=lambda: None,
    )

    monkeypatch.setattr(cli_main, "_interactive_cli_ready", lambda: True)
    monkeypatch.setattr(cli_main, "_run_install_wizard", lambda: called.__setitem__("wizard", True) or "Setup wizard saved")
    monkeypatch.setattr(cli_main, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(core_models, "init_db", _fake_init_db)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert called["wizard"] is True
    assert "Setup wizard saved" in result.stdout


def test_cli_project_lists_examples():
    result = runner.invoke(app, ["project", "--examples"])

    assert result.exit_code == 0
    assert "Studio examples" in result.stdout
    assert "habit-tracker" in result.stdout
    assert "skyn3t project --example habit-tracker" in result.stdout


def test_cli_project_without_brief_shows_examples():
    result = runner.invoke(app, ["project"])

    assert result.exit_code == 0
    assert "habit-tracker" in result.stdout
    assert "Provide a brief or pick a preset" in result.stdout


def test_cli_project_runs_example_preset(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None, **kwargs):
            calls["json"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "accepted": True,
                    "slug": "habit-demo",
                    "title": "Auto-planned",
                    "next_action": "Queued — waiting for a worker slot.",
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())
    monkeypatch.setattr(cli_main, "_interactive_cli_ready", lambda: False)

    result = runner.invoke(app, ["project", "--example", "habit-tracker", "--no-watch"])

    assert result.exit_code == 0
    assert calls["json"]["template"] == "auto"
    assert "habit tracker" in calls["json"]["brief"].lower()
    assert "habit-demo" in result.stdout


def test_cli_project_starts_studio_with_auto_template(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None, **kwargs):
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
        "mission_setup": {"audience": "", "autonomy": "balanced", "goal": ""},
        "repo_target": {"local_path": "", "focus_file": ""},
    }
    assert "demo-123" in result.stdout
    assert "Next: Queued" in result.stdout
    assert "Mode: Balanced" in result.stdout
    assert "Repo: Current SkyN3t workspace" in result.stdout


def test_cli_project_starts_live_watch_in_interactive_terminal(monkeypatch):
    watched = {"slug": None}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None, **kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "accepted": True,
                    "slug": "demo-live",
                    "title": "Auto-planned",
                    "next_action": "Queued — waiting for a worker slot.",
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())
    monkeypatch.setattr(cli_main, "_interactive_cli_ready", lambda: True)
    monkeypatch.setattr(cli_main, "_watch_studio_project", lambda slug: watched.__setitem__("slug", slug))

    result = runner.invoke(app, ["project", "build a live dashboard"])

    assert result.exit_code == 0
    assert watched["slug"] == "demo-live"


def test_format_studio_progress_line():
    line = cli_main._format_studio_progress_line(
        {
            "status": "running",
            "current_stage": "code",
            "current_agent": "CodeAgent",
            "next_action": "Generating scaffold",
        }
    )
    assert "running" in line
    assert "code" in line
    assert "CodeAgent" in line
    assert "Generating scaffold" in line


def test_watch_studio_project_prints_status_updates(monkeypatch):
    responses = [
        {
            "status": "queued",
            "next_action": "Queued — waiting for a worker slot.",
            "history": [],
        },
        {
            "status": "running",
            "current_stage": "brainstorm",
            "current_agent": "BrainstormAgent",
            "next_action": "SkyN3t is briefing the swarm.",
            "history": [],
        },
        {
            "status": "done",
            "next_action": "Build completed.",
            "history": [],
        },
    ]

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path):
            data = responses.pop(0)
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())
    monkeypatch.setattr(cli_main.time_mod, "sleep", lambda _: None)

    lines: list[str] = []
    monkeypatch.setattr(cli_main.console, "print", lambda *args, **kwargs: lines.append(str(args[0]) if args else ""))

    cli_main._watch_studio_project("demo-status")
    text = "\n".join(lines).lower()
    assert "queued" in text
    assert "brainstorm" in text
    assert "done" in text


def test_cli_project_sends_custom_mission_setup(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None, **kwargs):
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
        "goal": "",
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

        def post(self, path, json=None, **kwargs):
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

        def post(self, path, json=None, **kwargs):
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

        def post(self, path, json=None, **kwargs):
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


def test_cli_memory_drafts_renders_pending_items(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            assert path == "/api/memory/drafts"
            assert params == {"limit": 20}
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "drafts": [
                        {
                            "id": "draft-1",
                            "title": "Lesson draft",
                            "doc_type": "lesson",
                            "source": "reflection",
                            "meta": {"memory_layer": "operator"},
                        }
                    ]
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["memory", "drafts"])

    assert result.exit_code == 0
    assert "Memory drafts" in result.stdout
    assert "draft-1" in result.stdout


def test_cli_memory_approve_posts_to_endpoint(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None):
            calls["path"] = path
            calls["json"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"draft": {"id": "draft-1", "title": "Lesson draft"}},
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["memory", "approve", "draft-1"])

    assert result.exit_code == 0
    assert calls == {"path": "/api/memory/drafts/draft-1/approve", "json": None}
    assert "Approved" in result.stdout


def test_cli_memory_evals_lists_assets(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            assert path == "/api/memory/evaluations"
            assert params == {"status": "approved", "limit": 20}
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "evaluations": [
                        {
                            "id": "eval-1",
                            "title": "External eval",
                            "review_status": "approved",
                            "lane": "fit",
                            "language": "python",
                            "signals": ["cortex", "autonomy"],
                        }
                    ]
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["memory", "evals", "--status", "approved"])

    assert result.exit_code == 0
    assert "Evaluation assets" in result.stdout
    assert "eval-1" in result.stdout


def test_cli_memory_export_eval_prints_jsonl(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            assert path == "/api/memory/evaluations/eval-1/export"
            assert params == {"format": "jsonl"}
            return SimpleNamespace(
                raise_for_status=lambda: None,
                text='{"kind":"evaluation_asset","evaluation":{"id":"eval-1"}}\n',
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["memory", "export-eval", "eval-1", "--format", "jsonl"])

    assert result.exit_code == 0
    assert '"kind":"evaluation_asset"' in result.stdout


def test_cli_export_trajectories_can_include_evaluations(monkeypatch, tmp_path):
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
                content=b'{"task_id":"t-1"}\n{"kind":"evaluation_asset"}\n',
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())
    out_path = tmp_path / "bundle.jsonl"

    result = runner.invoke(
        app,
        ["export", "trajectories", "--agent", "designer", "--include-evaluations", "--output", str(out_path)],
    )

    assert result.exit_code == 0
    assert calls == {
        "path": "/api/trajectories/export",
        "params": {"agent": "designer", "include_evaluations": True},
    }
    assert out_path.read_text() == '{"task_id":"t-1"}\n{"kind":"evaluation_asset"}\n'
    assert "trajectory bundle" in result.stdout


def test_cli_export_penpot_package(monkeypatch, tmp_path):
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
                content=b"PK\x03\x04fakezip",
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())
    out_path = tmp_path / "demo-penpot.zip"

    result = runner.invoke(
        app,
        ["export", "penpot", "demo", "--output", str(out_path)],
    )

    assert result.exit_code == 0
    assert calls == {
        "path": "/api/studio/projects/demo/design-handoff/penpot/package",
        "params": None,
    }
    assert out_path.read_bytes() == b"PK\x03\x04fakezip"
    assert "Penpot handoff package" in result.stdout


def test_cli_export_penpot_falls_back_to_local_project(monkeypatch, tmp_path):
    project_dir = tmp_path / "demo"
    project_dir.mkdir(parents=True)
    (project_dir / "brand.md").write_text("# Brand\n", encoding="utf-8")
    (project_dir / "tokens.json").write_text('{"font":{}}', encoding="utf-8")

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            request = httpx.Request("GET", f"{cli_main.API_BASE}{path}")
            response = httpx.Response(404, request=request, text="Not Found")
            raise httpx.HTTPStatusError("Not Found", request=request, response=response)

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())
    monkeypatch.setattr(
        cli_main,
        "get_settings",
        lambda: SimpleNamespace(projects_dir=tmp_path),
    )
    out_path = tmp_path / "demo-penpot.zip"

    result = runner.invoke(
        app,
        ["export", "penpot", "demo", "--output", str(out_path)],
    )

    assert result.exit_code == 0
    with zipfile.ZipFile(io.BytesIO(out_path.read_bytes())) as archive:
        names = archive.namelist()
    assert "penpot_manifest.json" in names
    assert "brand.md" in names
    assert "local project artifacts" in result.stdout


def test_cli_skills_candidates_renders_memory_docs(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            assert path == "/api/skills/candidates"
            assert params == {"limit": 20}
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "candidates": [
                        {
                            "id": "mem-1",
                            "title": "Skillable lesson",
                            "doc_type": "lesson",
                            "meta": {"confidence": 0.8},
                        }
                    ]
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["skills", "candidates"])

    assert result.exit_code == 0
    assert "Skill candidates" in result.stdout
    assert "mem-1" in result.stdout


def test_cli_skills_draft_posts_memory_doc_id(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None):
            calls["path"] = path
            calls["json"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "draft": {"slug": "insight-from-architect-mem1", "name": "Insight from architect mem1"}
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["skills", "draft", "mem-1"])

    assert result.exit_code == 0
    assert calls == {"path": "/api/skills/drafts/from-memory/mem-1", "json": None}
    assert "Created skill draft" in result.stdout


def test_cli_skills_approve_draft_posts(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None):
            calls["path"] = path
            calls["json"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"installed": "draft-install"},
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["skills", "approve-draft", "draft-install"])

    assert result.exit_code == 0
    assert calls == {"path": "/api/skills/drafts/draft-install/approve", "json": None}
    assert "Installed skill draft" in result.stdout


def test_cli_github_scout_run_posts_and_renders(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None, **kwargs):
            calls["path"] = path
            calls["json"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "filed": 1,
                    "candidates_seen": 2,
                    "proposals": [
                        {
                            "repo": "octo/agent-flow",
                            "lane": "fit",
                            "license": "MIT",
                            "proposal_id": "prop-1",
                        }
                    ],
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["github", "scout", "--cadence", "weekly", "--limit", "2"])

    assert result.exit_code == 0
    assert calls["path"] == "/api/github/scout/run"
    assert calls["json"] == {"cadence": "weekly", "limit": 2, "queries": []}
    assert "GitHub scout result" in result.stdout
    assert "octo/agent-flow" in result.stdout


def test_cli_github_scout_schedule_posts(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None, **kwargs):
            calls["path"] = path
            calls["json"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"job_id": "job-1", "name": "github-scout-daily"},
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(
        app,
        ["github", "scout", "--every", "daily at 09:00", "--queries", "agent cli memory,design system ui"],
    )

    assert result.exit_code == 0
    assert calls["path"] == "/api/github/scout/schedule"
    assert calls["json"]["schedule_expr"] == "daily at 09:00"
    assert calls["json"]["queries"] == ["agent cli memory", "design system ui"]
    assert "Scheduled GitHub scout" in result.stdout


def test_cli_repo_scout_run_posts_multi_platform(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None, **kwargs):
            calls["path"] = path
            calls["json"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "filed": 2,
                    "candidates_seen": 3,
                    "proposals": [
                        {
                            "platform": "gitlab",
                            "repo": "gitlab-org/agent-lab",
                            "lane": "fit",
                            "license": "MIT",
                            "proposal_id": "prop-1",
                        }
                    ],
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(
        app,
        [
            "scout",
            "run",
            "--cadence",
            "weekly",
            "--platforms",
            "gitlab,bitbucket",
            "--queries",
            "agent cli memory",
        ],
    )

    assert result.exit_code == 0
    assert calls["path"] == "/api/repo-scout/run"
    assert calls["json"] == {
        "cadence": "weekly",
        "limit": 4,
        "queries": ["agent cli memory"],
        "platforms": ["gitlab", "bitbucket"],
    }
    assert "Repo scout result" in result.stdout
    assert "gitlab-org/agent-lab" in result.stdout


def test_cli_github_scout_platforms_switches_to_generic_endpoint(monkeypatch):
    calls = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None, **kwargs):
            calls["path"] = path
            calls["json"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"filed": 0, "candidates_seen": 0, "proposals": []},
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(
        app,
        ["github", "scout", "--platforms", "github,gitlab"],
    )

    assert result.exit_code == 0
    assert calls["path"] == "/api/repo-scout/run"
    assert calls["json"]["platforms"] == ["github", "gitlab"]


def test_cli_doctor_exits_zero_when_all_checks_pass(monkeypatch):
    monkeypatch.setattr(
        cli_doctor,
        "run_doctor",
        lambda _api_base: cli_doctor.DoctorReport(
            checks=[cli_doctor.DoctorCheck("api-health", "ok", "Server health is healthy.")]
        ),
    )

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "Doctor" in result.stdout
    assert "api-health" in result.stdout
    assert "OK" in result.stdout


def test_cli_doctor_exits_nonzero_when_any_check_fails(monkeypatch):
    monkeypatch.setattr(
        cli_doctor,
        "run_doctor",
        lambda _api_base: cli_doctor.DoctorReport(
            checks=[cli_doctor.DoctorCheck("api-health", "fail", "SkyN3t API is unreachable.")]
        ),
    )

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "SkyN3t API is unreachable." in result.stdout


def test_cli_memory_summary_renders_layers(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            assert path == "/api/memory/layers"
            assert params == {"limit": 5}
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "enabled": True,
                    "layers": {
                        "session": {"active_sessions": 2, "sessions": ["sess-1"]},
                        "operator": {
                            "insight_count": 3,
                            "skill_summary": {"total": 4},
                            "recent_insights": [{"agent": "writer", "capability": "ui", "insight": "Use cards"}],
                            "top_skills": [{"name": "layout-skill", "score": 0.8, "tags": ["ui"]}],
                        },
                        "project": {
                            "tasks": 10,
                            "knowledge_documents": 5,
                            "recent_documents": [{"title": "Landing page pattern", "doc_type": "lesson", "source": "repo"}],
                        },
                    },
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["memory", "summary"])

    assert result.exit_code == 0
    assert "Memory Layers" in result.stdout
    assert "Session memory" in result.stdout
    assert "Operator memory" in result.stdout
    assert "Project knowledge" in result.stdout


def test_cli_memory_session_renders_recent_activity(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            assert path == "/api/memory/sessions/sess-1"
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "session_id": "sess-1",
                    "context": {"participants": ["writer"], "history": [{"event": "x"}]},
                    "recent_activity": [{"type": "task", "title": "Build dashboard"}],
                },
            )

    monkeypatch.setattr(cli_main, "_client", lambda: FakeClient())

    result = runner.invoke(app, ["memory", "session", "sess-1"])

    assert result.exit_code == 0
    assert "Session Memory" in result.stdout
    assert "Build dashboard" in result.stdout
