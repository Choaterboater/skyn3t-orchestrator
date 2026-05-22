"""Tests for Cortex proposal storage."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.cortex.proposals import ProposalStore


def test_proposal_store_loads_legacy_records_without_origin(tmp_path):
    root = tmp_path / "proposals"
    (root / "pending").mkdir(parents=True)
    (root / "decided").mkdir(parents=True)
    (root / "pending" / "legacy.json").write_text(
        json.dumps(
            {
                "id": "legacy123",
                "kind": "feature",
                "title": "Legacy proposal",
                "summary": "Still pending",
                "detail": "Old file without origin field",
                "payload": {},
                "source": "feature_suggester:meta",
                "status": "pending",
                "created_at": time.time(),
                "decided_at": None,
                "applied_at": None,
                "error": None,
                "requires_approval": True,
            }
        ),
        encoding="utf-8",
    )

    store = ProposalStore(root=root)
    proposals = store.list(status="pending", origin="system")

    assert len(proposals) == 1
    assert proposals[0].id == "legacy123"
    assert proposals[0].origin == "system"


def test_proposal_store_filters_by_origin(tmp_path):
    store = ProposalStore(root=tmp_path / "proposals")
    store.create(
        kind="feature",
        title="Tune planner",
        summary="System suggestion",
        detail="detail",
        source="feature_suggester:failure_pattern",
    )
    store.create(
        kind="feature",
        title="User idea",
        summary="User suggestion",
        detail="detail",
        source="user_dashboard",
        origin="user",
    )

    system_only = store.list(origin="system")
    user_only = store.list(status="pending", origin="user")

    assert [proposal.title for proposal in system_only] == ["Tune planner"]
    assert [proposal.title for proposal in user_only] == ["User idea"]


def test_proposal_store_feature_proposals_stay_pending_under_selective_triage(tmp_path):
    store = ProposalStore(root=tmp_path / "proposals")

    proposal = store.create(
        kind="feature",
        title="Keep review gate",
        summary="System suggestion",
        detail="detail",
        source="feature_suggester:failure_pattern",
    )

    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "pending"
    assert current.requires_approval is True
    assert current.decided_at is None


@pytest.mark.asyncio
async def test_proposal_store_create_auto_applies_with_running_loop(tmp_path):
    store = ProposalStore(root=tmp_path / "proposals")
    applied = asyncio.Event()

    async def handler(payload):
        applied.set()
        return {"ok": True}

    store.register_handler("ingest", handler)
    proposal = store.create(
        kind="ingest",
        title="Auto apply now",
        summary="System suggestion",
        detail="detail",
        payload={"topic": "agentic rag"},
        source="feature_suggester:meta",
        requires_approval=False,
    )

    await asyncio.wait_for(applied.wait(), timeout=1)
    for _ in range(20):
        current = store.get(proposal.id)
        if current is not None and current.status == "applied":
            break
        await asyncio.sleep(0)

    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "applied"
    assert current.requires_approval is False


@pytest.mark.asyncio
async def test_proposal_store_sync_auto_approved_system_items_resume_without_user_review(
    tmp_path, monkeypatch
):
    store = ProposalStore(root=tmp_path / "proposals")
    applied = asyncio.Event()

    async def handler(payload):
        applied.set()
        return {"ok": True}

    store.register_handler("ingest", handler)
    monkeypatch.setattr(store, "_has_running_loop", lambda: False)
    proposal = store.create(
        kind="ingest",
        title="Replay later",
        summary="System suggestion",
        detail="detail",
        payload={"topic": "agentic rag", "limit": 3, "mode": "search"},
        source="explorer",
        auto_triage_eligible=True,
    )

    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "approved"
    assert current.requires_approval is False

    monkeypatch.setattr(store, "_has_running_loop", lambda: True)
    result = await store.resume_inflight()

    assert result == {"requeued": 1, "failed_no_handler": 0}
    await asyncio.wait_for(applied.wait(), timeout=1)
    for _ in range(20):
        current = store.get(proposal.id)
        if current is not None and current.status == "applied":
            break
        await asyncio.sleep(0)

    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "applied"


@pytest.mark.asyncio
async def test_retriage_pending_auto_approves_existing_explorer_ingest(tmp_path, monkeypatch):
    disabled = SimpleNamespace(cortex_auto_approve_system=False)
    enabled = SimpleNamespace(
        cortex_auto_approve_system=True,
        cortex_auto_reject_duplicates=True,
        cortex_auto_reject_low_signal_ingest=True,
        cortex_auto_approve_safe_ingest=True,
        cortex_auto_triage_duplicate_window_seconds=86_400,
        cortex_auto_triage_min_ingest_topic_length=6,
        cortex_auto_triage_max_safe_ingest_limit=3,
    )
    monkeypatch.setattr("skyn3t.config.settings.get_settings", lambda: disabled)
    store = ProposalStore(root=tmp_path / "proposals")
    applied = asyncio.Event()

    async def handler(payload):
        applied.set()
        return {"ok": True}

    store.register_handler("ingest", handler)
    proposal = store.create(
        kind="ingest",
        title="Replay later",
        summary="System suggestion",
        detail="detail",
        payload={"topic": "agentic rag", "limit": 3, "mode": "search"},
        source="explorer",
    )
    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "pending"

    monkeypatch.setattr("skyn3t.config.settings.get_settings", lambda: enabled)
    result = await store.retriage_pending()

    assert result == {"auto_approved": 1, "auto_rejected": 0}
    await asyncio.wait_for(applied.wait(), timeout=1)
    for _ in range(20):
        current = store.get(proposal.id)
        if current is not None and current.status == "applied":
            break
        await asyncio.sleep(0)

    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "applied"
    assert current.triage_decision == "auto_approved"


@pytest.mark.asyncio
async def test_retriage_pending_auto_rejects_invalid_studio_debug(tmp_path, monkeypatch):
    disabled = SimpleNamespace(cortex_auto_approve_system=False)
    enabled = SimpleNamespace(
        cortex_auto_approve_system=True,
        cortex_auto_reject_duplicates=True,
        cortex_auto_reject_low_signal_ingest=True,
        cortex_auto_approve_safe_ingest=True,
        cortex_auto_triage_duplicate_window_seconds=86_400,
        cortex_auto_triage_min_ingest_topic_length=6,
        cortex_auto_triage_max_safe_ingest_limit=3,
    )
    monkeypatch.setattr("skyn3t.config.settings.get_settings", lambda: disabled)
    store = ProposalStore(root=tmp_path / "proposals")

    proposal = store.create(
        kind="studio_debug",
        title="All LLM attempts failed for None",
        summary="Could not produce a valid unified diff for `None` after 4 attempts.",
        detail="detail",
        payload={"target_file": None, "attempts": [{"attempt": 1, "error": "boom"}]},
        source="code_improver",
    )
    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "pending"

    monkeypatch.setattr("skyn3t.config.settings.get_settings", lambda: enabled)
    result = await store.retriage_pending()

    assert result == {"auto_approved": 0, "auto_rejected": 1}
    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "rejected"
    assert current.error == "auto-rejected invalid studio_debug: missing target_file"
    assert current.triage_decision == "auto_rejected"


def test_proposal_store_auto_rejects_duplicate_system_feature_proposals(tmp_path):
    store = ProposalStore(root=tmp_path / "proposals")
    first = store.create(
        kind="feature",
        title="Prefer the winning FastAPI scaffold",
        summary="Use the higher-success pattern for FastAPI builds",
        detail="detail",
        payload={"kind": "build_pattern_bias", "stack": "fastapi"},
        source="meta_agent:thresholds",
    )
    second = store.create(
        kind="feature",
        title="Prefer the winning FastAPI scaffold",
        summary="Use the higher-success pattern for FastAPI builds",
        detail="detail",
        payload={"kind": "build_pattern_bias", "stack": "fastapi"},
        source="meta_agent:thresholds",
    )

    first_current = store.get(first.id)
    second_current = store.get(second.id)
    assert first_current is not None
    assert second_current is not None
    assert first_current.status == "pending"
    assert second_current.status == "rejected"
    assert second_current.error == f"auto-rejected duplicate of {first.id}"
    assert second_current.triage_decision == "auto_rejected"


def test_proposal_store_auto_rejects_low_signal_ingest(tmp_path):
    store = ProposalStore(root=tmp_path / "proposals")

    proposal = store.create(
        kind="ingest",
        title="Ingest topic: ai",
        summary="Explorer suggests ingesting open-source content for: ai",
        detail="detail",
        payload={"topic": "ai", "limit": 3, "mode": "search"},
        source="explorer",
        auto_triage_eligible=True,
    )

    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "rejected"
    assert current.error == "auto-rejected low-signal ingest: topic too short"
    assert current.triage_decision == "auto_rejected"


def test_feature_suggester_user_idea_includes_execution_brief(tmp_path, monkeypatch):
    from skyn3t.cortex.feature_suggester import FeatureSuggester

    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: store)
    monkeypatch.setattr(
        "skyn3t.cortex.feature_suggester.infer_feature_target_file",
        lambda idea, repo_root=None: "skyn3t/cortex/handlers.py",
    )

    suggester = FeatureSuggester(event_bus=SimpleNamespace())
    pid = suggester.file_user_idea("Make Cortex approvals start real work", source="user_dashboard")

    proposal = store.get(str(pid))
    assert proposal is not None
    assert proposal.payload["action"] == "user_request"
    assert proposal.payload["target_file"] == "skyn3t/cortex/handlers.py"
    assert "## Planned execution" in proposal.detail
    assert "On approval: SkyN3t will draft and auto-apply a repo patch" in proposal.detail


@pytest.mark.asyncio
async def test_proposal_store_approve_runs_apply_in_background(tmp_path):
    store = ProposalStore(root=tmp_path / "proposals")
    started = asyncio.Event()

    async def handler(payload):
        started.set()
        await asyncio.sleep(0)
        return {"ok": True, "payload": payload}

    store.register_handler("ingest", handler)
    proposal = store.create(
        kind="ingest",
        title="Ingest docs",
        summary="Ingest a topic",
        detail="detail",
        payload={"topic": "agentic rag"},
        source="feature_suggester:meta",
    )

    result = await store.approve(proposal.id)

    assert result == {"ok": True, "applied": False, "status": "approved"}
    assert store.get(proposal.id).status == "approved"

    await asyncio.wait_for(started.wait(), timeout=1)
    for _ in range(20):
        current = store.get(proposal.id)
        if current is not None and current.status == "applied":
            break
        await asyncio.sleep(0)

    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "applied"
    assert current.applied_at is not None


@pytest.mark.asyncio
async def test_feature_handler_treats_nested_code_patch_as_started(tmp_path, monkeypatch):
    from skyn3t.cortex.handlers import install_handlers

    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: store)

    improver_calls: list[dict] = []

    class StubImprover:
        async def execute(self, req):
            improver_calls.append(req.input_data)
            return SimpleNamespace(
                success=False,
                error="apply failed",
                output={"proposed": True, "proposal_id": "cp123", "applied": False, "branch": None},
            )

    orchestrator = SimpleNamespace(agents={"code_improver": StubImprover()})
    install_handlers(orchestrator)

    result = await store._handlers["feature"](
        {
            "idea": "Make Cortex approvals start real work",
            "target_file": "skyn3t/cortex/handlers.py",
            "action": "user_request",
        }
    )

    assert result == {
        "ok": True,
        "status": "applying",
        "spawned": "code_improver",
        "target_file": "skyn3t/cortex/handlers.py",
        "code_patch_proposal_id": "cp123",
        "branch": None,
        "details": "Patch proposal created and is applying in the background.",
    }
    assert improver_calls == [
        {
            "target_file": "skyn3t/cortex/handlers.py",
            "repo_root": str(Path(__file__).resolve().parents[1]),
            "rationale": "Make Cortex approvals start real work",
            "intent": "feature_implementation",
            "source": "cortex.feature",
            "user_initiated": True,
            "use_mcp": False,
        }
    ]


@pytest.mark.asyncio
async def test_feature_handler_collapses_duplicate_feature_runs(tmp_path, monkeypatch):
    from skyn3t.cortex.handlers import install_handlers

    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: store)

    first = store.create(
        kind="feature",
        title="First idea",
        summary="summary",
        detail="detail",
        payload={
            "idea": "Make Cortex approvals start real work",
            "target_file": "skyn3t/cortex/handlers.py",
            "repo_root": str(Path(__file__).resolve().parents[1]),
        },
        source="user_dashboard",
        origin="user",
    )
    second = store.create(
        kind="feature",
        title="Second idea",
        summary="summary",
        detail="detail",
        payload={
            "idea": "Make Cortex approvals start real work",
            "target_file": "skyn3t/cortex/handlers.py",
            "repo_root": str(Path(__file__).resolve().parents[1]),
        },
        source="user_dashboard",
        origin="user",
    )
    current = store.get(first.id)
    assert current is not None
    current.status = "applying"
    current.decided_at = time.time()
    store._move_decided(current)

    improver_calls: list[dict] = []

    class StubImprover:
        async def execute(self, req):
            improver_calls.append(req.input_data)
            return SimpleNamespace(success=True, output={"proposal_id": "cp123", "applied": True})

    orchestrator = SimpleNamespace(agents={"code_improver": StubImprover()})
    install_handlers(orchestrator)

    result = await store._handlers["feature"](
        {
            "idea": "Make Cortex approvals start real work",
            "target_file": "skyn3t/cortex/handlers.py",
            "repo_root": str(Path(__file__).resolve().parents[1]),
            "_proposal_id": second.id,
        }
    )

    assert result == {
        "ok": True,
        "status": "already-running",
        "target_file": "skyn3t/cortex/handlers.py",
        "feature_proposal_id": first.id,
        "details": "An older approved feature proposal is already running for that file.",
    }


@pytest.mark.asyncio
async def test_ingest_handler_caps_limit_and_returns_errors(tmp_path, monkeypatch):
    from skyn3t.cortex.handlers import install_handlers

    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: store)

    calls: list[dict] = []

    class StubIngestor:
        async def execute(self, req):
            calls.append(req.input_data)
            return SimpleNamespace(
                success=True,
                output={
                    "ingested": ["a", "b"],
                    "summary": "done",
                    "errors": ["skipped one"],
                },
            )

    orchestrator = SimpleNamespace(agents={"github_ingestor": StubIngestor()})
    install_handlers(orchestrator)

    result = await store._handlers["ingest"](
        {
            "topic": "agentic rag",
            "limit": 999999,
        }
    )

    assert calls == [{"max_files": 100, "mode": "search", "query": "agentic rag"}]
    assert result == {
        "ok": True,
        "ingested": 2,
        "summary": "done",
        "errors": ["skipped one"],
    }


def test_proposal_store_create_can_force_review_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "skyn3t.config.settings.get_settings",
        lambda: SimpleNamespace(cortex_auto_approve_system=True),
    )
    store = ProposalStore(root=tmp_path / "proposals")

    proposal = store.create(
        kind="ingest",
        title="Scout repo",
        summary="review gated",
        detail="detail",
        payload={"repo": "octo/agent-flow"},
        source="repo_scout:github",
        force_requires_approval=True,
    )

    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "pending"
    assert current.requires_approval is True


@pytest.mark.asyncio
async def test_external_learning_handler_persists_governed_memory(tmp_path, monkeypatch):
    from skyn3t.cortex.handlers import install_handlers

    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: store)

    calls = {}

    class FakeIngestor:
        def __init__(self, *, memory_store, rag_engine=None):
            calls["memory_store"] = memory_store
            calls["rag_engine"] = rag_engine

        async def ingest_repo_approval(self, **kwargs):
            calls["kwargs"] = kwargs
            return {
                "doc_id": "doc-1",
                "summary_embedding_id": "embed-1",
                "ingested_count": 2,
                "ingested_paths": ["README.md", "docs/README.md"],
                "warnings": ["docs/index.md: not_found"],
            }

    class FakeSynthesizer:
        def __init__(self, memory_store):
            calls["synth_memory_store"] = memory_store

        async def synthesize_for_doc(self, doc_id):
            calls["synth_doc_id"] = doc_id
            return {"status": "created", "doc_id": "pattern-1", "consensus_count": 2}

    monkeypatch.setattr("skyn3t.cortex.handlers.ExternalRepoDocIngestor", FakeIngestor)
    monkeypatch.setattr("skyn3t.cortex.handlers.ExternalPatternSynthesizer", FakeSynthesizer)

    orchestrator = SimpleNamespace(agents={}, memory_store=object(), _ingestor=SimpleNamespace(rag="rag-engine"))
    install_handlers(orchestrator)

    result = await store._handlers["external_learning"](
        {
            "source_platform": "gitlab",
            "repo": "gitlab-org/agent-lab",
            "repo_url": "https://gitlab.com/gitlab-org/agent-lab",
            "lane": "fit",
            "query": "agent cli memory",
            "description": "GitLab agent workflow repo",
            "language": "Python",
            "license": "MIT",
            "reuse_risk": "low",
            "topics": ["agents"],
            "selection_reason": "fit lane via 'agent cli memory'",
            "stars": 220,
        }
    )

    assert result == {
        "ok": True,
        "doc_id": "doc-1",
        "stored_as": "external_learning",
        "summary_embedding_id": "embed-1",
        "ingested": 2,
        "paths": ["README.md", "docs/README.md"],
        "warnings": ["docs/index.md: not_found"],
        "pattern": {"status": "created", "doc_id": "pattern-1", "consensus_count": 2},
    }
    assert calls["memory_store"] is orchestrator.memory_store
    assert calls["rag_engine"] == "rag-engine"
    assert calls["kwargs"]["platform"] == "gitlab"
    assert calls["kwargs"]["repo"] == "gitlab-org/agent-lab"
    assert calls["kwargs"]["reuse_risk"] == "low"
    assert calls["synth_memory_store"] is orchestrator.memory_store
    assert calls["synth_doc_id"] == "doc-1"


@pytest.mark.asyncio
async def test_proposal_store_apply_injects_proposal_id(tmp_path):
    store = ProposalStore(root=tmp_path / "proposals")
    captured: list[dict] = []

    async def handler(payload):
        captured.append(payload)
        return {"ok": True}

    store.register_handler("feature", handler)
    proposal = store.create(
        kind="feature",
        title="Idea",
        summary="summary",
        detail="detail",
        payload={"idea": "demo"},
        source="user_dashboard",
        origin="user",
    )

    await store.approve(proposal.id)
    for _ in range(20):
        current = store.get(proposal.id)
        if current is not None and current.status == "applied":
            break
        await asyncio.sleep(0)

    assert captured == [{"idea": "demo", "_proposal_id": proposal.id, "_proposal_kind": "feature"}]


def test_review_watcher_parse_filters_placeholder_risks():
    from skyn3t.cortex.review_watcher import ReviewWatcher

    watcher = ReviewWatcher(event_bus=SimpleNamespace())
    verdict, risks = watcher._parse(
        "**Verdict:** `go-with-fixes`  **Score:** 65/100\n\n"
        "## LLM review\n\n"
        "Verdict: no-go\n"
        "## Risks\n"
        "- LLM-only contradiction that should not be parsed.\n\n"
        "## Files reviewed\n"
        "- `architecture.md`\n\n"
        "## Risks\n"
        "- None detected.\n"
        "- No blocking architectural risks detected — design is coherent.\n"
        "- Planner expected but missing: spec.md\n"
    )

    assert verdict == "Verdict: go-with-fixes"
    assert risks == ["Planner expected but missing: spec.md"]


@pytest.mark.asyncio
async def test_review_watcher_inspect_skips_no_actionable_risks(tmp_path, monkeypatch):
    from skyn3t.cortex.review_watcher import ReviewWatcher

    project_root = tmp_path / "projects" / "demo-project"
    project_root.mkdir(parents=True)
    (project_root / "architecture.md").write_text("## Overview\n", encoding="utf-8")
    (project_root / "review.md").write_text(
        "## Verdict\nVerdict: no-go\n\n## Risks\n- None detected.\n",
        encoding="utf-8",
    )

    created: list[dict] = []

    class StubStore:
        def create(self, **kwargs):
            created.append(kwargs)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: StubStore())

    watcher = ReviewWatcher(event_bus=SimpleNamespace())
    await watcher._inspect("demo-project", {})

    assert created == []
    assert "demo-project" not in watcher._seen


@pytest.mark.asyncio
async def test_review_watcher_inspect_does_not_file_proposal_for_project_review(
    tmp_path, monkeypatch
):
    """ReviewWatcher used to file studio_debug proposals for project reviews,
    but that handler calls CodeImproverAgent against REPO_ROOT (the
    orchestrator), not the project — so approving the proposal made the
    orchestrator try to patch itself. The watcher should now log and
    mark-seen instead, without creating a proposal."""
    from skyn3t.cortex.review_watcher import ReviewWatcher

    project_root = tmp_path / "projects" / "demo-project"
    project_root.mkdir(parents=True)
    (project_root / "architecture.md").write_text("## Overview\n", encoding="utf-8")
    (project_root / "review.md").write_text(
        "## Verdict\nVerdict: no-go\n\n## Risks\n"
        "- Missing core: brief asks for an API but no backend exists.\n"
        "- Port mismatch between vite.config.js and README.md.\n",
        encoding="utf-8",
    )

    created: list[dict] = []

    class StubStore:
        def create(self, **kwargs):
            created.append(kwargs)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: StubStore())
    monkeypatch.setattr(
        "skyn3t.cortex.review_watcher.get_settings",
        lambda: SimpleNamespace(projects_dir=tmp_path / "projects"),
    )

    watcher = ReviewWatcher(event_bus=SimpleNamespace())
    await watcher._inspect("demo-project", {})

    # No proposal filed, but slug IS marked seen so we don't re-log on
    # every event for the same project.
    assert created == []
    assert "demo-project" in watcher._seen


@pytest.mark.asyncio
async def test_studio_debug_handler_rejects_placeholder_risks(tmp_path, monkeypatch):
    from skyn3t.cortex.handlers import install_handlers

    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: store)

    improver_calls: list[dict] = []

    class StubImprover:
        async def execute(self, req):
            improver_calls.append(req.input_data)
            return SimpleNamespace(success=True, output={"proposal_id": "p1"})

    orchestrator = SimpleNamespace(agents={"code_improver": StubImprover()})
    install_handlers(orchestrator)

    result = await store._handlers["studio_debug"](
        {
            "target_file": "projects/demo-project/architecture.md",
            "verdict": "Verdict: no-go",
            "risks": ["None detected."],
        }
    )

    assert result == {"ok": False, "error": "review flagged no actionable risks"}
    assert improver_calls == []


@pytest.mark.asyncio
async def test_proposal_store_approve_fails_truthfully_without_handler(tmp_path):
    store = ProposalStore(root=tmp_path / "proposals")
    proposal = store.create(
        kind="ingest",
        title="Ingest docs",
        summary="Ingest a topic",
        detail="detail",
        payload={"topic": "agentic rag"},
        source="feature_suggester:meta",
    )

    result = await store.approve(proposal.id)

    current = store.get(proposal.id)
    assert result["ok"] is False
    assert "no handler for kind 'ingest'" in result["error"]
    assert result["available_handlers"] == []
    assert current is not None
    assert current.status == "failed"
    assert "no handler for kind 'ingest'" in current.error


@pytest.mark.asyncio
async def test_proposal_store_resume_inflight_requeues_legacy_approved(tmp_path):
    root = tmp_path / "proposals"
    (root / "pending").mkdir(parents=True)
    (root / "decided").mkdir(parents=True)
    proposal = {
        "id": "legacy-approved",
        "kind": "ingest",
        "title": "Legacy approved proposal",
        "summary": "Resume after restart",
        "detail": "detail",
        "payload": {"topic": "agentic rag"},
        "source": "feature_suggester:meta",
        "status": "approved",
        "created_at": time.time(),
        "decided_at": time.time(),
        "applied_at": None,
        "error": None,
        "requires_approval": True,
        "origin": "system",
    }
    (root / "decided" / "legacy-approved.json").write_text(
        json.dumps(proposal),
        encoding="utf-8",
    )

    store = ProposalStore(root=root)
    resumed = asyncio.Event()

    async def handler(payload):
        resumed.set()
        await asyncio.sleep(0)
        return {"ok": True}

    store.register_handler("ingest", handler)

    result = await store.resume_inflight()

    assert result == {"requeued": 1, "failed_no_handler": 0}
    await asyncio.wait_for(resumed.wait(), timeout=1)
    for _ in range(20):
        current = store.get("legacy-approved")
        if current is not None and current.status == "applied":
            break
        await asyncio.sleep(0)

    current = store.get("legacy-approved")
    assert current is not None
    assert current.status == "applied"


@pytest.mark.asyncio
async def test_proposal_store_cancel_inflight_allows_resume(tmp_path):
    store = ProposalStore(root=tmp_path / "proposals")
    started = asyncio.Event()
    resumed = asyncio.Event()
    calls = 0

    async def handler(payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Event().wait()
        resumed.set()
        return {"ok": True}

    store.register_handler("ingest", handler)
    proposal = store.create(
        kind="ingest",
        title="Retry ingest",
        summary="Cancel then resume",
        detail="detail",
        payload={"topic": "agentic rag"},
        source="feature_suggester:meta",
    )

    result = await store.approve(proposal.id)
    assert result == {"ok": True, "applied": False, "status": "approved"}
    await asyncio.wait_for(started.wait(), timeout=1)

    cancelled = await store.cancel_inflight()
    replay = await store.resume_inflight()

    assert cancelled == {"cancelled": 1}
    assert replay == {"requeued": 1, "failed_no_handler": 0}
    await asyncio.wait_for(resumed.wait(), timeout=1)
    for _ in range(20):
        current = store.get(proposal.id)
        if current is not None and current.status == "applied":
            break
        await asyncio.sleep(0)

    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "applied"


@pytest.mark.asyncio
async def test_self_tuning_files_review_gated_tuning_proposal(tmp_path, monkeypatch):
    from skyn3t.core.events import EventBus
    from skyn3t.cortex.gated_tuner import GatedTuner
    from skyn3t.memory.tuner import SelfTuningEngine

    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: store)

    bus = EventBus()
    gated = GatedTuner(bus, config_path=tmp_path / "config" / "runtime.json")
    gated.start()
    tuner = SelfTuningEngine(event_bus=bus)

    await tuner.receive_suggestions(
        "claude",
        ["rate_limit"],
        [{"type": "prompt", "issue": "rate_limit", "advice": "slow down"}],
    )

    for _ in range(20):
        proposals = store.list(status="pending")
        if proposals:
            break
        await asyncio.sleep(0)

    proposals = store.list(status="pending")
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.kind == "tuning"
    assert proposal.payload["agent"] == "claude"
    assert proposal.payload["adjustments"][0]["parameter"] == "request_interval"

    await gated.stop()


@pytest.mark.asyncio
async def test_gated_tuner_apply_updates_agent_runtime_config(tmp_path, monkeypatch):
    from skyn3t.core.events import EventBus
    from skyn3t.cortex.gated_tuner import GatedTuner

    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: store)

    config_path = tmp_path / "config" / "runtime.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"agents": {"claude": {"timeout": 30}}}),
        encoding="utf-8",
    )

    gated = GatedTuner(EventBus(), config_path=config_path)
    gated.start()

    proposal = store.create(
        kind="tuning",
        title="Tune claude",
        summary="Increase timeout",
        detail="detail",
        payload={
            "agent": "claude",
            "adjustments": [
                {
                    "parameter": "timeout",
                    "change": "+10s",
                    "new_value": "min(previous + 10, 300)",
                    "reason": "Timeouts detected — increasing patience",
                }
            ],
            "reason": "Timeouts detected",
        },
        source="test",
    )

    result = await store.approve(proposal.id)
    assert result == {"ok": True, "applied": False, "status": "approved"}

    for _ in range(20):
        current = store.get(proposal.id)
        if current is not None and current.status == "applied":
            break
        await asyncio.sleep(0)

    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "applied"
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["agents"]["claude"]["timeout"] == 40
    snapshots = list((config_path.parent / "snapshots").glob("*.json"))
    assert snapshots

    await gated.stop()
