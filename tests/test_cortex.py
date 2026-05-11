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

    system_only = store.list(status="pending", origin="system")
    user_only = store.list(status="pending", origin="user")

    assert [proposal.title for proposal in system_only] == ["Tune planner"]
    assert [proposal.title for proposal in user_only] == ["User idea"]


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
    assert improver_calls == []


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
    assert result == {"ok": False, "error": "no handler for kind"}
    assert current is not None
    assert current.status == "failed"
    assert current.error == "no handler for kind"


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
