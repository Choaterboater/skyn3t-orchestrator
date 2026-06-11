"""Phase 2 — Owner F: GitHub doc ingestion in external_repo_ingest.py.

Proves the github branch lands docs (was a no-op pre-fix) and that _raw_url
emits the raw.githubusercontent.com HEAD URL. Uses a fake rag/memory_store and
a monkeypatched fetch — never touches data/ or the network.
"""
from __future__ import annotations

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


def test_raw_url_github_uses_raw_githubusercontent_head():
    """Before the fix _raw_url raised ValueError for github."""
    url = ExternalRepoDocIngestor._raw_url(
        platform="github", repo="owner/name", path="README.md"
    )
    assert url == "https://raw.githubusercontent.com/owner/name/HEAD/README.md"


@pytest.mark.asyncio
async def test_external_repo_ingestor_ingests_github_doc_and_summary(monkeypatch, tmp_path):
    """github was short-circuited to a no-op (0 files); now it ingests docs."""
    rag = _FakeRag()
    memory = _FakeMemoryStore()
    ingestor = ExternalRepoDocIngestor(
        memory_store=memory,
        rag_engine=rag,
        seen_hashes_path=tmp_path / "seen.json",
    )

    async def fake_fetch(_client, *, url, max_bytes):
        del _client, max_bytes
        if url.endswith("/HEAD/README.md"):
            # confirm the github raw host is what the loop fetches
            assert url == "https://raw.githubusercontent.com/octo/agent-kit/HEAD/README.md"
            return ("# Agent Kit\n\n" + "Reusable agent patterns. " * 10, "")
        return (None, "not_found")

    monkeypatch.setattr(ingestor, "_fetch_path_text", fake_fetch)

    result = await ingestor.ingest_repo_approval(
        platform="github",
        repo="octo/agent-kit",
        repo_url="https://github.com/octo/agent-kit",
        lane="fit",
        query="agent cli memory",
        description="GitHub agent workflow repo",
        language="Python",
        license_name="MIT",
        reuse_risk="low",
        selection_reason="fit lane via 'agent cli memory'",
        topics=["agents"],
        stars=512,
    )

    assert result["ingested_count"] == 1
    assert result["ingested_paths"] == ["README.md"]
    # doc + summary => two rag writes
    assert len(rag.calls) == 2
    assert rag.calls[0]["source"] == "github:octo/agent-kit/README.md"
    assert rag.calls[0]["metadata"]["source_platform"] == "github"
    assert rag.calls[0]["metadata"]["kind"] == "external-repo-doc"
    assert rag.calls[0]["metadata"]["raw_url"].startswith(
        "https://raw.githubusercontent.com/octo/agent-kit/HEAD/"
    )
    assert rag.calls[1]["metadata"]["kind"] == "external-learning-summary"
    # memory-store provenance reflects a real ingest, not summary_only
    assert memory.calls[0]["meta"]["external_doc_ingest_status"] == "docs_ingested"
    assert memory.calls[0]["meta"]["source_platform"] == "github"
    assert "docs ingest not supported" not in " ".join(result["warnings"])


@pytest.mark.asyncio
async def test_unsupported_platform_still_no_op(monkeypatch, tmp_path):
    """Guard the allowlist: a non-allowlisted platform stays a no-op."""
    rag = _FakeRag()
    memory = _FakeMemoryStore()
    ingestor = ExternalRepoDocIngestor(
        memory_store=memory,
        rag_engine=rag,
        seen_hashes_path=tmp_path / "seen.json",
    )

    async def fake_fetch(_client, *, url, max_bytes):  # pragma: no cover - must not run
        raise AssertionError("fetch must not run for unsupported platform")

    monkeypatch.setattr(ingestor, "_fetch_path_text", fake_fetch)

    result = await ingestor.ingest_repo_approval(
        platform="sourcehut",
        repo="~user/repo",
        repo_url="https://sr.ht/~user/repo",
        lane="fit",
        query="agent cli memory",
        description="unsupported platform repo",
        language="Python",
        license_name="MIT",
        reuse_risk="low",
        selection_reason="fit lane",
        topics=[],
        stars=10,
    )

    assert result["ingested_count"] == 0
    assert any("docs ingest not supported for sourcehut" in w for w in result["warnings"])
