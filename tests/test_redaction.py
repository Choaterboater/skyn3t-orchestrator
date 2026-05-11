"""Tests verifying LLM_EXCHANGE events are scrubbed before publish.

The risk: prompts/responses can carry secrets pasted by users. The
LLM_EXCHANGE event flows to the dashboard (and any subscriber); raw
content there leaks credentials. ``redact_text`` sits between
``llm_client.complete`` and ``event_bus.publish`` to mask known patterns.
"""

from __future__ import annotations

from typing import List

import pytest

from skyn3t.adapters.llm_client import LLMClient, LLMRequest
from skyn3t.core.events import Event, EventBus, EventType


class _StubBackend:
    """Minimal backend impl with the LLMClient-expected `.complete` shape."""

    def __init__(self, response: str):
        self._response = response

    async def complete(self, req: LLMRequest) -> str:  # noqa: ARG002
        return self._response


def _capture_events(bus: EventBus, kind: EventType) -> List[Event]:
    captured: List[Event] = []
    bus.subscribe(captured.append, kind)
    return captured


@pytest.mark.asyncio
async def test_llm_exchange_event_redacts_jwt_in_prompt():
    bus = EventBus()
    captured = _capture_events(bus, EventType.LLM_EXCHANGE)

    client = LLMClient(event_bus=bus, caller_name="test-agent")
    # Force the stub backend so we don't hit any real provider.
    client._impl = _StubBackend("ok")  # type: ignore[attr-defined]

    leaked = "header eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcDEF"
    await client.complete(prompt=leaked)

    assert captured, "LLM_EXCHANGE event was not published"
    payload = captured[0].payload
    assert "eyJ" not in payload.get("prompt", ""), payload
    assert "***REDACTED***" in payload.get("prompt", "")


@pytest.mark.asyncio
async def test_llm_exchange_event_redacts_email_in_response():
    bus = EventBus()
    captured = _capture_events(bus, EventType.LLM_EXCHANGE)

    client = LLMClient(event_bus=bus, caller_name="test-agent")
    client._impl = _StubBackend(  # type: ignore[attr-defined]
        "Reply containing alice@example.com"
    )

    await client.complete(prompt="hello")

    assert captured, "LLM_EXCHANGE event was not published"
    response_field = captured[0].payload.get("response", "")
    assert "alice@example.com" not in response_field
    assert "***REDACTED***" in response_field


@pytest.mark.asyncio
async def test_llm_exchange_event_redacts_system_prompt():
    bus = EventBus()
    captured = _capture_events(bus, EventType.LLM_EXCHANGE)

    client = LLMClient(event_bus=bus, caller_name="test-agent")
    client._impl = _StubBackend("ok")  # type: ignore[attr-defined]

    await client.complete(
        prompt="hi",
        system="auth: eyJhbGciOiJIUzI1NiJ9.eyJ.abc",
    )

    assert captured
    sys_field = captured[0].payload.get("system", "")
    assert "eyJ" not in sys_field
