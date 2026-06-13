"""Tests for moat-first plan implementations."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def test_studio_smoke_script_uses_bash3_compatible_py_compile(tmp_path: Path) -> None:
    script = Path("scripts/studio_smoke.sh").read_text(encoding="utf-8")
    assert "mapfile -t" not in script
    assert "py_compile" in script


def test_domain_corpus_prompt_for_networking_brief() -> None:
    from skyn3t.intelligence.domain_corpus_prompts import (
        brief_is_networking,
        corpus_prompt_block,
    )

    assert brief_is_networking("Build an Aruba Central field triage dashboard")
    block = asyncio.run(corpus_prompt_block("Aruba Central CLI with dry-run"))
    assert "dry-run" in block.lower() or "golden" in block.lower()


def test_live_read_gate_defaults_to_dry_run() -> None:
    from skyn3t.security.live_read_gate import live_read_gate_status

    status = live_read_gate_status(slug="demo")
    assert status["dry_run_default"] is True
    assert status["approved"] is False


@pytest.mark.asyncio
async def test_reconcile_stale_tasks_marks_running_failed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()

    from skyn3t.core.models import init_db
    from skyn3t.memory.store import MemoryStore

    await init_db()
    store = MemoryStore()
    await store.save_task(
        task_id="stale-1",
        title="running task",
        description="",
        status="running",
        priority=0,
        agent_id=None,
        agent_name=None,
        parent_task_id=None,
        input_data={},
        output_data={},
        error_message=None,
        retry_count=0,
        max_retries=3,
        started_at=None,
        completed_at=None,
    )
    report = await store.reconcile_stale_tasks()
    assert report["updated"] == 1
    task = await store.get_task("stale-1")
    assert task is not None
    assert task["status"] == "failed"


def test_token_tracker_prefers_provider_usage() -> None:
    from skyn3t.observability.token_tracker import TokenTracker

    tracker = TokenTracker()

    class _Evt:
        payload = {
            "agent": "test",
            "prompt_tokens": 1200,
            "response_tokens": 300,
        }

    tracker._on_exchange(_Evt())
    rows = tracker.per_agent()
    assert rows[0]["total_tokens"] == 1500


def test_code_agent_emits_smoke_tests(tmp_path: Path) -> None:
    from skyn3t.agents.code_agent import CodeAgent

    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "main.py").write_text("print('ok')\n", encoding="utf-8")
    written = CodeAgent._ensure_smoke_tests(scaffold, {})
    assert any("test_smoke.py" in p for p in written)


def test_debate_enabled_for_deep_profile(monkeypatch) -> None:
    from skyn3t.agents import debate as _debate

    monkeypatch.delenv("SKYN3T_DEBATE", raising=False)
    assert _debate.debate_enabled_for_profile("architect", "deep") is True
    assert _debate.debate_enabled_for_profile("architect", "balanced") is False


def test_a2a_enabled_for_deep_profile(monkeypatch) -> None:
    from skyn3t.core.orchestrator import a2a_conversation_enabled_for_profile

    monkeypatch.delenv("SKYN3T_A2A_CONVERSATION", raising=False)
    assert a2a_conversation_enabled_for_profile("deep") is True
    assert a2a_conversation_enabled_for_profile("balanced") is False


def test_distill_skill_from_high_scoring_build(tmp_path, monkeypatch) -> None:
    from skyn3t.intelligence.skill_library import SkillLibrary
    from skyn3t.intelligence import skills_hub

    lib = SkillLibrary(root=tmp_path / "skills")
    monkeypatch.setattr(
        "skyn3t.intelligence.skill_library.get_default_library",
        lambda: lib,
    )

    result = skills_hub.distill_skill_from_build(
        slug="unique-demo-app-xyz",
        brief="Build a FastAPI health dashboard",
        stack="python",
        score=88,
    )
    assert result is not None
    assert result.get("draft")


def test_live_read_api_endpoints(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import skyn3t.web.app as web_app

    approvals = tmp_path / "live_read_approvals.json"
    monkeypatch.setattr(
        "skyn3t.security.live_read_gate._APPROVAL_FILE",
        approvals,
    )
    client = TestClient(web_app.app)
    status = client.get("/api/security/live-read/status")
    assert status.status_code == 200
    assert status.json()["dry_run_default"] is True
    approve = client.post("/api/security/live-read/approve", json={"slug": "demo"})
    assert approve.status_code == 200
    assert approve.json()["ok"] is True
    status2 = client.get("/api/security/live-read/status", params={"slug": "demo"})
    assert status2.json()["approved"] is True


@pytest.mark.slow
@pytest.mark.asyncio
async def test_fleet_load_semaphore_invariants(monkeypatch, fake_autonomous):
    """N concurrent fake builds must respect the fleet semaphore cap."""
    from unittest.mock import MagicMock

    from skyn3t.config.settings import get_settings
    from skyn3t.cortex.agent_fleet import AgentFleetCoordinator, FleetSlot
    from skyn3t.cortex.autonomous_loop import AutonomousBrief

    monkeypatch.setenv("SKYN3T_AGENT_FLEET_SIZE", "4")
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_BUILDS", "1")
    get_settings.cache_clear()

    coord, runner, orch = fake_autonomous
    coord.set_fleet_delegates_builds(True)
    fleet = AgentFleetCoordinator(orch, MagicMock())
    fleet._fleet_sem = asyncio.Semaphore(3)
    fleet._learning_sem = asyncio.Semaphore(1)
    fleet._learning_parallel = 1
    fleet._slots = [FleetSlot(slot_id=i) for i in range(4)]

    for i in range(4):
        await coord.enqueue_brief(
            AutonomousBrief(brief=f"Build load test app {i}", source="test")
        )

    in_flight = 0
    peak = 0

    async def tracked_build(slot, brief):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await fleet._run_build_slot(slot, brief)
        finally:
            in_flight -= 1

    tasks = []
    for slot in fleet._slots[:4]:
        brief = coord.pop_highest_priority_brief()
        if brief is None:
            break
        tasks.append(asyncio.create_task(tracked_build(slot, brief)))
    await asyncio.gather(*tasks)
    assert peak <= 3
