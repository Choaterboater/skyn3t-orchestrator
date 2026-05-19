"""End-to-end audit of the self-improving loop.

The 12 layers individually have unit tests; this one proves they're
actually connected as a single pipeline:

   signature_for_findings  →  ingest_project_event
       →  experience_index row written
       →  rank_fixes_for_signature returns the row
       →  CodeAgent._collect_ranked_fix_blocks formats it for the prompt
       →  mark_latest_unresolved_fix_worked resolves on retry

If any commit between #1 (prompt_compression) and #12 (loop wire-up)
breaks the join between these layers, this single test goes red.
Keeps the autonomy story honest in CI.
"""

from __future__ import annotations

import pytest

from skyn3t.intelligence.error_signatures import signature_for_findings
from skyn3t.memory.ingestor import ExperienceIngestor
from skyn3t.memory.store import MemoryStore


class _FakeRAG:
    """Minimal RAGEngine — just records the call and returns an embedding id."""

    def __init__(self):
        self._counter = 0
        self.last_metadata: dict | None = None

    async def add_knowledge_one(self, **kwargs):
        self._counter += 1
        self.last_metadata = kwargs.get("metadata")
        return f"emb-{self._counter}"


@pytest.mark.asyncio
async def test_loop_end_to_end_from_finding_to_ranked_prompt_block():
    """Seed one failure → fix → resolution cycle, then verify the
    follow-up build's CodeAgent recall surfaces the right ranked block.

    This is the *integration* contract: signature canonicalization,
    structured ingestion, SQL ranking, prompt formatting, and outcome
    resolution all need to agree on field names and shapes."""
    store = MemoryStore()
    ingestor = ExperienceIngestor(rag_engine=_FakeRAG(), memory_store=store)

    # 1. Findings → signature
    findings = [
        {"category": "Palette Schism", "severity": "blocker", "file": "src/App.jsx"},
        {"category": "minor", "severity": "warning", "file": "src/x.jsx"},
    ]
    signature = signature_for_findings(findings, source="contract")
    assert signature == "contract:palette_schism:App.jsx", (
        "signature_for_findings must prefer the blocker and produce the canonical format"
    )

    # 2. Runner publishes a CONTRACT_VERIFIER_BLOCKERS event → ingestor
    #    writes the experience_index row. Simulating that here by
    #    calling ingest_project_event directly (which is what
    #    _on_system_alert dispatches to).
    embedding_id = await ingestor.ingest_project_event(
        "CONTRACT_VERIFIER_BLOCKERS",
        {
            "project_slug": "homelab-v99",
            "stage": "contract_verifier",
            "stack": "react_vite",
            "findings": findings,
            "verdict": "needs_fix",
            "error_signature": signature,
            "fix_applied": None,    # not yet attempted
            "fix_worked": None,
        },
    )
    assert embedding_id is not None, "ingest_project_event should return an embedding id"

    # 3. The targeted fix runs and writes a follow-up row with the
    #    same signature plus a fix label. (Simulating what the runner
    #    does after apply_targeted_fix returns.)
    await store.record_experience_index(
        embedding_id="emb-fix-1",
        task_id="homelab-v99",
        stack="react_vite",
        stage="contract_verifier",
        error_signature=signature,
        fix_applied="regenerate:App.jsx",
        fix_worked=None,         # outcome unknown until next pass
        success=False,
    )

    # 4. Next pass: contract verifier passes. Runner calls
    #    mark_latest_unresolved_fix_worked. The fix row should resolve.
    resolved_eid = await store.mark_latest_unresolved_fix_worked(signature, True)
    assert resolved_eid == "emb-fix-1", (
        "mark_latest_unresolved_fix_worked must pick the row with fix_applied set, "
        "not the original blocker row"
    )

    # 5. NEXT canary's CodeAgent recall: rank_fixes_for_signature
    #    should return the resolved fix with a 100% win rate.
    ranked = await store.rank_fixes_for_signature(signature, limit=3)
    assert ranked == [
        {
            "fix_applied": "regenerate:App.jsx",
            "wins": 1,
            "attempts": 1,
            "rate": 1.0,
        },
    ]

    # 6. CodeAgent helper formats it into a prompt-ready block.
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.events import EventBus

    agent = CodeAgent("code", EventBus())
    await agent.initialize()
    blocks = await agent._collect_ranked_fix_blocks([signature])
    assert len(blocks) == 1
    block = blocks[0]
    # Must contain the canonical pieces the prompt relies on:
    assert signature in block, "signature must appear in the block"
    assert "regenerate:App.jsx" in block, "fix label must appear in the block"
    assert "100%" in block, "rate must be formatted as a percentage"
    assert "1/1" in block, "wins/attempts must be formatted"


@pytest.mark.asyncio
async def test_loop_consistency_source_is_distinct_from_contract():
    """Same finding category from different sources (contract vs.
    consistency reviewer) must produce DISTINCT signatures so the
    two systems don't pollute each other's rank tables."""
    finding = {"category": "missing_mount", "severity": "blocker", "file": "src/App.jsx"}
    contract_sig = signature_for_findings([finding], source="contract")
    consistency_sig = signature_for_findings([finding], source="consistency")

    assert contract_sig != consistency_sig
    assert contract_sig.startswith("contract:")
    assert consistency_sig.startswith("consistency:")

    # And the index treats them as separate buckets.
    store = MemoryStore()
    await store.record_experience_index(
        embedding_id="c-1", task_id="t", stack="react_vite", stage="contract_verifier",
        error_signature=contract_sig, fix_applied="regen:contract", fix_worked=True,
        success=True,
    )
    await store.record_experience_index(
        embedding_id="r-1", task_id="t", stack="react_vite", stage="consistency_reviewer",
        error_signature=consistency_sig, fix_applied="regen:consistency", fix_worked=False,
        success=False,
    )

    contract_ranked = await store.rank_fixes_for_signature(contract_sig)
    consistency_ranked = await store.rank_fixes_for_signature(consistency_sig)
    assert contract_ranked[0]["fix_applied"] == "regen:contract"
    assert consistency_ranked[0]["fix_applied"] == "regen:consistency"


@pytest.mark.asyncio
async def test_loop_unresolved_fix_stays_out_of_ranking():
    """A fix that was applied but never verified must NOT pollute the
    ranking — otherwise we'd recommend fixes whose outcome is unknown."""
    store = MemoryStore()
    sig = "contract:missing_mount:App.jsx"
    await store.record_experience_index(
        embedding_id="pending-1",
        task_id="t", stack="react_vite", stage="contract_verifier",
        error_signature=sig,
        fix_applied="regenerate:App.jsx",
        fix_worked=None,         # never resolved
        success=False,
    )
    ranked = await store.rank_fixes_for_signature(sig)
    assert ranked == [], (
        "unresolved fixes must not appear in the ranker — they have no "
        "evidence either way"
    )
