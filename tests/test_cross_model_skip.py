"""Tests for LLMClient.skip_backends — the cross-model debate hook.

When the orchestrator's retry path detects that a previous attempt used
backend X and failed, it spawns the retry's LLMClient with
``skip_backends=['X']``. The auto chain then falls through to a sibling
model.
"""

from __future__ import annotations

import pytest

from skyn3t.adapters.llm_client import LLMClient


@pytest.mark.asyncio
async def test_skip_backends_blocks_try_cli():
    client = LLMClient(backend="auto", skip_backends=["claude_cli"])

    class _FakeBackend:
        async def available(self) -> bool:
            return True

    ok = await client._try_cli("claude_cli", _FakeBackend)
    assert ok is False

    ok = await client._try_cli("kimi_cli", _FakeBackend)
    assert ok is True
    assert client._backend_name == "kimi_cli"


@pytest.mark.asyncio
async def test_skip_backends_empty_set_allows_all():
    client = LLMClient(backend="auto")

    class _FakeBackend:
        async def available(self) -> bool:
            return True

    ok = await client._try_cli("claude_cli", _FakeBackend)
    assert ok is True


def test_skip_backends_stored_as_set():
    client = LLMClient(skip_backends=["claude_cli", "copilot_cli", "claude_cli"])
    assert client._skip_backends == {"claude_cli", "copilot_cli"}
