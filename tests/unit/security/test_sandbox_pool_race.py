"""H17 regression: concurrent DockerPoolBackend.execute() calls must not race
on pool initialization and create duplicate containers."""

import asyncio
from unittest.mock import patch

import pytest

from skyn3t.security.sandbox import DockerPoolBackend


@pytest.mark.asyncio
async def test_concurrent_execute_serializes_pool_creation() -> None:
    backend = DockerPoolBackend(pool_size=1, docker_path="docker")
    create_calls = 0

    async def fake_create_pool(language: str) -> None:
        nonlocal create_calls
        # If lock isn't held, two coroutines can enter concurrently and we
        # should observe create_calls > 1.
        create_calls += 1
        await asyncio.sleep(0.05)
        backend._initialized[language] = True
        backend._containers[language] = ["c1"]
        backend._locks.setdefault("c1", asyncio.Lock())

    with patch.object(backend, "available", return_value=True), patch.object(
        backend, "_create_pool", side_effect=fake_create_pool
    ):
        await asyncio.gather(
            backend.execute("print(1)", "python"),
            backend.execute("print(2)", "python"),
            backend.execute("print(3)", "python"),
        )

    assert create_calls == 1, f"expected exactly one pool creation, got {create_calls}"
