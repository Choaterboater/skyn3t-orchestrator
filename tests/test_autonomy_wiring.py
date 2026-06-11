"""Autonomous self-learning / self-healing wiring tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.core.self_healing import SelfHealingManager
from skyn3t.cortex.proposals import ProposalStore


@pytest.mark.asyncio
async def test_orchestrator_start_enables_autonomy_stack(tmp_path, monkeypatch):
    from skyn3t.core.orchestrator import Orchestrator

    monkeypatch.setenv("VECTOR_DB_PATH", str(tmp_path / "vectors"))
    monkeypatch.setenv("SKYN3T_AUTO_REGISTER_AGENTS", "false")
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()

    bus = EventBus()
    orch = Orchestrator(bus)
    assert orch._memory is None
    await orch.start(max_concurrent=1)
    try:
        assert orch._memory is not None
        assert orch._ingestor is not None
        assert orch._reflection is not None
        assert orch._tuner is not None
        assert orch._meta_agent is not None
        assert orch._cortex_started is True
    finally:
        await orch.stop()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_self_healing_restart_reinitializes_agent(event_bus):
    agent = MagicMock()
    agent.shutdown = AsyncMock()
    agent.initialize = AsyncMock()
    agent.start = AsyncMock()
    agent.status = "error"
    agent.config = {}
    agent.metadata = {"cache": {"x": 1}}
    agent._results = {"old": object()}

    orchestrator = SimpleNamespace(agents={"code": agent})
    shm = SelfHealingManager(event_bus)
    shm.set_orchestrator(orchestrator)
    await shm.start()

    action = MagicMock(agent_name="code", reason="error_threshold", attempts=0, max_attempts=3)
    await shm._handle_restart(action)

    agent.shutdown.assert_awaited_once()
    agent.initialize.assert_awaited_once()
    agent.start.assert_awaited_once()
    assert agent.status == "idle"
    await shm.stop()


@pytest.mark.asyncio
async def test_proposal_store_auto_approves_safe_tuning(tmp_path, monkeypatch):
    enabled = SimpleNamespace(
        cortex_auto_approve_system=True,
        cortex_auto_reject_duplicates=True,
        cortex_auto_reject_low_signal_ingest=True,
        cortex_auto_approve_safe_ingest=True,
        cortex_auto_triage_duplicate_window_seconds=86_400,
        cortex_auto_triage_min_ingest_topic_length=6,
        cortex_auto_triage_max_safe_ingest_limit=3,
        cortex_auto_approve_scout_ingest=True,
        cortex_auto_triage_max_scout_ingest_limit=10,
        cortex_auto_approve_safe_tuning=True,
        cortex_auto_approve_build_pattern_bias=True,
    )
    monkeypatch.setattr("skyn3t.config.settings.get_settings", lambda: enabled)

    store = ProposalStore(root=tmp_path / "proposals")
    applied = asyncio.Event()

    async def handler(payload):
        applied.set()
        return {"ok": True, "applied": True}

    store.register_handler("tuning", handler)
    proposal = store.create(
        kind="tuning",
        title="Tune claude",
        summary="Rate limit backoff",
        detail="detail",
        payload={
            "agent": "claude",
            "adjustments": [
                {
                    "parameter": "request_interval",
                    "change": "+0.5s",
                    "reason": "Rate limiting detected",
                }
            ],
        },
        source="self_tuner",
    )

    assert proposal.requires_approval is False
    await asyncio.wait_for(applied.wait(), timeout=1)
    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "applied"
    assert current.triage_decision == "auto_approved"


@pytest.mark.asyncio
async def test_proposal_store_auto_approves_build_pattern_bias(tmp_path, monkeypatch):
    enabled = SimpleNamespace(
        cortex_auto_approve_system=True,
        cortex_auto_reject_duplicates=True,
        cortex_auto_reject_low_signal_ingest=True,
        cortex_auto_approve_safe_ingest=True,
        cortex_auto_triage_duplicate_window_seconds=86_400,
        cortex_auto_triage_min_ingest_topic_length=6,
        cortex_auto_triage_max_safe_ingest_limit=3,
        cortex_auto_approve_scout_ingest=True,
        cortex_auto_triage_max_scout_ingest_limit=10,
        cortex_auto_approve_safe_tuning=True,
        cortex_auto_approve_build_pattern_bias=True,
    )
    monkeypatch.setattr("skyn3t.config.settings.get_settings", lambda: enabled)
    monkeypatch.setattr(
        "skyn3t.cortex.build_pattern_bias.apply_build_pattern_bias",
        AsyncMock(return_value={"ok": True, "status": "applied", "stack": "fastapi"}),
    )

    store = ProposalStore(root=tmp_path / "proposals")
    monkeypatch.setattr("skyn3t.cortex.get_store", lambda: store)
    from skyn3t.cortex.handlers import install_handlers

    install_handlers(SimpleNamespace(agents={}, memory_store=None, _ingestor=None))
    proposal = store.create(
        kind="feature",
        title="Build pattern: prefer winning shape for fastapi",
        summary="Adopt winning scaffold",
        detail="detail",
        payload={
            "kind": "build_pattern_bias",
            "stack": "fastapi",
            "winner_shape": ["main.py", "requirements.txt"],
            "winner_success_rate": 0.8,
            "winner_samples": 10,
            "loser_success_rate": 0.3,
            "distinguishing_files": ["tests/test_health.py"],
        },
        source="meta_agent:thresholds",
    )

    assert proposal.requires_approval is False
    for _ in range(30):
        current = store.get(proposal.id)
        if current is not None and current.status == "applied":
            break
        await asyncio.sleep(0)
    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "applied"
    assert current.triage_decision == "auto_approved"


@pytest.mark.asyncio
async def test_ingestor_records_successful_project_completion(tmp_path, monkeypatch):
    from skyn3t.memory.ingestor import ExperienceIngestor
    from skyn3t.rag.rag_engine import RAGEngine

    monkeypatch.setenv("VECTOR_DB_PATH", str(tmp_path / "vectors"))
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()

    rag = RAGEngine()
    await rag.initialize()
    ingestor = ExperienceIngestor(
        rag_engine=rag,
        seen_hashes_path=tmp_path / "seen.json",
    )
    await ingestor.initialize()

    embedding_id = await ingestor.ingest_project_event(
        "PROJECT_COMPLETED",
        {
            "slug": "habit-tracker",
            "status": "done",
            "stack": "react_vite",
            "verdict": "go",
        },
    )
    assert embedding_id is not None

    docs = await rag.query("habit-tracker PROJECT_COMPLETED", n_results=1)
    assert docs["documents"]
    metadata = docs["documents"][0].get("metadata") or {}
    assert metadata.get("success") is True

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_orchestrator_applies_live_tuning_from_alert(event_bus):
    from skyn3t.core.orchestrator import Orchestrator

    orch = Orchestrator(event_bus)
    agent = MagicMock()
    agent.config = {"timeout": 30}
    orch.agents = {"claude": agent}

    orch._on_system_alert(
        Event(
            event_type=EventType.SYSTEM_ALERT,
            source="gated_tuner",
            payload={
                "kind": "tuning_applied",
                "agent": "claude",
                "adjustments": [{"parameter": "timeout", "reason": "timeouts"}],
            },
        )
    )
    assert agent.config["timeout"] == 40


@pytest.mark.asyncio
async def test_proposal_store_auto_approves_ingest_in_no_approval_mode(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SKYN3T_AUTO_APPROVE", "1")
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()
    store = ProposalStore(root=tmp_path / "proposals")
    proposal = store.create(
        kind="ingest",
        title="Ingest high-limit scout repo",
        summary="scout finding",
        detail="detail",
        payload={
            "repo": "org/repo",
            "topic": "multi agent orchestrator",
            "limit": 50,
            "reuse_risk": "high",
        },
        source="repo_scout:github",
        force_requires_approval=True,
    )
    current = store.get(proposal.id)
    assert current is not None
    assert current.requires_approval is False
    assert current.triage_decision == "auto_approved"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_proposal_store_keeps_feature_gated_in_no_approval_mode(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SKYN3T_AUTO_APPROVE", "1")
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()
    store = ProposalStore(root=tmp_path / "proposals")
    proposal = store.create(
        kind="feature",
        title="Adapt scout finding",
        summary="self-update",
        detail="detail",
        payload={"idea": "port pattern from scout", "target_file": "skyn3t/web/app.py"},
        source="scout_adaptation:org/repo",
        force_requires_approval=True,
    )
    current = store.get(proposal.id)
    assert current is not None
    assert current.status == "pending"
    assert current.requires_approval is True
    get_settings.cache_clear()
