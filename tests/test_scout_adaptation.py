"""Tests for scout → feature adaptation bridge."""

from types import SimpleNamespace

import pytest

from skyn3t.cortex.scout_adaptation import (
    build_adaptation_idea,
    file_adaptation_feature,
    should_spawn_feature,
)


def test_should_spawn_feature_only_for_scout_github():
    payload = {
        "repo": "octo/agent-flow",
        "lane": "fit",
        "reuse_risk": "low",
    }
    assert should_spawn_feature(payload, source="repo_scout:github", ingested_count=2) is True
    assert should_spawn_feature(payload, source="explorer", ingested_count=2) is False
    assert should_spawn_feature(payload, source="repo_scout:gitlab", ingested_count=2) is False


def test_should_spawn_feature_allows_popularity_lane():
    payload = {
        "repo": "octo/trending-tool",
        "lane": "popularity",
        "reuse_risk": "low",
    }
    assert should_spawn_feature(payload, source="repo_scout:github", ingested_count=1) is True


def test_should_spawn_feature_respects_high_reuse_risk(monkeypatch):
    monkeypatch.setattr(
        "skyn3t.config.settings.get_settings",
        lambda: SimpleNamespace(
            cortex_scout_spawn_features=True,
            cortex_scout_spawn_min_ingested=1,
        ),
    )
    payload = {"repo": "octo/x", "lane": "fit", "reuse_risk": "high"}
    assert should_spawn_feature(payload, source="repo_scout:github", ingested_count=1) is False


def test_build_adaptation_idea_includes_repo_and_query():
    idea = build_adaptation_idea(
        {
            "repo": "octo/agent-flow",
            "query": "agent cli memory",
            "lane": "fit",
            "description": "Workflow toolkit",
            "topics": ["agents", "cli"],
            "language": "Python",
        }
    )
    assert "octo/agent-flow" in idea
    assert "agent cli memory" in idea
    assert "SkyN3t" in idea


def test_file_adaptation_feature_dedupes(tmp_path, monkeypatch):
    from skyn3t.cortex import get_store
    from skyn3t.cortex.proposals import ProposalStore

    monkeypatch.setattr(
        "skyn3t.config.settings.get_settings",
        lambda: SimpleNamespace(
            cortex_scout_spawn_features=True,
            cortex_scout_spawn_min_ingested=1,
        ),
    )
    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: store)

    payload = {
        "repo": "octo/agent-flow",
        "repo_key": "github:octo/agent-flow",
        "query": "agent cli memory",
        "lane": "fit",
        "reuse_risk": "low",
    }
    first = file_adaptation_feature(
        payload=payload,
        source="repo_scout:github",
        parent_proposal_id="ingest-1",
        ingested_count=2,
        ingested_paths=["README.md"],
    )
    second = file_adaptation_feature(
        payload=payload,
        source="repo_scout:github",
        parent_proposal_id="ingest-2",
        ingested_count=2,
        ingested_paths=["README.md"],
    )

    assert first
    assert second is None
    assert len([p for p in store.list() if p.kind == "feature"]) == 1
