"""Tests for GitHub ingestor client selection."""

import sys
from types import ModuleType

import pytest

from skyn3t.agents.github_ingestor import GitHubIngestorAgent
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
