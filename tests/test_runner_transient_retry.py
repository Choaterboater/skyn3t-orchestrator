"""PHASE 2: the runner's bounded wait-and-retry on a transient LLM throttle.

A sustained 429 surfaces as :class:`TransientLLMError` from the fix-callers.
``_with_transient_retry`` must wait a short backoff and retry the whole fix a
bounded number of times before giving up — so a transient throttle does not
permanently fail the build. These tests pin that contract against the helper in
isolation (no orchestrator, no data/).
"""

from __future__ import annotations

import pytest

from skyn3t.adapters import TransientLLMError
from skyn3t.studio import runner as runner_module
from skyn3t.studio.runner import _with_transient_retry


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(runner_module.asyncio, "sleep", _fast_sleep)


@pytest.mark.asyncio
async def test_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(runner_module, "_TRANSIENT_FIX_RETRIES", 2)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise TransientLLMError("openrouter 429 after 8 attempts")
        return "fixed"

    out = await _with_transient_retry(flaky, what="build fix")
    assert out == "fixed"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_gives_up_after_budget_and_reraises(monkeypatch):
    monkeypatch.setattr(runner_module, "_TRANSIENT_FIX_RETRIES", 2)
    calls = {"n": 0}

    async def always_throttled():
        calls["n"] += 1
        raise TransientLLMError("openrouter 429 after 8 attempts")

    with pytest.raises(TransientLLMError):
        await _with_transient_retry(always_throttled, what="build fix")
    # 1 initial attempt + 2 retries.
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_non_transient_error_propagates_immediately(monkeypatch):
    monkeypatch.setattr(runner_module, "_TRANSIENT_FIX_RETRIES", 2)
    calls = {"n": 0}

    async def permanent():
        calls["n"] += 1
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        await _with_transient_retry(permanent, what="build fix")
    # No retries on a non-transient error.
    assert calls["n"] == 1
