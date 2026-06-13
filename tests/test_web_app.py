"""Tests for web event broadcasting helpers."""

import asyncio
import io
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import skyn3t.web.app as web_app
from skyn3t.config.agent_overrides import AgentOverrideStore
from skyn3t.config.model_routing import ModelRoutingStore
from skyn3t.config.settings import get_settings
from skyn3t.core.agent import BaseAgent, TaskResult
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
async def test_llm_complete_returns_response(monkeypatch):
    class FakeLLMClient:
        def __init__(self, *, default_model=None, backend=None, event_bus=None, caller_name=None):
            self.default_model = default_model
            self.backend = backend
            self.event_bus = event_bus
            self.caller_name = caller_name

        async def complete(self, prompt, *, system=None, max_tokens=None, temperature=None, timeout=None):
            assert prompt == "hey"
            assert system == "be helpful"
            assert max_tokens == 1200
            assert temperature == 0.4
            assert timeout == 120.0
            return "hey back"

        def describe(self):
            return {"backend": self.backend or "auto", "default_model": self.default_model}

    monkeypatch.setattr("skyn3t.adapters.LLMClient", FakeLLMClient)

    result = await web_app.llm_complete(
        {
            "prompt": "hey",
            "system": "be helpful",
            "backend": "copilot_cli",
            "model": "gpt-5.4",
        }
    )

    assert result == {
        "response": "hey back",
        "backend": "copilot_cli",
        "model": "gpt-5.4",
    }


@pytest.mark.asyncio
async def test_llm_complete_prefers_openrouter_fast_path_when_unconfigured(monkeypatch):
    calls = {}

    class FakeLLMClient:
        def __init__(self, *, default_model=None, backend=None, event_bus=None, caller_name=None):
            calls["backend"] = backend
            calls["default_model"] = default_model

        async def complete(self, prompt, *, system=None, max_tokens=None, temperature=None, timeout=None):
            return "fast reply"

        def describe(self):
            return {
                "backend": calls.get("backend"),
                "default_model": calls.get("default_model"),
            }

    class FakeSettings:
        llm_backend = "auto"
        llm_model = None
        openrouter_api_key = "configured"

    monkeypatch.setattr(web_app, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr("skyn3t.adapters.LLMClient", FakeLLMClient)

    result = await web_app.llm_complete({"prompt": "hey"})

    assert result == {
        "response": "fast reply",
        "backend": "openrouter",
        "model": "openai/gpt-4.1",
    }


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
async def test_orchestrator_submit_creates_task_with_session_and_agent(monkeypatch):
    calls = {}

    class FakeOrchestrator:
        async def submit_task(self, task, agent_name=None):
            calls["task"] = task
            calls["agent_name"] = agent_name
            return "task-123"

    monkeypatch.setattr(web_app, "orchestrator", FakeOrchestrator())

    result = await web_app.orchestrator_submit(
        {"prompt": "Investigate latency", "session_id": "sess-1", "agent_name": "writer"}
    )

    assert result == {"task_id": "task-123", "status": "submitted"}
    assert calls["agent_name"] == "writer"
    assert calls["task"].description == "Investigate latency"
    assert calls["task"].input_data == {"message": "Investigate latency"}
    assert calls["task"].session_id == "sess-1"


@pytest.mark.asyncio
async def test_cancel_task_returns_cancelled_status(monkeypatch):
    class FakeOrchestrator:
        def cancel_task(self, task_id):
            return task_id == "task-123"

    monkeypatch.setattr(web_app, "orchestrator", FakeOrchestrator())

    result = await web_app.cancel_task("task-123")

    assert result == {"task_id": "task-123", "status": "cancelled"}


@pytest.mark.asyncio
async def test_cancel_task_returns_404_when_task_missing(monkeypatch):
    class FakeOrchestrator:
        def cancel_task(self, task_id):
            return False

    monkeypatch.setattr(web_app, "orchestrator", FakeOrchestrator())

    result = await web_app.cancel_task("missing")

    assert result.status_code == 404
    assert json.loads(result.body) == {"error": "Task 'missing' not found"}


@pytest.mark.asyncio
async def test_memory_layers_combines_session_operator_and_project(monkeypatch):
    import skyn3t.intelligence.skill_library as skill_library_mod

    class FakeStore:
        async def get_stats(self):
            return {"tasks": 12, "messages": 7, "knowledge_documents": 3, "success_rate": 0.75}

        async def get_lessons(self, limit=5):
            return [{"title": "Landing page", "source": "repo", "doc_type": "lesson", "created_at": "now"}]

    class FakeConsciousness:
        async def list_sessions(self):
            return ["sess-1", "sess-2"]

        async def get_insights(self, limit=5):
            return [{"agent": "writer", "capability": "ui", "insight": "Use cards", "timestamp": 1.0}]

    class FakeSkill:
        def __init__(self):
            self.name = "layout-skill"
            self.score = 0.9
            self.tags = ["ui"]
            self.source = "learned"

    class FakeLibrary:
        def summary(self):
            return {"total": 4}

        def find(self, min_score=0.1, limit=5):
            return [FakeSkill()]

    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore(), _consciousness=FakeConsciousness()),
    )
    monkeypatch.setattr(skill_library_mod, "get_default_library", lambda: FakeLibrary())

    result = await web_app.memory_layers(limit=5)

    assert result["enabled"] is True
    assert result["layers"]["session"]["active_sessions"] == 2
    assert result["layers"]["operator"]["skill_summary"] == {"total": 4}
    assert result["layers"]["project"]["tasks"] == 12
    assert result["layers"]["project"]["recent_documents"][0]["title"] == "Landing page"


@pytest.mark.asyncio
async def test_memory_drafts_lists_pending_docs(monkeypatch):
    class FakeStore:
        async def list_knowledge_drafts(self, review_status="draft", doc_type=None, limit=20):
            assert review_status == "draft"
            assert doc_type is None
            assert limit == 20
            return [
                {
                    "id": "draft-1",
                    "title": "Lesson draft",
                    "doc_type": "lesson",
                    "source": "reflection",
                    "meta": {"review_status": "draft", "memory_layer": "operator"},
                }
            ]

    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )

    result = await web_app.memory_drafts()

    assert result["drafts"][0]["id"] == "draft-1"


@pytest.mark.asyncio
async def test_memory_evaluations_lists_assets_by_status(monkeypatch):
    class FakeStore:
        async def list_knowledge_drafts(self, review_status="draft", doc_type=None, limit=20):
            assert review_status == "approved"
            assert doc_type == "evaluation"
            assert limit == 20
            return [
                {
                    "id": "eval-1",
                    "title": "External eval",
                    "doc_type": "evaluation",
                    "content": "Evaluation Asset",
                    "meta": {
                        "review_status": "approved",
                        "lane": "fit",
                        "language": "python",
                        "patterns": ["cortex", "autonomy"],
                        "checks": ["Confirm shared signals."],
                        "source_repos": ["org/repo-a", "org/repo-b"],
                    },
                }
            ]

    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )

    result = await web_app.memory_evaluations(status="approved")

    assert result["status"] == "approved"
    assert result["evaluations"][0]["id"] == "eval-1"
    assert result["evaluations"][0]["checks"] == ["Confirm shared signals."]


@pytest.mark.asyncio
async def test_memory_evaluation_export_requires_approval(monkeypatch):
    class FakeStore:
        async def get_knowledge_doc(self, doc_id):
            assert doc_id == "eval-1"
            return {
                "id": "eval-1",
                "title": "External eval",
                "doc_type": "evaluation",
                "content": "Evaluation Asset",
                "meta": {"review_status": "draft", "checks": ["Confirm shared signals."]},
            }

    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )

    result = await web_app.memory_evaluation_export("eval-1")

    assert result.status_code == 400
    assert json.loads(result.body) == {"error": "evaluation asset must be approved before export"}


@pytest.mark.asyncio
async def test_memory_evaluation_export_jsonl_returns_ndjson(monkeypatch):
    class FakeStore:
        async def get_knowledge_doc(self, doc_id):
            assert doc_id == "eval-1"
            return {
                "id": "eval-1",
                "title": "External eval",
                "doc_type": "evaluation",
                "content": "Evaluation Asset",
                "meta": {
                    "review_status": "approved",
                    "lane": "fit",
                    "language": "python",
                    "patterns": ["cortex", "autonomy"],
                    "checks": ["Confirm shared signals."],
                    "source_doc_ids": ["doc-1"],
                    "source_repos": ["org/repo-a", "org/repo-b"],
                    "source_platform": "external",
                    "synthesis_key": "external-eval:fit:python",
                    "consensus_count": 2,
                    "memory_layer": "project",
                    "confidence": 0.6,
                },
            }

    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )

    result = await web_app.memory_evaluation_export("eval-1", format="jsonl")

    assert result.media_type == "application/x-ndjson"
    exported = json.loads(result.body)
    assert exported["kind"] == "evaluation_asset"
    assert exported["evaluation"]["checks"] == ["Confirm shared signals."]


@pytest.mark.asyncio
async def test_export_trajectories_can_bundle_approved_evaluations(monkeypatch, tmp_path):
    class FakeTrajectoryLogger:
        def export_jsonl(self, output_path, **kwargs):
            assert kwargs["agent"] == "designer"
            output_path.write_text(json.dumps({"task_id": "t-1", "agent": "designer"}) + "\n", encoding="utf-8")
            return 1

    class FakeStore:
        async def list_knowledge_drafts(
            self,
            review_status="draft",
            doc_type=None,
            limit=20,
            preview_only=True,
        ):
            assert review_status == "approved"
            assert doc_type == "evaluation"
            assert preview_only is False
            return [
                {
                    "id": "eval-1",
                    "title": "External eval",
                    "doc_type": "evaluation",
                    "content": "Evaluation Asset full content",
                    "meta": {
                        "review_status": "approved",
                        "lane": "fit",
                        "language": "python",
                        "patterns": ["cortex"],
                        "checks": ["Confirm shared signals."],
                        "source_doc_ids": ["doc-1"],
                        "source_repos": ["org/repo-a"],
                        "source_platform": "external",
                        "synthesis_key": "external-eval:fit:python",
                        "consensus_count": 1,
                        "memory_layer": "project",
                        "confidence": 0.6,
                    },
                }
            ]

    monkeypatch.setattr(web_app, "trajectory_logger", FakeTrajectoryLogger())
    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )

    result = await web_app.export_trajectories(agent="designer", include_evaluations=True)

    body_path = Path(result.path)
    lines = body_path.read_text(encoding="utf-8").strip().splitlines()
    body_path.unlink(missing_ok=True)

    assert result.filename.startswith("trajectories_bundle_")
    assert json.loads(lines[0]) == {"task_id": "t-1", "agent": "designer"}
    assert json.loads(lines[1])["kind"] == "evaluation_asset"
    assert json.loads(lines[1])["evaluation"]["content"] == "Evaluation Asset full content"
    assert json.loads(lines[2])["kind"] == "bundle_manifest"
    assert json.loads(lines[2])["trajectory_count"] == 1
    assert json.loads(lines[2])["evaluation_count"] == 1


@pytest.mark.asyncio
async def test_export_trajectories_can_return_eval_only_bundle(monkeypatch):
    class FakeTrajectoryLogger:
        def export_jsonl(self, output_path, **kwargs):
            output_path.write_text("", encoding="utf-8")
            return 0

    class FakeStore:
        async def list_knowledge_drafts(
            self,
            review_status="draft",
            doc_type=None,
            limit=20,
            preview_only=True,
        ):
            return [
                {
                    "id": "eval-1",
                    "title": "External eval",
                    "doc_type": "evaluation",
                    "content": "Evaluation Asset full content",
                    "meta": {
                        "review_status": "approved",
                        "patterns": ["cortex"],
                        "checks": ["Confirm shared signals."],
                    },
                }
            ]

    monkeypatch.setattr(web_app, "trajectory_logger", FakeTrajectoryLogger())
    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )

    result = await web_app.export_trajectories(include_evaluations=True)

    body_path = Path(result.path)
    lines = body_path.read_text(encoding="utf-8").strip().splitlines()
    body_path.unlink(missing_ok=True)

    assert len(lines) == 2
    assert json.loads(lines[0])["kind"] == "evaluation_asset"
    assert json.loads(lines[1])["kind"] == "bundle_manifest"
    assert json.loads(lines[1])["trajectory_count"] == 0
    assert json.loads(lines[1])["evaluation_count"] == 1


@pytest.mark.asyncio
async def test_memory_draft_approve_promotes_into_rag(monkeypatch):
    calls = {}

    class FakeStore:
        async def get_knowledge_doc(self, doc_id):
            assert doc_id == "draft-1"
            return {
                "id": "draft-1",
                "title": "Lesson draft",
                "content": "Teach the planner to retry less",
                "source": "reflection",
                "doc_type": "lesson",
                "embedding_id": None,
                "meta": {"review_status": "draft", "memory_layer": "operator"},
            }

        async def update_knowledge_doc_review(self, doc_id, **kwargs):
            calls["doc_id"] = doc_id
            calls["kwargs"] = kwargs
            return {
                "id": doc_id,
                "title": "Lesson draft",
                "embedding_id": kwargs["embedding_id"],
                "meta": {"review_status": "approved"},
            }

    class FakeEngine:
        async def add_knowledge_one(self, **kwargs):
            calls["rag"] = kwargs
            return "emb-1"

    async def fake_get_rag_engine(_request):
        return FakeEngine()

    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )
    monkeypatch.setattr(web_app, "_get_rag_engine", fake_get_rag_engine)

    result = await web_app.memory_draft_approve("draft-1", SimpleNamespace(app=SimpleNamespace()))

    assert result["ok"] is True
    assert result["draft"]["embedding_id"] == "emb-1"
    assert calls["rag"]["doc_type"] == "lesson"
    assert calls["kwargs"]["review_status"] == "approved"


@pytest.mark.asyncio
async def test_memory_draft_reject_marks_doc_rejected(monkeypatch):
    class FakeStore:
        async def get_knowledge_doc(self, doc_id):
            assert doc_id == "draft-2"
            return {
                "id": "draft-2",
                "title": "Bad draft",
                "meta": {"review_status": "draft"},
            }

        async def update_knowledge_doc_review(self, doc_id, **kwargs):
            assert doc_id == "draft-2"
            assert kwargs["review_status"] == "rejected"
            assert kwargs["reason"] == "too generic"
            return {"id": doc_id, "title": "Bad draft", "meta": {"review_status": "rejected"}}

    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )

    result = await web_app.memory_draft_reject("draft-2", {"reason": "too generic"})

    assert result["ok"] is True
    assert result["draft"]["meta"]["review_status"] == "rejected"


@pytest.mark.asyncio
async def test_skill_candidates_lists_approved_memory_docs(monkeypatch):
    class FakeStore:
        async def list_skill_candidate_docs(self, min_confidence=0.7, limit=20):
            assert min_confidence == 0.7
            assert limit == 20
            return [{"id": "mem-1", "title": "Skillable lesson", "doc_type": "lesson", "meta": {}}]

    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )

    result = await web_app.get_skill_candidates()

    assert result["candidates"][0]["id"] == "mem-1"


@pytest.mark.asyncio
async def test_create_skill_draft_from_memory_marks_doc_status(monkeypatch, tmp_path):
    import skyn3t.intelligence.skill_library as skill_library_mod

    calls = {}

    class FakeStore:
        async def get_knowledge_doc(self, doc_id):
            assert doc_id == "mem-1"
            return {
                "id": "mem-1",
                "title": "Insight from architect",
                "content": "Agent: architect\nInsight: Prefer one boundary.\n",
                "source": "reflection",
                "doc_type": "insight",
                "meta": {"review_status": "approved", "reusable": True, "confidence": 0.8},
            }

        async def merge_knowledge_doc_meta(self, doc_id, extra_meta):
            calls["doc_id"] = doc_id
            calls["extra_meta"] = extra_meta
            return {"id": doc_id, "meta": extra_meta}

    lib = skill_library_mod.SkillLibrary(root=tmp_path / "skills")
    monkeypatch.setattr(skill_library_mod, "get_default_library", lambda: lib)
    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )

    result = await web_app.create_skill_draft_from_memory("mem-1")

    assert result["created"] is True
    assert result["draft"]["memory_doc_id"] == "mem-1"
    assert calls["extra_meta"]["skill_promotion_status"] == "draft"
    assert lib.all_drafts()


@pytest.mark.asyncio
async def test_create_skill_draft_from_external_memory_requires_ingested_docs(monkeypatch):
    class FakeStore:
        async def get_knowledge_doc(self, doc_id):
            assert doc_id == "mem-external"
            return {
                "id": "mem-external",
                "title": "External learning summary",
                "content": "summary",
                "source": "repo_scout:gitlab",
                "doc_type": "external_learning",
                "meta": {
                    "review_status": "approved",
                    "reusable": True,
                    "confidence": 0.8,
                    "external_doc_ingest_status": "summary_only",
                    "external_doc_paths_ingested": [],
                },
            }

    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )

    result = await web_app.create_skill_draft_from_memory("mem-external")

    assert result.status_code == 400
    assert json.loads(result.body) == {
        "error": "external learning document must ingest approved docs first"
    }


@pytest.mark.asyncio
async def test_create_skill_draft_from_evaluation_is_blocked(monkeypatch):
    class FakeStore:
        async def get_knowledge_doc(self, doc_id):
            assert doc_id == "eval-asset"
            return {
                "id": "eval-asset",
                "title": "External eval",
                "content": "Evaluation Asset",
                "source": "external_pattern_synthesizer",
                "doc_type": "evaluation",
                "meta": {"review_status": "approved", "reusable": True, "confidence": 0.9},
            }

    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )

    result = await web_app.create_skill_draft_from_memory("eval-asset")

    assert result.status_code == 400
    assert json.loads(result.body) == {"error": "evaluation assets cannot be promoted into skills"}


@pytest.mark.asyncio
async def test_approve_skill_draft_installs_and_updates_memory(monkeypatch, tmp_path):
    import skyn3t.intelligence.skill_library as skill_library_mod

    calls = {}

    class FakeStore:
        async def merge_knowledge_doc_meta(self, doc_id, extra_meta):
            calls["doc_id"] = doc_id
            calls["extra_meta"] = extra_meta
            return {"id": doc_id, "meta": extra_meta}

    lib = skill_library_mod.SkillLibrary(root=tmp_path / "skills")
    lib.upsert_draft(
        skill_library_mod.Skill(
            name="draft install",
            body="# body",
            tags=["memory-promoted"],
            memory_doc_id="mem-2",
        )
    )
    monkeypatch.setattr(skill_library_mod, "get_default_library", lambda: lib)
    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )

    result = await web_app.approve_skill_draft("draft-install")

    assert result["installed"] == "draft-install"
    assert calls["doc_id"] == "mem-2"
    assert calls["extra_meta"]["skill_promotion_status"] == "installed"
    assert lib.find(tag="memory-promoted", min_score=-1.0)


@pytest.mark.asyncio
async def test_reject_skill_draft_updates_memory(monkeypatch, tmp_path):
    import skyn3t.intelligence.skill_library as skill_library_mod

    calls = {}

    class FakeStore:
        async def merge_knowledge_doc_meta(self, doc_id, extra_meta):
            calls["doc_id"] = doc_id
            calls["extra_meta"] = extra_meta
            return {"id": doc_id, "meta": extra_meta}

    lib = skill_library_mod.SkillLibrary(root=tmp_path / "skills")
    lib.upsert_draft(
        skill_library_mod.Skill(
            name="draft reject",
            body="# body",
            tags=["memory-promoted"],
            memory_doc_id="mem-3",
        )
    )
    monkeypatch.setattr(skill_library_mod, "get_default_library", lambda: lib)
    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore(), _memory=FakeStore()),
    )

    result = await web_app.reject_skill_draft("draft-reject", {"reason": "too generic"})

    assert result["rejected"] is True
    assert calls["doc_id"] == "mem-3"
    assert calls["extra_meta"]["skill_promotion_status"] == "rejected"
    assert calls["extra_meta"]["skill_promotion_reason"] == "too generic"


@pytest.mark.asyncio
async def test_github_scout_config_returns_auto_mode(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "skyn3t.config.settings.get_settings",
        lambda: SimpleNamespace(cortex_scout_default_limit=3),
    )

    result = await web_app.github_scout_config()

    assert result["mode"] == "auto"
    assert result["default_limit"] == 3
    assert "trending" in result["discovery_lanes"]


@pytest.mark.asyncio
async def test_run_github_scout_returns_component_result(monkeypatch):
    class FakeScout:
        is_running = False

        def start_background(self, config):
            assert config == {"cadence": "weekly", "limit": 2, "platforms": ["github"]}
            return {"ok": True, "started": True, "state": "running"}

    async def fake_ensure():
        return FakeScout()

    monkeypatch.setattr(web_app, "orchestrator", SimpleNamespace(running_tasks={}))
    monkeypatch.setattr(web_app, "_ensure_repo_scout", fake_ensure)
    monkeypatch.setattr(web_app, "_count_active_studio_projects", lambda _app: 0)

    response = await web_app.run_github_scout({"cadence": "weekly", "limit": 2})

    assert response.status_code == 202
    body = json.loads(response.body)
    assert body["started"] is True
    assert body["state"] == "running"


@pytest.mark.asyncio
async def test_run_github_scout_skips_when_studio_busy(monkeypatch):
    class FakeScout:
        is_running = False

        def start_background(self, config):
            raise AssertionError("should not start when studio busy")

    async def fake_ensure():
        return FakeScout()

    monkeypatch.setattr(web_app, "orchestrator", SimpleNamespace(running_tasks={}))
    monkeypatch.setattr(web_app, "_ensure_repo_scout", fake_ensure)
    monkeypatch.setattr(web_app, "_count_active_studio_projects", lambda _app: 2)

    response = await web_app.run_github_scout({"limit": 1})

    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["reason"] == "system_busy"


@pytest.mark.asyncio
async def test_schedule_github_scout_persists_job(monkeypatch):
    calls = {}

    class FakeStore:
        async def save_scheduled_job(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore()),
    )

    result = await web_app.schedule_github_scout(
        {
            "schedule_expr": "daily at 09:00",
            "cadence": "daily",
            "limit": 3,
            "queries": ["agent cli memory"],
        }
    )

    assert result["created"] is True
    assert calls["agent_name"] == "github_repo_scout"
    config = json.loads(calls["prompt"])
    assert config["cadence"] == "daily"
    assert config["limit"] == 3
    assert config["queries"] == ["agent cli memory"]
    assert config["platforms"] == ["github"]


@pytest.mark.asyncio
async def test_run_repo_scout_defaults_to_multi_platform(monkeypatch):
    class FakeScout:
        is_running = False

        def start_background(self, config):
            assert config["platforms"] == ["github", "gitlab", "bitbucket"]
            assert config["cadence"] == "weekly"
            return {"ok": True, "started": True, "state": "running"}

    async def fake_ensure():
        return FakeScout()

    monkeypatch.setattr(web_app, "orchestrator", SimpleNamespace(running_tasks={}))
    monkeypatch.setattr(web_app, "_ensure_repo_scout", fake_ensure)
    monkeypatch.setattr(web_app, "_count_active_studio_projects", lambda _app: 0)

    response = await web_app.run_repo_scout({"cadence": "weekly"})

    assert response.status_code == 202
    body = json.loads(response.body)
    assert body["started"] is True


@pytest.mark.asyncio
async def test_schedule_repo_scout_persists_platforms(monkeypatch):
    calls = {}

    class FakeStore:
        async def save_scheduled_job(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(memory_store=FakeStore()),
    )

    result = await web_app.schedule_repo_scout(
        {
            "schedule_expr": "weekly",
            "cadence": "weekly",
            "limit": 2,
            "platforms": ["gitlab", "bitbucket"],
        }
    )

    assert result["created"] is True
    config = json.loads(calls["prompt"])
    assert config["platforms"] == ["gitlab", "bitbucket"]
    assert config["cadence"] == "weekly"


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
async def test_studio_project_clarify_rejects_non_waiting_project(monkeypatch):
    class FakeRunner:
        def get_project(self, slug):
            return {
                "slug": slug,
                "status": "running",
                "clarification": {"questions": ["What kind of thing is this?"]},
            }

    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: FakeRunner())

    result = await web_app.studio_project_clarify(
        "demo",
        {"answers": ["Web app (works in a browser)"]},
    )

    assert result.status_code == 409
    assert json.loads(result.body)["error"] == "project is not waiting for clarification"


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
async def test_cortex_status_reports_handlers_and_components(monkeypatch):
    fake_status = {
        "running": True,
        "booted": True,
        "components": [
            {
                "name": "gated_tuner",
                "class_name": "GatedTuner",
                "started": True,
                "subscriptions": ["SYSTEM_ALERT:tuning_suggestion"],
                "creates_proposals": ["tuning"],
                "handles_proposals": ["tuning"],
                "details": {"config_path": "data/config/runtime.json"},
                "error": None,
            }
        ],
        "proposal_handlers": ["feature", "studio_debug", "tuning"],
        "proposal_counts": {"pending": 2, "failed": 1},
        "recent_failures": [
            {"id": "p1", "kind": "tuning", "title": "Tune claude", "error": "boom"}
        ],
        "warnings": [],
    }
    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(get_cortex_status=lambda: fake_status),
    )

    result = await web_app.cortex_status()

    assert result == fake_status


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

        def recent_documents(self, limit: int):
            docs = self.all_documents()
            docs.sort(
                key=lambda d: d["metadata"].get("timestamp", ""), reverse=True
            )
            return docs[:limit]

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
    assert result["agents"][0]["effective_backend"] == "openrouter"
    assert result["agents"][0]["effective_model"] == "google/gemini-3.1-flash-lite"


@pytest.mark.asyncio
async def test_list_agents_surfaces_effective_policy_route(monkeypatch):
    class RoutedAgent(BaseAgent):
        async def initialize(self):
            return None

        async def execute(self, task, stdin_data=None):
            return TaskResult(task_id=task.task_id, success=True, output={})

        async def health_check(self):
            return True

    agent = RoutedAgent(
        name="reviewer",
        agent_type="review",
        provider="local",
        event_bus=EventBus(),
    )
    monkeypatch.setattr(
        web_app,
        "orchestrator",
        SimpleNamespace(agents={agent.name: agent}),
    )

    result = await web_app.list_agents()

    # reviewer is a reasoning stage → strong tier → claude_cli/opus
    # (owner directive 2026-06-11); effective route is sourced from policy.
    assert result["agents"][0]["backend"] == "claude_cli"
    assert result["agents"][0]["model"] == "opus"
    assert result["agents"][0]["effective_backend"] == "claude_cli"
    assert result["agents"][0]["effective_model"] == "opus"


@pytest.mark.asyncio
async def test_routing_policy_get_returns_routes_and_tiers(monkeypatch, tmp_path):
    import skyn3t.config.model_routing as routing_config

    store = ModelRoutingStore(tmp_path / "routing.json")
    store.set_entries({"reviewer": {"tier": "or_cheap", "applied_via": "recommendation"}})
    monkeypatch.setattr(routing_config, "_store", store)

    result = await web_app.routing_policy_get()

    assert any(route["stage"] == "reviewer" for route in result["routes"])
    assert any(tier["name"] == "or_cheap" for tier in result["tiers"])
    reviewer = next(route for route in result["routes"] if route["stage"] == "reviewer")
    assert reviewer["persisted_via"] == "recommendation"


@pytest.mark.asyncio
async def test_routing_recommendations_get_returns_rows(monkeypatch):
    monkeypatch.setattr(
        "skyn3t.intelligence.routing_recommendations.list_stage_recommendations",
        lambda: [
            {
                "stage": "brainstorm",
                "current_tier": "or_strong",
                "recommended_tier": "or_cheap",
                "recommendation_kind": "cheaper",
                "confidence": "medium",
                "reasons": ["Heavy-stage signal detected."],
                "signals": {"trajectory_samples": 8},
                "applyable": True,
            }
        ],
    )

    result = await web_app.routing_recommendations_get()

    assert result["recommendations"][0]["stage"] == "brainstorm"
    assert result["recommendations"][0]["recommended_tier"] == "or_cheap"
    assert result["recommendations"][0]["applyable"] is True


@pytest.mark.asyncio
async def test_routing_policy_patch_invalidates_live_agent_llm(monkeypatch, tmp_path):
    import skyn3t.config.model_routing as routing_config

    class RoutedAgent(BaseAgent):
        async def initialize(self):
            return None

        async def execute(self, task, stdin_data=None):
            return TaskResult(task_id=task.task_id, success=True, output={})

        async def health_check(self):
            return True

    monkeypatch.setattr(routing_config, "_store", ModelRoutingStore(tmp_path / "routing.json"))
    agent = RoutedAgent(
        name="research_agent",
        agent_type="research",
        provider="test",
        event_bus=EventBus(),
        config={},
    )
    before = agent.llm
    assert before is not None
    assert before._backend_name == "openrouter"

    monkeypatch.setattr(web_app, "orchestrator", SimpleNamespace(agents={agent.name: agent}))

    result = await web_app.routing_policy_patch({"policies": {"research": "balanced"}})

    assert result["ok"] is True
    assert agent._llm is None
    after = agent.llm
    assert after is not None
    assert after._backend_name == "claude_cli"


@pytest.mark.asyncio
async def test_routing_policy_patch_accepts_recommendation_metadata(monkeypatch, tmp_path):
    import skyn3t.config.model_routing as routing_config

    store = ModelRoutingStore(tmp_path / "routing.json")
    monkeypatch.setattr(routing_config, "_store", store)
    monkeypatch.setattr(web_app, "orchestrator", SimpleNamespace(agents={}))

    result = await web_app.routing_policy_patch(
        {"policies": {"research": {"tier": "or_cheap", "applied_via": "recommendation"}}}
    )

    assert result["ok"] is True
    route = next(route for route in result["routes"] if route["stage"] == "research")
    assert route["tier"] == "or_cheap"
    assert route["persisted_via"] == "recommendation"


@pytest.mark.asyncio
async def test_routing_policy_get_includes_studio_quality_preset(monkeypatch, tmp_path):
    import skyn3t.config.model_routing as routing_config

    monkeypatch.setattr(
        routing_config,
        "_store",
        ModelRoutingStore(tmp_path / "routing.json"),
    )

    result = await web_app.routing_policy_get()

    preset = result["presets"]["studio_quality"]
    assert preset["label"]
    assert preset["policies"]["code"] == "or_strong"
    assert preset["policies"]["designer"] == "or_ui"


@pytest.mark.asyncio
async def test_routing_preset_studio_quality_applies_policies(monkeypatch, tmp_path):
    import skyn3t.config.model_routing as routing_config

    store = ModelRoutingStore(tmp_path / "routing.json")
    monkeypatch.setattr(routing_config, "_store", store)
    monkeypatch.setattr(web_app, "orchestrator", SimpleNamespace(agents={}))

    result = await web_app.routing_preset_studio_quality()

    assert result["ok"] is True
    assert store.entries()["reviewer"]["tier"] == "strong"
    reviewer = next(route for route in result["routes"] if route["stage"] == "reviewer")
    assert reviewer["source"] == "persisted"


@pytest.mark.asyncio
async def test_execution_backend_get_reports_inline(monkeypatch):
    monkeypatch.setenv("SKYN3T_EXECUTION_BACKEND", "inline")
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()

    result = await web_app.execution_backend_get()

    assert result["configured"] == "inline"
    assert result["resolved_class"] == "InlineBackend"
    assert "auto" in result["valid_backends"]


@pytest.mark.asyncio
async def test_execution_backend_patch_writes_env(monkeypatch, tmp_path):
    from skyn3t.config.settings import get_settings

    env_path = tmp_path / ".env"
    env_path.write_text("SKYN3T_EXECUTION_BACKEND=inline\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKYN3T_EXECUTION_BACKEND", "inline")
    get_settings.cache_clear()

    result = await web_app.execution_backend_patch({"backend": "auto"})

    assert result["ok"] is True
    assert "SKYN3T_EXECUTION_BACKEND=auto" in env_path.read_text(encoding="utf-8")
    assert os.environ["SKYN3T_EXECUTION_BACKEND"] == "auto"


@pytest.mark.asyncio
async def test_studio_project_penpot_manifest_returns_design_handoff(monkeypatch, tmp_path):
    project_dir = tmp_path / "demo"
    project_dir.mkdir(parents=True)
    (project_dir / "palette.json").write_text(
        json.dumps(
            {
                "primary": "#112233",
                "secondary": "#223344",
                "accent": "#334455",
                "bg": "#0B1020",
                "text": "#F8FAFC",
            }
        ),
        encoding="utf-8",
    )
    (project_dir / "tokens.json").write_text(
        json.dumps(
            {
                "font": {
                    "heading": {"value": "Inter", "type": "fontFamily"},
                    "body": {"value": "Inter", "type": "fontFamily"},
                    "mono": {"value": "JetBrains Mono", "type": "fontFamily"},
                }
            }
        ),
        encoding="utf-8",
    )

    class FakeRunner:
        projects_root = tmp_path

        def get_project(self, slug):
            return {"slug": slug} if slug == "demo" else None

    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: FakeRunner())

    result = await web_app.studio_project_penpot_manifest("demo")

    assert result["tool_target"] == "penpot"
    assert result["handoff_kind"] == "design_tokens_package"
    assert result["colors"][0]["name"] == "primary"


@pytest.mark.asyncio
async def test_studio_project_penpot_package_returns_zip(monkeypatch, tmp_path):
    project_dir = tmp_path / "demo"
    project_dir.mkdir(parents=True)
    (project_dir / "brand.md").write_text("# Brand\n", encoding="utf-8")
    (project_dir / "tokens.json").write_text('{"font":{}}', encoding="utf-8")

    class FakeRunner:
        projects_root = tmp_path

        def get_project(self, slug):
            return {"slug": slug} if slug == "demo" else None

    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: FakeRunner())

    response = await web_app.studio_project_penpot_package("demo")

    assert response.media_type == "application/zip"
    assert "penpot-handoff.zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        names = archive.namelist()
    assert "penpot_manifest.json" in names
    assert "brand.md" in names


@pytest.mark.asyncio
async def test_agent_config_reset_clears_backend_model_only(monkeypatch, tmp_path):
    import skyn3t.config.agent_overrides as override_config

    class ResettableAgent(BaseAgent):
        async def initialize(self):
            return None

        async def execute(self, task, stdin_data=None):
            return TaskResult(task_id=task.task_id, success=True, output={})

        async def health_check(self):
            return True

    store = AgentOverrideStore(tmp_path / "agent_overrides.json")
    monkeypatch.setattr(override_config, "_store", store)
    store.set(
        "reviewer",
        {
            "backend": "claude_cli",
            "model": "opus",
            "temperature": 0.4,
        },
    )
    agent = ResettableAgent(
        name="reviewer",
        agent_type="reviewer",
        provider="test",
        event_bus=EventBus(),
        config={
            "backend": "claude_cli",
            "model": "opus",
            "temperature": 0.4,
        },
    )
    monkeypatch.setattr(web_app, "orchestrator", SimpleNamespace(agents={agent.name: agent}))

    result = await web_app.agent_config_reset("reviewer", {"keys": ["backend", "model"]})

    assert sorted(result["changed"]) == ["backend", "model"]
    assert "backend" not in agent.config
    assert "model" not in agent.config
    assert agent.config["temperature"] == 0.4
    assert store.get("reviewer") == {"temperature": 0.4}
    # reviewer policy route → strong tier → claude_cli (2026-06-11 directive)
    assert result["config_view"]["effective_backend"] == "claude_cli"
    assert result["config_view"]["effective_source"] == "policy"


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


def test_spa_shell_served_at_root():
    """The legacy dashboard.html has been removed; root should serve the
    built Vite+React SPA (or a placeholder if dist is missing)."""
    client = TestClient(web_app.app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # If the SPA is built, the shell is present; otherwise we get the
    # minimal placeholder.
    assert "SkyN3t" in body


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


# ─── Phase 3 critical security regressions ───────────────────────────────

@pytest.mark.asyncio
async def test_execution_backend_patch_rejects_inline_without_optin(monkeypatch):
    """C1: PATCH /api/execution/backend must refuse to persist inline unless
    SKYN3T_ALLOW_INLINE_EXEC is set."""
    monkeypatch.delenv("SKYN3T_ALLOW_INLINE_EXEC", raising=False)
    get_settings.cache_clear()

    result = await web_app.execution_backend_patch({"backend": "inline"})

    assert isinstance(result, web_app.JSONResponse)
    assert result.status_code == 403
    body = json.loads(result.body)
    assert "disabled" in body["error"].lower()


def test_exec_endpoint_disabled_by_default(monkeypatch):
    """POST /api/exec is off unless SKYN3T_ALLOW_EXEC_API=1."""
    monkeypatch.delenv("SKYN3T_ALLOW_EXEC_API", raising=False)
    monkeypatch.setenv("SKYN3T_ALLOW_UNAUTHENTICATED_LOOPBACK", "1")
    get_settings.cache_clear()

    client = TestClient(web_app.app)
    response = client.post(
        "/api/exec",
        json={"code": "print(1)", "language": "python", "timeout": 10},
    )

    assert response.status_code == 403
    assert "disabled" in response.json()["error"].lower()


def test_exec_endpoint_rejects_inline_without_optin(monkeypatch):
    """C1: POST /api/exec must not run in-process when inline is not opted in."""
    monkeypatch.setenv("SKYN3T_ALLOW_EXEC_API", "1")
    monkeypatch.setenv("SKYN3T_EXECUTION_BACKEND", "inline")
    monkeypatch.delenv("SKYN3T_ALLOW_INLINE_EXEC", raising=False)
    monkeypatch.setenv("SKYN3T_ALLOW_UNAUTHENTICATED_LOOPBACK", "1")
    get_settings.cache_clear()

    client = TestClient(web_app.app)
    response = client.post(
        "/api/exec",
        json={"code": "print(1)", "language": "python", "timeout": 10},
    )

    assert response.status_code == 403
    assert "disabled" in response.json()["error"].lower()


@pytest.mark.asyncio
async def test_studio_start_rejects_path_traversal_slug(monkeypatch):
    """C2: A caller-supplied slug that escapes projects_root must be rejected."""
    calls = {}

    class FakeRunner:
        def reserve_project(self, *args, **kwargs):
            calls["reserve"] = kwargs.get("slug")
            raise ValueError("project slug escapes projects root: ../evil")

    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: FakeRunner())
    web_app.app.state.studio_tasks = set()

    result = await web_app.studio_start(
        {"template": "auto", "brief": "x", "slug": "../evil"}
    )

    assert isinstance(result, web_app.JSONResponse)
    assert result.status_code == 400
    assert "escapes" in json.loads(result.body)["error"]
