"""SkyN3t Interactive REPL — Claude-Code-style swarm console.

Run with `skyn3t` (no args) or `skyn3t repl` to drop into an interactive
session with a live transcript on the left and a swarm-activity sidebar on
the right.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
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

from skyn3t.cli.main import (
    _STUDIO_START_TIMEOUT,
    _studio_progress_snapshot,
)
from skyn3t.cli.studio_approval import (
    approval_gate_key,
    approval_renderables,
    approval_summary,
    edit_markdown_in_editor,
    fetch_approval_document,
    resolve_approval_choice,
    run_interactive_approval,
    submit_reject,
)
from skyn3t.config.settings import resolve_api_base
from skyn3t.studio.repo_target import normalize_repo_target, resolve_repo_target

API_BASE = resolve_api_base()


def _ws_url() -> str:
    return API_BASE.replace("http://", "ws://").replace("https://", "wss://")

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

_STUDIO_BUILD_NOISE_MARKERS = (
    "building file",
    "deterministic manifest",
    " wrote ",
    "solo + ",
    "cluster(s)",
    "file(s):",
)

_STUDIO_SNIPPET_LIMIT = 240

# Approval-with-edits opens an interactive $EDITOR; an edit session can take
# arbitrarily long, so the dispatcher waits effectively unbounded (1 day)
# instead of the regular 120s RPC timeout that would orphan the editor.
_APPROVAL_EDITOR_TIMEOUT = 86400.0

_STUDIO_SLUG_RE = re.compile(
    r"\b([a-z0-9][a-z0-9-]{8,}-[a-f0-9]{6,8})\b",
    re.IGNORECASE,
)

_STATUS_QUERY_CUES = (
    "status",
    "progress",
    "going",
    "update",
    "how is",
    "where is",
    "what is the status",
    "what's the status",
    "how's the build",
    "how is the build",
    "what stage",
    "current stage",
    "state of",
)

_ACTIVE_STUDIO_STATUSES = frozenset(
    {"queued", "running", "awaiting_clarification", "awaiting_approval"}
)


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
    api_url: str = field(default_factory=resolve_api_base)
    active_agent: Optional[str] = None     # name of agent prompts route to
    active_backend: Optional[str] = None   # session-level override (None = use agent default)
    active_model: Optional[str] = None
    # Tab-completion caches
    known_backends: List[str] = field(default_factory=list)
    known_models: List[str] = field(default_factory=list)
    known_agents: List[str] = field(default_factory=list)
    render_version: int = 0
    studio_slug: Optional[str] = None
    studio_watching: bool = False
    studio_history_seen: int = 0
    studio_last_snapshot: Optional[tuple[str, str, str, str]] = None
    last_studio_activity_key: Optional[str] = None
    live_activity_key: Optional[str] = None
    live_activity_line: Optional[Text] = None
    studio_approval_gate_seen: Optional[tuple[Any, ...]] = None
    studio_clarification_gate_seen: Optional[tuple[str, ...]] = None
    studio_clarification_questions: Optional[tuple[str, ...]] = None
    studio_clarification_answers: List[str] = field(default_factory=list)
    paint_callback: Any = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _glyph(event_type: str) -> str:
    return EVENT_GLYPHS.get(event_type, "•")


def _terminal_width() -> int:
    return shutil.get_terminal_size((120, 40)).columns


def _terminal_height() -> int:
    return shutil.get_terminal_size((120, 40)).lines


def _prompt_reserve_lines() -> int:
    """Keep the `>` input band visible below the painted layout."""
    return 3


def _layout_frame_height() -> int:
    """Total rows for header + body panels (excludes the input prompt)."""
    return max(12, _terminal_height() - _prompt_reserve_lines())


def _render_height(state: ReplState) -> int:
    """Paint height — use the full band while a studio build is active."""
    frame_h = _layout_frame_height()
    if state.studio_slug:
        return frame_h
    transcript_items = min(_transcript_entry_budget(), len(state.transcript))
    transcript_rows = max(5, transcript_items * 4 + 3)
    activity_rows = max(5, min(_activity_line_budget(state), len(state.activity)) + 3)
    act_tail = _activity_lines_for_display(state)
    if _activity_sidebar_enabled(state, act_tail):
        body_rows = max(transcript_rows, activity_rows)
    elif act_tail:
        body_rows = transcript_rows + activity_rows
    else:
        body_rows = transcript_rows
    return min(frame_h, 4 + body_rows + 1)


def _activity_line_budget(state: ReplState) -> int:
    """Activity rows to show — scales with layout body height."""
    body_rows = _layout_frame_height() - 4  # header
    floor = 8 if state.studio_slug else 6
    cap = 24 if state.studio_slug else 18
    return max(floor, min(cap, body_rows - 3))


def _transcript_entry_budget() -> int:
    """Recent chat entries that fit above the input prompt."""
    body_rows = _layout_frame_height() - 4
    return max(4, min(10, body_rows // 5))


def _body_column_ratios(state: ReplState) -> tuple[int, int]:
    """Transcript (left) vs activity sidebar (right) width ratios."""
    width = _terminal_width()
    if state.studio_slug:
        if width >= 160:
            return (3, 2)
        if width >= 120:
            return (2, 1)
    if width >= 180:
        return (7, 3)
    if width >= 140:
        return (5, 2)
    if width >= 100:
        return (4, 1)
    return (2, 1)


def _activity_sidebar_enabled(state: ReplState, items: List[Text]) -> bool:
    if _terminal_width() < 100:
        return False
    return bool(items) or bool(state.studio_slug)


def _activity_lines_for_display(state: ReplState) -> List[Text]:
    """Timeline + optional rolling live-progress line for the right panel."""
    budget = _activity_line_budget(state)
    reserve = 1 if state.live_activity_line is not None else 0
    lines = list(state.activity)[-(budget - reserve) :]
    if not lines and state.studio_slug:
        lines = [Text("Syncing studio timeline…", style="dim")]
    if state.live_activity_line is not None:
        lines.append(state.live_activity_line)
    return lines


def _activity_panel_title(state: ReplState) -> str:
    if state.studio_slug:
        return f"Studio build · {state.studio_slug}"
    return "Agents at work"


def _format_activity_ts(ts: Any) -> str:
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return ""
    raw = str(ts).strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.strftime("%H:%M:%S")
    except ValueError:
        return raw[:8]


def _truncate_studio_text(text: str, limit: int = _STUDIO_SNIPPET_LIMIT) -> str:
    cleaned = str(text or "").replace("\n", " ").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _studio_history_label(event_name: str) -> str:
    labels = {
        "PROJECT_QUEUED": "Queued",
        "PROJECT_STARTED": "Started",
        "BRIEF_EXPANDED": "Brief expanded",
        "PROJECT_BRIEF_EXPANDED": "Brief expanded",
        "PROJECT_STAGE_STARTED": "Stage started",
        "PROJECT_STAGE_COMPLETED": "Stage completed",
        "PROJECT_STAGE_FAILED": "Stage failed",
        "PROJECT_AWAITING_CLARIFICATION": "Waiting for clarification",
        "PROJECT_AWAITING_APPROVAL": "Awaiting approval",
        "PROJECT_RESUMED": "Resumed",
        "PROJECT_RESUMED_AFTER_APPROVAL": "Resumed after approval",
        "PROJECT_COMPLETED": "Project finished",
        "PROJECT_FAILED": "Runner failed",
        "PROJECT_REAPED": "Recovered",
        "CRITIQUE_ISSUES_FOUND": "Critique issues",
        "STUDIO_STATUS": "Status",
    }
    key = str(event_name or "").upper()
    return labels.get(key, key.replace("_", " ").title())


def _studio_event_name_from_swarm(evt: dict) -> str:
    et = str(evt.get("event_type") or evt.get("type") or "").upper()
    kind = str(evt.get("kind") or "").lower()
    payload: dict = {}
    meta = evt.get("meta")
    if isinstance(meta, dict):
        nested = meta.get("payload")
        if isinstance(nested, dict):
            payload = nested
    if kind == "project":
        payload_kind = str(payload.get("kind") or "").upper()
        if payload_kind:
            return payload_kind
    if et == "SYSTEM_ALERT":
        payload_kind = str(payload.get("kind") or "").upper()
        if payload_kind:
            return payload_kind
    return et


def _format_studio_timeline_line(
    *,
    event_name: str,
    message: str = "",
    status: str = "",
    stage: str = "",
    agent: str = "",
    ts: Any = None,
) -> Text:
    """Rich single-line studio timeline entry — mirrors the web activity feed."""
    event_upper = str(event_name or "").upper()
    label = _studio_history_label(event_upper)
    detail_parts: List[str] = []
    if stage:
        detail_parts.append(stage)
    if agent:
        detail_parts.append(agent)
    if message:
        detail_parts.append(_truncate_studio_text(message))
    detail = " · ".join(detail_parts)

    style = "cyan"
    if event_upper.endswith("_FAILED") or event_upper == "PROJECT_FAILED":
        style = "red"
    elif event_upper.endswith("_COMPLETED") or event_upper == "PROJECT_COMPLETED":
        style = "green"
    elif "AWAITING" in event_upper or event_upper == "CRITIQUE_ISSUES_FOUND":
        style = "yellow"

    line = Text()
    stamp = _format_activity_ts(ts)
    if stamp:
        line.append(stamp, style="dim")
        line.append("  ")
    line.append(event_upper or "EVENT", style=f"bold {style}")
    if status:
        line.append(f"  {status}", style=style)
    line.append(f"  {label}", style=f"bold {style}")
    if detail:
        line.append(f"  {detail}", style="white")
    return line


def _format_studio_history_entry(item: dict) -> Text:
    return _format_studio_timeline_line(
        event_name=str(item.get("event") or ""),
        message=str(item.get("message") or ""),
        status=str(item.get("status") or ""),
        stage=str(item.get("stage") or ""),
        agent=str(item.get("agent") or ""),
        ts=item.get("ts"),
    )


def _format_studio_swarm_event(evt: dict) -> Text:
    event_name = _studio_event_name_from_swarm(evt)
    payload: dict = {}
    meta = evt.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("payload"), dict):
        payload = meta["payload"]
    message = str(
        evt.get("label")
        or payload.get("message")
        or payload.get("summary")
        or payload.get("preview")
        or ""
    ).strip()
    if event_name in {"BRIEF_EXPANDED", "PROJECT_BRIEF_EXPANDED"}:
        preview = str(payload.get("preview") or message).strip()
        defaults = payload.get("category_defaults")
        if isinstance(defaults, list) and defaults:
            assumed = ", ".join(str(item) for item in defaults[:6])
            message = f"{preview} · Assumed: {assumed}" if preview else f"Assumed: {assumed}"
        else:
            message = preview or message
    return _format_studio_timeline_line(
        event_name=event_name,
        message=message,
        status=str(payload.get("status") or "").strip(),
        stage=str(payload.get("stage") or payload.get("current_stage") or "").strip(),
        agent=str(payload.get("agent") or payload.get("current_agent") or evt.get("from") or "").strip(),
        ts=evt.get("ts"),
    )


def _studio_activity_key(*, event_name: str, status: str = "", stage: str = "", message: str = "") -> str:
    return "|".join(
        [
            str(event_name or "").upper(),
            str(status or "").strip(),
            str(stage or "").strip(),
            _truncate_studio_text(message, 120),
        ]
    )


def _is_studio_build_noise(evt: dict, state: ReplState) -> bool:
    kind = str(evt.get("kind") or "").lower()
    et = str(evt.get("event_type") or evt.get("type") or "").upper()
    if kind in {"project", "task", "stage", "message"}:
        return False
    if kind == "project" or _studio_event_name_from_swarm(evt).startswith(
        ("PROJECT_", "BRIEF_", "CRITIQUE_")
    ):
        return False
    if kind in {"convo"} or et in _LOW_SIGNAL_ACTIVITY_TYPES:
        return True

    snippet = _event_snippet(evt).lower()
    if "entrypoint fast-path" in snippet:
        return True
    if "fast-path success" in snippet:
        return True
    if "deterministic manifest" in snippet and " wrote " in snippet:
        return True
    if _compress_swarm_to_live_line(evt) is not None:
        return True
    return False


def _compress_swarm_to_live_line(evt: dict) -> Optional[tuple[str, Text]]:
    """Collapse high-frequency build chatter into one rolling sidebar line."""
    source = str(evt.get("from") or evt.get("source") or "agent").strip()
    snippet = _event_snippet(evt)
    if not snippet:
        return None

    match = re.search(r"building file (\d+)/(\d+):\s*(.+)", snippet, re.IGNORECASE)
    if match:
        idx, total, path = match.groups()
        line = Text("⚡ ", style="yellow")
        line.append(source, style="bold yellow")
        line.append(f"  file {idx}/{total}", style="yellow")
        line.append(f"  {_truncate_studio_text(path, 72)}", style="white")
        return (f"{source}:file-progress", line)

    lower = snippet.lower()
    if "building" in lower and "file" in lower:
        line = Text("⚡ ", style="yellow")
        line.append(source, style="bold yellow")
        line.append(f"  {_truncate_studio_text(snippet, 96)}", style="white")
        return (f"{source}:build", line)

    if "solo +" in lower or "cluster(s)" in lower or "file(s):" in lower:
        line = Text("⚡ ", style="cyan")
        line.append(source, style="bold cyan")
        line.append(f"  {_truncate_studio_text(snippet, 96)}", style="white")
        return (f"{source}:batch", line)

    kind = str(evt.get("kind") or "").lower()
    et = str(evt.get("event_type") or evt.get("type") or "").upper()
    if kind == "thought" or et.startswith("AGENT_THOUGHT"):
        if any(marker in lower for marker in _STUDIO_BUILD_NOISE_MARKERS):
            return None
        line = Text("✦ ", style="magenta")
        line.append(source, style="bold magenta")
        line.append(f"  {_truncate_studio_text(snippet, 96)}", style="dim")
        return (f"{source}:thought", line)

    return None


def _maybe_update_live_activity(state: ReplState, evt: dict) -> bool:
    compressed = _compress_swarm_to_live_line(evt)
    if compressed is None:
        return False
    key, line = compressed
    if state.live_activity_key == key and state.live_activity_line is not None:
        if state.live_activity_line.plain == line.plain:
            return True
    state.live_activity_key = key
    state.live_activity_line = line
    _touch_state(state)
    return True


def _pick_active_studio_project(projects: List[dict]) -> Optional[dict]:
    active: List[dict] = []
    for project in projects:
        if not isinstance(project, dict):
            continue
        status = str(project.get("status") or "").strip().lower()
        slug = str(project.get("slug") or "").strip()
        if not slug or status not in _ACTIVE_STUDIO_STATUSES:
            continue
        active.append(project)
    if not active:
        return None
    return max(active, key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0))


def _apply_studio_project_activity(state: ReplState, project: dict) -> bool:
    """Merge manifest history + status into the activity sidebar."""
    updated = False
    snapshot = _studio_progress_snapshot(project)
    if snapshot != state.studio_last_snapshot:
        status, stage, agent, next_action = snapshot
        _add_studio_activity(
            state,
            _format_studio_timeline_line(
                event_name="STUDIO_STATUS",
                status=status,
                stage=stage,
                agent=agent,
                message=next_action,
            ),
            event_name="STUDIO_STATUS",
            status=status,
            stage=stage,
            message=next_action,
        )
        state.studio_last_snapshot = snapshot
        updated = True

    history = project.get("history") or []
    if not isinstance(history, list):
        history = []
    for item in history[state.studio_history_seen :]:
        if not isinstance(item, dict):
            continue
        _add_studio_activity(
            state,
            _format_studio_history_entry(item),
            event_name=str(item.get("event") or ""),
            status=str(item.get("status") or ""),
            stage=str(item.get("stage") or ""),
            message=str(item.get("message") or ""),
        )
        updated = True
    state.studio_history_seen = len(history)
    return updated


def _request_repaint(state: ReplState) -> None:
    callback = state.paint_callback
    if callable(callback):
        try:
            callback()
        except Exception:
            pass


def _event_snippet(evt: dict) -> str:
    data = evt.get("data") or {}
    if isinstance(data, dict) and "data" in data and "event_type" in data:
        evt = data
        data = evt.get("data") or {}
    snippet = str(evt.get("label") or "").strip()
    if isinstance(data, dict):
        if not snippet:
            for key in ("message", "thought", "title", "query", "summary", "stage", "repo", "name", "line"):
                value = data.get(key)
                if value:
                    snippet = str(value)
                    break
        if not snippet:
            snippet = ", ".join(f"{k}={v}" for k, v in list(data.items())[:2])
    elif data:
        snippet = str(data)
    return snippet.replace("\n", " ").strip()


def _format_event(evt: dict, state: Optional[ReplState] = None) -> Optional[Text]:
    if state is not None:
        if _maybe_update_live_activity(state, evt):
            return None
        if _is_studio_build_noise(evt, state):
            return None

    et = str(evt.get("event_type") or evt.get("type") or "").upper()
    kind = str(evt.get("kind") or "").lower()
    if et in _LOW_SIGNAL_ACTIVITY_TYPES or kind == "convo":
        return None

    studio_event = _studio_event_name_from_swarm(evt)
    if kind == "project" or studio_event.startswith(("PROJECT_", "BRIEF_", "CRITIQUE_")):
        return _format_studio_swarm_event(evt)

    data = evt.get("data") or {}
    if isinstance(data, dict) and "data" in data and "event_type" in data:
        evt = data
        et = str(evt.get("event_type", et) or "").upper()
        data = evt.get("data") or {}
        if et in _LOW_SIGNAL_ACTIVITY_TYPES:
            return None
        if et.startswith("PROJECT_") or et.startswith("CRITIQUE_"):
            return _format_studio_swarm_event(evt)

    snippet = _event_snippet(evt)
    if len(snippet) > 96:
        snippet = snippet[:93] + "..."

    glyph = _glyph(et)
    source = (
        evt.get("from")
        or evt.get("source")
        or (data.get("agent") if isinstance(data, dict) else None)
        or ""
    )

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
        return _format_studio_swarm_event(evt)
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
    frame_h = _render_height(state)
    act_tail = _activity_lines_for_display(state)
    body_rows = max(6, frame_h - 4)
    if _activity_sidebar_enabled(state, act_tail):
        transcript_ratio, activity_ratio = _body_column_ratios(state)
        layout.split_column(
            Layout(name="header", size=4),
            Layout(name="body", size=body_rows),
        )
        layout["body"].split_row(
            Layout(name="transcript", ratio=transcript_ratio),
            Layout(name="activity", ratio=activity_ratio),
        )
    else:
        sections: List[Layout] = [
            Layout(name="header", size=4),
            Layout(name="transcript", size=max(5, body_rows)),
        ]
        if act_tail:
            sections.append(
                Layout(name="activity", size=max(6, min(14, len(act_tail) + 2)))
            )
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
    hint = Text(
        'Type naturally — chat or "build a …"  •  """ for multi-line  •  /help for shortcuts',
        style="dim",
    )
    layout["header"].update(Panel(Group(title, hint), border_style="cyan", padding=(0, 1)))

    tail = state.transcript[-_transcript_entry_budget():]
    transcript = Group(*tail) if tail else Text("(empty — type a prompt below)", style="dim")
    layout["transcript"].update(
        Panel(transcript, title="Chat", border_style="white", padding=(0, 1))
    )

    if act_tail:
        activity = Group(*act_tail)
        layout["activity"].update(
            Panel(
                activity,
                title=_activity_panel_title(state),
                border_style="magenta",
                padding=(0, 1),
            )
        )

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


def _studio_clarification_questions(project: Dict[str, Any]) -> tuple[str, ...]:
    clarification = project.get("clarification") or {}
    return tuple(
        str(question).strip()
        for question in (clarification.get("questions") or [])
        if str(question).strip()
    )


def _emit_studio_clarification_prompt(
    state: ReplState,
    project: Dict[str, Any],
) -> bool:
    """Show planner questions in chat when a build is paused for clarification."""
    status = str(project.get("status") or "").strip().lower()
    if status != "awaiting_clarification":
        state.studio_clarification_gate_seen = None
        state.studio_clarification_questions = None
        state.studio_clarification_answers = []
        return False
    questions = _studio_clarification_questions(project)
    if not questions or questions == state.studio_clarification_gate_seen:
        return False
    slug = str(project.get("slug") or state.studio_slug or "").strip()
    if slug:
        state.studio_slug = slug
    state.studio_clarification_gate_seen = questions
    state.studio_clarification_questions = questions
    state.studio_clarification_answers = []
    _add_transcript(
        state,
        Panel(
            "\n".join(f"{index}. {question}" for index, question in enumerate(questions, start=1))
            + "\n\nReply in chat — one message per question, in order.",
            title="Clarification needed",
            border_style="yellow",
        ),
    )
    _add_studio_activity(
        state,
        Text("? clarification needed — answer in chat", style="bold yellow"),
        event_name="STUDIO_CLARIFY",
        status="awaiting_clarification",
        message=questions[0][:80],
    )
    return True


def _handle_studio_clarification_reply(state: ReplState, line: str) -> bool:
    """Collect clarification answers in the main REPL (web-started builds)."""
    questions = state.studio_clarification_questions
    if not questions:
        return False
    answer = str(line or "").strip()
    if not answer:
        _add_transcript(state, _info_line("please enter an answer", "yellow"))
        return True
    state.studio_clarification_answers.append(answer)
    index = len(state.studio_clarification_answers)
    if index < len(questions):
        _add_transcript(
            state,
            _info_line(f"got it — next: {questions[index]}", "cyan"),
        )
        return True
    slug = state.studio_slug
    if not slug:
        _add_transcript(state, _info_line("no active studio project", "red"))
        return True
    try:
        with httpx.Client(base_url=state.api_url, timeout=30.0) as client:
            resp = client.post(
                f"/api/studio/projects/{slug}/clarify",
                json={"answers": list(state.studio_clarification_answers)},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(str(data.get("error") or data))
    except Exception as exc:
        _add_transcript(state, _info_line(f"clarification failed: {exc}", "red"))
        return True
    state.studio_clarification_questions = None
    state.studio_clarification_answers = []
    state.studio_clarification_gate_seen = None
    _add_transcript(state, _info_line("clarifications sent — build resuming…", "green"))
    return True


def _emit_studio_approval_prompt(
    state: ReplState,
    project: Dict[str, Any],
    *,
    client: httpx.Client,
) -> bool:
    """Surface architecture approval in the REPL transcript (once per gate)."""
    status = str(project.get("status") or "").strip().lower()
    if status != "awaiting_approval":
        state.studio_approval_gate_seen = None
        return False
    gate_key = approval_gate_key(project)
    if gate_key == state.studio_approval_gate_seen:
        return False
    state.studio_approval_gate_seen = gate_key
    slug = str(project.get("slug") or state.studio_slug or "").strip()
    if slug:
        state.studio_slug = slug
    summary = approval_summary(project)
    try:
        original = fetch_approval_document(client, slug) if slug else ""
    except Exception as exc:
        original = f"(could not load architecture.md: {exc})"
    for renderable in approval_renderables(project, original):
        _add_transcript(state, renderable)
    _add_studio_activity(
        state,
        Text(f"⏸ approval required · {summary}", style="bold yellow"),
        event_name="STUDIO_APPROVAL",
        status="awaiting_approval",
        stage=summary,
        message="approve / approve with edits / reject …",
    )
    return True


def _add_studio_activity(
    state: ReplState,
    renderable: Text,
    *,
    event_name: str,
    status: str = "",
    stage: str = "",
    message: str = "",
) -> None:
    key = _studio_activity_key(
        event_name=event_name,
        status=status,
        stage=stage,
        message=message,
    )
    if state.last_studio_activity_key == key:
        return
    state.last_studio_activity_key = key
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


def _is_studio_swarm_event(evt: dict) -> bool:
    name = _studio_event_name_from_swarm(evt)
    return name.startswith(("PROJECT_", "BRIEF_", "CRITIQUE_"))


# ---------------------------------------------------------------------------
# Background: WebSocket + polling fallback
# ---------------------------------------------------------------------------


async def _ws_listener(state: ReplState) -> None:
    """Listen for swarm events; reconnect every 3s on disconnect."""
    import websockets as wsmod
    from websockets.exceptions import ConnectionClosed

    candidate_paths = ["/ws/swarm", "/ws"]
    while not state.stop:
        connected = False
        for path in candidate_paths:
            url = _ws_url().rstrip("/") + path
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
                        if state.studio_slug and isinstance(payload, dict) and _is_studio_swarm_event(payload):
                            continue
                        if isinstance(payload, dict) and _maybe_update_live_activity(state, payload):
                            _request_repaint(state)
                            continue
                        if isinstance(payload, dict) and _is_studio_build_noise(payload, state):
                            continue
                        line = _format_event(payload, state)
                        if line is not None:
                            _add_activity(state, line)
                            _request_repaint(state)
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
                        if state.studio_slug and isinstance(evt, dict) and _is_studio_swarm_event(evt):
                            continue
                        if isinstance(evt, dict) and _maybe_update_live_activity(state, evt):
                            _request_repaint(state)
                            continue
                        if isinstance(evt, dict) and _is_studio_build_noise(evt, state):
                            continue
                        line = _format_event(evt, state)
                        if line is not None:
                            _add_activity(state, line)
                            _request_repaint(state)
                else:
                    state.connected = False
            except Exception:
                state.connected = False
            await asyncio.sleep(1)


async def _studio_sync_listener(state: ReplState) -> None:
    """Track active Studio builds started elsewhere (web UI, another terminal)."""
    async with httpx.AsyncClient(base_url=state.api_url, timeout=10.0) as client:
        while not state.stop:
            if state.studio_watching:
                await asyncio.sleep(1)
                continue
            try:
                resp = await client.get("/api/studio/projects")
                if resp.status_code != 200:
                    await asyncio.sleep(3)
                    continue
                projects = resp.json().get("projects") or []
                active = _pick_active_studio_project(projects)
                if active is None:
                    if state.studio_slug and not state.studio_watching:
                        state.studio_slug = None
                        state.studio_history_seen = 0
                        state.studio_last_snapshot = None
                        state.last_studio_activity_key = None
                    await asyncio.sleep(3)
                    continue

                slug = str(active.get("slug") or "").strip()
                if slug and slug != state.studio_slug:
                    state.studio_slug = slug
                    state.studio_history_seen = 0
                    state.studio_last_snapshot = None
                    state.last_studio_activity_key = None

                detail = await client.get(f"/api/studio/projects/{slug}")
                if detail.status_code != 200:
                    await asyncio.sleep(3)
                    continue
                project = detail.json()
                with httpx.Client(base_url=state.api_url, timeout=30.0) as sync_client:
                    if _emit_studio_clarification_prompt(state, project):
                        _request_repaint(state)
                    elif _emit_studio_approval_prompt(state, project, client=sync_client):
                        _request_repaint(state)
                    elif _apply_studio_project_activity(state, project):
                        _request_repaint(state)
            except Exception:
                pass
            await asyncio.sleep(2)


async def _background_listeners(state: ReplState) -> None:
    swarm_task = _ws_listener(state) if _HAS_WS else _poll_listener(state)
    await asyncio.gather(swarm_task, _studio_sync_listener(state))


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
**Plain language (no slash required)**

Just type at the `>` prompt:

- **Chat** — ask anything; SkyN3t routes it to `/api/llm/complete` when no agent is selected.
- **Build** — prompts like `build a habit tracker with streaks` or `create a dashboard for my team`
  auto-start a Studio run (same as `/project`, when no agent is active).
- **Status** — ask `status of my build`, `what's the progress?`, or
  `status of build-a-habit-tracker-…-1539a8` to read the live Studio manifest
  (not the chat model).
- **Multi-line** — type `\"\"\"` on its own line, then close with `\"\"\"` again.

Slash commands are optional power-user shortcuts — `/help` lists them all.

**Slash commands**

- `/help` — show this help
- `/quit`, `/exit` — leave the REPL (Ctrl-D also exits)
- `/clear` — clear the transcript
- `/agents` — list registered agents
- `/tasks` — list currently running tasks
- `/pipeline NAME [PROMPT...]` — run a pipeline by name
- `/project BRIEF...` — start a Studio run (defaults to the auto planner)
- `/status [SLUG]` — show live Studio build status (defaults to active/last project)
- `/approve [SLUG]` — approve a paused Studio gate (uses last project if omitted)
- `/approve-edits [SLUG]` — edit architecture.md in `$EDITOR`, then approve
- `/reject FEEDBACK...` — reject the paused gate and re-run with feedback
- When a build pauses for approval, the architecture preview appears in chat — reply with
  **approve**, **approve with edits**, or **reject** *feedback…* (same as the web UI)
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
- `/skills` — list installed skills
- `/skills hub` — list Skills Hub seed entries
- `/skills install hub` — install missing safe skills from the hub
- `/skills search QUERY` — search installed skills by relevance

**Optional:** `/project`, `/agent`, `/backend`, and `/model` override the defaults above.
Ctrl-C cancels an in-flight task.
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
    if cmd == "/status":
        await _cmd_status(state, rest)
        return False
    if cmd in ("/approve", "/approve-edits"):
        await _cmd_studio_approve(state, rest, with_edits=cmd == "/approve-edits")
        return False
    if cmd == "/reject":
        await _cmd_studio_reject(state, rest)
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
    if cmd == "/skills":
        await _cmd_skills(state, rest)
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


def _match_studio_slug_hint(hint: str, projects: List[dict]) -> Optional[str]:
    """Resolve a partial slug (e.g. ``build-choatelab``) to a unique project slug."""
    needle = str(hint or "").strip().lower()
    if not needle:
        return None
    slugs = [str(p.get("slug") or "").strip() for p in projects if p.get("slug")]
    if needle in slugs:
        return needle
    prefix = [s for s in slugs if s.lower().startswith(needle)]
    if len(prefix) == 1:
        return prefix[0]
    contains = [s for s in slugs if needle in s.lower()]
    if len(contains) == 1:
        return contains[0]
    return None


def _resolve_studio_slug(state: ReplState, rest: str) -> Optional[str]:
    slug = str(rest or "").strip()
    if slug:
        return slug
    return state.studio_slug


async def _cmd_studio_approve(
    state: ReplState,
    rest: str,
    *,
    with_edits: bool,
) -> None:
    slug = _resolve_studio_slug(state, rest)
    if not slug:
        _add_transcript(
            state,
            _info_line(
                "usage: /approve [slug]  or /approve-edits [slug] — "
                "start a project first so the slug is remembered",
                "yellow",
            ),
        )
        return
    try:
        with httpx.Client(base_url=state.api_url, timeout=30.0) as client:
            resp = client.get(f"/api/studio/projects/{slug}")
            resp.raise_for_status()
            project = resp.json()
            if str(project.get("status") or "").lower() != "awaiting_approval":
                _add_transcript(
                    state,
                    _info_line(
                        f"{slug} is not awaiting approval (status={project.get('status')})",
                        "yellow",
                    ),
                )
                return
            original = fetch_approval_document(client, slug)
            for renderable in approval_renderables(project, original):
                _add_transcript(state, renderable)
            edited: Optional[str] = None
            if with_edits:
                # Run the blocking $EDITOR off the event loop so the background
                # ws/poll/studio-sync listeners stay responsive while editing.
                edited = await asyncio.to_thread(edit_markdown_in_editor, original)
                if edited is None:
                    _add_transcript(state, _info_line("edit cancelled", "yellow"))
                    return
            message = resolve_approval_choice(
                client,
                slug,
                original=original,
                choice="e" if with_edits else "a",
                edited=edited,
            )
    except Exception as exc:
        _add_transcript(state, _info_line(f"approve failed: {exc}", "red"))
        return
    state.studio_approval_gate_seen = None
    _add_transcript(state, _info_line(message, "green"))


async def _cmd_studio_reject(state: ReplState, rest: str) -> None:
    parts = rest.split(maxsplit=1)
    # Only consume the first word as an explicit slug when it actually matches
    # the active project. Otherwise the whole input is feedback against the
    # active slug (mirrors the plain-language reject path), so multi-word
    # feedback like "/reject use SQLite only" is no longer mistaken for a slug.
    if parts and state.studio_slug and parts[0] == state.studio_slug:
        slug = state.studio_slug
        feedback = parts[1].strip() if len(parts) > 1 else ""
    else:
        slug = state.studio_slug
        feedback = rest.strip()
    if not slug:
        _add_transcript(
            state,
            _info_line("usage: /reject [slug] FEEDBACK...", "yellow"),
        )
        return
    if not feedback:
        _add_transcript(
            state,
            _info_line("usage: /reject FEEDBACK...  or  /reject SLUG FEEDBACK...", "yellow"),
        )
        return
    try:
        with httpx.Client(base_url=state.api_url, timeout=30.0) as client:
            submit_reject(client, slug, feedback)
    except Exception as exc:
        _add_transcript(state, _info_line(f"reject failed: {exc}", "red"))
        return
    state.studio_approval_gate_seen = None
    _add_transcript(
        state,
        _info_line(f"rejected {slug} — re-running stage with feedback", "green"),
    )


def _parse_studio_approval_plain(line: str) -> Optional[tuple[str, str, str]]:
    """Map natural language to approval choice. Returns (choice, feedback, error)."""
    stripped = str(line or "").strip()
    if not stripped:
        return None
    lower = stripped.lower()
    if lower in {"approve", "approved"}:
        return ("a", "", "")
    if lower.startswith("approve with edits") or lower.startswith("approve edits"):
        return ("e", "", "")
    if lower.startswith("reject "):
        return ("r", stripped[7:].strip(), "")
    if lower == "reject":
        return ("r", "", "reject needs feedback — e.g. reject use SQLite only")
    return None


async def _handle_studio_approval_plain(state: ReplState, line: str) -> bool:
    """Handle approve / approve with edits / reject when a gate is open."""
    parsed = _parse_studio_approval_plain(line)
    if parsed is None:
        return False
    choice, feedback, err = parsed
    if err:
        _add_transcript(state, _info_line(err, "yellow"))
        return True
    slug = state.studio_slug
    if not slug:
        _add_transcript(state, _info_line("no active studio project for approval", "yellow"))
        return True
    try:
        with httpx.Client(base_url=state.api_url, timeout=30.0) as client:
            resp = client.get(f"/api/studio/projects/{slug}")
            resp.raise_for_status()
            project = resp.json()
            if str(project.get("status") or "").lower() != "awaiting_approval":
                return False
            original = fetch_approval_document(client, slug)
            edited: Optional[str] = None
            if choice == "e":
                # Run the blocking $EDITOR off the event loop so the background
                # ws/poll/studio-sync listeners stay responsive while editing.
                edited = await asyncio.to_thread(edit_markdown_in_editor, original)
                if edited is None:
                    _add_transcript(state, _info_line("edit cancelled", "yellow"))
                    return True
            message = resolve_approval_choice(
                client,
                slug,
                original=original,
                choice=choice,
                edited=edited,
                feedback=feedback,
            )
    except Exception as exc:
        _add_transcript(state, _info_line(f"approval failed: {exc}", "red"))
        return True
    state.studio_approval_gate_seen = None
    _add_transcript(state, _info_line(message, "green"))
    return True


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
        async with httpx.AsyncClient(base_url=state.api_url, timeout=_STUDIO_START_TIMEOUT) as client:
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
        "or /project [options] TEMPLATE :: BRIEF...\n"
        "Presets: run `skyn3t project --examples` in a shell to list web-UI showcase briefs."
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


def _extract_studio_slug(text: str) -> Optional[str]:
    match = _STUDIO_SLUG_RE.search(str(text or ""))
    return match.group(1).lower() if match else None


def _resolve_status_query_slug(state: ReplState, prompt: str) -> Optional[str]:
    normalized = re.sub(r"\s+", " ", str(prompt or "").strip().lower())
    if not normalized or normalized.startswith("/"):
        return None
    slug = _extract_studio_slug(normalized)
    if slug and any(cue in normalized for cue in _STATUS_QUERY_CUES):
        return slug
    build_words = ("build", "project", "studio", "run", "habit", "tracker", "mvp")
    if state.studio_slug and any(cue in normalized for cue in _STATUS_QUERY_CUES):
        if any(word in normalized for word in build_words):
            return state.studio_slug
    if normalized.rstrip("?") in {"status", "progress"} and state.studio_slug:
        return state.studio_slug
    return None


def _attach_studio_project(state: ReplState, slug: str) -> None:
    if slug != state.studio_slug:
        state.studio_slug = slug
        state.studio_history_seen = 0
        state.studio_last_snapshot = None
        state.last_studio_activity_key = None


def _format_studio_status_panel(project: dict) -> Panel:
    slug = str(project.get("slug") or "")
    title = str(project.get("title") or slug)
    status = str(project.get("status") or "unknown")
    stage = str(project.get("current_stage") or "—")
    agent = str(project.get("current_agent") or "—")
    next_action = str(project.get("next_action") or "").strip()
    lines = [
        f"[bold]Slug[/bold]    {slug}",
        f"[bold]Status[/bold]  {status}",
        f"[bold]Stage[/bold]   {stage}",
        f"[bold]Agent[/bold]   {agent}",
    ]
    if next_action:
        lines.append(f"[bold]Next[/bold]     {next_action}")
    history = project.get("history") or []
    if isinstance(history, list) and history:
        lines.append("")
        lines.append("[bold]Recent[/bold]")
        for item in history[-5:]:
            if not isinstance(item, dict):
                continue
            event = str(item.get("event") or "")
            message = str(item.get("message") or "").strip()
            stage_name = str(item.get("stage") or "").strip()
            row = _studio_history_label(event)
            if stage_name:
                row += f" · {stage_name}"
            if message:
                row += f" · {_truncate_studio_text(message, 120)}"
            lines.append(f"  • {row}")
    return Panel("\n".join(lines), title=f"Studio · {title}", border_style="cyan", padding=(0, 1))


def _show_project_status_in_repl(state: ReplState, slug: str) -> None:
    try:
        with httpx.Client(base_url=state.api_url, timeout=30.0) as client:
            resp = client.get(f"/api/studio/projects/{slug}")
            if resp.status_code == 404:
                list_resp = client.get("/api/studio/projects")
                if list_resp.status_code == 200:
                    matched = _match_studio_slug_hint(
                        slug, list_resp.json().get("projects") or []
                    )
                    if matched:
                        slug = matched
                        resp = client.get(f"/api/studio/projects/{slug}")
            if resp.status_code == 404:
                _add_transcript(
                    state,
                    _info_line(
                        f"project not found: {slug} "
                        "(try /status with no slug, or a longer prefix)",
                        "red",
                    ),
                )
                return
            resp.raise_for_status()
            project = resp.json()
    except Exception as exc:
        _add_transcript(state, _info_line(f"status lookup failed: {exc}", "red"))
        return

    _attach_studio_project(state, slug)
    _apply_studio_project_activity(state, project)
    _add_transcript(state, _format_studio_status_panel(project))
    _request_repaint(state)


async def _cmd_status(state: ReplState, rest: str) -> None:
    slug = rest.strip() or state.studio_slug
    try:
        with httpx.Client(base_url=state.api_url, timeout=30.0) as client:
            if not slug:
                resp = client.get("/api/studio/projects")
                resp.raise_for_status()
                projects = resp.json().get("projects") or []
                active = _pick_active_studio_project(projects)
                if active:
                    slug = str(active.get("slug") or "").strip()
                elif projects:
                    latest = max(
                        projects,
                        key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0),
                    )
                    slug = str(latest.get("slug") or "").strip()
            else:
                probe = client.get(f"/api/studio/projects/{slug}")
                if probe.status_code == 404:
                    list_resp = client.get("/api/studio/projects")
                    if list_resp.status_code == 200:
                        matched = _match_studio_slug_hint(
                            slug, list_resp.json().get("projects") or []
                        )
                        if matched:
                            slug = matched
    except Exception as exc:
        _add_transcript(state, _info_line(f"status lookup failed: {exc}", "red"))
        return
    if not slug:
        _add_transcript(
            state,
            _info_line("usage: /status [SLUG] — or ask: status of my build", "yellow"),
        )
        return
    _show_project_status_in_repl(state, slug)


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
    seen_clarification: Optional[tuple[str, ...]] = None
    seen_approval: Optional[tuple[Any, ...]] = None
    terminal_statuses = {"done", "needs_fixes", "failed"}
    state.studio_slug = slug
    state.studio_watching = True
    state.studio_history_seen = 0
    state.studio_last_snapshot = None
    state.last_studio_activity_key = None
    try:
        with httpx.Client(base_url=state.api_url, timeout=30.0) as client:
            while True:
                updated = False
                resp = client.get(f"/api/studio/projects/{slug}")
                resp.raise_for_status()
                project = resp.json()
                if _apply_studio_project_activity(state, project):
                    updated = True
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

                if status == "awaiting_approval":
                    gate_key = approval_gate_key(project)
                    if gate_key != seen_approval:
                        _emit_studio_approval_prompt(state, project, client=client)
                        paint()
                        try:
                            message = run_interactive_approval(
                                console=Console(),
                                client=client,
                                slug=slug,
                                project=project,
                                prompt_choice=lambda: prompt_reader(
                                    1,
                                    "Approve? [a/e/r/w]",
                                ),
                                prompt_feedback=lambda: prompt_reader(
                                    1,
                                    "Feedback for architect",
                                ),
                                edit_text=edit_markdown_in_editor,
                                display=False,
                            )
                        except Exception as exc:
                            _add_transcript(
                                state,
                                _info_line(f"approval failed: {exc}", "red"),
                            )
                            paint()
                            return
                        if message:
                            _add_transcript(state, _info_line(message, "green"))
                            seen_approval = gate_key
                            state.studio_history_seen = len(project.get("history") or [])
                            state.studio_last_snapshot = None
                            state.last_studio_activity_key = None
                            paint()
                            continue
                        _add_transcript(
                            state,
                            _info_line(
                                "approval skipped — still paused "
                                "(use /approve, /approve-edits, or /reject)",
                                "yellow",
                            ),
                        )
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
    finally:
        state.studio_watching = False


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
        with httpx.Client(base_url=state.api_url, timeout=_STUDIO_START_TIMEOUT) as client:
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
    state.studio_slug = str(slug)
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


async def _cmd_skills(state: ReplState, rest: str) -> None:
    sub = (rest or "").strip().lower()
    try:
        if sub.startswith("search "):
            query = rest.strip()[7:].strip()
            from skyn3t.intelligence.skill_library import get_default_library

            lib = get_default_library()
            hits = lib.find_relevant(query, limit=10)
            if not hits:
                _add_transcript(state, _info_line(f"No skills match '{query}'", "yellow"))
                return
            table = Table(title=f"Skills: {query}", header_style="bold magenta")
            table.add_column("name", style="cyan")
            table.add_column("score")
            table.add_column("tags", style="dim")
            for skill in hits:
                table.add_row(skill.name, f"{skill.score:+.2f}", ", ".join(skill.tags[:4]))
            _add_transcript(state, table)
            return

        if sub in {"install hub", "hub install"}:
            from skyn3t.intelligence.skills_hub import install_from_hub

            result = install_from_hub(only_missing=True, reject_unsafe=True)
            installed = result.get("installed") or []
            if installed:
                _add_transcript(
                    state,
                    _info_line(
                        f"installed {len(installed)} hub skill(s): {', '.join(installed[:6])}",
                        "green",
                    ),
                )
            else:
                _add_transcript(state, _info_line("no new hub skills to install", "yellow"))
            return

        if sub == "hub":
            from skyn3t.intelligence.skills_hub import list_hub_entries

            catalog = list_hub_entries()
            lines = [
                f"Hub roots: {', '.join(catalog.get('roots') or [])}",
                f"Markdown skills: {len(catalog.get('markdown_skills') or [])}",
                f"Agent SKILL.md dirs: {len(catalog.get('agent_skill_dirs') or [])}",
                "Run `/skills install hub` to install missing safe skills.",
            ]
            _add_transcript(state, _info_line("\n".join(lines), "cyan"))
            return

        from skyn3t.intelligence.skill_library import get_default_library

        lib = get_default_library()
        skills = lib.all()[:20]
        if not skills:
            _add_transcript(state, _info_line("No skills installed yet.", "yellow"))
            return
        table = Table(title="Installed skills", header_style="bold magenta")
        table.add_column("name", style="cyan")
        table.add_column("score")
        table.add_column("tags", style="dim")
        for skill in skills:
            table.add_row(skill.name, f"{skill.score:+.2f}", ", ".join(skill.tags[:4]))
        _add_transcript(state, table)
    except Exception as exc:
        _add_transcript(state, _info_line(f"skills command failed: {exc}", "red"))


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
    "/agents", "/tasks", "/agent", "/pipeline", "/project",
    "/approve", "/approve-edits", "/reject",
    "/rag", "/ingest",
    "/doctor", "/memory", "/resume", "/retry",
    "/model", "/backend", "/whoami", "/skills",
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

    if _handle_studio_clarification_reply(state, line):
        paint()
        return

    status_slug = _resolve_status_query_slug(state, line)
    if status_slug:
        _show_project_status_in_repl(state, status_slug)
        paint()
        return

    _approval_parsed = _parse_studio_approval_plain(line)
    if _approval_parsed is not None:
        approval_future = asyncio.run_coroutine_threadsafe(
            _handle_studio_approval_plain(state, line),
            loop,
        )
        # "approve with edits" opens $EDITOR (off-loop), which can take far
        # longer than the regular RPC timeout — give it effectively unbounded
        # time so the editor isn't orphaned and the approval finishes cleanly.
        approval_timeout = _APPROVAL_EDITOR_TIMEOUT if _approval_parsed[0] == "e" else 120
        try:
            if approval_future.result(timeout=approval_timeout):
                paint()
                return
        except Exception as exc:
            _add_transcript(state, _info_line(f"approval error: {exc}", "red"))
            paint()
            return

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
            f"Connected to {state.api_url}. Type naturally to chat or start a build "
            '(e.g. "build a habit tracker"). /help for optional slash commands. '
            "Use \"\"\" on its own line for multi-line input.",
            "green",
        )
    )

    # Background loop in a dedicated thread so the Live render + input loop
    # share the main thread.
    loop = asyncio.new_event_loop()

    def _bg() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_background_listeners(state))

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
            console.print(_render_layout(state), height=_render_height(state))
        except Exception:
            pass

    state.paint_callback = _paint_snapshot

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
                # /approve-edits opens $EDITOR (off-loop), which can take far
                # longer than the regular command timeout — give it effectively
                # unbounded time so the editor isn't orphaned mid-approval.
                _slash_cmd = line.split(maxsplit=1)[0]
                command_timeout = (
                    _APPROVAL_EDITOR_TIMEOUT if _slash_cmd == "/approve-edits" else 120
                )
                try:
                    should_exit = command_future.result(timeout=command_timeout)
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
