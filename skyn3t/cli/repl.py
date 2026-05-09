"""SkyN3t Interactive REPL — Claude-Code-style swarm console.

Run with `skyn3t` (no args) or `skyn3t repl` to drop into an interactive
session with a live transcript on the left and a swarm-activity sidebar on
the right.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, List, Optional

import httpx
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

API_BASE = os.environ.get("SKYN3T_API_URL", "http://localhost:6660")
WS_URL = API_BASE.replace("http://", "ws://").replace("https://", "wss://")

# Optional dependencies ------------------------------------------------------
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.patch_stdout import patch_stdout
    _HAS_PT = True
except Exception:  # pragma: no cover
    _HAS_PT = False
    Completer = object  # type: ignore[assignment,misc]

try:
    import websockets  # noqa: F401
    _HAS_WS = True
except Exception:  # pragma: no cover
    _HAS_WS = False


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

EVENT_GLYPHS = {
    "AGENT_THOUGHT": "✦",
    "AGENT_MESSAGE_SENT": "💬",
    "AGENT_MESSAGE_RECEIVED": "💬",
    "AGENT_COLLABORATION": "💬",
    "RAG_QUERY_STARTED": "🔍",
    "RAG_RETRIEVED": "🔍",
    "RAG_CRITIQUED": "🔍",
    "RAG_REQUERY": "🔍",
    "AGENT_LEARNING": "🎓",
    "COLLECTIVE_INSIGHT": "🎓",
    "TASK_CREATED": "⚡",
    "TASK_STARTED": "⚡",
    "TASK_EXECUTION_STARTED": "⚡",
    "TASK_COMPLETED": "✅",
    "TASK_FAILED": "❌",
    "TASK_FAILED_FINAL": "❌",
    "PIPELINE_STARTED": "▣",
    "PIPELINE_STAGE_COMPLETED": "▣",
    "PIPELINE_COMPLETED": "▣",
    "INGEST_STARTED": "📥",
    "INGEST_PROGRESS": "📥",
    "INGEST_COMPLETE": "📥",
    "AGENT_REGISTERED": "👤",
    "SYSTEM_ALERT": "🚨",
}


@dataclass
class ReplState:
    session_id: str = field(default_factory=lambda: secrets.token_hex(2))
    transcript: List[Any] = field(default_factory=list)  # list of Renderables
    activity: Deque[Text] = field(default_factory=lambda: deque(maxlen=500))
    connected: bool = False
    busy: bool = False
    busy_label: str = ""
    current_task_id: Optional[str] = None
    stop: bool = False
    # Routing / model selection
    api_url: str = API_BASE
    active_agent: Optional[str] = None     # name of agent prompts route to
    active_backend: Optional[str] = None   # session-level override (None = use agent default)
    active_model: Optional[str] = None
    # Tab-completion caches
    known_backends: List[str] = field(default_factory=list)
    known_models: List[str] = field(default_factory=list)
    known_agents: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _glyph(event_type: str) -> str:
    return EVENT_GLYPHS.get(event_type, "•")


def _format_event(evt: dict) -> Optional[Text]:
    et = evt.get("event_type") or evt.get("type") or ""
    data = evt.get("data") or {}
    if isinstance(data, dict) and "data" in data and "event_type" in data:
        # nested {"type": "event", "data": {...}}
        evt = data
        et = evt.get("event_type", et)
        data = evt.get("data") or {}

    glyph = _glyph(et)
    source = evt.get("source") or (data.get("agent") if isinstance(data, dict) else None) or ""
    snippet = ""
    if isinstance(data, dict):
        for k in ("message", "thought", "title", "query", "summary", "stage", "repo", "name"):
            v = data.get(k)
            if v:
                snippet = str(v)
                break
        if not snippet:
            snippet = ", ".join(f"{k}={v}" for k, v in list(data.items())[:2])
    elif data:
        snippet = str(data)
    snippet = snippet.replace("\n", " ").strip()
    if len(snippet) > 70:
        snippet = snippet[:67] + "..."

    style = "white"
    if et.startswith("TASK_FAIL"):
        style = "red"
    elif et.startswith("TASK_COMPLETE") or et == "PIPELINE_COMPLETED":
        style = "green"
    elif et.startswith("RAG_"):
        style = "cyan"
    elif et.startswith("AGENT_THOUGHT"):
        style = "magenta"
    elif et.startswith("AGENT_LEARNING") or et == "COLLECTIVE_INSIGHT":
        style = "yellow"
    elif et.startswith("INGEST_"):
        style = "blue"

    label = source or et.lower()
    line = Text(f"{glyph} ", style=style)
    line.append(label, style=f"bold {style}")
    if snippet:
        line.append(f"  {snippet}", style="dim")
    return line


def _render_layout(state: ReplState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
    )
    layout["body"].split_row(
        Layout(name="transcript", ratio=2),
        Layout(name="activity", ratio=1, minimum_size=32),
    )

    dot = Text("●", style="green" if state.connected else "red")
    title = Text("SkyN3t", style="bold cyan")
    title.append(f"  ·  session: {state.session_id}", style="dim")
    title.append("  ·  model: orchestrator", style="dim")
    if state.active_agent or state.active_backend or state.active_model:
        agent = state.active_agent or "—"
        be = state.active_backend or "auto"
        mdl = state.active_model or "default"
        title.append(f"  ·  {agent} → {be}/{mdl}", style="dim")
    else:
        title.append("  ·  no agent selected", style="dim")
    title.append("   ")
    title.append_text(dot)
    if state.busy:
        title.append("  ")
        title.append(f"⏳ {state.busy_label}", style="yellow")
    layout["header"].update(Panel(title, border_style="cyan"))

    # Transcript: cap last ~60 entries
    tail = state.transcript[-60:]
    transcript = Group(*tail) if tail else Text("(empty — type a prompt below)", style="dim")
    layout["transcript"].update(
        Panel(transcript, title="Transcript", border_style="white")
    )

    # Activity: tail
    act_tail = list(state.activity)[-200:]
    activity = Group(*act_tail) if act_tail else Text("(no activity yet)", style="dim")
    layout["activity"].update(
        Panel(activity, title="Swarm activity", border_style="magenta")
    )

    return layout


def _add_transcript(state: ReplState, renderable: Any) -> None:
    state.transcript.append(renderable)


def _user_line(text: str) -> Text:
    t = Text("> you: ", style="bold blue")
    t.append(text, style="white")
    return t


def _agent_line(name: str, text: str) -> Any:
    header = Text("★ ", style="bold green")
    header.append(f"{name}", style="bold green")
    header.append(f"  [{datetime.now().strftime('%H:%M:%S')}]", style="dim")
    md = Markdown(text) if text else Text("(no output)", style="dim")
    return Group(header, md, Text(""))


def _info_line(text: str, style: str = "yellow") -> Text:
    return Text(text, style=style)


# ---------------------------------------------------------------------------
# Background: WebSocket + polling fallback
# ---------------------------------------------------------------------------


async def _ws_listener(state: ReplState) -> None:
    """Listen for swarm events; reconnect every 3s on disconnect."""
    if not _HAS_WS:
        await _poll_listener(state)
        return

    import websockets as wsmod
    from websockets.exceptions import ConnectionClosed

    candidate_paths = ["/ws/swarm", "/ws"]
    while not state.stop:
        connected = False
        for path in candidate_paths:
            url = WS_URL.rstrip("/") + path
            try:
                async with wsmod.connect(url, open_timeout=3, ping_interval=20) as ws:
                    state.connected = True
                    connected = True
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        # Possible shapes: {"type":"event","data":{event_type:...}}
                        payload = msg.get("data", msg) if isinstance(msg, dict) else {}
                        line = _format_event(payload)
                        if line is not None:
                            state.activity.append(line)
                    break  # ws closed cleanly
            except ConnectionClosed:
                pass
            except Exception:
                continue
        state.connected = False
        if connected:
            # re-try same path first next loop
            pass
        await asyncio.sleep(3)


async def _poll_listener(state: ReplState) -> None:
    """Fallback: poll a snapshot endpoint every 1s."""
    async with httpx.AsyncClient(base_url=API_BASE, timeout=5.0) as client:
        while not state.stop:
            try:
                resp = await client.get("/api/swarm/snapshot")
                if resp.status_code == 200:
                    state.connected = True
                    items = resp.json().get("events", [])
                    for evt in items[-20:]:
                        line = _format_event(evt)
                        if line is not None:
                            state.activity.append(line)
                else:
                    state.connected = False
            except Exception:
                state.connected = False
            await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# API actions
# ---------------------------------------------------------------------------


async def _submit_prompt(state: ReplState, prompt: str) -> None:
    """Submit a free-form prompt and stream/poll the result."""
    state.busy = True
    state.busy_label = "thinking"
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=60.0) as client:
            # Try orchestrator submit, fall back to a registered agent's exec.
            task_id: Optional[str] = None
            try:
                resp = await client.post(
                    "/api/orchestrator/submit",
                    json={"prompt": prompt, "session_id": state.session_id},
                )
                if resp.status_code == 200:
                    task_id = resp.json().get("task_id")
            except Exception:
                task_id = None

            if not task_id:
                # Pick an agent and use exec for a synchronous round-trip.
                agent_name = await _pick_agent(client)
                if not agent_name:
                    _add_transcript(
                        state,
                        _info_line(
                            "No agents registered. Add one with `skyn3t agent add ...`",
                            "red",
                        ),
                    )
                    return
                try:
                    resp = await client.post(
                        f"/api/agents/{agent_name}/exec",
                        json={"prompt": prompt},
                        timeout=120.0,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("error"):
                        _add_transcript(state, _info_line(f"error: {data['error']}", "red"))
                    else:
                        _add_transcript(state, _agent_line(agent_name, data.get("output") or ""))
                except httpx.HTTPError as exc:
                    _add_transcript(state, _info_line(f"request failed: {exc}", "red"))
                return

            # Poll the task result
            state.current_task_id = task_id
            while not state.stop:
                try:
                    resp = await client.get(f"/api/tasks/{task_id}/result")
                    data = resp.json() if resp.status_code == 200 else {}
                except Exception:
                    data = {}
                if data.get("status") == "pending" or "success" not in data:
                    await asyncio.sleep(0.8)
                    continue
                if data.get("success"):
                    out = data.get("output") or {}
                    text = out.get("response") if isinstance(out, dict) else str(out)
                    _add_transcript(state, _agent_line("orchestrator", text or ""))
                else:
                    _add_transcript(
                        state,
                        _info_line(f"task failed: {data.get('error')}", "red"),
                    )
                break
    finally:
        state.busy = False
        state.busy_label = ""
        state.current_task_id = None


async def _pick_agent(client: httpx.AsyncClient) -> Optional[str]:
    try:
        resp = await client.get("/api/agents")
        agents = resp.json().get("agents", [])
        if not agents:
            return None
        for a in agents:
            if a.get("status", "idle") in ("idle", "busy"):
                return a.get("name")
        return agents[0].get("name")
    except Exception:
        return None


async def _cancel_current(state: ReplState) -> None:
    if not state.current_task_id:
        return
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=5.0) as client:
            await client.post(f"/api/tasks/{state.current_task_id}/cancel")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


HELP_TEXT = """\
**Slash commands**

- `/help` — show this help
- `/quit`, `/exit` — leave the REPL (Ctrl-D also exits)
- `/clear` — clear the transcript
- `/agents` — list registered agents
- `/pipeline NAME [PROMPT...]` — run a pipeline by name
- `/project TEMPLATE BRIEF...` — invoke project studio
- `/rag QUERY` — run agentic RAG
- `/ingest REPO` — kick off a GitHub repo ingestion

**Routing / model selection**

- `/backend` — show current backend + list available
- `/backend NAME` — set active backend (persists on active agent if set)
- `/model` — show current model + top models for backend
- `/model list` — full model list for current backend
- `/model search QUERY` — filter models by id/label
- `/model ID` — set active model
- `/agent` — list all agents
- `/agent NAME` — set active agent (loads its backend/model)
- `/agent NAME backend BACKEND` — patch agent backend
- `/agent NAME model ID` — patch agent model
- `/agent NAME enable` / `/agent NAME disable`
- `/whoami` — show api_url, active agent/backend/model, connection state

Multi-line input: type `\"\"\"` on its own line to open a block,
then `\"\"\"` again to send. Ctrl-C cancels an in-flight task.
"""


async def _slash(state: ReplState, raw: str) -> bool:
    """Handle slash commands. Returns True if the REPL should exit."""
    parts = raw.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if cmd in ("/quit", "/exit"):
        return True
    if cmd == "/help":
        _add_transcript(state, Markdown(HELP_TEXT))
        return False
    if cmd == "/clear":
        state.transcript.clear()
        return False
    if cmd == "/agents":
        await _cmd_agents(state)
        return False
    if cmd == "/pipeline":
        await _cmd_pipeline(state, rest)
        return False
    if cmd == "/project":
        await _cmd_project(state, rest)
        return False
    if cmd == "/rag":
        await _cmd_rag(state, rest)
        return False
    if cmd == "/ingest":
        await _cmd_ingest(state, rest)
        return False
    if cmd == "/backend":
        await _cmd_backend(state, rest)
        return False
    if cmd == "/model":
        await _cmd_model(state, rest)
        return False
    if cmd == "/agent":
        await _cmd_agent(state, rest)
        return False
    if cmd == "/whoami":
        await _cmd_whoami(state)
        return False

    _add_transcript(state, _info_line(f"unknown command: {cmd} (try /help)", "red"))
    return False


async def _cmd_agents(state: ReplState) -> None:
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as client:
            resp = await client.get("/api/agents")
            data = resp.json()
    except Exception as exc:
        _add_transcript(state, _info_line(f"failed: {exc}", "red"))
        return
    agents = data.get("agents", [])
    if not agents:
        _add_transcript(state, _info_line("no agents registered", "yellow"))
        return
    table = Table(title="Registered agents", header_style="bold magenta")
    table.add_column("name", style="cyan")
    table.add_column("provider")
    table.add_column("status")
    table.add_column("queue", justify="right")
    for a in agents:
        table.add_row(
            a.get("name", "-"),
            a.get("provider", "-"),
            a.get("status", "-"),
            str(a.get("queue_size", 0)),
        )
    _add_transcript(state, table)


async def _cmd_pipeline(state: ReplState, rest: str) -> None:
    if not rest:
        _add_transcript(state, _info_line("usage: /pipeline NAME [prompt...]", "yellow"))
        return
    parts = rest.split(maxsplit=1)
    name = parts[0]
    prompt = parts[1] if len(parts) > 1 else ""
    payload = {"name": name, "prompt": prompt, "run": True}
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=30.0) as client:
            resp = await client.post(f"/api/pipeline/{name}/run", json=payload)
            if resp.status_code == 404:
                # Fall back to generic create endpoint
                resp = await client.post("/api/pipeline", json=payload)
            data = resp.json() if resp.status_code < 500 else {"error": resp.text}
    except Exception as exc:
        _add_transcript(state, _info_line(f"pipeline failed: {exc}", "red"))
        return
    _add_transcript(
        state,
        _info_line(f"pipeline {name}: {data.get('status', data)}", "green"),
    )


async def _cmd_project(state: ReplState, rest: str) -> None:
    if not rest:
        _add_transcript(state, _info_line("usage: /project TEMPLATE BRIEF...", "yellow"))
        return
    parts = rest.split(maxsplit=1)
    template = parts[0]
    brief = parts[1] if len(parts) > 1 else ""
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=30.0) as client:
            resp = await client.post(
                "/api/project/start",
                json={"template": template, "brief": brief},
            )
            if resp.status_code == 404:
                _add_transcript(
                    state, _info_line("project studio not yet available", "yellow")
                )
                return
            data = resp.json()
    except Exception as exc:
        _add_transcript(state, _info_line(f"project failed: {exc}", "red"))
        return
    _add_transcript(state, _info_line(f"project started: {data}", "green"))


async def _cmd_rag(state: ReplState, rest: str) -> None:
    if not rest:
        _add_transcript(state, _info_line("usage: /rag QUERY", "yellow"))
        return
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=60.0) as client:
            resp = await client.post(
                "/api/rag/query", json={"query": rest, "n_results": 5}
            )
            data = resp.json()
    except Exception as exc:
        _add_transcript(state, _info_line(f"rag failed: {exc}", "red"))
        return
    answer = data.get("answer") or "(no answer)"
    _add_transcript(state, _agent_line("rag", answer))
    sources = data.get("sources") or []
    if sources:
        table = Table(title="sources", header_style="bold cyan")
        table.add_column("title", style="cyan")
        table.add_column("score", justify="right")
        for s in sources[:5]:
            table.add_row(str(s.get("title", "-")), f"{s.get('score', 0):.3f}")
        _add_transcript(state, table)


async def _cmd_ingest(state: ReplState, rest: str) -> None:
    if not rest:
        _add_transcript(state, _info_line("usage: /ingest owner/repo", "yellow"))
        return
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=15.0) as client:
            resp = await client.post("/api/ingest", json={"repo": rest})
            if resp.status_code == 404:
                # fallback to github explorer
                if "/" in rest:
                    owner, repo = rest.split("/", 1)
                    payload = {
                        "title": f"Ingest {rest}",
                        "input": {"task_type": "repo_analysis", "owner": owner, "repo": repo},
                    }
                    resp = await client.post(
                        "/api/agents/github_explorer/task", json=payload
                    )
            data = resp.json()
    except Exception as exc:
        _add_transcript(state, _info_line(f"ingest failed: {exc}", "red"))
        return
    _add_transcript(state, _info_line(f"ingest queued: {data}", "green"))


# ---------------------------------------------------------------------------
# Routing / model-selection commands
# ---------------------------------------------------------------------------


async def _fetch_backends(state: ReplState) -> List[str]:
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=10.0) as client:
            resp = await client.get("/api/llm/backends")
            if resp.status_code == 200:
                payload = resp.json() or {}
                backends = payload.get("backends") or []
                # accept either ["a","b"] or [{"name":"a"}, ...]
                names: List[str] = []
                for b in backends:
                    if isinstance(b, str):
                        names.append(b)
                    elif isinstance(b, dict):
                        n = b.get("name") or b.get("id")
                        if n:
                            names.append(n)
                state.known_backends = names
                return names
    except Exception:
        pass
    return state.known_backends


async def _fetch_models(state: ReplState, backend: Optional[str] = None) -> List[dict]:
    be = backend or state.active_backend or "auto"
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=10.0) as client:
            resp = await client.get(f"/api/llm/models?backend={be}")
            if resp.status_code == 200:
                payload = resp.json() or {}
                models = payload.get("models") or []
                state.known_models = [
                    m.get("id") for m in models if isinstance(m, dict) and m.get("id")
                ]
                return [m for m in models if isinstance(m, dict)]
    except Exception:
        pass
    return []


async def _fetch_agent_config(state: ReplState, name: str) -> dict:
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=10.0) as client:
            resp = await client.get(f"/api/agents/{name}/config")
            if resp.status_code == 200:
                payload = resp.json() or {}
                return payload.get("config") or payload or {}
    except Exception:
        pass
    return {}


async def _patch_agent_config(state: ReplState, name: str, patch: dict) -> bool:
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=10.0) as client:
            resp = await client.patch(f"/api/agents/{name}/config", json=patch)
            return 200 <= resp.status_code < 300
    except Exception as exc:
        _add_transcript(state, _info_line(f"error: {exc}", "red"))
        return False


async def _cmd_backend(state: ReplState, rest: str) -> None:
    rest = rest.strip()
    backends = await _fetch_backends(state)
    if not rest:
        current = state.active_backend
        if not current and state.active_agent:
            cfg = await _fetch_agent_config(state, state.active_agent)
            current = cfg.get("backend") or "auto"
        current = current or "auto"
        if not backends:
            _add_transcript(state, _info_line(f"current backend: {current} (no backends listed)", "yellow"))
            return
        table = Table(title="Available backends", header_style="bold magenta")
        table.add_column("", width=2)
        table.add_column("name", style="cyan")
        for b in backends:
            mark = "▸" if b == current else " "
            table.add_row(mark, b)
        _add_transcript(state, table)
        _add_transcript(state, _info_line(f"current backend: {current}", "green"))
        return

    name = rest.split()[0]
    state.active_backend = name
    if state.active_agent:
        ok = await _patch_agent_config(state, state.active_agent, {"backend": name})
        if ok:
            _add_transcript(
                state,
                _info_line(
                    f"backend set to '{name}' (persisted on agent '{state.active_agent}')",
                    "green",
                ),
            )
        else:
            _add_transcript(
                state,
                _info_line(
                    f"backend set to '{name}' (session only — failed to persist on agent)",
                    "yellow",
                ),
            )
    else:
        _add_transcript(state, _info_line(f"backend set to '{name}' (session)", "green"))
    # Refresh model list for the new backend
    await _fetch_models(state, name)


async def _cmd_model(state: ReplState, rest: str) -> None:
    rest = rest.strip()
    parts = rest.split(maxsplit=1)
    sub = parts[0].lower() if parts else ""

    if not rest:
        # show current + top 8
        current = state.active_model
        if not current and state.active_agent:
            cfg = await _fetch_agent_config(state, state.active_agent)
            current = cfg.get("model")
        current_disp = current or "default"
        models = await _fetch_models(state)
        be = state.active_backend or "auto"
        if not models:
            _add_transcript(
                state, _info_line(f"current model: {current_disp} (backend={be}; no models listed)", "yellow")
            )
            return
        table = Table(title=f"Models (backend={be}, top 8)", header_style="bold magenta")
        table.add_column("", width=2)
        table.add_column("id", style="cyan")
        table.add_column("label")
        for m in models[:8]:
            mid = m.get("id", "-")
            mark = "▸" if mid == current else " "
            table.add_row(mark, str(mid), str(m.get("label") or m.get("name") or ""))
        _add_transcript(state, table)
        _add_transcript(state, _info_line(f"current model: {current_disp}", "green"))
        if len(models) > 8:
            _add_transcript(state, _info_line("type `/model list` for full list", "dim"))
        return

    if sub == "list":
        models = await _fetch_models(state)
        be = state.active_backend or "auto"
        if not models:
            _add_transcript(state, _info_line(f"no models for backend={be}", "yellow"))
            return
        table = Table(title=f"All models (backend={be})", header_style="bold magenta")
        table.add_column("id", style="cyan")
        table.add_column("label")
        for m in models:
            table.add_row(str(m.get("id", "-")), str(m.get("label") or m.get("name") or ""))
        _add_transcript(state, table)
        return

    if sub == "search":
        q = parts[1].strip().lower() if len(parts) > 1 else ""
        if not q:
            _add_transcript(state, _info_line("usage: /model search QUERY", "yellow"))
            return
        models = await _fetch_models(state)
        hits = [
            m for m in models
            if q in str(m.get("id", "")).lower() or q in str(m.get("label") or m.get("name") or "").lower()
        ]
        if not hits:
            _add_transcript(state, _info_line(f"no models match '{q}'", "yellow"))
            return
        table = Table(title=f"Search '{q}'", header_style="bold magenta")
        table.add_column("id", style="cyan")
        table.add_column("label")
        for m in hits:
            table.add_row(str(m.get("id", "-")), str(m.get("label") or m.get("name") or ""))
        _add_transcript(state, table)
        return

    # Otherwise treat the rest as a model id
    model_id = rest.split()[0]
    state.active_model = model_id
    if state.active_agent:
        ok = await _patch_agent_config(state, state.active_agent, {"model": model_id})
        if ok:
            _add_transcript(
                state,
                _info_line(
                    f"model set to '{model_id}' (persisted on agent '{state.active_agent}')",
                    "green",
                ),
            )
        else:
            _add_transcript(
                state,
                _info_line(
                    f"model set to '{model_id}' (session only — failed to persist on agent)",
                    "yellow",
                ),
            )
    else:
        _add_transcript(state, _info_line(f"model set to '{model_id}' (session)", "green"))


async def _list_agents_raw(state: ReplState) -> List[dict]:
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=10.0) as client:
            resp = await client.get("/api/agents")
            if resp.status_code == 200:
                payload = resp.json() or {}
                agents = payload.get("agents") or []
                if isinstance(agents, dict):
                    out = []
                    for name, body in agents.items():
                        rec = dict(body) if isinstance(body, dict) else {}
                        rec.setdefault("name", name)
                        out.append(rec)
                    agents = out
                state.known_agents = [
                    a.get("name") for a in agents if isinstance(a, dict) and a.get("name")
                ]
                return [a for a in agents if isinstance(a, dict)]
    except Exception as exc:
        _add_transcript(state, _info_line(f"error: {exc}", "red"))
    return []


async def _cmd_agent(state: ReplState, rest: str) -> None:
    rest = rest.strip()
    if not rest:
        agents = await _list_agents_raw(state)
        if not agents:
            _add_transcript(state, _info_line("no agents registered", "yellow"))
            return
        table = Table(title="Agents", header_style="bold magenta")
        table.add_column("", width=2)
        table.add_column("name", style="cyan")
        table.add_column("type")
        table.add_column("backend")
        table.add_column("model")
        table.add_column("enabled")
        for a in agents:
            cfg = a.get("config") if isinstance(a.get("config"), dict) else {}
            backend = a.get("backend") or cfg.get("backend") or a.get("provider") or "-"
            model = a.get("model") or cfg.get("model") or "-"
            atype = a.get("type") or a.get("kind") or a.get("role") or "-"
            enabled = a.get("enabled")
            if enabled is None:
                enabled = a.get("status", "-")
            mark = "▸" if a.get("name") == state.active_agent else " "
            table.add_row(
                mark,
                str(a.get("name", "-")),
                str(atype),
                str(backend),
                str(model),
                str(enabled),
            )
        _add_transcript(state, table)
        return

    parts = rest.split()
    name = parts[0]
    if len(parts) == 1:
        # set active agent + load its config
        state.active_agent = name
        cfg = await _fetch_agent_config(state, name)
        state.active_backend = cfg.get("backend") or "auto"
        state.active_model = cfg.get("model")
        # refresh agent/backend caches
        if name not in state.known_agents:
            state.known_agents.append(name)
        await _fetch_models(state, state.active_backend)
        _add_transcript(
            state,
            _info_line(
                f"active agent: {name}  (backend={state.active_backend or 'auto'}, model={state.active_model or 'default'})",
                "green",
            ),
        )
        return

    sub = parts[1].lower()
    if sub == "backend":
        if len(parts) < 3:
            _add_transcript(state, _info_line(f"usage: /agent {name} backend BACKEND", "yellow"))
            return
        be = parts[2]
        ok = await _patch_agent_config(state, name, {"backend": be})
        if ok:
            if state.active_agent == name:
                state.active_backend = be
            _add_transcript(state, _info_line(f"agent '{name}' backend → {be}", "green"))
        else:
            _add_transcript(state, _info_line(f"failed to set backend on '{name}'", "red"))
        return
    if sub == "model":
        if len(parts) < 3:
            _add_transcript(state, _info_line(f"usage: /agent {name} model ID", "yellow"))
            return
        mid = parts[2]
        ok = await _patch_agent_config(state, name, {"model": mid})
        if ok:
            if state.active_agent == name:
                state.active_model = mid
            _add_transcript(state, _info_line(f"agent '{name}' model → {mid}", "green"))
        else:
            _add_transcript(state, _info_line(f"failed to set model on '{name}'", "red"))
        return
    if sub in ("enable", "disable"):
        try:
            async with httpx.AsyncClient(base_url=state.api_url, timeout=10.0) as client:
                resp = await client.post(f"/api/agents/{name}/{sub}")
            if 200 <= resp.status_code < 300:
                _add_transcript(state, _info_line(f"agent '{name}' {sub}d", "green"))
            else:
                _add_transcript(
                    state,
                    _info_line(f"agent {sub} failed: HTTP {resp.status_code}", "red"),
                )
        except Exception as exc:
            _add_transcript(state, _info_line(f"error: {exc}", "red"))
        return

    _add_transcript(state, _info_line(f"unknown /agent subcommand: {sub}", "red"))


async def _cmd_whoami(state: ReplState) -> None:
    table = Table(title="whoami", header_style="bold magenta")
    table.add_column("field", style="cyan")
    table.add_column("value")
    table.add_row("api_url", str(state.api_url))
    table.add_row("active_agent", str(state.active_agent or "—"))
    table.add_row("active_backend", str(state.active_backend or "auto"))
    table.add_row("active_model", str(state.active_model or "default"))
    table.add_row("connected", "yes" if state.connected else "no")
    table.add_row("session_id", str(state.session_id))
    _add_transcript(state, table)


# ---------------------------------------------------------------------------
# Tab-completion
# ---------------------------------------------------------------------------


_SLASH_COMMANDS = [
    "/help", "/quit", "/exit", "/clear",
    "/agents", "/agent", "/pipeline", "/project", "/rag", "/ingest",
    "/model", "/backend", "/whoami",
]


if _HAS_PT:
    class _SkyCompleter(Completer):  # type: ignore[misc,valid-type]
        def __init__(self, state: ReplState) -> None:
            self.state = state

        def get_completions(self, document, complete_event):  # type: ignore[no-untyped-def]
            text = document.text_before_cursor.lstrip()
            if not text.startswith("/"):
                return
            parts = text.split()
            head = parts[0] if parts else ""
            # If the cursor sits right after a space, treat as an empty new token
            trailing_space = text.endswith(" ")
            if trailing_space:
                parts.append("")

            if len(parts) <= 1:
                for c in _SLASH_COMMANDS:
                    if c.startswith(head):
                        yield Completion(c, start_position=-len(head))
                return

            if head == "/backend" and len(parts) == 2:
                token = parts[1]
                for b in self.state.known_backends:
                    if b.startswith(token):
                        yield Completion(b, start_position=-len(token))
                return

            if head == "/model" and len(parts) == 2:
                token = parts[1]
                for cand in ("list", "search"):
                    if cand.startswith(token):
                        yield Completion(cand, start_position=-len(token))
                for m in self.state.known_models:
                    if m and m.startswith(token):
                        yield Completion(m, start_position=-len(token))
                return

            if head == "/agent" and len(parts) == 2:
                token = parts[1]
                for a in self.state.known_agents:
                    if a and a.startswith(token):
                        yield Completion(a, start_position=-len(token))
                return
            if head == "/agent" and len(parts) == 3:
                token = parts[2]
                for sub in ("backend", "model", "enable", "disable"):
                    if sub.startswith(token):
                        yield Completion(sub, start_position=-len(token))
                return
            if head == "/agent" and len(parts) == 4:
                token = parts[3]
                sub = parts[2].lower()
                if sub == "backend":
                    for b in self.state.known_backends:
                        if b.startswith(token):
                            yield Completion(b, start_position=-len(token))
                elif sub == "model":
                    for m in self.state.known_models:
                        if m and m.startswith(token):
                            yield Completion(m, start_position=-len(token))
                return


# ---------------------------------------------------------------------------
# Warmup — populate completion caches and pick a default agent
# ---------------------------------------------------------------------------


async def _warmup(state: ReplState) -> None:
    # backends
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=5.0) as client:
            r = await client.get("/api/llm/backends")
            if r.status_code == 200:
                payload = r.json() or {}
                items = payload.get("backends") or []
                names: List[str] = []
                for b in items:
                    if isinstance(b, str):
                        names.append(b)
                    elif isinstance(b, dict):
                        n = b.get("name") or b.get("id")
                        if n:
                            names.append(n)
                state.known_backends = names
    except Exception:
        pass
    # agents
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=5.0) as client:
            r = await client.get("/api/agents")
            if r.status_code == 200:
                payload = r.json() or {}
                ag = payload.get("agents") or {}
                if isinstance(ag, dict):
                    state.known_agents = list(ag.keys())
                else:
                    state.known_agents = [
                        a.get("name") for a in ag if isinstance(a, dict) and a.get("name")
                    ]
                # default active_agent if not set: prefer 'writer', else first
                if state.active_agent is None and state.known_agents:
                    state.active_agent = next(
                        (n for n in state.known_agents if n == "writer"),
                        state.known_agents[0],
                    )
                    try:
                        r2 = await client.get(f"/api/agents/{state.active_agent}/config")
                        if r2.status_code == 200:
                            cfg = (r2.json() or {}).get("config") or {}
                            state.active_backend = cfg.get("backend") or "auto"
                            state.active_model = cfg.get("model")
                    except Exception:
                        pass
    except Exception:
        pass
    # models for current backend
    try:
        be = state.active_backend or "auto"
        async with httpx.AsyncClient(base_url=state.api_url, timeout=5.0) as client:
            r = await client.get(f"/api/llm/models?backend={be}")
            if r.status_code == 200:
                payload = r.json() or {}
                state.known_models = [
                    m.get("id") for m in (payload.get("models") or [])
                    if isinstance(m, dict) and m.get("id")
                ]
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Input loop (prompt_toolkit if available, else basic input())
# ---------------------------------------------------------------------------


def _read_one(session: Any) -> Optional[str]:
    """Read one logical input (single line or multi-line block)."""
    try:
        first = session.prompt("> ")
    except (EOFError, KeyboardInterrupt):
        raise
    if first.strip() == '"""':
        # Multi-line block
        lines: List[str] = []
        while True:
            try:
                ln = session.prompt("... ")
            except (EOFError, KeyboardInterrupt):
                raise
            if ln.strip() == '"""':
                break
            lines.append(ln)
        return "\n".join(lines)
    return first


def _read_one_basic() -> Optional[str]:
    first = input("> ")
    if first.strip() == '"""':
        lines: List[str] = []
        while True:
            ln = input("... ")
            if ln.strip() == '"""':
                break
            lines.append(ln)
        return "\n".join(lines)
    return first


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def run() -> None:
    """Entry point for the interactive REPL."""
    console = Console()
    state = ReplState()

    # Probe API
    try:
        with httpx.Client(base_url=API_BASE, timeout=2.0) as client:
            client.get("/api/status").raise_for_status()
    except Exception:
        console.print(
            f"[red]skyn3t API not reachable at {API_BASE}[/red] — start the server "
            "with [bold]skyn3t start[/bold] (or `python -m skyn3t.web`)."
        )
        return

    if not _HAS_PT:
        console.print(
            "[yellow]warning:[/yellow] prompt_toolkit not installed — "
            "using basic input(). For full features run "
            "[bold]pip install prompt_toolkit[/bold]."
        )

    state.transcript.append(
        _info_line(
            f"Connected to {API_BASE}. Type /help for commands. "
            "Use \"\"\" on its own line for multi-line input.",
            "green",
        )
    )

    # Background loop in a dedicated thread so the Live render + input loop
    # share the main thread.
    loop = asyncio.new_event_loop()

    def _bg() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_ws_listener(state))

    bg_thread = threading.Thread(target=_bg, daemon=True)
    bg_thread.start()

    # Kick off a non-blocking warmup to populate completion caches / default agent.
    try:
        asyncio.run_coroutine_threadsafe(_warmup(state), loop)
    except Exception:
        pass

    pt_session = None
    if _HAS_PT:
        pt_session = PromptSession(
            history=InMemoryHistory(),
            completer=_SkyCompleter(state),
            complete_while_typing=False,
        )

    # Snapshot rendering: paint the layout above each prompt rather than running
    # rich.Live, which stomps over the input line and hides what the user types.
    def _paint_snapshot() -> None:
        try:
            console.clear()
            console.print(_render_layout(state))
        except Exception:
            pass

    try:
        # Initial snapshot
        _paint_snapshot()
        while not state.stop:
            try:
                if pt_session is not None:
                    line = _read_one(pt_session)
                else:
                    line = _read_one_basic()
            except EOFError:
                break
            except KeyboardInterrupt:
                if state.busy:
                    asyncio.run_coroutine_threadsafe(_cancel_current(state), loop)
                    _add_transcript(state, _info_line("cancelled", "yellow"))
                else:
                    break
                continue

            if line is None:
                continue
            line = line.strip("\n")
            if not line.strip():
                continue

            if line.startswith("/"):
                fut = asyncio.run_coroutine_threadsafe(_slash(state, line), loop)
                try:
                    should_exit = fut.result(timeout=120)
                except Exception as exc:
                    _add_transcript(state, _info_line(f"command error: {exc}", "red"))
                    should_exit = False
                if should_exit:
                    break
            else:
                _add_transcript(state, _user_line(line))
                fut = asyncio.run_coroutine_threadsafe(_submit_prompt(state, line), loop)
                try:
                    fut.result(timeout=600)
                except KeyboardInterrupt:
                    asyncio.run_coroutine_threadsafe(_cancel_current(state), loop)
                    _add_transcript(state, _info_line("cancelled", "yellow"))
                except Exception as exc:
                    _add_transcript(state, _info_line(f"error: {exc}", "red"))

            # Repaint the swarm/transcript snapshot above the next prompt
            _paint_snapshot()
    finally:
        state.stop = True
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        console.print("[dim]bye.[/dim]")


__all__ = ["run"]
