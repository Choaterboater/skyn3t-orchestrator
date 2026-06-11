"""Tests for competitive intel catalog and scout query merging."""

from skyn3t.cortex.competitive_intel import (
    build_competitive_adaptation_brief,
    competitive_scout_queries,
    extract_readme_signals,
    match_competitor,
    merge_scout_fit_queries,
)
from skyn3t.cortex.scout_adaptation import build_adaptation_idea


def test_match_competitor_known_repo():
    match = match_competitor("NousResearch/hermes-agent")
    assert match is not None
    assert match["name"] == "Hermes Agent"
    assert "messaging gateway" in match["patterns"][0]


def test_match_competitor_unknown_repo():
    assert match_competitor("acme/unknown-tool") is None


def test_competitive_adaptation_brief_includes_targets():
    brief = build_competitive_adaptation_brief(
        "dsifry/metaswarm",
        description="Spec-driven multi-agent framework",
        ingested_paths=["README.md"],
    )
    assert brief is not None
    assert "MetaSwarm" in brief
    assert "skyn3t/studio/runner.py" in brief
    assert "TDD enforcement" in brief


def test_build_adaptation_idea_uses_competitive_brief():
    idea = build_adaptation_idea(
        {
            "repo": "artaeon/forge-ai",
            "lane": "fit",
            "description": "Multi-agent build orchestrator",
            "ingested_paths": ["README.md"],
        }
    )
    assert "Forge" in idea
    assert "cost/token budget" in idea or "cost tracking" in idea


def test_merge_scout_fit_queries_dedupes():
    merged = merge_scout_fit_queries(
        ["multi agent orchestrator cli memory rag", "forge ai orchestrator cost tracking resume pipeline"]
    )
    assert len(merged) >= len(competitive_scout_queries())
    lowered = [q.lower() for q in merged]
    assert lowered.count("multi agent orchestrator cli memory rag") == 1


def test_extract_readme_signals():
    text = """
    # Agent orchestrator
    Uses git worktree isolation, MCP tools, cron scheduling,
    and SQLite for resume checkpoints with token budget caps.
    """
    signals = extract_readme_signals(text)
    assert "git worktree isolation" in signals
    assert "MCP tool surface" in signals
    assert "pipeline resume" in signals or "pipeline checkpoint" in signals
