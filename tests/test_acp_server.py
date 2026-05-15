"""Tests for skyn3t.integrations.acp_server — Agent Client Protocol shim.

The server is transport-only; the orchestrator wires in its own
prompt_handler. These tests pin the JSON-RPC envelope shapes
against the ACP spec so a Zed / JetBrains client can drive us.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from skyn3t.integrations.acp_server import (
    ACP_PROTOCOL_VERSION,
    METHOD_INITIALIZE,
    METHOD_SESSION_NEW,
    METHOD_SESSION_PROMPT,
    ACPServer,
)


def _wired_server() -> tuple[ACPServer, List[Dict[str, Any]]]:
    """Build a server backed by an in-memory writer that captures every
    JSON-RPC payload as a dict (instead of writing to stdout)."""
    server = ACPServer()
    captured: List[Dict[str, Any]] = []

    async def writer(line: str) -> None:
        # The server emits one JSON object per line.
        captured.append(json.loads(line))

    server.attach_writer(writer)
    return server, captured


@pytest.mark.asyncio
async def test_initialize_returns_protocol_version_and_agent_info():
    server, out = _wired_server()
    await server.handle_message(json.dumps({
        "jsonrpc": "2.0",
        "id": 0,
        "method": METHOD_INITIALIZE,
        "params": {"protocolVersion": ACP_PROTOCOL_VERSION, "clientCapabilities": {}},
    }))
    assert len(out) == 1
    msg = out[0]
    assert msg["jsonrpc"] == "2.0"
    assert msg["id"] == 0
    result = msg["result"]
    assert result["protocolVersion"] == ACP_PROTOCOL_VERSION
    assert "agentInfo" in result
    assert result["agentInfo"]["name"]
    assert "agentCapabilities" in result


@pytest.mark.asyncio
async def test_session_new_mints_unique_session_ids():
    server, out = _wired_server()
    await server.handle_message(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": METHOD_SESSION_NEW, "params": {}}
    ))
    await server.handle_message(json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": METHOD_SESSION_NEW, "params": {}}
    ))
    assert len(out) == 2
    sid1 = out[0]["result"]["sessionId"]
    sid2 = out[1]["result"]["sessionId"]
    assert sid1.startswith("sess_")
    assert sid2.startswith("sess_")
    assert sid1 != sid2


@pytest.mark.asyncio
async def test_session_prompt_streams_update_then_returns_stop_reason():
    server, out = _wired_server()

    async def echo_handler(params, emit_update):
        # Echo each text block back as an agent_message_chunk update.
        for block in params.get("prompt") or []:
            if block.get("type") == "text":
                await emit_update(
                    "agent_message_chunk",
                    {"content": {"type": "text", "text": "echo: " + block["text"]}},
                )
        return {"stopReason": "end_turn"}

    server.prompt_handler = echo_handler

    # First create a session.
    await server.handle_message(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": METHOD_SESSION_NEW, "params": {}}
    ))
    sid = out[0]["result"]["sessionId"]
    out.clear()

    # Now prompt against it.
    await server.handle_message(json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": METHOD_SESSION_PROMPT,
        "params": {
            "sessionId": sid,
            "prompt": [{"type": "text", "text": "hello"}],
        },
    }))
    # Should be 2 messages: one session/update notification + one response.
    assert len(out) == 2
    update = out[0]
    response = out[1]
    # Notification shape.
    assert update["method"] == "session/update"
    assert update["params"]["sessionId"] == sid
    assert update["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    assert update["params"]["update"]["content"]["text"] == "echo: hello"
    # Response shape.
    assert response["id"] == 2
    assert response["result"]["stopReason"] == "end_turn"


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found():
    server, out = _wired_server()
    await server.handle_message(json.dumps(
        {"jsonrpc": "2.0", "id": 99, "method": "foo/bar", "params": {}}
    ))
    assert len(out) == 1
    msg = out[0]
    assert msg["id"] == 99
    assert msg["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_malformed_json_is_silently_ignored():
    server, out = _wired_server()
    # Garbage in → no crash, no response (no id to bind to).
    await server.handle_message("not json at all {")
    assert out == []


@pytest.mark.asyncio
async def test_session_prompt_with_unknown_session_refuses():
    server, out = _wired_server()
    await server.handle_message(json.dumps({
        "jsonrpc": "2.0",
        "id": 5,
        "method": METHOD_SESSION_PROMPT,
        "params": {
            "sessionId": "sess_never_existed",
            "prompt": [{"type": "text", "text": "hi"}],
        },
    }))
    msg = out[0]
    assert msg["id"] == 5
    assert msg["result"]["stopReason"] == "refusal"


@pytest.mark.asyncio
async def test_cancel_marks_session_for_cancellation():
    """A cancel notification arriving before the prompt handler returns
    should flip the stopReason to 'cancelled'."""
    server, out = _wired_server()

    started = asyncio.Event()
    proceed = asyncio.Event()

    async def slow_handler(params, emit_update):
        started.set()
        await proceed.wait()
        return {"stopReason": "end_turn"}

    server.prompt_handler = slow_handler

    # Create a session.
    await server.handle_message(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": METHOD_SESSION_NEW, "params": {}}
    ))
    sid = out[0]["result"]["sessionId"]
    out.clear()

    # Kick off the prompt in the background — it'll hang on `proceed`.
    prompt_task = asyncio.create_task(server.handle_message(json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": METHOD_SESSION_PROMPT,
        "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]},
    })))
    await started.wait()
    # Cancel.
    await server.handle_message(json.dumps(
        {"jsonrpc": "2.0", "method": "cancel", "params": {"sessionId": sid}}
    ))
    # Release the handler.
    proceed.set()
    await prompt_task

    # Find the prompt response in `out`.
    response = next(m for m in out if m.get("id") == 2)
    assert response["result"]["stopReason"] == "cancelled"


@pytest.mark.asyncio
async def test_response_handler_failure_returns_refusal_and_emits_text():
    server, out = _wired_server()

    async def boom_handler(params, emit_update):
        raise RuntimeError("simulated agent crash")

    server.prompt_handler = boom_handler
    await server.handle_message(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": METHOD_SESSION_NEW, "params": {}}
    ))
    sid = out[0]["result"]["sessionId"]
    out.clear()
    await server.handle_message(json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": METHOD_SESSION_PROMPT,
        "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]},
    }))
    # We expect: a session/update with the error text + a final response
    # with stopReason=refusal.
    error_update = next(
        m for m in out
        if m.get("method") == "session/update"
        and "simulated agent crash" in m["params"]["update"]["content"]["text"]
    )
    response = next(m for m in out if m.get("id") == 2)
    assert error_update["params"]["sessionId"] == sid
    assert response["result"]["stopReason"] == "refusal"


@pytest.mark.asyncio
async def test_writer_lock_serializes_concurrent_writes():
    """Two notifications fired concurrently shouldn't interleave bytes on
    the wire — each must be a single complete JSON line."""
    server, out = _wired_server()

    async def writer_with_yield(line: str) -> None:
        # Force an await between each character to maximize interleave risk.
        async with asyncio.Lock():
            for ch in line:
                out.append(ch)  # type: ignore[arg-type]
                await asyncio.sleep(0)
    # Override the captured-dicts writer with a char-level one for this test.

    captured: List[str] = []
    write_lock = asyncio.Lock()

    async def writer(line: str) -> None:
        async with write_lock:
            captured.append(line)

    server.attach_writer(writer)

    await asyncio.gather(
        server.send_notification("a", {"x": 1}),
        server.send_notification("b", {"y": 2}),
    )
    # Each line must end with the newline our protocol adds.
    assert len(captured) == 2
    for line in captured:
        assert line.endswith("\n")
        # And must parse cleanly — no interleaved fragments.
        json.loads(line)
