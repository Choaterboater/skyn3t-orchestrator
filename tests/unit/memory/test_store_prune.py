"""H24 regression: MemoryStore retention pruners delete old rows while
keeping the most recent ``keep_last`` records."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from skyn3t.core.models import SystemLog
from skyn3t.memory.store import MemoryStore


@pytest.fixture
def store():
    return MemoryStore()


@pytest.mark.asyncio
async def test_prune_system_logs_older_than_with_keep_last(store: MemoryStore) -> None:
    base = datetime.now(timezone.utc)
    async with await store._session() as session:
        async with session.begin():
            for i in range(5):
                session.add(
                    SystemLog(
                        id=str(uuid4()),
                        level="INFO",
                        source="test",
                        message=f"log {i}",
                        meta={},
                        created_at=base - timedelta(days=i),
                    )
                )

    pruned = await store.prune_system_logs(older_than_days=2, keep_last=2)
    assert pruned == 2

    async with await store._session() as session:
        remaining = (await session.execute(select(SystemLog))).scalars().all()
    assert len(remaining) == 3
