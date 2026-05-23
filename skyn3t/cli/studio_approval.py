"""Studio approval gate — shared helpers for CLI and REPL."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

APPROVAL_ARTIFACT = "architecture.md"


def approval_gate_key(project: Dict[str, Any]) -> Tuple[Any, ...]:
    """Stable key for a specific approval pause (avoid re-prompting same gate)."""
    gate = project.get("awaiting_approval_for") or {}
    return (
        str(gate.get("stage") or ""),
        str(gate.get("agent") or ""),
        gate.get("stage_index"),
        gate.get("started_at"),
    )


def approval_summary(project: Dict[str, Any]) -> str:
    gate = project.get("awaiting_approval_for") or {}
    stage = str(gate.get("stage") or "stage")
    agent = str(gate.get("agent") or "agent")
    return f"{stage} ({agent})"


def fetch_approval_document(
    client: httpx.Client,
    slug: str,
    *,
    path: str = APPROVAL_ARTIFACT,
) -> str:
    resp = client.get(
        f"/api/studio/projects/{slug}/file",
        params={"path": path},
    )
    resp.raise_for_status()
    return resp.text


def submit_approve(client: httpx.Client, slug: str) -> None:
    resp = client.post(f"/api/studio/projects/{slug}/approve")
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or "approve failed"))


def submit_approve_with_edits(client: httpx.Client, slug: str, content: str) -> None:
    body = (content or "").strip()
    if not body:
        raise ValueError("content required")
    resp = client.post(
        f"/api/studio/projects/{slug}/approve-with-edits",
        json={"content": body},
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or "approve-with-edits failed"))


def submit_reject(client: httpx.Client, slug: str, feedback: str) -> None:
    resp = client.post(
        f"/api/studio/projects/{slug}/reject",
        json={"feedback": (feedback or "").strip()},
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or "reject failed"))


def resolve_approval_choice(
    client: httpx.Client,
    slug: str,
    *,
    original: str,
    choice: str,
    edited: Optional[str] = None,
    feedback: str = "",
) -> str:
    """Apply an approval decision. Returns a short status message."""
    normalized = str(choice or "").strip().lower()
    if normalized in {"a", "approve", "yes", "y"}:
        if edited is not None and edited.strip() != original.strip():
            submit_approve_with_edits(client, slug, edited)
            return "Approved with edits — build resuming."
        submit_approve(client, slug)
        return "Approved — build resuming."
    if normalized in {"e", "edit", "edits", "approve-edits", "approve_with_edits"}:
        if edited is None:
            raise ValueError("edited content required for approve-with-edits")
        if edited.strip() == original.strip():
            submit_approve(client, slug)
            return "No edits detected — approved unchanged."
        submit_approve_with_edits(client, slug, edited)
        return "Approved with edits — build resuming."
    if normalized in {"r", "reject", "no", "n"}:
        submit_reject(client, slug, feedback)
        return "Rejected — re-running stage with your feedback."
    raise ValueError("unknown approval choice")


def run_interactive_approval(
    *,
    console: Any,
    client: httpx.Client,
    slug: str,
    project: Dict[str, Any],
    prompt_choice: Callable[[], str],
    prompt_feedback: Callable[[], str],
    edit_text: Callable[[str], Optional[str]],
) -> Optional[str]:
    """Walk the operator through approve / edits / reject. Returns status line."""
    from rich.panel import Panel

    summary = approval_summary(project)
    try:
        original = fetch_approval_document(client, slug)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not load {APPROVAL_ARTIFACT}: {exc}") from exc

    preview = original if len(original) <= 3500 else original[:3500] + "\n\n…"
    console.print(
        Panel(
            preview,
            title=f"[bold yellow]Approval required · {summary}[/bold yellow]",
            border_style="yellow",
        )
    )
    console.print(
        "[dim](a)pprove  (e)dits in $EDITOR  (r)eject  (w)ait — "
        "same actions as Studio web UI[/dim]"
    )
    choice = prompt_choice().strip().lower() or "a"
    if choice in {"w", "wait", "skip", "s"}:
        return None

    edited: Optional[str] = None
    if choice in {"e", "edit", "edits"}:
        edited = edit_text(original)
        if edited is None:
            return None
        choice = "e"

    feedback = ""
    if choice in {"r", "reject", "no", "n"}:
        feedback = prompt_feedback().strip()

    return resolve_approval_choice(
        client,
        slug,
        original=original,
        choice=choice,
        edited=edited,
        feedback=feedback,
    )


def edit_markdown_in_editor(text: str) -> Optional[str]:
    """Open ``$EDITOR`` on a temp file; return edited text or None if unchanged/cancelled."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    editor_cmd = shlex.split(editor)
    if not editor_cmd:
        editor_cmd = ["nano"]
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        delete=False,
        encoding="utf-8",
    ) as handle:
        handle.write(text)
        path = Path(handle.name)
    try:
        result = subprocess.run([*editor_cmd, str(path)], check=False)
        if result.returncode != 0:
            return None
        return path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)
