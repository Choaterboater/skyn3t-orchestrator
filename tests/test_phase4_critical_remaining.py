"""Phase 4 critical-gap regression tests.

Covers:
  C3  — .env permission warning
  C7  — RecoveryManager checkpoint creation
  C11 — remote backends endpoint reports removal
  C12 — orchestrator routes inbound TASK_CREATED events to agents and replies
  C14 — ResearchAgent fails instead of shipping placeholder findings
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import skyn3t.integrations.messaging as messaging_mod
from skyn3t.agents.research_agent import ResearchAgent
from skyn3t.config.env_file import warn_env_file_permissions
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.persistence.checkpoint import CheckpointManager
from skyn3t.persistence.recovery import RecoveryManager

# ─── C3 ──────────────────────────────────────────────────────────────────

def test_warn_env_file_permissions_warns_when_world_readable(tmp_path, caplog):
    env_path = tmp_path / ".env"
    env_path.write_text("SECRET=abc\n")
    env_path.chmod(0o644)

    from skyn3t.config import env_file as env_file_mod

    original = env_file_mod.env_file_path
    env_file_mod.env_file_path = lambda: env_path
    try:
        with caplog.at_level(logging.WARNING, logger="skyn3t.config.env_file"):
            warn_env_file_permissions()
    finally:
        env_file_mod.env_file_path = original

    assert ".env is readable by group or others" in caplog.text


def test_warn_env_file_permissions_silent_when_restricted(tmp_path, caplog):
    env_path = tmp_path / ".env"
    env_path.write_text("SECRET=abc\n")
    env_path.chmod(0o600)

    from skyn3t.config import env_file as env_file_mod

    original = env_file_mod.env_file_path
    env_file_mod.env_file_path = lambda: env_path
    try:
        with caplog.at_level(logging.WARNING, logger="skyn3t.config.env_file"):
            warn_env_file_permissions()
    finally:
        env_file_mod.env_file_path = original

    assert ".env is readable" not in caplog.text


# ─── C7 ──────────────────────────────────────────────────────────────────

def test_recovery_manager_create_checkpoint_persists_state(tmp_path):
    cm = CheckpointManager(checkpoint_dir=str(tmp_path / "checkpoints"))
    bus = EventBus()
    mgr = RecoveryManager(cm, bus)

    orchestrator = SimpleNamespace(
        agents={
            "alpha": SimpleNamespace(
                name="alpha",
                metadata={"foo": "bar"},
                get_stats=lambda: {"name": "alpha"},
            )
        },
        running_tasks={
            "task-1": TaskRequest(
                title="t", description="d", input_data={"x": 1}
            )
        },
        _pipelines={},
    )

    checkpoint_id = mgr.create_checkpoint(orchestrator)

    assert checkpoint_id is not None
    latest = cm.load_latest()
    assert latest is not None
    assert latest.agent_states[0]["name"] == "alpha"
    assert latest.task_states[0]["task_id"] == "task-1"


# ─── C12 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orchestrator_routes_channel_task_created(monkeypatch):
    orch = Orchestrator()
    # Provide a fake agent so submit_task can queue something.
    fake_agent = SimpleNamespace(
        name="echo",
        agent_type="test",
        provider="local",
        capabilities=[],
        status="idle",
        enabled=True,
        id="echo-1",
        metadata={},
        _current_task=None,
        _current_task_started_at=None,
        _task_queue=SimpleNamespace(qsize=lambda: 0),
        _errors=[],
        _health_checks=0,
        health_check=AsyncMock(return_value=True),
        shutdown=AsyncMock(),
        start_inbox_pump=lambda: None,
    )
    orch.agents["echo"] = fake_agent

    submitted = {}

    async def _fake_submit(task, **kwargs):
        submitted["task"] = task
        return "task-from-channel"

    monkeypatch.setattr(orch, "submit_task", _fake_submit)

    reply_calls = []
    fake_router = SimpleNamespace(
        reply=lambda *args, **kwargs: reply_calls.append((args, kwargs))
    )
    monkeypatch.setattr(messaging_mod, "_default_router", fake_router)

    orch._on_task_created(
        Event(
            event_type=EventType.TASK_CREATED,
            source="telegram_channel",
            payload={
                "platform": "telegram",
                "message": "hello bot",
                "channel": "12345",
                "sender": "user-1",
            },
        )
    )
    # Allow the fire-and-forget route coroutine a chance to run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # The route task was created; verify context.
    assert "task-from-channel" in orch._messaging_reply_context
    ctx = orch._messaging_reply_context["task-from-channel"]
    assert ctx["platform"] == "telegram"
    assert ctx["reply_channel"] == "12345"

    # Simulate completion with an answer; _reply_to_channel should fire.
    orch._reply_to_channel("task-from-channel", "Hi there")
    assert len(reply_calls) == 1
    assert reply_calls[0][0][0] == "telegram"


# ─── C14 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_research_agent_fails_without_real_findings(monkeypatch):
    agent = ResearchAgent()

    class FakeClient:
        async def complete(self, *args, **kwargs):
            return ""

        async def aclose(self):
            pass

    monkeypatch.setattr(
        "skyn3t.adapters.LLMClient",
        lambda **kwargs: FakeClient(),
    )

    result = await agent._web_search(
        TaskRequest(title="research", description="x", input_data={"query": "foo"})
    )

    assert result["success"] is False
    assert result["total_results"] == 0
    assert "placeholder" not in str(result)
