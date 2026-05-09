"""Tests for the SkyN3t brain/memory layer."""

import asyncio
import pytest

from skyn3t.memory.store import MemoryStore
from skyn3t.memory.consciousness import CollectiveConsciousness
from skyn3t.memory.tuner import SelfTuningEngine
from skyn3t.memory.meta_agent import MetaAgent
from skyn3t.core.events import EventBus, EventType


def run_async(coro):
    """Helper to run async code in sync tests."""
    return asyncio.run(coro)


class TestMemoryStore:
    def test_save_and_get_task(self):
        store = MemoryStore()
        run_async(store.save_task(
            task_id="t-1",
            title="Test",
            description="Desc",
            status="completed",
            priority=1,
            agent_id=None,
            agent_name="claude",
            parent_task_id=None,
            input_data={"msg": "hi"},
            output_data={"resp": "hello", "_meta": {"agent_name": "claude"}},
            error_message=None,
            retry_count=0,
            max_retries=3,
            started_at=None,
            completed_at=None,
            session_id="sess-1",
        ))
        task = run_async(store.get_task("t-1"))
        assert task is not None
        assert task["title"] == "Test"
        assert task["status"] == "completed"
        assert task["agent_name"] == "claude"

    def test_save_agent(self):
        store = MemoryStore()
        run_async(store.save_agent(
            agent_id="a-1", name="claude", agent_type="llm", provider="anthropic",
            status="idle", capabilities=["code"], config={}, meta={},
        ))
        agent = run_async(store.get_agent("claude"))
        assert agent is not None
        assert agent["name"] == "claude"
        assert "code" in agent["capabilities"]

    def test_save_message(self):
        import uuid
        store = MemoryStore()
        unique = str(uuid.uuid4())[:8]
        msg_id = run_async(store.save_message(
            source_agent=f"claude-{unique}", target_agent=f"kimi-{unique}",
            content=f"Hello-{unique}", message_type="chat",
        ))
        assert msg_id is not None
        msgs = run_async(store.get_messages_between(f"claude-{unique}", f"kimi-{unique}"))
        assert len(msgs) >= 1
        assert msgs[0]["content"] == f"Hello-{unique}"

    def test_save_lesson(self):
        import uuid
        store = MemoryStore()
        unique = str(uuid.uuid4())[:8]
        doc_id = run_async(store.save_lesson(
            title=f"Lesson {unique}", content=f"Always test {unique}", source="reflection",
            doc_type="lesson", meta={"agent": "claude"},
        ))
        assert doc_id is not None
        lessons = run_async(store.get_lessons(doc_type="lesson"))
        titles = [l["title"] for l in lessons]
        assert f"Lesson {unique}" in titles

    def test_stats(self):
        store = MemoryStore()
        stats = run_async(store.get_stats())
        assert "tasks" in stats
        assert "agents" in stats
        assert "success_rate" in stats


class TestCollectiveConsciousness:
    def test_working_memory_ttl(self):
        cc = CollectiveConsciousness()
        run_async(cc.set("key1", "value1", ttl=3600))
        val = run_async(cc.get("key1"))
        assert val == "value1"

    def test_session_management(self):
        cc = CollectiveConsciousness()
        run_async(cc.join_session("sess-1", "claude"))
        run_async(cc.join_session("sess-1", "kimi"))
        sess = run_async(cc.get_session("sess-1"))
        assert len(sess["participants"]) == 2
        run_async(cc.add_to_session_history("sess-1", {"event": "start"}))
        sess = run_async(cc.get_session("sess-1"))
        assert len(sess["history"]) == 1

    def test_insights(self):
        cc = CollectiveConsciousness()
        run_async(cc.add_insight("claude", "Use asyncio", "code_generation"))
        run_async(cc.add_insight("kimi", "Design boundaries", "system_design"))
        insights = run_async(cc.get_insights(capability="code_generation"))
        assert len(insights) == 1
        assert insights[0]["agent"] == "claude"

    def test_relevant_context(self):
        cc = CollectiveConsciousness()
        run_async(cc.join_session("sess-1", "claude"))
        run_async(cc.add_insight("kimi", "Design boundaries", "system_design"))
        ctx = run_async(cc.get_relevant_context(
            "claude", "Build a system", capability="system_design", session_id="sess-1"
        ))
        assert "session_participants" in ctx
        assert "active_insights_from_others" in ctx


class TestSelfTuningEngine:
    def test_receives_suggestions(self):
        bus = EventBus()
        tuner = SelfTuningEngine(event_bus=bus)
        run_async(tuner.receive_suggestions("claude", ["timeout"], [
            {"type": "prompt", "issue": "timeout", "advice": "increase timeout"}
        ]))
        status = tuner.get_status()
        # Non-urgent pattern with only 1 suggestion stays pending
        assert status["pending_suggestions"]["claude"] == 1

    def test_urgent_pattern_applies(self):
        bus = EventBus()
        tuner = SelfTuningEngine(event_bus=bus)
        # Urgent pattern: rate_limit should trigger immediate consideration
        run_async(tuner.receive_suggestions("claude", ["rate_limit"], [
            {"type": "prompt", "issue": "rate_limit", "advice": "slow down"}
        ]))
        # With 1 urgent suggestion, it applies immediately
        status = tuner.get_status()
        assert status["pending_suggestions"].get("claude", 0) == 0

    def test_apply_to_agent_config(self):
        tuner = SelfTuningEngine()
        run_async(tuner.receive_suggestions("claude", ["timeout"], [
            {"type": "prompt", "issue": "timeout", "advice": "increase timeout"}
        ]))
        config = {"timeout": 30}
        new_config = run_async(tuner.apply_to_agent("claude", config))
        assert new_config["timeout"] > 30


class TestMetaAgent:
    def test_observe_and_think(self):
        bus = EventBus()
        meta = MetaAgent(event_bus=bus, enabled=True, interval_seconds=1)
        run_async(meta.start())
        asyncio.run(asyncio.sleep(0.1))  # Let it run one observation cycle
        status = meta.get_status()
        assert status["running"] is True
        assert status["observations_collected"] >= 1
        run_async(meta.stop())

    def test_pause_resume(self):
        bus = EventBus()
        meta = MetaAgent(event_bus=bus, enabled=True, interval_seconds=1)
        meta.pause()
        assert meta._enabled is False
        meta.resume()
        assert meta._enabled is True

    def test_hypothesis_generation(self):
        bus = EventBus()
        meta = MetaAgent(event_bus=bus, enabled=False)
        # Seed observations
        meta._observation_window.append({
            "timestamp": "2024-01-01T00:00:00",
            "memory_stats": {"success_rate": 0.5, "total_failed": 10, "agents": 1, "tasks": 30},
            "consciousness": {"total_insights": 15},
        })
        hypotheses = run_async(meta._think())
        assert len(hypotheses) > 0
        # Should detect low success rate
        types = [h["type"] for h in hypotheses]
        assert "suggest_fallback_review" in types
