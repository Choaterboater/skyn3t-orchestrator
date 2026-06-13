"""H18 regression: CollectiveConsciousness snapshots persist to SQLite and
hydrate on load."""

import pytest

from skyn3t.memory.consciousness import CollectiveConsciousness
from skyn3t.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_consciousness_snapshot_round_trip() -> None:
    store = MemoryStore()
    cc = CollectiveConsciousness(memory_store=store)

    await cc.set("greeting", "hello")
    await cc.add_insight("greeter", "be polite")

    blob = await cc.to_snapshot()
    snap_id = await store.save_consciousness_snapshot(blob, reason="test")
    assert snap_id

    loaded = await store.load_latest_consciousness_snapshot()
    assert loaded is not None
    assert loaded["working_memory"]["greeting"]["value"] == "hello"

    cc2 = CollectiveConsciousness(memory_store=store)
    await cc2.restore_snapshot(loaded)
    assert await cc2.get("greeting") == "hello"
    insights = await cc2.get_insights("greeter")
    assert insights
