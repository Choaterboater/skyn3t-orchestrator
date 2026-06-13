from __future__ import annotations

import pytest

from skyn3t.cortex.autonomous_loop import AutonomousBrief, AutonomousCoordinator


@pytest.mark.asyncio
async def test_offer_brief_rejects_when_queue_full(monkeypatch):
    monkeypatch.setenv("SKYN3T_AUTONOMOUS_QUEUE_MAX_DEPTH", "2")
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()

    coord = AutonomousCoordinator(orchestrator=object(), event_bus=object())
    assert coord._pending.maxsize == 2

    brief = AutonomousBrief(
        brief="Build a tiny dashboard",
        template="auto",
        source="test",
        trigger="unit",
    )
    assert await coord._offer_brief(brief) is True
    assert await coord._offer_brief(
        AutonomousBrief(
            brief="Build another dashboard",
            template="auto",
            source="test",
            trigger="unit",
        )
    ) is True
    assert await coord._offer_brief(
        AutonomousBrief(
            brief="Build a third dashboard",
            template="auto",
            source="test",
            trigger="unit",
        )
    ) is False
    assert coord._pending.qsize() == 2

    get_settings.cache_clear()
