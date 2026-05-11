"""Tests for web event broadcasting helpers."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import skyn3t.web.app as web_app
from skyn3t.config.settings import get_settings
from skyn3t.core.events import Event, EventBus, EventType


def test_broadcast_event_skips_websocket_tasks_without_running_loop(monkeypatch):
    web_app._broadcast_tasks.clear()
    web_app._recent_swarm_events.clear()
    monkeypatch.setattr(
        web_app.asyncio,
        "get_running_loop",
        lambda: (_ for _ in ()).throw(RuntimeError("no running event loop")),
    )

    web_app.broadcast_event(
        Event(
            event_type=EventType.TASK_COMPLETED,
            source="tester",
            payload={"task_id": "task-1", "title": "Completed task"},
        )
    )

    assert len(web_app._broadcast_tasks) == 0
    assert web_app._recent_swarm_events[-1]["meta"]["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_broadcast_event_schedules_tasks_with_running_loop(monkeypatch):
    web_app._broadcast_tasks.clear()
    web_app._recent_swarm_events.clear()

    event_broadcast = AsyncMock()
    swarm_broadcast = AsyncMock()
    monkeypatch.setattr(web_app.manager, "broadcast", event_broadcast)
    monkeypatch.setattr(web_app.swarm_manager, "broadcast", swarm_broadcast)

    web_app.broadcast_event(
        Event(
            event_type=EventType.TASK_COMPLETED,
            source="tester",
            payload={"task_id": "task-2", "title": "Completed task"},
        )
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    event_broadcast.assert_awaited_once()
    swarm_broadcast.assert_awaited_once()
    assert len(web_app._broadcast_tasks) == 0


@pytest.mark.asyncio
async def test_studio_start_returns_reserved_slug(monkeypatch):
    calls = {}

    class FakeRunner:
        def reserve_project(
            self,
            template_key,
            brief,
            slug=None,
            mission_setup=None,
            repo_target=None,
        ):
            calls["reserve"] = {
                "template_key": template_key,
                "brief": brief,
                "slug": slug,
                "mission_setup": mission_setup,
                "repo_target": repo_target,
            }
            return {
                "slug": "demo-123",
                "title": "Auto-planned",
                "status": "queued",
                "next_action": "Queued — waiting for a worker slot.",
                "workflow_summary": {"title": "Auto-planned"},
                "mission_setup": {"audience": "builders", "autonomy": "confirm_first"},
                "repo_target": {
                    "local_path": "/tmp/customer-portal",
                    "focus_file": "src/login.tsx",
                },
            }

        async def start(
            self,
            template_key,
            brief,
            slug=None,
            extra=None,
            mission_setup=None,
            repo_target=None,
        ):
            calls["start"] = {
                "template_key": template_key,
                "brief": brief,
                "slug": slug,
                "extra": extra,
                "mission_setup": mission_setup,
                "repo_target": repo_target,
            }
            return {"slug": slug}

    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: FakeRunner())
    web_app.app.state.studio_tasks = set()

    result = await web_app.studio_start(
        {
            "template": "auto",
            "brief": "Build a habit tracker",
            "mission_setup": {"audience": "builders", "autonomy": "confirm_first"},
            "repo_target": {
                "local_path": "/tmp/customer-portal",
                "focus_file": "src/login.tsx",
            },
        }
    )
    await asyncio.sleep(0)

    assert result["accepted"] is True
    assert result["slug"] == "demo-123"
    assert result["next_action"] == "Queued — waiting for a worker slot."
    assert result["mission_setup"] == {"audience": "builders", "autonomy": "confirm_first"}
    assert result["repo_target"] == {
        "local_path": "/tmp/customer-portal",
        "focus_file": "src/login.tsx",
    }
    assert calls["reserve"]["template_key"] == "auto"
    assert calls["start"]["slug"] == "demo-123"
    assert calls["reserve"]["mission_setup"] == {
        "audience": "builders",
        "autonomy": "confirm_first",
    }
    assert calls["reserve"]["repo_target"] == {
        "local_path": "/tmp/customer-portal",
        "focus_file": "src/login.tsx",
    }
    assert calls["start"]["mission_setup"] == {
        "audience": "builders",
        "autonomy": "confirm_first",
    }
    assert calls["start"]["repo_target"] == {
        "local_path": "/tmp/customer-portal",
        "focus_file": "src/login.tsx",
    }


def test_get_studio_runner_uses_configured_projects_dir(monkeypatch, tmp_path):
    projects_dir = tmp_path / "external-projects"
    previous_runner = getattr(web_app.app.state, "studio_runner", None)

    monkeypatch.setenv("PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(web_app, "event_bus", EventBus())
    get_settings.cache_clear()

    try:
        web_app.app.state.studio_runner = None
        runner = web_app._get_studio_runner(web_app.app)
        assert runner.projects_root == projects_dir
        assert runner.projects_root.exists()
    finally:
        web_app.app.state.studio_runner = previous_runner
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_track_studio_task_marks_project_failed_on_crash():
    calls = {}

    class FakeRunner:
        def mark_project_failed(self, slug, error, *, next_action):
            calls["slug"] = slug
            calls["error"] = error
            calls["next_action"] = next_action

    async def boom():
        raise RuntimeError("runner exploded")

    web_app.app.state.studio_tasks = set()
    task = asyncio.create_task(boom())
    web_app._track_studio_task(
        task,
        runner=FakeRunner(),
        slug="demo-123",
        action="starting",
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert task.done() is True
    assert calls == {
        "slug": "demo-123",
        "error": "RuntimeError: runner exploded",
        "next_action": "Project stopped while starting.",
    }
    assert len(web_app.app.state.studio_tasks) == 0


@pytest.mark.asyncio
async def test_studio_start_rejects_focus_file_without_repo_path(monkeypatch):
    class FakeRunner:
        def reserve_project(self, *args, **kwargs):
            raise ValueError("focus file requires a repo path")

    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: FakeRunner())

    result = await web_app.studio_start(
        {
            "template": "auto",
            "brief": "Fix the login form",
            "repo_target": {"local_path": "", "focus_file": "src/login.tsx"},
        }
    )

    assert result.status_code == 400
    assert json.loads(result.body) == {"error": "focus file requires a repo path"}


@pytest.mark.asyncio
async def test_studio_project_clarify_rejects_missing_project(monkeypatch):
    class FakeRunner:
        def get_project(self, slug):
            return None

    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: FakeRunner())

    result = await web_app.studio_project_clarify("missing-project", {"answers": ["yes"]})

    assert result.status_code == 404
    assert json.loads(result.body) == {"error": "project not found"}


@pytest.mark.asyncio
async def test_proposals_list_filters_system_origin(tmp_path, monkeypatch):
    from skyn3t.cortex.proposals import ProposalStore

    store = ProposalStore(root=tmp_path / "proposals")
    store.create(
        kind="feature",
        title="Tune planner",
        summary="System proposal",
        detail="detail",
        source="feature_suggester:meta",
    )
    store.create(
        kind="feature",
        title="User idea",
        summary="User proposal",
        detail="detail",
        source="user_dashboard",
        origin="user",
    )
    monkeypatch.setattr("skyn3t.cortex.proposals._store", store)

    result = await web_app.proposals_list(status="pending", origin="system")

    assert [proposal["title"] for proposal in result["proposals"]] == ["Tune planner"]
    assert all(proposal["origin"] == "system" for proposal in result["proposals"])


@pytest.mark.asyncio
async def test_services_reset_restarts_cortex_and_replays_inflight(monkeypatch):
    calls = {"reset": 0, "cancel": 0, "resume": 0}

    class FakeStore:
        async def cancel_inflight(self):
            calls["cancel"] += 1
            return {"cancelled": 2}

        async def resume_inflight(self):
            calls["resume"] += 1
            return {"requeued": 2, "failed_no_handler": 1}

    async def reset_cortex():
        calls["reset"] += 1

    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(reset_cortex=reset_cortex),
    )

    import skyn3t.cortex as cortex_mod

    monkeypatch.setattr(cortex_mod, "get_store", lambda: FakeStore())

    result = await web_app.services_reset()

    assert result == {
        "ok": True,
        "services": ["cortex"],
        "cancelled": {"cancelled": 2},
        "replayed": {"requeued": 2, "failed_no_handler": 1},
    }
    assert calls == {"reset": 1, "cancel": 1, "resume": 1}


@pytest.mark.asyncio
async def test_rag_stats_and_recent_surface_engine_state(monkeypatch):
    class FakeVectorStore:
        def all_documents(self):
            return [
                {
                    "id": "doc-older",
                    "content": "Older chunk preview",
                    "metadata": {
                        "title": "Older doc",
                        "source": "notes.md",
                        "doc_type": "markdown",
                        "timestamp": "2026-05-09T10:00:00+00:00",
                    },
                },
                {
                    "id": "doc-newer",
                    "content": "Newest chunk preview",
                    "metadata": {
                        "title": "Newest doc",
                        "source": "latest.md",
                        "doc_type": "markdown",
                        "timestamp": "2026-05-10T10:00:00+00:00",
                    },
                },
            ]

    class FakeEngine:
        def __init__(self):
            self.vector_store = FakeVectorStore()

        async def get_stats(self):
            return {"count": 2, "embedding_model": "test-embed"}

    async def fake_get_rag_engine(_request):
        return FakeEngine()

    monkeypatch.setattr(web_app, "_get_rag_engine", fake_get_rag_engine)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    stats = await web_app.rag_stats(request)
    recent = await web_app.rag_recent(request, limit=1)

    assert stats == {"count": 2, "embedding_model": "test-embed"}
    assert recent == {
        "documents": [
            {
                "id": "doc-newer",
                "title": "Newest doc",
                "source": "latest.md",
                "doc_type": "markdown",
                "timestamp": "2026-05-10T10:00:00+00:00",
                "chunk_index": None,
                "total_chunks": None,
                "preview": "Newest chunk preview",
            }
        ]
    }


@pytest.mark.asyncio
async def test_rag_add_returns_visible_counts(monkeypatch):
    captured = {}

    class FakeEngine:
        async def add_knowledge(self, *, content, title, source, doc_type):
            captured.update(
                {
                    "content": content,
                    "title": title,
                    "source": source,
                    "doc_type": doc_type,
                }
            )
            return ["chunk-a", "chunk-b"]

        async def get_stats(self):
            return {"count": 9}

    async def fake_get_rag_engine(_request):
        return FakeEngine()

    monkeypatch.setattr(web_app, "_get_rag_engine", fake_get_rag_engine)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = await web_app.rag_add(
        request,
        {
            "content": "RAG body",
            "title": "Doc title",
            "source": "notes.md",
            "doc_type": "markdown",
        },
    )

    assert captured == {
        "content": "RAG body",
        "title": "Doc title",
        "source": "notes.md",
        "doc_type": "markdown",
    }
    assert result == {
        "ids": ["chunk-a", "chunk-b"],
        "status": "added",
        "chunks_added": 2,
        "collection_count": 9,
    }


@pytest.mark.asyncio
async def test_rag_query_builds_llm_client_for_answering(monkeypatch):
    captured = {}

    class FakeEngine:
        async def answer(self, query, llm_provider=None, n_results=5, system_prompt=None):
            captured["query"] = query
            captured["llm_provider"] = llm_provider
            captured["n_results"] = n_results
            captured["system_prompt"] = system_prompt
            return {"answer": "ok", "sources": []}

    async def fake_get_rag_engine(_request):
        return FakeEngine()

    class FakeLLMClient:
        def __init__(self, **kwargs):
            captured["llm_kwargs"] = kwargs

    monkeypatch.setattr(web_app, "_get_rag_engine", fake_get_rag_engine)
    import skyn3t.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "LLMClient", FakeLLMClient)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = await web_app.rag_query(request, {"query": "hello", "n_results": 3})

    assert result == {"answer": "ok", "sources": []}
    assert captured["query"] == "hello"
    assert isinstance(captured["llm_provider"], FakeLLMClient)
    assert captured["n_results"] == 3
    assert captured["llm_kwargs"]["caller_name"] == "rag"


@pytest.mark.asyncio
async def test_exec_agent_preserves_structured_output_response(monkeypatch):
    class FakeAgent:
        metadata = {"initialized": True}

        async def execute(self, task):
            return SimpleNamespace(
                success=True,
                output={"response": "hello from agent", "mode": "demo"},
                error=None,
                execution_time_ms=12,
            )

    fake_orchestrator = SimpleNamespace(get_agent=lambda _name: FakeAgent())
    monkeypatch.setattr(web_app, "orchestrator", fake_orchestrator)

    result = await web_app.exec_agent("demo", {"message": "hi"})

    assert result["success"] is True
    assert result["output"] == {"response": "hello from agent", "mode": "demo"}
    assert result["execution_time_ms"] == 12


@pytest.mark.asyncio
async def test_list_agents_includes_catalog_metadata(monkeypatch):
    from skyn3t.agents.verifier import VerifierAgent

    agent = VerifierAgent(event_bus=EventBus())
    await agent.initialize()
    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(agents={agent.name: agent}),
    )

    result = await web_app.list_agents()

    assert result["agents"][0]["name"] == "verifier"
    assert result["agents"][0]["tier"] == "internal"
    assert result["agents"][0]["recommended_backend"] == "claude_cli"
    assert result["agents"][0]["config"]["backend"] is None


@pytest.mark.asyncio
async def test_register_new_agent_maps_anthropic_to_claude_cli(monkeypatch):
    class FakeClaudeAgent:
        def __init__(self, name, event_bus, config=None):
            self.name = name
            self.event_bus = event_bus
            self.config = config or {}
            self.agent_type = "assistant"
            self.provider = "local"
            self.capabilities = []
            self.status = "idle"
            self._enabled = True

        async def initialize(self):
            return None

        async def start(self):
            return None

        def get_stats(self):
            return {
                "id": "agent-1",
                "name": self.name,
                "type": self.agent_type,
                "provider": self.provider,
                "status": self.status,
                "capabilities": [],
                "queue_size": 0,
                "recent_errors": 0,
                "last_task": "",
                "metadata": {},
            }

        def get_config_view(self):
            return {
                "name": self.name,
                "agent_type": self.agent_type,
                "provider": self.provider,
                "enabled": True,
                "capabilities": [],
                "config": {
                    "backend": self.config.get("backend"),
                    "model": self.config.get("model"),
                    "system_prompt": None,
                    "temperature": None,
                    "max_tokens": None,
                },
            }

    fake_orchestrator = SimpleNamespace(agents={}, event_bus=EventBus())

    def register_agent(agent):
        fake_orchestrator.agents[agent.name] = agent

    fake_orchestrator.register_agent = register_agent
    monkeypatch.setattr(web_app, "orchestrator", fake_orchestrator)

    import skyn3t.adapters.claude_cli as claude_cli

    monkeypatch.setattr(claude_cli, "ClaudeCLIAgent", FakeClaudeAgent)

    result = await web_app.register_new_agent(
        {"name": "alias-test", "provider": "anthropic", "model": "sonnet"}
    )

    assert result["status"] == "registered"
    assert result["agent"]["name"] == "alias-test"
    assert result["agent"]["config"]["model"] == "sonnet"
    assert result["agent"]["tier"] == "primary"


def test_dashboard_html_surfaces_guided_overview():
    dashboard_html = Path(web_app.__file__).with_name("dashboard.html").read_text()

    assert "Tell SkyN3t what to build" in dashboard_html
    assert "Create first project" in dashboard_html
    assert "nav-section-title\">Advanced" in dashboard_html
    assert "No setup wizard" in dashboard_html
    assert "Suggested workflows are optional" in dashboard_html
    assert "Mission setup" in dashboard_html
    assert "Codebase target" in dashboard_html
    assert "Choose the few options that really steer the run" in dashboard_html
    assert "New mission" in dashboard_html
    assert "What SkyN3t will do" in dashboard_html
    assert "Current handoff" in dashboard_html
    assert "Latest stage results" in dashboard_html
    assert "Live collaboration" in dashboard_html
    assert "Run again" in dashboard_html
    assert "Edit as new mission" in dashboard_html
    assert "studioLaunchMode" in dashboard_html
    assert "studioProjectMode" in dashboard_html
    assert "studioLiveFeed" in dashboard_html
    assert "studioArtifactFrame" in dashboard_html
    assert "studioPreviewOpen" in dashboard_html
    assert "studioRunAgain" in dashboard_html
    assert "studioQueueProjectRefresh" in dashboard_html
    assert "studioRenderMissionSetupControls" in dashboard_html
    assert "studioRepoPath" in dashboard_html
    assert "studioFocusFile" in dashboard_html
    assert "studioNormalizeRepoTarget" in dashboard_html
    assert "studioRepoTargetForLaunch" in dashboard_html
    assert "repo_target: launchRepoTarget" in dashboard_html
    assert "studioRepoTargetSummaryHtml" in dashboard_html
    assert "studioArtifactPreviewMode" in dashboard_html
    assert "studioPreviewArtifactUrl" in dashboard_html
    assert "Current SkyN3t workspace" in dashboard_html
    assert "Local git repo path or GitHub URL" in dashboard_html
    assert "another local git repo or GitHub repo URL" in dashboard_html
    assert "needs a repo path or GitHub URL before it can be pinned" in dashboard_html
    assert "focus file ignored until a repo target is set" in dashboard_html
    assert "Could not open project" in dashboard_html
    assert "No missions yet — start one above." in dashboard_html
    assert "Mission is live — open Activity or Projects to follow it." in dashboard_html
    assert "Open the mission to inspect the failure." in dashboard_html
    assert "studioClarificationThreadHtml" in dashboard_html
    assert "Reply here like chat" in dashboard_html
    assert "preview stays live while you answer" in dashboard_html
    assert "Self-update inbox" in dashboard_html
    assert "overviewProposalInbox" in dashboard_html
    assert "overview-hero-layout" in dashboard_html
    assert "overview-promise-card" in dashboard_html
    assert "overview-section-note" in dashboard_html
    assert "Recent projects" in dashboard_html
    assert "System diagnostics" in dashboard_html
    assert "Recent activity" in dashboard_html
    assert "overviewDiagnosticsBody" in dashboard_html
    assert "Mission quality" in dashboard_html
    assert "Quality signal will appear after a reviewer or verifier stage finishes." in dashboard_html
    assert "Demo preview" in dashboard_html
    assert "studioRenderQualityBadge" in dashboard_html
    assert "quality-summary-card" in dashboard_html
    assert "recent-quality" in dashboard_html
    assert "SkyN3t keeps building your projects. Its own upgrades wait here for review." in dashboard_html
    assert "turn this into a concrete execution brief" in dashboard_html
    assert "Queue for review" in dashboard_html
    assert "openProposalModal(result.proposal_id)" in dashboard_html
    assert "_proposalOrigin" in dashboard_html
    assert "const DASHBOARD_DEFAULT_PAGE = 'studio'" in dashboard_html
    assert "const OVERVIEW_DIAGNOSTICS_KEY = 'skyn3t_overview_diagnostics_open'" in dashboard_html
    assert "window.addEventListener('hashchange'" in dashboard_html
    assert "nav-item active\" onclick=\"showPage('studio')\"" in dashboard_html
    assert "id=\"page-overview\" class=\"page hidden\"" in dashboard_html
    assert "id=\"page-studio\" class=\"page\"" in dashboard_html
    assert "id=\"swarmMapToolbar\"" in dashboard_html
    assert "id=\"swarmMapLegend\"" in dashboard_html
    assert "id=\"bmZoomPct\"" in dashboard_html
    assert "swarm-map-toolbar" in dashboard_html
    assert "Activity <span>feed</span>" in dashboard_html
    assert "Projects <span>workspace</span>" in dashboard_html
    assert "Knowledge <span>base</span>" in dashboard_html
    assert "function _bmNormalizedWheelDelta" in dashboard_html
    assert "function _bmWheelFactor" in dashboard_html
    assert "data-cap-for" in dashboard_html
    assert "function _agentRows" in dashboard_html
    assert "function _visibleAgentRows" in dashboard_html
    assert "function toggleInternalAgents" in dashboard_html
    assert "function toggleOverviewDiagnostics" in dashboard_html
    assert "skyn3t_show_internal_agents" in dashboard_html
    assert "Show internal" in dashboard_html
    assert "Hide internal" in dashboard_html
    assert "Showing ${primaryRows.length} primary agents" in dashboard_html
    assert "function _appendAgentOptions" in dashboard_html
    assert "data-agent-select" in dashboard_html
    assert "data-agent-config" in dashboard_html
    assert "function _agentTierBadge" in dashboard_html
    assert "const roster = Array.isArray(data.agents)" in dashboard_html
    assert "r.tier === 'internal'" in dashboard_html
    assert "Recommended" in dashboard_html
    assert "Reset services" in dashboard_html
    assert "cortexResetServicesBtn" in dashboard_html
    assert "async function resetServices" in dashboard_html
    assert "/api/services/reset" in dashboard_html
    assert "p.status==='applying'" in dashboard_html
    assert "needs_fixes: 'var(--yellow, #f59e0b)'" in dashboard_html
    assert "interrupted: 'var(--yellow, #f59e0b)'" in dashboard_html
    assert "function studioHasActiveProjects(projects = studioState.projects)" in dashboard_html
    assert "if (!studioHasActiveProjects()) return;" in dashboard_html
    assert "ragDocCount" in dashboard_html
    assert "ragEmbeddingModel" in dashboard_html
    assert "ragRecentList" in dashboard_html
    assert "ragKnowledgeStatus" in dashboard_html
    assert "ragQueryStatus" in dashboard_html
    assert "loadRAGOverview" in dashboard_html
    assert "/api/rag/stats" in dashboard_html
    assert "/api/rag/recent" in dashboard_html
    assert "queryRAG(this)" in dashboard_html
    assert "addRAG(this)" in dashboard_html
    assert "Knowledge added" in dashboard_html
    assert "Searching..." in dashboard_html
    assert "notifyDashboardError" in dashboard_html
    assert "Could not refresh agents" in dashboard_html
    assert "Could not load swarm snapshot" in dashboard_html
    assert "stats refresh failed" in dashboard_html
    assert "healthEl.textContent = 'ERROR'" in dashboard_html
    assert "async function fetchJSON" in dashboard_html
    assert "Could not load LLM backends" in dashboard_html
    assert "Could not load agent types" in dashboard_html
    assert "Could not load cleanup preview" in dashboard_html
    assert "Could not load examples" in dashboard_html
    assert "Could not load recent missions" in dashboard_html
    assert "Could not load agent capabilities" in dashboard_html
    assert "Could not load Studio templates" in dashboard_html
    assert "Could not load Studio missions" in dashboard_html
    assert "Could not load Cortex proposals" in dashboard_html
    assert "Services reset" in dashboard_html
    assert "function refreshMetricCharts" in dashboard_html
    assert "label: 'Running tasks'" in dashboard_html
    assert "labels: ['Healthy', 'Errored']" in dashboard_html
    assert "label: 'Queue depth'" in dashboard_html
    assert "data-example-index" in dashboard_html
    assert "auto' isn't a real template" not in dashboard_html
    assert "_syncRedesignAgents();" in dashboard_html
    assert "_syncRedesignTemplates();" in dashboard_html
    assert "dashboardSetInterval(_refreshRedesignAgents" not in dashboard_html
    assert "dashboardSetInterval(_refreshRedesignTemplates" not in dashboard_html
    assert "Math.random() * 100" not in dashboard_html
    assert "onclick='startExample" not in dashboard_html
    assert "output.innerHTML = results.map" not in dashboard_html
    assert "function _agentSafeName" not in dashboard_html


def test_project_system_alert_projects_into_swarm_feed():
    projected = web_app._project_swarm_event(
        Event(
            event_type=EventType.SYSTEM_ALERT,
            source="studio",
            payload={
                "kind": "PROJECT_STAGE_STARTED",
                "project_slug": "demo-app",
                "stage": "writer",
                "summary": "Writer picked up the draft.",
            },
        )
    )

    assert projected is not None
    assert projected["kind"] == "project"
    assert projected["label"] == "writer"
    assert projected["meta"]["payload"]["project_slug"] == "demo-app"


def test_non_project_system_alert_does_not_project_into_swarm_feed():
    projected = web_app._project_swarm_event(
        Event(
            event_type=EventType.SYSTEM_ALERT,
            source="studio",
            payload={"kind": "HEARTBEAT", "message": "all good"},
        )
    )

    assert projected is None
