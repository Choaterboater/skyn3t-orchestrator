from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from skyn3t.cortex.repo_scout import GitHubRepoScout, _ScoutCandidate


class _FakeProposal:
    def __init__(self, pid: str, payload: dict):
        self.id = pid
        self.kind = "ingest"
        self.status = "pending"
        self.payload = payload
        self.requires_approval = True


class _FakeStore:
    def __init__(self):
        self.created: list[_FakeProposal] = []

    def list(self, origin=None):
        return list(self.created)

    def create(self, *, kind, title, summary, detail, payload, source, **kwargs):
        proposal = _FakeProposal(f"prop-{len(self.created)+1}", payload)
        proposal.kind = kind
        proposal.title = title
        proposal.summary = summary
        proposal.detail = detail
        proposal.source = source
        proposal.requires_approval = bool(kwargs.get("force_requires_approval", False))
        self.created.append(proposal)
        return proposal


class _FakeExplorer:
    async def execute(self, task):
        task_type = task.input_data.get("task_type")
        if task_type == "trending_repos":
            return SimpleNamespace(
                output={
                    "repositories": [
                        {
                            "full_name": "octo/trending-ui",
                            "description": "UI kit",
                            "stars": 5000,
                            "language": "TypeScript",
                            "url": "https://github.com/octo/trending-ui",
                        }
                    ]
                }
            )
        if task_type == "code_search":
            return SimpleNamespace(
                output={
                    "repositories": [
                        {
                            "full_name": "octo/agent-flow",
                            "description": "Agent workflow system",
                            "stars": 1200,
                            "language": "Python",
                            "url": "https://github.com/octo/agent-flow",
                        }
                    ]
                }
            )
        if task_type == "repo_analysis":
            return SimpleNamespace(
                output={
                    "license": "MIT",
                    "topics": ["agents", "workflow"],
                    "url": f"https://github.com/{task.input_data['owner']}/{task.input_data['repo']}",
                }
            )
        raise AssertionError(f"unexpected task_type: {task_type}")


@pytest.mark.asyncio
async def test_repo_scout_files_ingest_proposals(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr("skyn3t.cortex.repo_scout.get_store", lambda: store)
    orch = SimpleNamespace(agents={"github_explorer": _FakeExplorer()})
    scout = GitHubRepoScout(orchestrator=orch, event_bus=SimpleNamespace())

    result = await scout.run_once({"limit": 2, "queries": ["agent cli memory"]})

    assert result["ok"] is True
    assert result["filed"] == 2
    assert all(proposal.kind == "ingest" for proposal in store.created)
    assert all(proposal.requires_approval is True for proposal in store.created)
    assert {proposal.payload["repo"] for proposal in store.created} == {
        "octo/trending-ui",
        "octo/agent-flow",
    }


@pytest.mark.asyncio
async def test_repo_scout_files_external_learning_for_non_github(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr("skyn3t.cortex.repo_scout.get_store", lambda: store)
    orch = SimpleNamespace(agents={"github_explorer": _FakeExplorer()})
    scout = GitHubRepoScout(orchestrator=orch, event_bus=SimpleNamespace())

    async def fake_gitlab_candidates(*, cadence, fit_queries):
        assert cadence == "weekly"
        assert fit_queries == ["agent cli memory"]
        return [
            _ScoutCandidate(
                platform="gitlab",
                lane="fit",
                query="agent cli memory",
                full_name="gitlab-org/agent-lab",
                description="GitLab agent workflow repo",
                stars=220,
                language="Python",
                url="https://gitlab.com/gitlab-org/agent-lab",
                license_name="MIT",
                topics=["agents"],
            )
        ]

    async def fake_bitbucket_candidates(*, cadence, fit_queries):
        assert cadence == "weekly"
        assert fit_queries == ["agent cli memory"]
        return [
            _ScoutCandidate(
                platform="bitbucket",
                lane="activity",
                query="updated",
                full_name="team/automation-kit",
                description="Bitbucket automation toolkit",
                stars=0,
                language="Go",
                url="https://bitbucket.org/team/automation-kit",
                license_name="unknown",
                topics=[],
            )
        ]

    monkeypatch.setattr(scout, "_collect_gitlab_candidates", fake_gitlab_candidates)
    monkeypatch.setattr(scout, "_collect_bitbucket_candidates", fake_bitbucket_candidates)

    result = await scout.run_once(
        {
            "limit": 3,
            "cadence": "weekly",
            "queries": ["agent cli memory"],
            "platforms": ["gitlab", "bitbucket"],
        }
    )

    assert result["ok"] is True
    assert result["filed"] == 2
    assert {proposal.kind for proposal in store.created} == {"external_learning"}
    assert {proposal.payload["source_platform"] for proposal in store.created} == {"gitlab", "bitbucket"}
    assert {proposal.payload["repo"] for proposal in store.created} == {
        "gitlab-org/agent-lab",
        "team/automation-kit",
    }


@pytest.mark.asyncio
async def test_repo_scout_dedupes_across_external_learning(monkeypatch):
    store = _FakeStore()
    existing = _FakeProposal(
        "prop-existing",
        {
            "repo": "gitlab-org/agent-lab",
            "repo_key": "https://gitlab.com/gitlab-org/agent-lab",
            "repo_url": "https://gitlab.com/gitlab-org/agent-lab",
            "source_platform": "gitlab",
        },
    )
    existing.kind = "external_learning"
    store.created.append(existing)
    monkeypatch.setattr("skyn3t.cortex.repo_scout.get_store", lambda: store)
    orch = SimpleNamespace(agents={"github_explorer": _FakeExplorer()})
    scout = GitHubRepoScout(orchestrator=orch, event_bus=SimpleNamespace())

    async def fake_gitlab_candidates(*, cadence, fit_queries):
        return [
            _ScoutCandidate(
                platform="gitlab",
                lane="fit",
                query="agent cli memory",
                full_name="gitlab-org/agent-lab",
                description="GitLab agent workflow repo",
                stars=220,
                language="Python",
                url="https://gitlab.com/gitlab-org/agent-lab",
                license_name="MIT",
                topics=["agents"],
            )
        ]

    async def fake_bitbucket_candidates(*, cadence, fit_queries):
        return []

    monkeypatch.setattr(scout, "_collect_gitlab_candidates", fake_gitlab_candidates)
    monkeypatch.setattr(scout, "_collect_bitbucket_candidates", fake_bitbucket_candidates)

    result = await scout.run_once(
        {
            "limit": 3,
            "queries": ["agent cli memory"],
            "platforms": ["gitlab"],
        }
    )

    assert result["ok"] is True
    assert result["candidates_seen"] == 1
    assert result["filed"] == 0
    assert len(store.created) == 1


@pytest.mark.asyncio
async def test_repo_scout_scheduled_trigger_parses_prompt_and_runs(monkeypatch):
    ran = {}
    orch = SimpleNamespace(agents={})
    scout = GitHubRepoScout(orchestrator=orch, event_bus=SimpleNamespace())

    async def _fake_run_once(config):
        ran["config"] = config
        return {"ok": True}

    monkeypatch.setattr(scout, "run_once", _fake_run_once)

    scout._on_event(
        SimpleNamespace(
            payload={
                "kind": "scheduled_job_triggered",
                "payload": {
                    "agent_name": "github_repo_scout",
                    "prompt": json.dumps({"cadence": "weekly", "limit": 3}),
                },
            }
        )
    )
    await asyncio.sleep(0)

    assert ran["config"] == {"cadence": "weekly", "limit": 3}
