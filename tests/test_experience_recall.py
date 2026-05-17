"""Tests for the Phase-2 experience index + ranked fix recall.

The vector store handles "find experiences similar to this prose"; this
table answers a tighter question the planner cares about: for THIS
error signature, which fix has the best historical win rate?

Tests cover:
- record_experience_index inserts one row per embedding_id (dedup)
- mark_fix_worked updates an existing row's resolution
- rank_fixes_for_signature aggregates by fix_applied, sorted by rate
  with attempts as the tiebreaker
- only resolved rows (fix_applied + non-null fix_worked) are scored
- ExperienceIngestor.ingest_task_experience writes through to the
  index when called with the structured kwargs
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import pytest

from skyn3t.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_record_experience_index_inserts_row():
    store = MemoryStore()
    await store.record_experience_index(
        embedding_id="emb-001",
        task_id="task-A",
        stack="react_vite",
        stage="code",
        error_signature="vite_dryrun:missing_mount",
        fix_applied="add_root_div",
        fix_worked=True,
        success=False,
    )
    ranked = await store.rank_fixes_for_signature("vite_dryrun:missing_mount")
    assert ranked == [
        {"fix_applied": "add_root_div", "wins": 1, "attempts": 1, "rate": 1.0},
    ]


@pytest.mark.asyncio
async def test_record_experience_index_dedupes_on_embedding_id():
    store = MemoryStore()
    for _ in range(3):
        await store.record_experience_index(
            embedding_id="emb-dup",
            task_id="task-A",
            stack="react_vite",
            stage="code",
            error_signature="sig",
            fix_applied="fix-1",
            fix_worked=True,
            success=False,
        )
    ranked = await store.rank_fixes_for_signature("sig")
    # Three inserts, one logical row.
    assert ranked[0]["attempts"] == 1


@pytest.mark.asyncio
async def test_record_experience_index_ignores_empty_embedding_id():
    store = MemoryStore()
    await store.record_experience_index(
        embedding_id="",
        task_id="task-A",
        stack=None, stage=None, error_signature=None,
        fix_applied=None, fix_worked=None, success=False,
    )
    assert await store.rank_fixes_for_signature("anything") == []


@pytest.mark.asyncio
async def test_mark_fix_worked_updates_existing_row():
    store = MemoryStore()
    await store.record_experience_index(
        embedding_id="emb-100",
        task_id="task-B",
        stack="react_vite", stage="code",
        error_signature="sig-A",
        fix_applied="fix-X",
        fix_worked=None,        # not yet resolved
        success=False,
    )
    # Not yet scoreable.
    assert await store.rank_fixes_for_signature("sig-A") == []
    updated = await store.mark_fix_worked("emb-100", True)
    assert updated is True
    ranked = await store.rank_fixes_for_signature("sig-A")
    assert ranked[0] == {"fix_applied": "fix-X", "wins": 1, "attempts": 1, "rate": 1.0}


@pytest.mark.asyncio
async def test_mark_fix_worked_returns_false_when_missing():
    store = MemoryStore()
    assert await store.mark_fix_worked("never-recorded", True) is False


@pytest.mark.asyncio
async def test_rank_fixes_sorts_by_rate_then_attempts():
    store = MemoryStore()
    # Seed:
    #   add_root_div: 4 wins / 0 loss   → rate 1.00, attempts 4
    #   rewrite_main: 1 win  / 0 loss   → rate 1.00, attempts 1
    #   hack_html:    1 win  / 3 loss   → rate 0.25, attempts 4
    seeds = [
        ("add_root_div", True), ("add_root_div", True),
        ("add_root_div", True), ("add_root_div", True),
        ("rewrite_main", True),
        ("hack_html", True),
        ("hack_html", False), ("hack_html", False), ("hack_html", False),
    ]
    for i, (label, worked) in enumerate(seeds):
        await store.record_experience_index(
            embedding_id=f"emb-{i}",
            task_id=f"task-{i}",
            stack="react_vite", stage="code",
            error_signature="vite:missing_mount",
            fix_applied=label,
            fix_worked=worked,
            success=worked,
        )
    ranked = await store.rank_fixes_for_signature("vite:missing_mount")
    # First two share rate 1.0 → ranked by attempts descending.
    assert ranked[0]["fix_applied"] == "add_root_div"
    assert ranked[0]["attempts"] == 4
    assert ranked[1]["fix_applied"] == "rewrite_main"
    assert ranked[1]["attempts"] == 1
    assert ranked[2]["fix_applied"] == "hack_html"
    assert abs(ranked[2]["rate"] - 0.25) < 1e-6


@pytest.mark.asyncio
async def test_rank_fixes_skips_unresolved_rows():
    """Rows where fix_worked is None must NOT count toward the
    denominator — they're "we tried but don't know if it worked"."""
    store = MemoryStore()
    await store.record_experience_index(
        embedding_id="emb-resolved",
        task_id="t1",
        stack="react_vite", stage="code",
        error_signature="sig",
        fix_applied="fix-A", fix_worked=True, success=True,
    )
    await store.record_experience_index(
        embedding_id="emb-pending",
        task_id="t2",
        stack="react_vite", stage="code",
        error_signature="sig",
        fix_applied="fix-A", fix_worked=None, success=False,
    )
    ranked = await store.rank_fixes_for_signature("sig")
    assert ranked[0]["attempts"] == 1
    assert ranked[0]["wins"] == 1


@pytest.mark.asyncio
async def test_rank_fixes_skips_rows_without_fix_label():
    """A failure with no fix attempted shouldn't appear in the ranking
    even though it shares an error_signature."""
    store = MemoryStore()
    await store.record_experience_index(
        embedding_id="emb-no-fix",
        task_id="t",
        stack="react_vite", stage="code",
        error_signature="sig",
        fix_applied=None, fix_worked=None, success=False,
    )
    await store.record_experience_index(
        embedding_id="emb-with-fix",
        task_id="t",
        stack="react_vite", stage="code",
        error_signature="sig",
        fix_applied="fix-A", fix_worked=True, success=True,
    )
    ranked = await store.rank_fixes_for_signature("sig")
    assert len(ranked) == 1
    assert ranked[0]["fix_applied"] == "fix-A"


@pytest.mark.asyncio
async def test_rank_fixes_respects_limit():
    store = MemoryStore()
    for i in range(7):
        await store.record_experience_index(
            embedding_id=f"emb-{i}",
            task_id="t",
            stack="react_vite", stage="code",
            error_signature="sig",
            fix_applied=f"fix-{i}", fix_worked=True, success=True,
        )
    ranked = await store.rank_fixes_for_signature("sig", limit=3)
    assert len(ranked) == 3


@pytest.mark.asyncio
async def test_rank_fixes_returns_empty_for_unknown_signature():
    store = MemoryStore()
    assert await store.rank_fixes_for_signature("") == []
    assert await store.rank_fixes_for_signature("never-recorded") == []


# ---------------------------------------------------------------------
# Ingestor integration: ingest_task_experience writes the index row
# ---------------------------------------------------------------------


class _FakeRAG:
    """Minimal RAGEngine stand-in: records add_knowledge_one calls
    and returns deterministic embedding ids."""

    def __init__(self):
        self.calls = []
        self._counter = 0

    async def add_knowledge_one(self, **kwargs):
        self._counter += 1
        eid = f"emb-{self._counter}"
        self.calls.append({"embedding_id": eid, **kwargs})
        return eid


@pytest.mark.asyncio
async def test_ingest_task_experience_writes_index_row():
    from skyn3t.memory.ingestor import ExperienceIngestor

    store = MemoryStore()
    rag = _FakeRAG()
    ingestor = ExperienceIngestor(rag_engine=rag, memory_store=store)

    embedding_id = await ingestor.ingest_task_experience(
        task_id="task-X",
        agent_name="code_agent",
        success=False,
        output={},
        error="Vite dry-run found no mount node",
        stack="react_vite",
        stage="code",
        error_signature="vite:missing_mount",
        fix_applied="add_root_div",
        fix_worked=True,
        brief_shape=["dashboard", "integrations"],
    )
    assert embedding_id is not None
    ranked = await store.rank_fixes_for_signature("vite:missing_mount")
    assert len(ranked) == 1
    assert ranked[0]["fix_applied"] == "add_root_div"
    # The RAG side also captured the structured fields in metadata.
    assert rag.calls[0]["metadata"]["error_signature"] == "vite:missing_mount"
    assert rag.calls[0]["metadata"]["stack"] == "react_vite"


@pytest.mark.asyncio
async def test_ingest_without_structured_fields_still_works():
    """Legacy callers that don't pass the new kwargs should still
    succeed; the index row lands with nulls and isn't queryable
    by error_signature, but the embedding is fine."""
    from skyn3t.memory.ingestor import ExperienceIngestor

    store = MemoryStore()
    ingestor = ExperienceIngestor(rag_engine=_FakeRAG(), memory_store=store)
    eid = await ingestor.ingest_task_experience(
        task_id="legacy", agent_name="some_agent",
        success=True, output={"ok": True},
    )
    assert eid is not None
    assert await store.rank_fixes_for_signature("anything") == []


# ---------------------------------------------------------------------
# CodeAgent ranked-fix prompt injection (Phase 2 last mile)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_code_agent_collects_ranked_fix_blocks():
    """The injection helper queries MemoryStore for each signature
    and formats prompt-ready blocks. Signatures with no resolved
    fixes are silently skipped (no empty block emitted)."""
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.events import EventBus

    store = MemoryStore()
    # Two resolved fixes for sig-A — winner has more attempts.
    for i, worked in enumerate([True, True, True, False, False]):
        await store.record_experience_index(
            embedding_id=f"emb-A-{i}",
            task_id="t",
            stack="react_vite", stage="code",
            error_signature="sig-A",
            fix_applied="add_root_div" if worked else "rewrite_main",
            fix_worked=worked,
            success=worked,
        )
    # sig-B has no recorded fixes at all.
    agent = CodeAgent("code", EventBus())
    await agent.initialize()

    blocks = await agent._collect_ranked_fix_blocks(["sig-A", "sig-B"])
    assert len(blocks) == 1
    assert "sig-A" in blocks[0]
    assert "add_root_div" in blocks[0]
    # Win-rate formatting: 3 wins / 3 attempts = 100%.
    assert "3/3" in blocks[0]
    assert "100%" in blocks[0]


@pytest.mark.asyncio
async def test_code_agent_collect_returns_empty_when_store_unreachable(monkeypatch):
    """If MemoryStore() raises (no DB), the helper returns [] without
    crashing the scaffold flow."""
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.events import EventBus

    class _BrokenStore:
        def __init__(self, *_a, **_k):
            raise RuntimeError("no db today")

    import skyn3t.memory.store as store_module
    monkeypatch.setattr(store_module, "MemoryStore", _BrokenStore)

    agent = CodeAgent("code", EventBus())
    await agent.initialize()
    assert await agent._collect_ranked_fix_blocks(["sig-X"]) == []


@pytest.mark.asyncio
async def test_ingest_index_failure_does_not_drop_embedding(caplog):
    """If MemoryStore.record_experience_index raises, the experience
    is still ingested into RAG (return value non-None). Index failures
    log at debug level."""
    from skyn3t.memory.ingestor import ExperienceIngestor

    class _BrokenStore:
        async def record_experience_index(self, **_k):
            raise RuntimeError("index on fire")

    rag = _FakeRAG()
    ingestor = ExperienceIngestor(rag_engine=rag, memory_store=_BrokenStore())
    with caplog.at_level(logging.DEBUG, logger="skyn3t.memory.ingestor"):
        eid = await ingestor.ingest_task_experience(
            task_id="t", agent_name="a", success=True, output={},
            stack="react_vite", error_signature="sig", fix_applied="x",
        )
    assert eid is not None  # RAG add succeeded
    assert any("index on fire" in (r.getMessage() + str(r.exc_info)) for r in caplog.records)
