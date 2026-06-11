from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from skyn3t.cortex.proposals import ProposalStore


@pytest.mark.asyncio
async def test_project_improvement_handler_marks_candidate_without_push(tmp_path, monkeypatch):
    from skyn3t.cortex.handlers import install_handlers

    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: store)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    install_handlers(SimpleNamespace(agents={}))

    proposal = store.create(
        kind="project_improvement",
        title="Promote candidate",
        summary="beats baseline",
        detail="detail",
        payload={
            "source_uri": "https://github.com/example/original",
            "candidate_dir": str(candidate),
            "read_only_original": True,
            "git_push_allowed": False,
            "comparison": {"improved": True, "delta": 4},
        },
        source="test",
    )

    await store.approve(proposal.id)
    for _ in range(20):
        current = store.get(proposal.id)
        if current is not None and current.status == "applied":
            break
        await __import__("asyncio").sleep(0.05)

    marker = candidate / ".skyn3t_project_improvement_approved.json"
    assert marker.is_file()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["read_only_original"] is True
    assert data["git_push_allowed"] is False


@pytest.mark.asyncio
async def test_project_improvement_handler_rejects_direct_push_flag(tmp_path, monkeypatch):
    from skyn3t.cortex.handlers import install_handlers

    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: store)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    install_handlers(SimpleNamespace(agents={}))

    proposal = store.create(
        kind="project_improvement",
        title="Unsafe candidate",
        summary="bad",
        detail="detail",
        payload={
            "source_uri": "https://github.com/example/original",
            "candidate_dir": str(candidate),
            "read_only_original": True,
            "git_push_allowed": True,
            "comparison": {"improved": True},
        },
        source="test",
    )

    await store.approve(proposal.id)
    for _ in range(20):
        current = store.get(proposal.id)
        if current is not None and current.status == "failed":
            break
        await __import__("asyncio").sleep(0.05)

    assert not (candidate / ".skyn3t_project_improvement_approved.json").exists()
    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "failed"
