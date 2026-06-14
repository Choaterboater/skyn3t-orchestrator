"""Tests for GitHub ingestor client selection and execution status."""

import logging
import sys
from types import ModuleType

import pytest

from skyn3t.agents.github_ingestor import GitHubIngestorAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


@pytest.mark.asyncio
async def test_github_ingestor_prefers_httpx_without_token(monkeypatch):
    httpx_mod = ModuleType("httpx")
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    httpx_mod.Client = FakeClient

    github_mod = ModuleType("github")

    def fail_github(*args, **kwargs):
        raise AssertionError("PyGithub should not be used without a token")

    github_mod.Github = fail_github

    monkeypatch.setitem(sys.modules, "httpx", httpx_mod)
    monkeypatch.setitem(sys.modules, "github", github_mod)

    agent = GitHubIngestorAgent(event_bus=EventBus())
    await agent.initialize()

    assert agent.metadata["client"] == "httpx"
    assert agent.metadata["authenticated"] is False
    assert agent._github_client is None
    assert agent._http_client is not None
    assert captured["base_url"] == "https://api.github.com"
    assert "Authorization" not in captured["headers"]


@pytest.mark.asyncio
async def test_github_ingestor_uses_pygithub_with_token(monkeypatch):
    github_mod = ModuleType("github")
    captured = {}

    class FakeGithub:
        def __init__(self, token):
            captured["token"] = token

    github_mod.Github = FakeGithub

    httpx_mod = ModuleType("httpx")

    def fail_httpx(**kwargs):
        raise AssertionError("httpx should not be used when authenticated PyGithub is available")

    httpx_mod.Client = fail_httpx

    monkeypatch.setitem(sys.modules, "github", github_mod)
    monkeypatch.setitem(sys.modules, "httpx", httpx_mod)

    agent = GitHubIngestorAgent(event_bus=EventBus(), github_token="secret-token")
    await agent.initialize()

    assert agent.metadata["client"] == "pygithub"
    assert agent.metadata["authenticated"] is True
    assert captured["token"] == "secret-token"
    assert agent._github_client is not None
    assert agent._http_client is None


class FakeRAG:
    def __init__(self):
        self.calls = []

    async def add_knowledge_one(self, **kwargs):
        self.calls.append(kwargs)
        return f"emb-{len(self.calls)}"


def _ready_agent(*, rag=None):
    agent = GitHubIngestorAgent(event_bus=EventBus(), rag=rag)
    agent.metadata["initialized"] = True
    agent._http_client = object()
    return agent


@pytest.mark.asyncio
async def test_github_ingestor_ingests_with_fake_rag(monkeypatch):
    rag = FakeRAG()
    agent = _ready_agent(rag=rag)

    async def fake_list(repo_full_name, path_prefixes, remaining):
        return [("README.md", 7)]

    async def fake_fetch(repo_full_name, path, max_bytes):
        return "# Demo\n"

    monkeypatch.setattr(agent, "_list_repo_files", fake_list)
    monkeypatch.setattr(agent, "_fetch_file_content", fake_fetch)

    result = await agent.execute(
        TaskRequest(input_data={"mode": "single_repo", "repo": "octo/demo"})
    )

    assert result.success is True
    assert result.output["rag_available"] is True
    assert result.output["ingested"][0]["embedding_id"] == "emb-1"
    assert rag.calls[0]["source"] == "github:octo/demo/README.md"


@pytest.mark.asyncio
async def test_github_ingestor_logs_missing_rag(monkeypatch, caplog):
    agent = _ready_agent(rag=None)

    async def fake_list(repo_full_name, path_prefixes, remaining):
        return [("README.md", 7)]

    async def fake_fetch(repo_full_name, path, max_bytes):
        return "# Demo\n"

    monkeypatch.setattr(agent, "_list_repo_files", fake_list)
    monkeypatch.setattr(agent, "_fetch_file_content", fake_fetch)
    caplog.set_level(logging.WARNING, logger="skyn3t.agents.github_ingestor")

    result = await agent.execute(
        TaskRequest(input_data={"mode": "single_repo", "repo": "octo/demo"})
    )

    assert result.success is True
    assert result.output["rag_available"] is False
    assert result.output["ingested"][0]["embedding_id"] is None
    assert result.metadata["status"] == "completed_missing_rag"
    assert "RAG unavailable" in caplog.text
    assert "secret" not in caplog.text.lower()


@pytest.mark.asyncio
async def test_github_ingestor_reports_missing_client(caplog):
    agent = GitHubIngestorAgent(event_bus=EventBus())
    agent.metadata["initialized"] = True
    caplog.set_level(logging.WARNING, logger="skyn3t.agents.github_ingestor")

    result = await agent.execute(
        TaskRequest(input_data={"mode": "single_repo", "repo": "octo/demo"})
    )

    assert result.success is False
    assert result.error == "github client not available"
    assert result.metadata["status"] == "missing_client"
    assert "no GitHub client is available" in caplog.text


@pytest.mark.asyncio
async def test_github_ingestor_reports_missing_seeds(caplog):
    agent = _ready_agent(rag=FakeRAG())
    agent.seeds_path = "data/does-not-exist-for-test.yaml"
    caplog.set_level(logging.WARNING, logger="skyn3t.agents.github_ingestor")

    result = await agent.execute(TaskRequest(input_data={"mode": "seed_list"}))

    assert result.success is False
    assert "no seeds available" in (result.error or "")
    assert result.metadata["status"] == "missing_seeds"
    assert "no usable GitHub ingest seeds" in caplog.text


@pytest.mark.asyncio
async def test_github_ingestor_reports_rate_limit_and_skip_counts(monkeypatch, caplog):
    agent = _ready_agent(rag=FakeRAG())

    async def fake_list(repo_full_name, path_prefixes, remaining):
        raise RuntimeError("github rate limit (403)")

    monkeypatch.setattr(agent, "_list_repo_files", fake_list)
    caplog.set_level(logging.WARNING, logger="skyn3t.agents.github_ingestor")

    result = await agent.execute(
        TaskRequest(input_data={"mode": "single_repo", "repo": "octo/demo"})
    )

    assert result.success is True
    assert result.output["rate_limited"] is True
    assert result.output["skip_counts"] == {"rate_limited": 1}
    assert result.metadata["status"] == "rate_limited"
    assert "GitHub rate limit" in caplog.text


@pytest.mark.asyncio
async def test_run_github_ingest_endpoint_submits_to_ingestor(monkeypatch):
    from skyn3t.web import app as web_app

    calls = {}

    class FakeOrchestrator:
        agents = {"github_ingestor": object()}

        async def submit_task(self, task, agent_name=None):
            calls["task"] = task
            calls["agent_name"] = agent_name
            return "task-123"

    monkeypatch.setattr(web_app, "orchestrator", FakeOrchestrator())

    result = await web_app.run_github_ingest(
        {"repo": "octo/demo", "paths": ["README.md"], "max_files": 1}
    )

    assert result["status"] == "submitted"
    assert result["agent"] == "github_ingestor"
    assert calls["agent_name"] == "github_ingestor"
    assert calls["task"].input_data == {
        "mode": "single_repo",
        "repo": "octo/demo",
        "paths": ["README.md"],
        "max_files": 1,
    }
