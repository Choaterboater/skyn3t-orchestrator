from __future__ import annotations

from pathlib import Path

import pytest

from skyn3t.cortex.external_repo_ingest import ExternalRepoDocIngestor


class _FakeRag:
    def __init__(self):
        self.calls = []

    async def initialize(self):
        return None

    async def add_knowledge_one(self, **kwargs):
        self.calls.append(kwargs)
        return f"embed-{len(self.calls)}"


class _FakeMemoryStore:
    def __init__(self):
        self.calls = []

    async def save_knowledge_doc(self, **kwargs):
        self.calls.append(kwargs)
        return f"doc-{len(self.calls)}"


@pytest.mark.asyncio
async def test_external_repo_ingestor_ingests_gitlab_doc_and_summary(monkeypatch, tmp_path):
    rag = _FakeRag()
    memory = _FakeMemoryStore()
    ingestor = ExternalRepoDocIngestor(
        memory_store=memory,
        rag_engine=rag,
        seen_hashes_path=tmp_path / "seen.json",
    )

    async def fake_fetch(_client, *, url, max_bytes):
        del _client, max_bytes
        if url.endswith("/-/raw/HEAD/README.md"):
            return ("# Agent Lab\n\n" + "Useful repo notes. " * 10, "")
        return (None, "not_found")

    monkeypatch.setattr(ingestor, "_fetch_path_text", fake_fetch)

    result = await ingestor.ingest_repo_approval(
        platform="gitlab",
        repo="gitlab-org/agent-lab",
        repo_url="https://gitlab.com/gitlab-org/agent-lab",
        lane="fit",
        query="agent cli memory",
        description="GitLab agent workflow repo",
        language="Python",
        license_name="MIT",
        reuse_risk="low",
        selection_reason="fit lane via 'agent cli memory'",
        topics=["agents"],
        stars=220,
    )

    assert result["doc_id"] == "doc-1"
    assert result["summary_embedding_id"] == "embed-2"
    assert result["ingested_count"] == 1
    assert result["ingested_paths"] == ["README.md"]
    assert memory.calls[0]["doc_type"] == "external_learning"
    assert memory.calls[0]["meta"]["external_doc_ingest_status"] == "docs_ingested"
    assert memory.calls[0]["meta"]["external_doc_paths_ingested"] == ["README.md"]
    assert len(rag.calls) == 2
    assert rag.calls[0]["source"] == "gitlab:gitlab-org/agent-lab/README.md"
    assert rag.calls[0]["metadata"]["kind"] == "external-repo-doc"
    assert rag.calls[1]["metadata"]["kind"] == "external-learning-summary"


@pytest.mark.asyncio
async def test_external_repo_ingestor_skips_duplicate_doc_content(monkeypatch, tmp_path):
    rag = _FakeRag()
    memory = _FakeMemoryStore()
    seen_path = tmp_path / "seen.json"
    ingestor = ExternalRepoDocIngestor(
        memory_store=memory,
        rag_engine=rag,
        seen_hashes_path=seen_path,
    )

    async def fake_fetch(_client, *, url, max_bytes):
        del _client, url, max_bytes
        return ("# Agent Lab\n\n" + "Useful repo notes. " * 10, "")

    monkeypatch.setattr(ingestor, "_fetch_path_text", fake_fetch)

    first = await ingestor.ingest_repo_approval(
        platform="gitlab",
        repo="gitlab-org/agent-lab",
        repo_url="https://gitlab.com/gitlab-org/agent-lab",
        lane="fit",
        query="agent cli memory",
        description="GitLab agent workflow repo",
        language="Python",
        license_name="MIT",
        reuse_risk="low",
        selection_reason="fit lane via 'agent cli memory'",
        topics=["agents"],
        stars=220,
    )
    second = await ingestor.ingest_repo_approval(
        platform="gitlab",
        repo="gitlab-org/agent-lab",
        repo_url="https://gitlab.com/gitlab-org/agent-lab",
        lane="fit",
        query="agent cli memory",
        description="GitLab agent workflow repo",
        language="Python",
        license_name="MIT",
        reuse_risk="low",
        selection_reason="fit lane via 'agent cli memory'",
        topics=["agents"],
        stars=220,
    )

    assert first["ingested_count"] == 4
    assert second["ingested_count"] == 0
    assert any("duplicate content skipped" in warning for warning in second["warnings"])
    root_readmes = [call for call in rag.calls if call["metadata"].get("path") == "README.md"]
    assert len(root_readmes) == 1
    assert Path(seen_path).exists()
