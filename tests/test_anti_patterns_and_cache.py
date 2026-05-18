"""Tests for the smarter-loop additions:

- detect_stack memoization (cache hit doesn't re-evaluate triggers)
- MemoryStore.anti_patterns_for_signature (loser-end of rank_fixes_for)
- CodeAgent's combined winners + anti-patterns prompt block
"""

from __future__ import annotations

import asyncio

import pytest

from skyn3t.agents.stack_templates import detect_stack
from skyn3t.memory.store import MemoryStore


# ---------------------------------------------------------------------
# detect_stack memoization
# ---------------------------------------------------------------------


def test_detect_stack_returns_consistent_values():
    """Sanity check on the cached function — same inputs, same outputs."""
    brief = "Build a Vite React dashboard with a server/index.js"
    assert detect_stack(brief) == detect_stack(brief)
    assert detect_stack("") is None


def test_detect_stack_is_cached():
    """Calling many times should reuse the cache, not re-walk triggers.

    We don't peek at internal state — pull the wrapped cache stats
    via functools' attribute and check hit count grows."""
    detect_stack.cache_clear()
    brief = "React Vite homelab dashboard"
    detect_stack(brief)
    detect_stack(brief)
    detect_stack(brief)
    info = detect_stack.cache_info()
    # First call is a miss; next two are hits.
    assert info.hits == 2
    assert info.misses == 1


def test_detect_stack_distinct_briefs_get_distinct_cache_entries():
    detect_stack.cache_clear()
    detect_stack("React Vite app")
    detect_stack("FastAPI service")
    detect_stack("Next.js dashboard")
    info = detect_stack.cache_info()
    assert info.misses == 3
    assert info.currsize == 3


# ---------------------------------------------------------------------
# anti_patterns_for_signature
# ---------------------------------------------------------------------


async def _seed(store: MemoryStore, fix: str, outcomes: list[bool], sig: str = "sig"):
    """Helper: drop N (embedding_id, fix, fix_worked) rows for one sig."""
    for i, worked in enumerate(outcomes):
        await store.record_experience_index(
            embedding_id=f"emb-{fix}-{i}",
            task_id="t", stack="react_vite", stage="contract_verifier",
            error_signature=sig,
            fix_applied=fix,
            fix_worked=worked,
            success=worked,
        )


@pytest.mark.asyncio
async def test_anti_patterns_returns_losers_only():
    store = MemoryStore()
    await _seed(store, "winner", [True] * 5)         # 100% rate
    await _seed(store, "loser",  [False] * 4 + [True])  # 1/5 = 20%
    await _seed(store, "middle", [True, False])      # 50%

    anti = await store.anti_patterns_for_signature("sig")
    labels = [r["fix_applied"] for r in anti]
    assert "loser" in labels
    assert "winner" not in labels   # too good
    assert "middle" not in labels   # above default max_rate


@pytest.mark.asyncio
async def test_anti_patterns_respects_min_attempts():
    """One-shot failures must not count as an anti-pattern — too noisy."""
    store = MemoryStore()
    await _seed(store, "one_off", [False])           # 1 attempt
    await _seed(store, "tried_hard", [False] * 4)    # 4 attempts

    anti = await store.anti_patterns_for_signature("sig", min_attempts=2)
    labels = [r["fix_applied"] for r in anti]
    assert "tried_hard" in labels
    assert "one_off" not in labels


@pytest.mark.asyncio
async def test_anti_patterns_sorted_worst_first():
    store = MemoryStore()
    await _seed(store, "bad",   [False] * 5)         # 0%
    await _seed(store, "worse", [False] * 10)        # 0% but more attempts
    await _seed(store, "okayish", [False] * 2 + [True])  # 33%

    anti = await store.anti_patterns_for_signature("sig", limit=3)
    # Both 0%-rate fixes appear; the one with more attempts comes first
    # (tiebreaker is -attempts so the more-tried failure ranks worst).
    assert anti[0]["fix_applied"] == "worse"
    assert anti[1]["fix_applied"] == "bad"
    assert anti[2]["fix_applied"] == "okayish"


@pytest.mark.asyncio
async def test_anti_patterns_respects_limit():
    store = MemoryStore()
    for i in range(6):
        await _seed(store, f"bad-{i}", [False] * 3)
    anti = await store.anti_patterns_for_signature("sig", limit=2)
    assert len(anti) == 2


@pytest.mark.asyncio
async def test_anti_patterns_returns_empty_for_unknown_signature():
    store = MemoryStore()
    assert await store.anti_patterns_for_signature("never-recorded") == []
    assert await store.anti_patterns_for_signature("") == []


@pytest.mark.asyncio
async def test_anti_patterns_excludes_unresolved_fixes():
    """fix_worked=None means unresolved — not enough evidence either way."""
    store = MemoryStore()
    await store.record_experience_index(
        embedding_id="pending", task_id="t",
        stack="x", stage="y", error_signature="sig",
        fix_applied="unknown_outcome", fix_worked=None, success=False,
    )
    assert await store.anti_patterns_for_signature("sig") == []


# ---------------------------------------------------------------------
# CodeAgent prompt block now carries both Winners + Anti-patterns
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_code_agent_block_has_winners_and_anti_patterns():
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.events import EventBus

    store = MemoryStore()
    # 3 wins for winner, 3 losses for loser, all under same signature.
    await _seed(store, "regenerate:App.jsx", [True, True, True], sig="contract:palette")
    await _seed(store, "rewrite_main", [False, False, False], sig="contract:palette")

    agent = CodeAgent("code", EventBus())
    await agent.initialize()
    blocks = await agent._collect_ranked_fix_blocks(["contract:palette"])

    assert len(blocks) == 1
    block = blocks[0]
    assert "Winners (prefer):" in block
    assert "regenerate:App.jsx" in block
    assert "Anti-patterns (avoid):" in block
    assert "rewrite_main" in block
    # Win-rate format intact.
    assert "100%" in block
    assert "0%" in block


@pytest.mark.asyncio
async def test_code_agent_block_omits_anti_section_when_none_qualify():
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.events import EventBus

    store = MemoryStore()
    # Only winners exist for this signature.
    await _seed(store, "regenerate:App.jsx", [True, True], sig="contract:only_winners")

    agent = CodeAgent("code", EventBus())
    await agent.initialize()
    blocks = await agent._collect_ranked_fix_blocks(["contract:only_winners"])

    block = blocks[0]
    assert "Winners (prefer):" in block
    assert "Anti-patterns (avoid):" not in block


@pytest.mark.asyncio
async def test_code_agent_block_omits_winners_section_when_only_losers():
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.events import EventBus

    store = MemoryStore()
    # Only losers exist for this signature.
    await _seed(store, "always_fails", [False, False, False], sig="contract:only_losers")

    agent = CodeAgent("code", EventBus())
    await agent.initialize()
    blocks = await agent._collect_ranked_fix_blocks(["contract:only_losers"])

    # 0% rate fixes still appear in rank_fixes_for_signature (sorted by
    # rate desc, attempts as tiebreaker) AND in anti_patterns. We
    # render them in BOTH sections — the model sees the same label
    # under "Winners" with 0% rate and under "Anti-patterns" with the
    # explicit avoid framing. That's fine; the avoid framing is the
    # stronger signal.
    block = blocks[0]
    assert "always_fails" in block
    assert "Anti-patterns (avoid):" in block
