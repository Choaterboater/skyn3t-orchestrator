"""SkyN3t Interactive REPL — Claude-Code-style swarm console.

Run with `skyn3t` (no args) or `skyn3t repl` to drop into an interactive
session with a live transcript on the left and a swarm-activity sidebar on
the right.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import secrets
import shlex
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

import httpx
from rich.console import Console, Group
from rich.layout import Layout
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from skyn3t.studio.repo_target import normalize_repo_target, resolve_repo_target

API_BASE = os.environ.get("SKYN3T_API_URL", "http://localhost:6660")
WS_URL = API_BASE.replace("http://", "ws://").replace("https://", "wss://")

# Optional dependencies ------------------------------------------------------
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import InMemoryHistory
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

_LOW_SIGNAL_ACTIVITY_TYPES = {
    "AGENT_REGISTERED",
    "LLM_EXCHANGE",
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
    current_prompt: Optional[str] = None
    last_prompt: Optional[str] = None
    last_failed_prompt: Optional[str] = None
    last_interrupted_prompt: Optional[str] = None
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
    render_version: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _glyph(event_type: str) -> str:
    return EVENT_GLYPHS.get(event_type, "•")


def _terminal_width() -> int:
    return shutil.get_terminal_size((120, 40)).columns


def _activity_sidebar_enabled(items: List[Text]) -> bool:
    return bool(items) and _terminal_width() >= 120


def _format_event(evt: dict) -> Optional[Text]:
    et = str(evt.get("event_type") or evt.get("type") or "").upper()
    kind = str(evt.get("kind") or "").lower()
    if et in _LOW_SIGNAL_ACTIVITY_TYPES or kind == "convo":
        return None

    if kind == "project" and not et.startswith("PROJECT_"):
        payload = evt.get("meta", {}).get("payload") if isinstance(evt.get("meta"), dict) else {}
        payload_kind = str((payload or {}).get("kind") or "").upper()
        if payload_kind.startswith("PROJECT_"):
            et = payload_kind

    data = evt.get("data") or {}
    if isinstance(data, dict) and "data" in data and "event_type" in data:
        # nested {"type": "event", "data": {...}}
        evt = data
        et = str(evt.get("event_type", et) or "").upper()
        data = evt.get("data") or {}
        if et in _LOW_SIGNAL_ACTIVITY_TYPES:
            return None

    glyph = _glyph(et)
    source = (
        evt.get("from")
        or evt.get("source")
        or (data.get("agent") if isinstance(data, dict) else None)
        or ""
    )
    snippet = str(evt.get("label") or "").strip()
    if isinstance(data, dict):
        if not snippet:
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
    if len(snippet) > 64:
        snippet = snippet[:61] + "..."

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

    verb = ""
    if et in {"TASK_STARTED", "TASK_EXECUTION_STARTED"}:
        verb = "started"
    elif et == "TASK_COMPLETED":
        verb = "finished"
    elif et.startswith("TASK_FAIL"):
        verb = "failed"
    elif et == "PIPELINE_STARTED":
        verb = "pipeline"
    elif et == "PIPELINE_COMPLETED":
        verb = "pipeline done"
    elif et == "PIPELINE_STAGE_COMPLETED":
        verb = "stage done"
    elif et == "PIPELINE_STAGE_FAILED":
        verb = "stage failed"
    elif et.startswith("PROJECT_"):
        verb = _studio_history_label(et)
    elif et == "AGENT_THOUGHT":
        verb = "thinking"
    elif et.startswith("AGENT_MESSAGE"):
        verb = "message"
    elif et.startswith("RAG_"):
        verb = "retrieving"
    elif et.startswith("INGEST_"):
        verb = "ingesting"

    label = str(source or evt.get("kind") or et.lower() or "agent")
    line = Text(f"{glyph} ", style=style)
    line.append(label, style=f"bold {style}")
    if verb:
        line.append(f"  {verb}", style=style)
    if snippet:
        line.append(f"  {snippet}", style="dim")
    return line


def _render_layout(state: ReplState) -> Layout:
    layout = Layout()
    act_tail = list(state.activity)[-8:]
    if _activity_sidebar_enabled(act_tail):
        layout.split_column(
            Layout(name="header", size=4),
            Layout(name="body", ratio=1),
        )
        layout["body"].split_row(
            Layout(name="transcript", ratio=3),
            Layout(name="activity", size=38),
        )
    else:
        sections: List[Layout] = [
            Layout(name="header", size=4),
            Layout(name="transcript", ratio=1),
        ]
        if act_tail:
            sections.append(Layout(name="activity", size=max(8, min(12, len(act_tail) + 2))))
        layout.split_column(*sections)

    dot = Text("●", style="green" if state.connected else "red")
    title = Text("SkyN3t Chat", style="bold cyan")
    title.append(f"  ·  session {state.session_id}", style="dim")
    route_target = state.active_agent or "chat"
    route_backend = state.active_backend or "auto"
    route_model = state.active_model or "default"
    title.append(f"  ·  {route_target} → {route_backend}/{route_model}", style="dim")
    title.append("   ")
    title.append_text(dot)
    if state.busy:
        title.append("  ")
        title.append(f"⏳ {state.busy_label}", style="yellow")
    hint = Text('Enter to send  •  """ for multi-line  •  /help for commands', style="dim")
    layout["header"].update(Panel(Group(title, hint), border_style="cyan"))

    # Transcript: cap recent entries so long replies don't swamp the next prompt.
    tail = state.transcript[-14:]
    transcript = Group(*tail) if tail else Text("(empty — type a prompt below)", style="dim")
    layout["transcript"].update(Panel(transcript, title="Chat", border_style="white"))

    if act_tail:
        activity = Group(*act_tail)
        layout["activity"].update(Panel(activity, title="Agents at work", border_style="magenta"))

    return layout


def _touch_state(state: ReplState) -> None:
    state.render_version += 1


def _add_transcript(state: ReplState, renderable: Any) -> None:
    state.transcript.append(renderable)
    _touch_state(state)


def _add_activity(state: ReplState, renderable: Text) -> None:
    if state.activity and state.activity[-1].plain == renderable.plain:
        return
    state.activity.append(renderable)
    _touch_state(state)


def _user_line(text: str) -> Text:
    t = Text("You  ", style="bold cyan")
    t.append(text, style="white")
    return t


def _agent_line(name: str, text: str) -> Any:
    md = Markdown(text) if text else Text("(no output)", style="dim")
    title = Text(f"{name}", style="bold green")
    title.append(f"  {datetime.now().strftime('%H:%M:%S')}", style="dim")
    return Panel(md, title=title, border_style="green", padding=(0, 1))


def _info_line(text: str, style: str = "yellow") -> Text:
    return Text(text, style=style)


def _studio_history_label(event_name: str) -> str:
    labels = {
        "PROJECT_QUEUED": "Queued",
        "PROJECT_STARTED": "Started",
        "PROJECT_STAGE_STARTED": "Stage started",
        "PROJECT_STAGE_COMPLETED": "Stage completed",
        "PROJECT_STAGE_FAILED": "Stage failed",
        "PROJECT_AWAITING_CLARIFICATION": "Waiting for clarification",
        "PROJECT_RESUMED": "Resumed",
        "PROJECT_COMPLETED": "Project finished",
        "PROJECT_FAILED": "Runner failed",
        "PROJECT_REAPED": "Recovered",
    }
    return labels.get(
        str(event_name or "").upper(),
        str(event_name or "update").replace("_", " ").title(),
    )


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
                            _add_activity(state, line)
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
    async with httpx.AsyncClient(base_url=state.api_url, timeout=5.0) as client:
        while not state.stop:
            try:
                resp = await client.get("/api/swarm/snapshot")
                if resp.status_code == 200:
                    state.connected = True
                    items = resp.json().get("events", [])
                    for evt in items[-20:]:
                        line = _format_event(evt)
                        if line is not None:
                            _add_activity(state, line)
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
    state.current_prompt = prompt
    state.last_prompt = prompt
    _touch_state(state)
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=60.0) as client:
            if not state.active_agent:
                await _submit_chat_prompt(state, client, prompt)
                return
            # Try orchestrator submit, fall back to a registered agent's exec.
            task_id: Optional[str] = None
            try:
                resp = await client.post(
                    "/api/orchestrator/submit",
                    json={
                        "prompt": prompt,
                        "session_id": state.session_id,
                        "agent_name": state.active_agent,
                    },
                )
                if resp.status_code == 200:
                    task_id = resp.json().get("task_id")
            except Exception:
                task_id = None

            if not task_id:
                # Pick an agent and use exec for a synchronous round-trip.
                await _exec_prompt_direct(state, client, prompt)
                return

            # Poll the task result
            state.current_task_id = task_id
            while not state.stop:
                try:
                    resp = await client.get(f"/api/tasks/{task_id}/result")
                    if resp.status_code == 404:
                        await _best_effort_cancel_task(client, task_id)
                        await _exec_prompt_direct(
                            state,
                            client,
                            prompt,
                            notice=(
                                "orchestrator result API unavailable — "
                                "falling back to direct agent reply"
                            ),
                        )
                        return
                    data = resp.json() if resp.status_code == 200 else {}
                except Exception:
                    data = {}
                if data.get("status") == "pending" or "success" not in data:
                    await asyncio.sleep(0.8)
                    continue
                if data.get("success"):
                    out = data.get("output") or {}
                    text = out.get("response") if isinstance(out, dict) else str(out)
                    state.last_failed_prompt = None
                    _add_transcript(state, _agent_line("orchestrator", text or ""))
                else:
                    state.last_failed_prompt = prompt
                    _add_transcript(
                        state,
                        _info_line(f"task failed: {data.get('error')}", "red"),
                    )
                break
    finally:
        state.busy = False
        state.busy_label = ""
        state.current_task_id = None
        state.current_prompt = None
        _touch_state(state)


async def _submit_chat_prompt(
    state: ReplState,
    client: httpx.AsyncClient,
    prompt: str,
) -> None:
    try:
        resp = await client.post(
            "/api/llm/complete",
            json={
                "prompt": prompt,
                "backend": state.active_backend,
                "model": state.active_model,
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()
        response = str(data.get("response") or "").strip()
        if not response:
            state.last_failed_prompt = prompt
            _add_transcript(state, _info_line("empty response from chat backend", "red"))
            return
        backend = str(data.get("backend") or state.active_backend or "auto")
        state.last_failed_prompt = None
        _add_transcript(state, _agent_line(backend, response))
    except httpx.HTTPError as exc:
        state.last_failed_prompt = prompt
        _add_transcript(state, _info_line(f"chat request failed: {exc}", "red"))


async def _exec_prompt_direct(
    state: ReplState,
    client: httpx.AsyncClient,
    prompt: str,
    *,
    notice: Optional[str] = None,
) -> None:
    agent_name = state.active_agent or await _pick_agent(client)
    if not agent_name:
        _add_transcript(
            state,
            _info_line(
                "No agents registered. Add one with `skyn3t agent add ...`",
                "red",
            ),
        )
        return
    if notice:
        _add_transcript(state, _info_line(notice, "yellow"))
    try:
        resp = await client.post(
            f"/api/agents/{agent_name}/exec",
            json={"prompt": prompt},
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            state.last_failed_prompt = prompt
            _add_transcript(state, _info_line(f"error: {data['error']}", "red"))
        else:
            state.last_failed_prompt = None
            _add_transcript(state, _agent_line(agent_name, data.get("output") or ""))
    except httpx.HTTPError as exc:
        state.last_failed_prompt = prompt
        _add_transcript(state, _info_line(f"request failed: {exc}", "red"))


async def _best_effort_cancel_task(client: httpx.AsyncClient, task_id: str) -> None:
    try:
        await client.post(f"/api/tasks/{task_id}/cancel")
    except Exception:
        pass


async def _pick_agent(client: httpx.AsyncClient) -> Optional[str]:
    try:
        resp = await client.get("/api/agents")
        payload = resp.json()
        agents = payload.get("agents", []) if isinstance(payload, dict) else []
        if not agents:
            return None
        for a in agents:
            if a.get("status", "idle") in ("idle", "busy"):
                name = a.get("name")
                if isinstance(name, str):
                    return name
        first_name = agents[0].get("name")
        return first_name if isinstance(first_name, str) else None
    except Exception:
        return None


async def _cancel_current(state: ReplState) -> None:
    if state.current_prompt:
        state.last_interrupted_prompt = state.current_prompt
    if not state.current_task_id:
        return
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=5.0) as client:
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
- `/tasks` — list currently running tasks
- `/pipeline NAME [PROMPT...]` — run a pipeline by name
- `/project BRIEF...` — start a Studio run (defaults to the auto planner)
- `/project --audience builders --autonomy confirm_first BRIEF...` — steer the run before launch
- `/project --repo-path ../customer-portal --focus-file src/login.tsx BRIEF...` — target another local git repo or GitHub URL
- `/project TEMPLATE :: BRIEF...` — force a specific Studio template
- `/rag QUERY` — run agentic RAG
- `/ingest REPO` — kick off a GitHub repo ingestion
- `/doctor` — run API health checks
- `/memory` — show session/operator/project memory layers
- `/memory drafts` — list pending reviewable memory drafts
- `/memory approve DOC_ID` — promote a draft into trusted memory
- `/memory reject DOC_ID [reason]` — reject a pending memory draft
- `/memory evals [STATUS]` — list governed evaluation assets
- `/memory eval DOC_ID` — inspect one evaluation asset
- `/memory export-eval DOC_ID [json|jsonl]` — export an approved evaluation asset
- `/memory SESSION_ID` — inspect one active session memory
- `/resume` — re-run the last interrupted prompt
- `/retry` — re-run the last failed prompt

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

PROJECT_TEMPLATE_KEYS = {
    "auto",
    "app_saas",
    "marketing",
    "business_site",
    "brand_kit",
    "business_plan",
    "product_idea",
    "frontend_redesign",
}

_PROJECT_INTENT_PREFIXES = (
    "build ",
    "build me ",
    "create ",
    "create me ",
    "make ",
    "make me ",
    "start ",
    "start me ",
    "generate ",
    "generate me ",
    "design ",
    "launch ",
    "ship ",
)

_PROJECT_INTENT_TERMS = (
    "app",
    "application",
    "website",
    "site",
    "landing page",
    "dashboard",
    "tracker",
    "habit tracker",
    "tool",
    "platform",
    "portal",
    "service",
    "api",
    "backend",
    "frontend",
    "game",
    "saas",
    "project",
    "product",
    "mvp",
    "workflow",
    "cli",
)


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
    if cmd == "/tasks":
        await _cmd_tasks(state)
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
    if cmd == "/doctor":
        await _cmd_doctor(state)
        return False
    if cmd == "/memory":
        await _cmd_memory(state, rest)
        return False
    if cmd == "/resume":
        await _cmd_resume(state)
        return False
    if cmd == "/retry":
        await _cmd_retry(state)
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
        async with httpx.AsyncClient(base_url=state.api_url, timeout=10.0) as client:
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


async def _cmd_tasks(state: ReplState) -> None:
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=10.0) as client:
            resp = await client.get("/api/swarm/snapshot")
            data = resp.json()
    except Exception as exc:
        _add_transcript(state, _info_line(f"tasks failed: {exc}", "red"))
        return
    tasks = data.get("running_tasks") or []
    if not tasks:
        _add_transcript(state, _info_line("no running tasks", "yellow"))
        return
    table = Table(title="Running tasks", header_style="bold magenta")
    table.add_column("task_id", style="cyan")
    table.add_column("agent")
    table.add_column("title")
    table.add_column("session", style="dim")
    for task in tasks:
        table.add_row(
            str(task.get("task_id", "-"))[:8],
            str(task.get("agent") or "—"),
            str(task.get("title") or "Untitled"),
            str(task.get("session_id") or "—"),
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
        async with httpx.AsyncClient(base_url=state.api_url, timeout=30.0) as client:
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
    parsed = await asyncio.to_thread(_parse_project_command, rest)
    if parsed.get("error"):
        _add_transcript(state, _info_line(str(parsed["error"]), "yellow"))
        return
    template = str(parsed["template"])
    brief = str(parsed["brief"])
    audience = str(parsed["audience"])
    autonomy = str(parsed["autonomy"])
    repo_target = parsed["repo_target"]
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=30.0) as client:
            resp = await client.post(
                "/api/studio/start",
                json={
                    "template": template,
                    "brief": brief,
                    "mission_setup": {
                        "audience": _project_audience_map()[audience],
                        "autonomy": autonomy,
                    },
                    "repo_target": repo_target,
                },
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
    if data.get("accepted"):
        slug = data.get("slug") or "pending"
        repo_target = normalize_repo_target(data.get("repo_target") or repo_target)
        next_action = data.get("next_action") or "Queued — waiting for a worker slot."
        _add_transcript(
            state,
            _info_line(
                "project queued: "
                f"{slug} · template={template} · audience={audience} · mode={autonomy}"
                + f" · next={next_action}"
                + (
                    f" · repo={repo_target['local_path']}"
                    if repo_target["local_path"]
                    else ""
                )
                + (
                    f" · focus={repo_target['focus_file']}"
                    if repo_target["focus_file"]
                    else ""
                ),
                "green",
            ),
        )
        return
    _add_transcript(state, _info_line(f"project failed: {data}", "red"))


def _project_usage() -> str:
    return (
        "usage: /project [--audience auto|general|builders|team|leaders|investors] "
        "[--autonomy balanced|confirm_first|move_fast] [--repo-path PATH_OR_GITHUB_URL] [--focus-file PATH] BRIEF... "
        "or /project [options] TEMPLATE :: BRIEF..."
    )


def _project_audience_map() -> Dict[str, str]:
    return {
        "auto": "",
        "general": "general",
        "builders": "builders",
        "team": "team",
        "leaders": "leaders",
        "investors": "investors",
    }


def _looks_like_project_request(prompt: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(prompt or "").strip().lower())
    if not normalized or normalized.startswith("/"):
        return False
    if not normalized.startswith(_PROJECT_INTENT_PREFIXES):
        return False
    return any(term in normalized for term in _PROJECT_INTENT_TERMS)


def _should_route_prompt_to_project(state: ReplState, prompt: str) -> bool:
    return state.active_agent is None and _looks_like_project_request(prompt)


def _parse_project_command(rest: str) -> Dict[str, Any]:
    usage = _project_usage()
    if not rest:
        return {"error": usage}
    try:
        tokens = shlex.split(rest)
    except ValueError:
        return {"error": usage}
    audience = "auto"
    autonomy = "balanced"
    repo_path = ""
    focus_file = ""
    cleaned: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--audience="):
            audience = token.split("=", 1)[1]
            i += 1
            continue
        if token == "--audience" and i + 1 < len(tokens):
            audience = tokens[i + 1]
            i += 2
            continue
        if token.startswith("--autonomy="):
            autonomy = token.split("=", 1)[1]
            i += 1
            continue
        if token == "--autonomy" and i + 1 < len(tokens):
            autonomy = tokens[i + 1]
            i += 2
            continue
        if token.startswith("--repo-path="):
            repo_path = token.split("=", 1)[1]
            i += 1
            continue
        if token == "--repo-path" and i + 1 < len(tokens):
            repo_path = tokens[i + 1]
            i += 2
            continue
        if token.startswith("--focus-file="):
            focus_file = token.split("=", 1)[1]
            i += 1
            continue
        if token == "--focus-file" and i + 1 < len(tokens):
            focus_file = tokens[i + 1]
            i += 2
            continue
        cleaned.append(token)
        i += 1

    audience_map = _project_audience_map()
    audience = str(audience or "auto").strip().lower()
    autonomy = str(autonomy or "balanced").strip().lower()
    allowed_autonomy = {"balanced", "confirm_first", "move_fast"}
    if audience not in audience_map or autonomy not in allowed_autonomy:
        return {"error": usage}
    template = "auto"
    brief = " ".join(cleaned).strip()

    if "::" in brief:
        maybe_template, maybe_brief = [part.strip() for part in brief.split("::", 1)]
        if maybe_template:
            template = maybe_template
        brief = maybe_brief
    else:
        parts = rest.split(maxsplit=1)
        if len(parts) > 1 and parts[0] in PROJECT_TEMPLATE_KEYS:
            template = parts[0]
            brief = parts[1]

    if not brief:
        return {"error": usage}
    try:
        repo_target = resolve_repo_target(
            {"local_path": repo_path, "focus_file": focus_file},
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "template": template,
        "brief": brief,
        "audience": audience,
        "autonomy": autonomy,
        "repo_target": repo_target,
    }


def _watch_project_in_repl(
    state: ReplState,
    slug: str,
    *,
    paint: Any,
    prompt_reader: Any,
) -> None:
    seen_history = 0
    seen_clarification: Optional[tuple[str, ...]] = None
    terminal_statuses = {"done", "needs_fixes", "failed"}
    try:
        with httpx.Client(base_url=state.api_url, timeout=30.0) as client:
            while True:
                updated = False
                resp = client.get(f"/api/studio/projects/{slug}")
                resp.raise_for_status()
                project = resp.json()
                history = project.get("history") or []
                if not isinstance(history, list):
                    history = []
                for item in history[seen_history:]:
                    event_name = str(item.get("event") or "")
                    label = _studio_history_label(event_name)
                    stage = str(item.get("stage") or "").strip()
                    message = str(item.get("message") or "").strip()
                    line = f"{label}"
                    if stage:
                        line += f" · {stage}"
                    if message:
                        line += f" · {message}"
                    _add_transcript(state, _info_line(line, "cyan"))
                    updated = True
                seen_history = len(history)
                if updated:
                    paint()

                status = str(project.get("status") or "").strip().lower()
                if status == "awaiting_clarification":
                    clarification = project.get("clarification") or {}
                    questions = tuple(
                        str(question).strip()
                        for question in (clarification.get("questions") or [])
                        if str(question).strip()
                    )
                    if questions and questions != seen_clarification:
                        _add_transcript(
                            state,
                            Panel(
                                "\n".join(
                                    f"{index}. {question}"
                                    for index, question in enumerate(questions, start=1)
                                )
                                + "\n\nReply inline now. After the last answer, the build resumes.",
                                title="Clarification needed",
                                border_style="yellow",
                            ),
                        )
                        paint()
                        answers = [
                            str(prompt_reader(index, question)).strip()
                            for index, question in enumerate(questions, start=1)
                        ]
                        clarify_resp = client.post(
                            f"/api/studio/projects/{slug}/clarify",
                            json={"answers": answers},
                        )
                        clarify_resp.raise_for_status()
                        data = clarify_resp.json()
                        if not data.get("ok"):
                            _add_transcript(
                                state,
                                _info_line(f"clarification failed: {data}", "red"),
                            )
                            paint()
                            return
                        _add_transcript(
                            state,
                            _info_line("clarifications sent. Resuming build…", "green"),
                        )
                        seen_clarification = questions
                        paint()

                if status in terminal_statuses:
                    title = "Project finished" if status != "failed" else "Project failed"
                    border = "green" if status != "failed" else "red"
                    next_action = str(project.get("next_action") or "").strip()
                    summary = next_action or (
                        "Build completed." if status != "failed" else "Build failed."
                    )
                    _add_transcript(
                        state,
                        Panel(
                            f"{slug}\n{summary}",
                            title=title,
                            border_style=border,
                        ),
                    )
                    paint()
                    return
                time.sleep(1.0)
    except Exception as exc:
        _add_transcript(state, _info_line(f"project watch failed: {exc}", "red"))
        paint()


def _run_project_command(
    state: ReplState,
    rest: str,
    *,
    paint: Any,
    prompt_reader: Any,
) -> None:
    parsed = _parse_project_command(rest)
    if parsed.get("error"):
        _add_transcript(state, _info_line(str(parsed["error"]), "yellow"))
        paint()
        return

    template = str(parsed["template"])
    brief = str(parsed["brief"])
    audience = str(parsed["audience"])
    autonomy = str(parsed["autonomy"])
    repo_target = parsed["repo_target"]
    _add_transcript(state, _info_line("starting project build…", "cyan"))
    paint()
    try:
        with httpx.Client(base_url=state.api_url, timeout=30.0) as client:
            resp = client.post(
                "/api/studio/start",
                json={
                    "template": template,
                    "brief": brief,
                    "mission_setup": {
                        "audience": _project_audience_map()[audience],
                        "autonomy": autonomy,
                    },
                    "repo_target": repo_target,
                },
            )
            if resp.status_code == 404:
                _add_transcript(
                    state, _info_line("project studio not yet available", "yellow")
                )
                paint()
                return
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        _add_transcript(state, _info_line(f"project failed: {exc}", "red"))
        paint()
        return

    if not data.get("accepted"):
        _add_transcript(state, _info_line(f"project failed: {data}", "red"))
        paint()
        return

    slug = data.get("slug") or "pending"
    repo_target = normalize_repo_target(data.get("repo_target") or repo_target)
    next_action = data.get("next_action") or "Queued — waiting for a worker slot."
    _add_transcript(
        state,
        _info_line(
            "project queued: "
            f"{slug} · template={template} · audience={audience} · mode={autonomy}"
            + f" · next={next_action}"
            + (
                f" · repo={repo_target['local_path']}"
                if repo_target["local_path"]
                else ""
            )
            + (
                f" · focus={repo_target['focus_file']}"
                if repo_target["focus_file"]
                else ""
            ),
            "green",
        ),
    )
    paint()
    _watch_project_in_repl(state, str(slug), paint=paint, prompt_reader=prompt_reader)


async def _cmd_rag(state: ReplState, rest: str) -> None:
    if not rest:
        _add_transcript(state, _info_line("usage: /rag QUERY", "yellow"))
        return
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=60.0) as client:
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
        async with httpx.AsyncClient(base_url=state.api_url, timeout=15.0) as client:
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


async def _cmd_doctor(state: ReplState) -> None:
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=10.0) as client:
            resp = await client.get("/health")
            data = resp.json()
    except Exception as exc:
        _add_transcript(state, _info_line(f"doctor failed: {exc}", "red"))
        return

    summary = data.get("summary") or {}
    _add_transcript(
        state,
        _info_line(
            "doctor: "
            f"status={data.get('status', 'unknown')} "
            f"healthy={summary.get('healthy', 0)} "
            f"degraded={summary.get('degraded', 0)} "
            f"unhealthy={summary.get('unhealthy', 0)}",
            "green" if data.get("status") == "healthy" else "yellow",
        ),
    )
    checks = data.get("checks") or {}
    if not isinstance(checks, dict) or not checks:
        return
    table = Table(title="health checks", header_style="bold cyan")
    table.add_column("name", style="cyan")
    table.add_column("status")
    table.add_column("detail")
    for name, check in checks.items():
        if not isinstance(check, dict):
            continue
        detail = check.get("error") or ", ".join(
            f"{key}={value}" for key, value in list((check.get("details") or {}).items())[:2]
        )
        status_text = str(check.get("status") or "unknown")
        table.add_row(name, status_text, detail)
    _add_transcript(state, table)


async def _cmd_memory(state: ReplState, rest: str) -> None:
    args = shlex.split(rest) if rest.strip() else []
    session_id = rest.strip()
    known_subcommands = {"drafts", "approve", "reject", "evals", "eval", "export-eval"}
    try:
        async with httpx.AsyncClient(base_url=state.api_url, timeout=10.0) as client:
            response_is_json = True
            if args[:1] == ["drafts"]:
                resp = await client.get("/api/memory/drafts", params={"limit": 5})
            elif args[:1] == ["evals"]:
                status = args[1] if len(args) >= 2 else "draft"
                resp = await client.get("/api/memory/evaluations", params={"status": status, "limit": 5})
            elif len(args) >= 2 and args[0] == "eval":
                resp = await client.get(f"/api/memory/evaluations/{args[1]}")
            elif len(args) >= 2 and args[0] == "export-eval":
                export_format = args[2] if len(args) >= 3 else "json"
                resp = await client.get(
                    f"/api/memory/evaluations/{args[1]}/export",
                    params={"format": export_format},
                )
                response_is_json = export_format != "jsonl"
            elif len(args) >= 2 and args[0] == "approve":
                resp = await client.post(f"/api/memory/drafts/{args[1]}/approve")
            elif len(args) >= 2 and args[0] == "reject":
                reason = " ".join(args[2:])
                resp = await client.post(
                    f"/api/memory/drafts/{args[1]}/reject",
                    json={"reason": reason},
                )
            elif args[:1] and args[0] in known_subcommands:
                _add_transcript(state, _info_line(f"unknown /memory usage: {rest}", "yellow"))
                return
            elif session_id:
                resp = await client.get(f"/api/memory/sessions/{session_id}")
            else:
                resp = await client.get("/api/memory/layers", params={"limit": 5})
            if response_is_json:
                data = resp.json()
            else:
                try:
                    data = resp.json()
                except Exception:
                    data = {}
    except Exception as exc:
        _add_transcript(state, _info_line(f"memory failed: {exc}", "red"))
        return

    if args[:1] == ["drafts"]:
        drafts = data.get("drafts") or []
        if not drafts:
            _add_transcript(state, _info_line("no pending memory drafts", "yellow"))
            return
        table = Table(title="memory drafts", header_style="bold cyan")
        table.add_column("id", style="cyan")
        table.add_column("type")
        table.add_column("layer")
        table.add_column("title")
        for draft in drafts:
            meta = draft.get("meta") or {}
            table.add_row(
                str(draft.get("id") or "—"),
                str(draft.get("doc_type") or "—"),
                str(meta.get("memory_layer") or "—"),
                str(draft.get("title") or "—")[:80],
            )
        _add_transcript(state, table)
        return

    if args[:1] == ["evals"]:
        evaluations = data.get("evaluations") or []
        if not evaluations:
            _add_transcript(state, _info_line("no evaluation assets", "yellow"))
            return
        table = Table(title="evaluation assets", header_style="bold cyan")
        table.add_column("id", style="cyan")
        table.add_column("status")
        table.add_column("lane")
        table.add_column("lang")
        table.add_column("signals")
        for item in evaluations:
            table.add_row(
                str(item.get("id") or "—"),
                str(item.get("review_status") or "—"),
                str(item.get("lane") or "—"),
                str(item.get("language") or "—"),
                ", ".join(item.get("signals") or []) or "—",
            )
        _add_transcript(state, table)
        return

    if len(args) >= 2 and args[0] == "eval":
        if data.get("error"):
            _add_transcript(state, _info_line(str(data["error"]), "yellow"))
            return
        evaluation = data.get("evaluation") or {}
        info = Table(title="evaluation asset", header_style="bold cyan")
        info.add_column("field", style="cyan")
        info.add_column("value")
        info.add_row("id", str(evaluation.get("id") or args[1]))
        info.add_row("status", str(evaluation.get("review_status") or "—"))
        info.add_row("lane", str(evaluation.get("lane") or "—"))
        info.add_row("language", str(evaluation.get("language") or "—"))
        info.add_row("signals", ", ".join(evaluation.get("signals") or []) or "—")
        _add_transcript(state, info)
        checks = evaluation.get("checks") or []
        if checks:
            checks_table = Table(title="evaluation checks", header_style="bold magenta")
            checks_table.add_column("check")
            for check in checks:
                checks_table.add_row(str(check))
            _add_transcript(state, checks_table)
        return

    if len(args) >= 2 and args[0] == "export-eval":
        if data.get("error"):
            _add_transcript(state, _info_line(str(data["error"]), "yellow"))
            return
        export_format = args[2] if len(args) >= 3 else "json"
        if export_format == "jsonl":
            text = getattr(resp, "text", "").rstrip()
            _add_transcript(state, Panel(text, title="evaluation export"))
        else:
            _add_transcript(
                state,
                Panel(json.dumps(data, indent=2), title="evaluation export"),
            )
        return

    if len(args) >= 2 and args[0] in {"approve", "reject"}:
        if data.get("error"):
            _add_transcript(state, _info_line(str(data["error"]), "yellow"))
            return
        action = "approved" if args[0] == "approve" else "rejected"
        draft = data.get("draft") or {}
        _add_transcript(
            state,
            _info_line(
                f"{action}: {draft.get('id', args[1])} — {draft.get('title', 'memory draft')}",
                "green" if action == "approved" else "yellow",
            ),
        )
        return

    if session_id:
        if data.get("error"):
            _add_transcript(state, _info_line(str(data["error"]), "yellow"))
            return
        context = data.get("context") or {}
        info = Table(title="session memory", header_style="bold cyan")
        info.add_column("field", style="cyan")
        info.add_column("value")
        info.add_row("session_id", str(data.get("session_id") or session_id))
        info.add_row("participants", ", ".join(context.get("participants") or []) or "—")
        info.add_row("history", str(len(context.get("history") or [])))
        _add_transcript(state, info)
        activity = data.get("recent_activity") or []
        if activity:
            table = Table(title="recent activity", header_style="bold magenta")
            table.add_column("type", style="cyan")
            table.add_column("summary")
            for item in activity:
                summary = item.get("title") or item.get("content") or item.get("description") or "—"
                table.add_row(str(item.get("type") or "—"), str(summary)[:90])
            _add_transcript(state, table)
        return

    layers = data.get("layers") or {}
    session = layers.get("session") or {}
    operator = layers.get("operator") or {}
    project = layers.get("project") or {}
    table = Table(title="memory layers", header_style="bold cyan")
    table.add_column("layer", style="cyan")
    table.add_column("summary")
    table.add_row("session", f"{session.get('active_sessions', 0)} active sessions")
    table.add_row(
        "operator",
        f"{operator.get('insight_count', 0)} insights, {operator.get('skill_summary', {}).get('total', 0)} skills",
    )
    table.add_row(
        "project",
        f"{project.get('tasks', 0)} tasks, {project.get('knowledge_documents', 0)} docs, success={project.get('success_rate', 0.0)}",
    )
    _add_transcript(state, table)


def _queue_prompt_submission(state: ReplState, prompt: str) -> None:
    _add_transcript(state, _user_line(prompt))
    asyncio.create_task(_submit_prompt(state, prompt))


async def _cmd_resume(state: ReplState) -> None:
    if state.busy:
        _add_transcript(state, _info_line("already busy — wait for the current task first", "yellow"))
        return
    prompt = (state.last_interrupted_prompt or "").strip()
    if not prompt:
        _add_transcript(state, _info_line("no interrupted prompt to resume", "yellow"))
        return
    _queue_prompt_submission(state, prompt)


async def _cmd_retry(state: ReplState) -> None:
    if state.busy:
        _add_transcript(state, _info_line("already busy — wait for the current task first", "yellow"))
        return
    prompt = (state.last_failed_prompt or "").strip()
    if not prompt:
        _add_transcript(state, _info_line("no failed prompt to retry", "yellow"))
        return
    _queue_prompt_submission(state, prompt)


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
                known_models: List[str] = []
                for model in models:
                    if not isinstance(model, dict):
                        continue
                    model_id = model.get("id")
                    if isinstance(model_id, str):
                        known_models.append(model_id)
                state.known_models = known_models
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
                known_agents: List[str] = []
                for agent in agents:
                    if not isinstance(agent, dict):
                        continue
                    name = agent.get("name")
                    if isinstance(name, str):
                        known_agents.append(name)
                state.known_agents = known_agents
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
            raw_config = a.get("config")
            cfg = raw_config if isinstance(raw_config, dict) else {}
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
    "/agents", "/tasks", "/agent", "/pipeline", "/project", "/rag", "/ingest",
    "/doctor", "/memory", "/resume", "/retry",
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
                    state.known_agents = [str(name) for name in ag.keys()]
                else:
                    known_agents: List[str] = []
                    for agent in ag:
                        if not isinstance(agent, dict):
                            continue
                        name = agent.get("name")
                        if isinstance(name, str):
                            known_agents.append(name)
                    state.known_agents = known_agents
    except Exception:
        pass
    # models for current backend
    try:
        be = state.active_backend or "auto"
        async with httpx.AsyncClient(base_url=state.api_url, timeout=5.0) as client:
            r = await client.get(f"/api/llm/models?backend={be}")
            if r.status_code == 200:
                payload = r.json() or {}
                known_models: List[str] = []
                for model in payload.get("models") or []:
                    if not isinstance(model, dict):
                        continue
                    model_id = model.get("id")
                    if isinstance(model_id, str):
                        known_models.append(model_id)
                state.known_models = known_models
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Input loop (prompt_toolkit if available, else basic input())
# ---------------------------------------------------------------------------


def _read_one(
    session: Any,
    prompt_label: str = "> ",
    multiline_label: str = "... ",
) -> Optional[str]:
    """Read one logical input (single line or multi-line block)."""
    try:
        first = session.prompt(prompt_label, multiline=False)
    except (EOFError, KeyboardInterrupt):
        raise
    if first.strip() == '"""':
        # Multi-line block
        lines: List[str] = []
        while True:
            try:
                ln = session.prompt(multiline_label, multiline=False)
            except (EOFError, KeyboardInterrupt):
                raise
            if ln.strip() == '"""':
                break
            lines.append(ln)
        return "\n".join(lines)
    return str(first)


def _read_one_basic(
    prompt_label: str = "> ",
    multiline_label: str = "... ",
) -> Optional[str]:
    first = input(prompt_label)
    if first.strip() == '"""':
        lines: List[str] = []
        while True:
            ln = input(multiline_label)
            if ln.strip() == '"""':
                break
            lines.append(ln)
        return "\n".join(lines)
    return str(first)


def _wait_for_repl_future(
    future: concurrent.futures.Future[Any],
    *,
    state: ReplState,
    paint: Any,
    timeout: float,
    tick: float = 0.1,
) -> Any:
    """Wait for a background REPL task while keeping the screen responsive."""
    deadline = time.monotonic() + timeout
    last_render_version = state.render_version
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise concurrent.futures.TimeoutError()
        try:
            return future.result(timeout=min(tick, remaining))
        except concurrent.futures.TimeoutError:
            if state.render_version != last_render_version:
                paint()
                last_render_version = state.render_version


def _run_plain_prompt(
    state: ReplState,
    line: str,
    *,
    paint: Any,
    prompt_reader: Any,
    loop: asyncio.AbstractEventLoop,
) -> None:
    if state.busy:
        _add_transcript(
            state,
            _info_line("still working on the previous prompt — wait or press Ctrl-C", "yellow"),
        )
        paint()
        return

    _add_transcript(state, _user_line(line))

    if _should_route_prompt_to_project(state, line):
        _add_transcript(state, _info_line("routing this into a project build…", "cyan"))
        paint()
        _run_project_command(state, line, paint=paint, prompt_reader=prompt_reader)
        return

    prompt_future = asyncio.run_coroutine_threadsafe(_submit_prompt(state, line), loop)
    paint()
    try:
        _wait_for_repl_future(
            prompt_future,
            state=state,
            paint=paint,
            timeout=600,
        )
    except KeyboardInterrupt:
        cancel_future = asyncio.run_coroutine_threadsafe(_cancel_current(state), loop)
        cancel_future.result(timeout=5)
        _add_transcript(state, _info_line("cancelled", "yellow"))
    except concurrent.futures.TimeoutError:
        _add_transcript(state, _info_line("timed out waiting for reply", "red"))
    except Exception as exc:
        _add_transcript(state, _info_line(f"error: {exc}", "red"))


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def run() -> None:
    """Entry point for the interactive REPL."""
    console = Console()
    state = ReplState()

    # Probe API
    try:
        with httpx.Client(base_url=state.api_url, timeout=2.0) as client:
            client.get("/api/status").raise_for_status()
    except Exception:
        console.print(
            f"[red]skyn3t API not reachable at {state.api_url}[/red] — start the server "
            "with [bold]skyn3t start[/bold] (or `python -m skyn3t.web`)."
        )
        return

    if not _HAS_PT:
        console.print(
            "[yellow]warning:[/yellow] prompt_toolkit not installed — "
            "using basic input(). For full features run "
            "[bold]pip install prompt_toolkit[/bold]."
        )

    _add_transcript(
        state,
        _info_line(
            f"Connected to {state.api_url}. Type /help for commands. "
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

    pt_session: Any = None
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

    def _prompt_reader(index: int, question: str) -> str:
        del question
        return (
            _read_one(
                pt_session,
                prompt_label=f"clarify {index}> ",
                multiline_label="clarify... ",
            )
            if pt_session is not None
            else _read_one_basic(
                prompt_label=f"clarify {index}> ",
                multiline_label="clarify... ",
            )
        ) or ""

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
                if line == "/project" or line.startswith("/project "):
                    rest = line[len("/project"):].strip()
                    try:
                        _run_project_command(
                            state,
                            rest,
                            paint=_paint_snapshot,
                            prompt_reader=_prompt_reader,
                        )
                    except KeyboardInterrupt:
                        _add_transcript(
                            state,
                            _info_line(
                                "detached from project watch — build keeps running in the background",
                                "yellow",
                            ),
                        )
                    _paint_snapshot()
                    continue
                command_future = asyncio.run_coroutine_threadsafe(_slash(state, line), loop)
                try:
                    should_exit = command_future.result(timeout=120)
                except Exception as exc:
                    _add_transcript(state, _info_line(f"command error: {exc}", "red"))
                    should_exit = False
                if should_exit:
                    break
            else:
                _run_plain_prompt(
                    state,
                    line,
                    paint=_paint_snapshot,
                    prompt_reader=_prompt_reader,
                    loop=loop,
                )

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
