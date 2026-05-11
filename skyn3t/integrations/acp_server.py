"""Agent Client Protocol (ACP) server for SkyN3t.

ACP is the JSON-RPC standard Zed and JetBrains use to talk to AI
coding agents. Once installed in the IDE's agent registry, the IDE
spawns SkyN3t as a subprocess and talks to it on stdin/stdout.

This module is intentionally **transport-only** — it does not know
anything about the orchestrator's internals. It exposes:

  - ``run_stdio()`` — block on stdin, dispatch JSON-RPC, write
    responses + notifications to stdout. The IDE's job to spawn us.
  - ``ACPServer`` — request/notification dispatcher with a tiny
    handler registry. The orchestrator wires its own handlers in.

Supported methods (the bare minimum for a working Zed/JetBrains
integration):

  ``initialize``     — capability handshake. Reply with protocolVersion=1
                       and our agentInfo.
  ``session/new``    — create a session. We map this to a SkyN3t
                       project slug + open a chat-only orchestrator
                       context.
  ``session/prompt`` — the user said something. Returns ``end_turn``
                       after streaming ``session/update`` notifications.
  ``cancel``         — abort the current prompt turn.

We deliberately don't implement filesystem, terminals, or MCP capabilities
in this first cut — the IDE will mark those as unsupported and route
around them. A follow-up PR can wire them to BaseAgent's existing
``file_ops`` + studio subprocess hooks.

Reference: https://agentclientprotocol.com/protocol/initialization
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("skyn3t.integrations.acp_server")

# Protocol version 1 is what Zed and JetBrains currently support
# (Dec 2025 / Jan 2026 rollout). Bump when ACP itself bumps.
ACP_PROTOCOL_VERSION = 1

# Method names declared in the spec we DO implement.
METHOD_INITIALIZE = "initialize"
METHOD_SESSION_NEW = "session/new"
METHOD_SESSION_PROMPT = "session/prompt"
METHOD_CANCEL = "cancel"


# Type aliases — keep the dispatcher cleanly typed.
RequestHandler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]
NotificationEmitter = Callable[[str, Dict[str, Any]], Awaitable[None]]


@dataclass
class _ACPSession:
    """Server-side bookkeeping for an active ACP session."""

    session_id: str
    created_at: float = 0.0
    cancelled: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class ACPServer:
    """Async JSON-RPC dispatcher implementing the ACP protocol.

    Wire your orchestrator in by setting ``server.prompt_handler``. The
    handler receives the parsed ``session/prompt`` params plus an
    ``emit_update`` callback it can use to stream ``session/update``
    notifications back to the client during the turn.
    """

    def __init__(
        self,
        *,
        agent_name: str = "skyn3t",
        agent_title: str = "SkyN3t",
        agent_version: str = "0.1.0",
    ):
        self.agent_name = agent_name
        self.agent_title = agent_title
        self.agent_version = agent_version
        self._sessions: Dict[str, _ACPSession] = {}
        # Wired by the host (orchestrator) at startup.
        self.prompt_handler: Optional[
            Callable[[Dict[str, Any], NotificationEmitter], Awaitable[Dict[str, Any]]]
        ] = None
        # Used by send_notification to write to whatever output channel
        # the transport set up (stdout for stdio, websocket for ws).
        self._writer: Optional[Callable[[str], Awaitable[None]]] = None

    # ------------------------------------------------------------------
    # Connection wiring — transport layer calls this once on startup.
    # ------------------------------------------------------------------

    def attach_writer(self, writer: Callable[[str], Awaitable[None]]) -> None:
        self._writer = writer

    # ------------------------------------------------------------------
    # Outgoing helpers — agent → client.
    # ------------------------------------------------------------------

    async def send_response(self, request_id: Any, result: Dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def send_error(
        self, request_id: Any, code: int, message: str, data: Any = None
    ) -> None:
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
        if data is not None:
            payload["error"]["data"] = data
        await self._send(payload)

    async def send_notification(self, method: str, params: Dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, payload: Dict[str, Any]) -> None:
        if self._writer is None:
            logger.warning("ACP server has no writer attached; dropping %s", payload.get("method"))
            return
        try:
            await self._writer(json.dumps(payload) + "\n")
        except Exception:
            logger.exception("ACP write failed")

    # ------------------------------------------------------------------
    # Dispatcher — transport layer calls this per incoming JSON-RPC msg.
    # ------------------------------------------------------------------

    async def handle_message(self, raw: str) -> None:
        """Parse and dispatch one JSON-RPC message. Errors stay in band
        — we never raise; we send a JSON-RPC error response or log."""
        try:
            msg = json.loads(raw)
        except Exception:
            logger.warning("ACP: malformed JSON ignored")
            return
        if not isinstance(msg, dict):
            return
        method = msg.get("method")
        rid = msg.get("id")
        # ACP notifications have no `id`; requests do. We handle requests
        # by method name; unknown methods get -32601 Method not found.
        if method is None:
            return  # response to a request we sent — ignore for now.

        try:
            if method == METHOD_INITIALIZE:
                result = self._handle_initialize(msg.get("params") or {})
                await self.send_response(rid, result)
                return
            if method == METHOD_SESSION_NEW:
                result = await self._handle_session_new(msg.get("params") or {})
                await self.send_response(rid, result)
                return
            if method == METHOD_SESSION_PROMPT:
                result = await self._handle_session_prompt(msg.get("params") or {})
                await self.send_response(rid, result)
                return
            if method == METHOD_CANCEL:
                self._handle_cancel(msg.get("params") or {})
                # cancel is a notification in some ACP versions; reply
                # only if the client sent an id.
                if rid is not None:
                    await self.send_response(rid, {})
                return
            # Method not implemented — respond with the standard JSON-RPC
            # not-found error so the client can route around us.
            if rid is not None:
                await self.send_error(rid, -32601, f"Method not found: {method}")
        except Exception as exc:
            logger.exception("ACP dispatch error for method=%s", method)
            if rid is not None:
                await self.send_error(rid, -32603, f"Internal error: {exc}")

    # ------------------------------------------------------------------
    # Method handlers
    # ------------------------------------------------------------------

    def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Reply with our capabilities. We don't expose filesystem or
        terminal capabilities yet (client will route around them)."""
        client_version = params.get("protocolVersion")
        # We accept whatever the client says it speaks — if we ever
        # diverge from v1 we'll reject mismatches here.
        logger.info("ACP initialize from client protocolVersion=%s", client_version)
        return {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": True,
                },
                "mcpCapabilities": {
                    "http": False,
                    "sse": False,
                },
            },
            "agentInfo": {
                "name": self.agent_name,
                "title": self.agent_title,
                "version": self.agent_version,
            },
            "authMethods": [],
        }

    async def _handle_session_new(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Mint a new session id and register it."""
        import time as _time
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        self._sessions[session_id] = _ACPSession(
            session_id=session_id,
            created_at=_time.time(),
            metadata=dict(params or {}),
        )
        return {"sessionId": session_id}

    async def _handle_session_prompt(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Run the user prompt through the orchestrator, streaming
        ``session/update`` notifications, then return a final stopReason."""
        session_id = params.get("sessionId")
        session = self._sessions.get(session_id)
        if session is None:
            return {"stopReason": "refusal"}
        if self.prompt_handler is None:
            # No orchestrator wired — emit a single message chunk so the
            # IDE has something to show, then end the turn.
            await self._emit_text(session_id, "SkyN3t agent has no prompt handler wired.")
            return {"stopReason": "end_turn"}
        # Build a small emit callback so the host doesn't need to know
        # the session id or message shape.
        async def emit_update(kind: str, payload: Dict[str, Any]) -> None:
            await self.send_notification(
                "session/update",
                {"sessionId": session_id, "update": {"sessionUpdate": kind, **payload}},
            )
        try:
            result = await self.prompt_handler(params, emit_update)
        except Exception as exc:
            logger.exception("prompt_handler failed")
            await self._emit_text(session_id, f"_internal error: {exc}_")
            return {"stopReason": "refusal"}
        if session.cancelled:
            session.cancelled = False
            return {"stopReason": "cancelled"}
        # Normalize the result — accept either a plain dict with
        # stopReason or a bare string for convenience.
        if isinstance(result, dict) and "stopReason" in result:
            return result
        return {"stopReason": "end_turn"}

    def _handle_cancel(self, params: Dict[str, Any]) -> None:
        session_id = params.get("sessionId")
        session = self._sessions.get(session_id) if session_id else None
        if session:
            session.cancelled = True

    # ------------------------------------------------------------------
    # Convenience: emit a single text chunk back to the client.
    # ------------------------------------------------------------------

    async def _emit_text(self, session_id: str, text: str) -> None:
        await self.send_notification(
            "session/update",
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            },
        )


# ── stdio transport ────────────────────────────────────────────────────


async def run_stdio(server: ACPServer) -> None:
    """Run the ACP server on stdin/stdout. Blocks until stdin EOFs.

    The IDE spawns us as a child process; we read line-delimited JSON-RPC
    messages off stdin and write responses + notifications to stdout.
    Errors go to stderr (logger default) so they don't corrupt the
    protocol stream.

    ACP doesn't require Content-Length framing for stdio (some specs do
    — LSP does). Each message is a single line of JSON.
    """
    loop = asyncio.get_event_loop()

    # Bind the writer. stdout writes must NOT race — wrap in a lock.
    write_lock = asyncio.Lock()
    async def writer(line: str) -> None:
        async with write_lock:
            await loop.run_in_executor(None, _write_stdout, line)
    server.attach_writer(writer)

    # Spawn the read loop.
    while True:
        line = await loop.run_in_executor(None, _read_stdin_line)
        if not line:
            # EOF — client closed the channel.
            break
        await server.handle_message(line)


def _read_stdin_line() -> str:
    """Blocking stdin read used inside an executor thread."""
    line = sys.stdin.readline()
    return line.rstrip("\n")


def _write_stdout(line: str) -> None:
    """Blocking stdout write used inside an executor thread."""
    sys.stdout.write(line)
    sys.stdout.flush()


# ── default prompt handler: route to an in-process orchestrator agent ──


async def default_prompt_handler(
    params: Dict[str, Any],
    emit_update: NotificationEmitter,
) -> Dict[str, Any]:
    """Reference handler that routes ``session/prompt`` to the default
    LLM client. The orchestrator can override this — it's here so the
    server is useful standalone.

    Extracts the user text from the ``prompt`` content array, runs it
    through LLMClient, streams the response as a single text chunk.
    """
    prompt_blocks = params.get("prompt") or []
    user_text_parts: List[str] = []
    for block in prompt_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            txt = block.get("text") or ""
            if txt:
                user_text_parts.append(txt)
    user_text = "\n".join(user_text_parts).strip()
    if not user_text:
        await emit_update(
            "agent_message_chunk",
            {"content": {"type": "text", "text": "(empty prompt)"}},
        )
        return {"stopReason": "end_turn"}

    try:
        from skyn3t.adapters import LLMClient
        client = LLMClient(caller_name="acp")
        out = await client.complete(user_text)
    except Exception as exc:
        logger.exception("default prompt handler LLM call failed")
        out = f"(error: {exc})"

    await emit_update(
        "agent_message_chunk",
        {"content": {"type": "text", "text": out or "(empty response)"}},
    )
    return {"stopReason": "end_turn"}


# ── entrypoint shim, called by ``python -m skyn3t.integrations.acp_server`` ──


def main() -> int:
    """Module entrypoint: start the server on stdio with the default
    prompt handler. The IDE invokes us via this entry point per the
    ACP agent-registry manifest."""
    logging.basicConfig(level=logging.INFO)
    server = ACPServer()
    server.prompt_handler = default_prompt_handler
    try:
        asyncio.run(run_stdio(server))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
