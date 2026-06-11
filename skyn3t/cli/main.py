"""SkyN3t CLI — Rich command-line interface for the orchestrator."""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import sys
import time as time_mod
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import httpx
import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.syntax import Syntax
from rich.table import Table

from skyn3t.config.settings import get_settings, resolve_api_base
from skyn3t.studio.mission_setup import mission_setup_labels, normalize_mission_setup
from skyn3t.studio.penpot_handoff import build_penpot_package
from skyn3t.studio.repo_target import normalize_repo_target, resolve_repo_target

API_BASE = resolve_api_base()

console = Console()
app = typer.Typer(
    name="skyn3t",
    help="🤖 SkyN3t Orchestrator CLI",
    rich_markup_mode="rich",
    no_args_is_help=False,
)

_LOCAL_WIZARD_BACKENDS: List[Dict[str, str]] = [
    {
        "backend": "copilot_cli",
        "label": "Copilot CLI",
        "command": "copilot",
        "summary": "Best all-around local default; good for chat and coding.",
    },
    {
        "backend": "claude_cli",
        "label": "Claude CLI",
        "command": "claude",
        "summary": "Best local reasoning/review choice when you want one strong backend.",
    },
    {
        "backend": "kimi_cli",
        "label": "Kimi CLI",
        "command": "kimi",
        "summary": "Cheap local-first option for lightweight chat and fan-out work.",
    },
]


def _print_getting_started(*, server_up: bool = False) -> None:
    """Show a compact first-run path when the REPL cannot start yet."""
    if not server_up:
        console.print(
            "[yellow]Backend is not running.[/yellow] In one terminal run "
            f"[bold]skyn3t start[/bold] (dashboard on localhost:{get_settings().web_port}). "
            "In another, run [bold]skyn3t[/bold] again — that opens the chat REPL."
        )
        console.print()
    quickstart = Table(show_header=False, box=box.SIMPLE, pad_edge=False)
    quickstart.add_row(
        "[bold cyan]skyn3t[/bold cyan]",
        "Interactive chat (after [bold]skyn3t start[/bold] is running)",
    )
    quickstart.add_row("[bold cyan]skyn3t init[/bold cyan]", "Initialize data + multi-LLM setup wizard")
    quickstart.add_row("[bold cyan]skyn3t wizard[/bold cyan]", "Re-run Studio Quality (OpenRouter) or local CLI routing")
    quickstart.add_row(
        "[bold cyan]skyn3t start[/bold cyan]",
        f"Start the API and dashboard on localhost:{get_settings().web_port}",
    )
    quickstart.add_row("[bold cyan]skyn3t status[/bold cyan]", "Check whether the system is up")
    quickstart.add_row("[bold cyan]skyn3t project --examples[/bold cyan]", "List preset briefs like the web UI showcase")
    quickstart.add_row("[bold cyan]skyn3t studio approve SLUG[/bold cyan]", "Approve a paused Studio build from the terminal")
    quickstart.add_row("[bold cyan]skyn3t project \"build a habit tracker\"[/bold cyan]", "Start a Studio run from your own brief")
    quickstart.add_row("[bold cyan]skyn3t repl[/bold cyan]", "Open the interactive console when you want it")

    console.print(
        Panel.fit(
            quickstart,
            title="[bold cyan]SkyN3t Getting Started[/bold cyan]",
            border_style="cyan",
        )
    )
    console.print(
        "[dim]Need the full command list? Run [bold]skyn3t --help[/bold].[/dim]"
    )


def _interactive_cli_ready() -> bool:
    try:
        return bool(sys.stdin.isatty()) and bool(console.is_terminal)
    except Exception:
        return False


def _server_is_reachable() -> bool:
    try:
        with _client() as client:
            resp = client.get("/api/status")
            return resp.status_code < 500
    except Exception:
        return False


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """Open the REPL when the API is up; otherwise show how to get there."""
    if ctx.invoked_subcommand is None:
        if _interactive_cli_ready() and _server_is_reachable():
            from skyn3t.cli.repl import run as run_repl

            run_repl()
            return
        _print_getting_started(server_up=_server_is_reachable())


@app.command()
def repl() -> None:
    """💬 Launch the interactive REPL (Claude-Code-style swarm console)."""
    from skyn3t.cli.repl import run as run_repl
    run_repl()


def _print_studio_examples() -> None:
    from skyn3t.studio.examples import list_studio_examples

    table = Table(title="Studio examples", box=box.ROUNDED, show_lines=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Title", style="bold")
    table.add_column("Template", style="dim")
    table.add_column("Summary")
    for item in list_studio_examples():
        table.add_row(
            str(item.get("id") or ""),
            str(item.get("title") or ""),
            str(item.get("template") or "auto"),
            str(item.get("subtitle") or ""),
        )
    console.print(table)
    console.print(
        "\nRun a preset: [bold cyan]skyn3t project --example habit-tracker[/bold cyan]\n"
        "Custom brief: [bold cyan]skyn3t project \"describe what you want\"[/bold cyan]"
    )


@app.command()
def project(
    brief: Optional[str] = typer.Argument(
        None,
        help="Describe the project you want SkyN3t to build",
    ),
    template: str = typer.Option("auto", "--template", "-t", help="Studio template key; defaults to auto"),
    example: Optional[str] = typer.Option(
        None,
        "--example",
        "-e",
        help="Run a preset brief by id (same cards as the web UI — see --examples)",
    ),
    list_examples: bool = typer.Option(
        False,
        "--examples",
        help="List preset briefs like the web UI showcase",
    ),
    audience: str = typer.Option(
        "auto",
        "--audience",
        help="Mission audience: auto, general, builders, team, leaders, investors",
    ),
    autonomy: str = typer.Option(
        "balanced",
        "--autonomy",
        help="Mission mode: balanced, confirm_first, move_fast",
    ),
    repo_path: str = typer.Option(
        "",
        "--repo-path",
        help="Optional local git repo path or GitHub repo URL to target for code work",
    ),
    focus_file: str = typer.Option(
        "",
        "--focus-file",
        help="Optional repo-relative file path to focus code changes on",
    ),
    watch: bool = typer.Option(
        True,
        "--watch/--no-watch",
        help="Stay attached and stream the project build in the terminal",
    ),
) -> None:
    """🚀 Start a Studio project from one brief."""
    from skyn3t.studio.examples import get_studio_example

    if list_examples:
        _print_studio_examples()
        return

    if example:
        preset = get_studio_example(example)
        if preset is None:
            _error(
                f"Unknown example [bold]{example}[/bold]. "
                "Run [bold]skyn3t project --examples[/bold] to list ids."
            )
            raise typer.Exit(1)
        brief = str(preset.get("brief") or "").strip()
        preset_template = str(preset.get("template") or "").strip()
        if preset_template and template == "auto":
            template = preset_template
        console.print(
            f"[dim]Using preset[/dim] [bold cyan]{preset.get('title')}[/bold cyan] "
            f"([cyan]{preset.get('id')}[/cyan] · template [bold]{template}[/bold])"
        )

    if not brief or not str(brief).strip():
        console.print("[yellow]Provide a brief or pick a preset.[/yellow]\n")
        _print_studio_examples()
        raise typer.Exit(0)

    audience_map = {
        "auto": "",
        "general": "general",
        "builders": "builders",
        "team": "team",
        "leaders": "leaders",
        "investors": "investors",
    }
    audience = str(audience or "auto").strip().lower()
    autonomy = str(autonomy or "balanced").strip().lower()
    allowed_autonomy = {"balanced", "confirm_first", "move_fast"}
    if audience not in audience_map:
        _error("Unknown audience. Use one of: auto, general, builders, team, leaders, investors.")
        raise typer.Exit(1)
    if autonomy not in allowed_autonomy:
        _error("Unknown autonomy mode. Use one of: balanced, confirm_first, move_fast.")
        raise typer.Exit(1)
    mission_setup = normalize_mission_setup(
        {
            "audience": audience_map.get(audience, audience),
            "autonomy": autonomy,
        }
    )
    try:
        repo_target = resolve_repo_target(
            {"local_path": repo_path, "focus_file": focus_file}
        )
    except ValueError as exc:
        _error(str(exc))
        raise typer.Exit(1)
    labels = mission_setup_labels(mission_setup)
    start_payload = {
        "template": template,
        "brief": brief,
        "mission_setup": mission_setup,
        "repo_target": repo_target,
    }
    try:
        with _client() as client:
            data = _post_studio_start(client, start_payload)
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    if not data.get("accepted"):
        _error(data.get("error") or "Project could not be queued.")
        raise typer.Exit(1)

    slug = data.get("slug") or "(pending)"
    title = data.get("title") or "Studio project"
    repo_target = normalize_repo_target(data.get("repo_target") or repo_target)
    next_action = data.get("next_action") or "Queued — waiting for a worker slot."
    _success(
        "Project queued\n"
        f"• Slug: [bold]{slug}[/bold]\n"
        f"• Template: {template}\n"
        f"• Title: {title}\n"
        f"• Next: {next_action}\n"
        f"• Audience: {labels['audience'] or 'Auto / infer from brief'}\n"
        f"• Mode: {labels['autonomy']}\n"
        f"• Repo: {repo_target['local_path'] or 'Current SkyN3t workspace'}\n"
        + (f"• Focus file: {repo_target['focus_file']}\n" if repo_target["focus_file"] else "")
        + "\n"
        + (
            "Watching live progress below…"
            if watch and _interactive_cli_ready()
            else "Use [bold]skyn3t[/bold] or [bold]skyn3t repl[/bold] for interactive chat while builds run."
        )
    )
    if watch and _interactive_cli_ready():
        try:
            _watch_studio_project(slug)
        except KeyboardInterrupt:
            console.print(
                "[yellow]Detached from live build watch.[/yellow] "
                "[dim]The project is still running in the background.[/dim]"
            )
    elif watch:
        console.print(
            "[dim]Re-run in a terminal to stream live progress, or open "
            f"[bold]http://localhost:{get_settings().web_port}/studio[/bold].[/dim]"
        )

studio_app = typer.Typer(help="Studio build management", no_args_is_help=True)


@studio_app.command("approve")
def studio_approve(
    slug: str = typer.Argument(..., help="Studio project slug"),
    with_edits: bool = typer.Option(
        False,
        "--with-edits",
        help="Edit architecture.md in $EDITOR before approving",
    ),
    file: Optional[str] = typer.Option(
        None,
        "--file",
        "-f",
        help="Approve with markdown content from this file",
    ),
) -> None:
    """Approve a Studio project paused at the architect gate."""
    from skyn3t.cli.studio_approval import (
        fetch_approval_document,
        resolve_approval_choice,
    )

    try:
        with _client() as client:
            resp = client.get(f"/api/studio/projects/{slug}")
            resp.raise_for_status()
            project = resp.json()
            if str(project.get("status") or "").lower() != "awaiting_approval":
                _error(
                    f"Project [bold]{slug}[/bold] is not awaiting approval "
                    f"(status={project.get('status')})."
                )
                raise typer.Exit(1)
            original = fetch_approval_document(client, slug)
            edited: Optional[str] = None
            if file:
                edited = Path(file).expanduser().read_text(encoding="utf-8")
            elif with_edits:
                edited = typer.edit(original, extension=".md")
                if edited is None:
                    console.print("[yellow]Edit cancelled — nothing submitted.[/yellow]")
                    raise typer.Exit(0)
            message = resolve_approval_choice(
                client,
                slug,
                original=original,
                choice="e" if edited is not None else "a",
                edited=edited,
            )
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)
    except (RuntimeError, ValueError) as exc:
        _error(str(exc))
        raise typer.Exit(1)

    _success(message)


@studio_app.command("reject")
def studio_reject(
    slug: str = typer.Argument(..., help="Studio project slug"),
    feedback: str = typer.Argument(..., help="What the gated stage should change"),
) -> None:
    """Reject a Studio approval gate and re-run the stage with feedback."""
    from skyn3t.cli.studio_approval import submit_reject

    try:
        with _client() as client:
            resp = client.get(f"/api/studio/projects/{slug}")
            resp.raise_for_status()
            project = resp.json()
            if str(project.get("status") or "").lower() != "awaiting_approval":
                _error(
                    f"Project [bold]{slug}[/bold] is not awaiting approval "
                    f"(status={project.get('status')})."
                )
                raise typer.Exit(1)
            submit_reject(client, slug, feedback)
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)
    except RuntimeError as exc:
        _error(str(exc))
        raise typer.Exit(1)

    _success(f"Rejected [bold]{slug}[/bold] — re-running stage with your feedback.")


agent_app = typer.Typer(help="Agent management commands", no_args_is_help=True)
task_app = typer.Typer(help="Task management commands", no_args_is_help=True)
pipeline_app = typer.Typer(help="Pipeline management commands", no_args_is_help=True)
rag_app = typer.Typer(help="RAG knowledge base commands", no_args_is_help=True)
github_app = typer.Typer(help="GitHub exploration commands", no_args_is_help=True)
scout_app = typer.Typer(help="External repo scout commands", no_args_is_help=True)
proposal_app = typer.Typer(help="Self-update proposal review", no_args_is_help=True)
export_app = typer.Typer(help="Export data for analysis or training", no_args_is_help=True)
user_app = typer.Typer(help="User profile management", no_args_is_help=True)
schedule_app = typer.Typer(help="Schedule recurring tasks", no_args_is_help=True)
memory_app = typer.Typer(help="Memory inspection commands", no_args_is_help=True)
skills_app = typer.Typer(help="Skill registry commands", no_args_is_help=True)
models_app = typer.Typer(help="LLM model catalog commands", no_args_is_help=True)

app.add_typer(studio_app, name="studio")
app.add_typer(agent_app, name="agent")
app.add_typer(task_app, name="task")
app.add_typer(pipeline_app, name="pipeline")
app.add_typer(rag_app, name="rag")
app.add_typer(github_app, name="github")
app.add_typer(scout_app, name="scout")
app.add_typer(proposal_app, name="proposal")
app.add_typer(export_app, name="export")
app.add_typer(user_app, name="user")
app.add_typer(schedule_app, name="schedule")
app.add_typer(memory_app, name="memory")
app.add_typer(skills_app, name="skills")
app.add_typer(models_app, name="models")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_STUDIO_START_TIMEOUT = 120.0
_STUDIO_TERMINAL_STATUSES = frozenset({"done", "needs_fixes", "failed"})


def _client() -> httpx.Client:
    return httpx.Client(base_url=API_BASE, timeout=30.0)


def _studio_progress_snapshot(project: dict) -> tuple[str, str, str, str]:
    return (
        str(project.get("status") or "").strip().lower(),
        str(project.get("current_stage") or "").strip(),
        str(project.get("current_agent") or "").strip(),
        str(project.get("next_action") or "").strip(),
    )


def _format_studio_progress_line(project: dict) -> str:
    status, stage, agent, next_action = _studio_progress_snapshot(project)
    parts: List[str] = [f"[bold]{status or 'unknown'}[/bold]"]
    if stage:
        parts.append(f"stage [bold]{stage}[/bold]")
    if agent:
        parts.append(f"via [bold]{agent}[/bold]")
    if next_action:
        parts.append(next_action)
    return " · ".join(parts)


def _post_studio_start(client: httpx.Client, payload: dict) -> dict:
    """POST /api/studio/start with a spinner — reserve_project can take ~30s."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Queuing Studio project (may take ~30s)…", total=None)
        resp = client.post(
            "/api/studio/start",
            json=payload,
            timeout=_STUDIO_START_TIMEOUT,
        )
    resp.raise_for_status()
    return cast(dict[Any, Any], resp.json())


def _error(message: str) -> None:
    console.print(Panel(str(message) if message is not None else "Unknown error", title="[bold red]Error", border_style="red"))


def _success(message: str) -> None:
    console.print(Panel(message, title="[bold green]Success", border_style="green"))


def _server_unavailable() -> None:
    port = get_settings().web_port
    _error(
        f"Could not connect to SkyN3t server at [bold]localhost:{port}[/bold].\n"
        "Is the server running? Try: [bold]skyn3t start[/bold]"
    )


def _env_file_path() -> Path:
    from skyn3t.config.env_file import env_file_path

    return env_file_path()


def _upsert_env_setting(path: Path, key: str, value: str) -> None:
    from skyn3t.config.env_file import upsert_env_setting

    upsert_env_setting(path, key, value)


def _detect_local_wizard_backends() -> List[Dict[str, str]]:
    available: List[Dict[str, str]] = []
    for entry in _LOCAL_WIZARD_BACKENDS:
        command_path = shutil.which(entry["command"])
        if command_path:
            available.append({**entry, "path": command_path})
    return available


def _wizard_stage_policies(backend: str) -> Dict[str, str]:
    from skyn3t.core.model_router import list_stage_routes, studio_quality_policies

    if backend in {"openrouter", "studio_quality", "auto"}:
        return studio_quality_policies()

    stages = [
        str(item.get("stage") or "").strip().lower()
        for item in list_stage_routes()
        if str(item.get("stage") or "").strip()
    ]
    if backend == "copilot_cli":
        return {
            stage: ("ui" if stage in {"code", "code_agent", "code_improver"} else "balanced")
            for stage in stages
        }
    if backend == "claude_cli":
        return {stage: "strong" for stage in stages}
    if backend == "kimi_cli":
        return {stage: "cheap" for stage in stages}
    return {}


def _env_has_openrouter_key(env_path: Path) -> bool:
    if not env_path.is_file():
        return False
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == "OPENROUTER_API_KEY" and value.strip() not in {"", "sk-..."}:
            return True
    return False


def _apply_install_wizard_choice(
    backend: str,
    *,
    apply_routing_profile: bool,
    env_path: Optional[Path] = None,
    openrouter_api_key: Optional[str] = None,
    enable_quality_env: bool = False,
) -> Dict[str, Any]:
    from skyn3t.config.model_routing import get_model_routing_store

    target_env = env_path or _env_file_path()
    _upsert_env_setting(target_env, "SKYN3T_LLM_BACKEND", backend)
    if openrouter_api_key:
        _upsert_env_setting(target_env, "OPENROUTER_API_KEY", openrouter_api_key)
    if enable_quality_env:
        _upsert_env_setting(target_env, "SKYN3T_AUTO_RETRY", "1")
        # Prefer Docker pool isolation for CodeAgent Python execution when
        # Docker is available; get_backend("auto") falls back to inline.
        _upsert_env_setting(target_env, "SKYN3T_EXECUTION_BACKEND", "auto")

    applied_policies: Dict[str, str] = {}
    if apply_routing_profile:
        applied_policies = _wizard_stage_policies(backend)
        if applied_policies:
            get_model_routing_store().set_many(applied_policies, applied_via="manual")

    return {
        "backend": backend,
        "env_path": str(target_env),
        "routing_applied": bool(applied_policies),
        "routing_stage_count": len(applied_policies),
        "quality_env": enable_quality_env,
    }


def _run_local_cli_wizard_subflow() -> Optional[Dict[str, Any]]:
    available = _detect_local_wizard_backends()
    if not available:
        console.print(
            "[yellow]No local CLI backends detected.[/yellow] "
            "[dim]Install Copilot CLI, Claude CLI, or Kimi CLI, or pick "
            "Studio Quality (OpenRouter) instead.[/dim]"
        )
        return None

    table = Table(title="Local CLI backends", box=box.SIMPLE, header_style="bold cyan")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Backend", style="white")
    table.add_column("Command", style="dim")
    table.add_column("Why choose it", style="white")
    for index, entry in enumerate(available, start=1):
        table.add_row(
            str(index),
            entry["label"],
            entry["command"],
            entry["summary"],
        )
    console.print(table)
    choice = typer.prompt("Choose your default local backend", default="1").strip()
    try:
        selected = available[int(choice) - 1]
    except (ValueError, IndexError):
        raise typer.BadParameter("Pick one of the numbered local backend options.")

    apply_profile = typer.confirm(
        "Apply a local-only routing profile now?",
        default=True,
    )
    result = _apply_install_wizard_choice(
        selected["backend"],
        apply_routing_profile=apply_profile,
    )
    return {**result, "label": selected["label"]}


def _run_studio_quality_wizard_subflow(env_path: Path) -> Dict[str, Any]:
    api_key: Optional[str] = None
    if not _env_has_openrouter_key(env_path):
        console.print(
            "[dim]Studio Quality uses OpenRouter for per-stage routing "
            "(strong code + strong review + UI designer).[/dim]"
        )
        if typer.confirm("Add an OpenRouter API key now?", default=True):
            api_key = typer.prompt(
                "OpenRouter API key",
                hide_input=True,
            ).strip()
            if not api_key:
                console.print("[yellow]Skipping API key — add OPENROUTER_API_KEY to .env later.[/yellow]")

    result = _apply_install_wizard_choice(
        "openrouter",
        apply_routing_profile=True,
        env_path=env_path,
        openrouter_api_key=api_key,
        enable_quality_env=True,
    )
    return {**result, "label": "Studio Quality (OpenRouter)"}


def _run_install_wizard() -> Optional[str]:
    env_path = _env_file_path()
    console.print(
        Panel(
            "[bold]Multi-LLM setup[/bold]\n"
            "Pick how SkyN3t should route agents. For best Project Studio "
            "output, use [cyan]Studio Quality[/cyan] (OpenRouter per-stage tiers).",
            border_style="cyan",
        )
    )
    modes = [
        {
            "id": "studio",
            "label": "Studio Quality (OpenRouter)",
            "summary": "Recommended for builds — strong code + review + UI tiers",
        },
        {
            "id": "local",
            "label": "Local CLI subscription",
            "summary": "Copilot / Claude / Kimi CLIs on this machine",
        },
        {
            "id": "skip",
            "label": "Skip for now",
            "summary": "Leave .env and routing unchanged",
        },
    ]
    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Mode", style="white")
    table.add_column("Summary", style="dim")
    for index, mode in enumerate(modes, start=1):
        table.add_row(str(index), mode["label"], mode["summary"])
    console.print(table)

    choice = typer.prompt("Choose setup mode", default="1").strip()
    try:
        selected_mode = modes[int(choice) - 1]
    except (ValueError, IndexError):
        raise typer.BadParameter("Pick 1, 2, or 3.")

    if selected_mode["id"] == "skip":
        return None

    if selected_mode["id"] == "studio":
        result = _run_studio_quality_wizard_subflow(env_path)
    else:
        local_result = _run_local_cli_wizard_subflow()
        if local_result is None:
            return None
        result = local_result

    routing_line = (
        f"routing profile updated for {result['routing_stage_count']} stages"
        if result["routing_applied"]
        else "routing profile left unchanged"
    )
    quality_line = (
        "SKYN3T_AUTO_RETRY=1 enabled"
        if result.get("quality_env")
        else "quality env left unchanged"
    )
    return (
        "Setup wizard saved\n"
        f"• Mode: {result['label']}\n"
        f"• Backend: {result['backend']}\n"
        f"• Env: {result['env_path']}\n"
        f"• Routing: {routing_line}\n"
        f"• Retries: {quality_line}"
    )


def _local_penpot_package(slug: str) -> Optional[bytes]:
    project_dir = Path(get_settings().projects_dir).expanduser().resolve() / slug
    if not project_dir.is_dir():
        return None
    return build_penpot_package(project_dir)


def _studio_history_label(event_name: str) -> str:
    labels = {
        "PROJECT_QUEUED": "Queued",
        "PROJECT_STARTED": "Started",
        "PROJECT_STAGE_STARTED": "Stage started",
        "PROJECT_STAGE_COMPLETED": "Stage completed",
        "PROJECT_STAGE_FAILED": "Stage failed",
        "PROJECT_AWAITING_CLARIFICATION": "Waiting for clarification",
        "PROJECT_AWAITING_APPROVAL": "Waiting for approval",
        "PROJECT_RESUMED_AFTER_APPROVAL": "Resumed after approval",
        "PROJECT_COMPLETED": "Project finished",
        "PROJECT_FAILED": "Runner failed",
        "PROJECT_REAPED": "Recovered",
    }
    return labels.get(str(event_name or "").upper(), str(event_name or "update").replace("_", " ").title())


def _watch_studio_project(slug: str) -> None:
    console.print(
        Panel.fit(
            f"[bold cyan]Live build watch[/bold cyan]\n"
            f"Project: [bold]{slug}[/bold]\n"
            "Press [bold]Ctrl-C[/bold] to stop watching without stopping the build.",
            border_style="cyan",
        )
    )
    seen_history = 0
    seen_clarification: Optional[tuple[str, ...]] = None
    seen_approval: Optional[tuple[Any, ...]] = None
    last_snapshot: Optional[tuple[str, str, str, str]] = None
    try:
        with _client() as client:
            while True:
                resp = client.get(f"/api/studio/projects/{slug}")
                resp.raise_for_status()
                project = resp.json()
                snapshot = _studio_progress_snapshot(project)
                if snapshot != last_snapshot:
                    console.print(f"[dim]Status[/dim] · {_format_studio_progress_line(project)}")
                    last_snapshot = snapshot
                history = project.get("history") or []
                if not isinstance(history, list):
                    history = []
                for item in history[seen_history:]:
                    event_name = str(item.get("event") or "")
                    label = _studio_history_label(event_name)
                    stage = str(item.get("stage") or "").strip()
                    message = str(item.get("message") or "").strip()
                    line = f"[cyan]{label}[/cyan]"
                    if stage:
                        line += f" · [bold]{stage}[/bold]"
                    if message:
                        line += f" · {message}"
                    console.print(line)
                seen_history = len(history)

                status = str(project.get("status") or "").strip().lower()
                if status == "awaiting_clarification":
                    clarification = project.get("clarification") or {}
                    questions = tuple(
                        str(question).strip()
                        for question in (clarification.get("questions") or [])
                        if str(question).strip()
                    )
                    if questions and questions != seen_clarification:
                        console.print(
                            Panel(
                                "\n".join(
                                    f"{index}. {question}" for index, question in enumerate(questions, start=1)
                                ),
                                title="[bold yellow]Clarification needed[/bold yellow]",
                                border_style="yellow",
                            )
                        )
                        answers = [
                            typer.prompt(f"{index}. {question}")
                            for index, question in enumerate(questions, start=1)
                        ]
                        clarify_resp = client.post(
                            f"/api/studio/projects/{slug}/clarify",
                            json={"answers": answers},
                        )
                        clarify_resp.raise_for_status()
                        data = clarify_resp.json()
                        if not data.get("ok"):
                            raise typer.Exit(1)
                        console.print("[green]Clarifications sent. Resuming build…[/green]")
                        seen_clarification = questions

                if status == "awaiting_approval":
                    from skyn3t.cli.studio_approval import (
                        approval_gate_key,
                        run_interactive_approval,
                    )

                    gate_key = approval_gate_key(project)
                    if gate_key != seen_approval:
                        try:
                            approval_message = run_interactive_approval(
                                console=console,
                                client=client,
                                slug=slug,
                                project=project,
                                prompt_choice=lambda: typer.prompt(
                                    "Approve?",
                                    default="a",
                                ),
                                prompt_feedback=lambda: typer.prompt(
                                    "Feedback for the architect"
                                ),
                                edit_text=lambda text: typer.edit(
                                    text,
                                    extension=".md",
                                ),
                            )
                        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                            _error(str(exc))
                            raise typer.Exit(1) from exc
                        if approval_message:
                            console.print(f"[green]{approval_message}[/green]")
                            seen_approval = gate_key
                            seen_history = len(project.get("history") or [])
                            last_snapshot = None
                            continue

                if status in _STUDIO_TERMINAL_STATUSES:
                    title = "Project finished" if status != "failed" else "Project failed"
                    border = "green" if status != "failed" else "red"
                    next_action = str(project.get("next_action") or "").strip()
                    summary = next_action or ("Build completed." if status != "failed" else "Build failed.")
                    console.print(
                        Panel.fit(
                            f"[bold]{slug}[/bold]\n{summary}",
                            title=f"[bold {border}]{title}[/bold {border}]",
                            border_style=border,
                        )
                    )
                    return

                time_mod.sleep(1.0)
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)


def _extract_text(out: Any) -> str:
    """Extract a text payload from a task output (dict or scalar)."""
    if isinstance(out, dict):
        response = out.get("response")
        return str(response) if response is not None else str(out)
    return str(out) if out is not None else ""


def _proposal_rel_time(value: Any) -> str:
    if value is None:
        return "—"
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return str(value)
    now = time_mod.time()
    diff = max(0, now - timestamp)
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    if diff < 86400:
        return f"{int(diff // 3600)}h ago"
    return f"{int(diff // 86400)}d ago"


def _load_local_proposals(
    *, status: Optional[str], origin: Optional[str]
) -> List[Dict[str, Any]]:
    from skyn3t.cortex.proposals import ProposalStore

    store = ProposalStore()
    return [p.to_public() for p in store.list(status=status, origin=origin)]


def _load_local_proposal(pid: str) -> Optional[Dict[str, Any]]:
    from skyn3t.cortex.proposals import ProposalStore

    proposal = ProposalStore().get(pid)
    return proposal.to_public() if proposal else None


def _proposal_status_filter(status: str) -> Optional[str]:
    normalized = str(status or "pending").strip().lower()
    allowed = {"pending", "approved", "rejected", "applied", "failed", "all"}
    if normalized not in allowed:
        _error(
            "Unknown proposal status. Use one of: pending, approved, rejected, applied, failed, all."
        )
        raise typer.Exit(1)
    return None if normalized == "all" else normalized


@proposal_app.command("list")
def proposal_list(
    status: str = typer.Option("pending", "--status", help="pending, approved, rejected, applied, failed, all"),
    all_origins: bool = typer.Option(
        False, "--all", help="Include user-filed ideas instead of only SkyN3t self-update proposals"
    ),
) -> None:
    """📥 List pending self-update proposals."""
    status_filter = _proposal_status_filter(status)
    origin_filter = None if all_origins else "system"
    source_label = "server"
    try:
        with _client() as client:
            params: Dict[str, Any] = {}
            if status_filter is not None:
                params["status"] = status_filter
            if origin_filter is not None:
                params["origin"] = origin_filter
            resp = client.get(
                "/api/proposals",
                params=params,
            )
            resp.raise_for_status()
            proposals = resp.json().get("proposals", [])
    except httpx.ConnectError:
        proposals = _load_local_proposals(status=status_filter, origin=origin_filter)
        source_label = "local"
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    if source_label == "local":
        console.print(
            "[dim]Server unavailable — showing local proposal files only.[/dim]"
        )

    if not proposals:
        scope = "self-update" if not all_origins else "proposal"
        typer.echo(f"No {scope} proposals found.")
        return

    table = Table(
        title="[bold]Proposal Inbox[/bold]",
        box=box.ROUNDED,
        header_style="bold magenta",
    )
    table.add_column("ID", style="cyan")
    table.add_column("Kind", style="blue")
    table.add_column("Origin", style="yellow")
    table.add_column("Title", style="white")
    table.add_column("When", style="green")
    table.add_column("Summary", style="dim")

    for proposal in proposals:
        summary = str(proposal.get("summary") or "")
        table.add_row(
            str(proposal.get("id") or ""),
            str(proposal.get("kind") or "—"),
            str(proposal.get("origin") or "system"),
            str(proposal.get("title") or "(untitled)"),
            _proposal_rel_time(proposal.get("created_at") or proposal.get("decided_at")),
            summary if len(summary) <= 80 else summary[:80] + "…",
        )

    console.print(table)


@proposal_app.command("show")
def proposal_show(
    proposal_id: str = typer.Argument(..., help="Proposal ID"),
    all_origins: bool = typer.Option(
        False, "--all", help="Allow viewing user-filed ideas as well as system proposals"
    ),
) -> None:
    """🔎 Show full details for one proposal."""
    source_label = "server"
    try:
        with _client() as client:
            resp = client.get(f"/api/proposals/{proposal_id}")
            resp.raise_for_status()
            proposal = resp.json()
    except httpx.ConnectError:
        proposal = _load_local_proposal(proposal_id)
        source_label = "local"
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    if not proposal:
        _error(f"Proposal not found: {proposal_id}")
        raise typer.Exit(1)

    if not all_origins and str(proposal.get("origin") or "system") != "system":
        _error("That proposal is a user-filed idea. Re-run with --all to inspect it.")
        raise typer.Exit(1)

    if source_label == "local":
        console.print(
            "[dim]Server unavailable — showing local proposal file only.[/dim]"
        )

    meta_lines = [
        f"ID: {proposal.get('id') or '—'}",
        f"Kind: {proposal.get('kind') or '—'}",
        f"Origin: {proposal.get('origin') or 'system'}",
        f"Status: {proposal.get('status') or '—'}",
        f"Source: {proposal.get('source') or '—'}",
        f"Created: {_proposal_rel_time(proposal.get('created_at'))}",
        f"Summary: {proposal.get('summary') or '—'}",
    ]
    detail = str(proposal.get("detail") or "(no detail)")
    console.print(
        Panel(
            "\n".join(meta_lines) + f"\n\nDetail\n{detail}",
            title=f"[bold cyan]{proposal.get('title') or 'Proposal'}[/bold cyan]",
            border_style="cyan",
        )
    )


@proposal_app.command("approve")
def proposal_approve(proposal_id: str = typer.Argument(..., help="Proposal ID")) -> None:
    """✅ Approve and apply a proposal."""
    try:
        with _client() as client:
            resp = client.post(f"/api/proposals/{proposal_id}/approve")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _error(
            "Proposal approval needs the SkyN3t server running so handlers are wired.\n"
            "Start it with [bold]skyn3t start[/bold] and try again."
        )
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    if not data.get("ok", True):
        _error(data.get("error") or "Proposal approval failed.")
        raise typer.Exit(1)

    result = data.get("result") or {}
    _success(
        "Proposal approved\n"
        f"• ID: [bold]{proposal_id}[/bold]\n"
        f"• Applied: {'yes' if data.get('applied') else 'no'}\n"
        f"• Result: {json.dumps(result, indent=2) if result else 'No handler output'}"
    )


@proposal_app.command("reject")
def proposal_reject(
    proposal_id: str = typer.Argument(..., help="Proposal ID"),
    reason: str = typer.Option("", "--reason", help="Optional rejection reason"),
) -> None:
    """🛑 Reject a proposal."""
    try:
        with _client() as client:
            resp = client.post(
                f"/api/proposals/{proposal_id}/reject",
                json={"reason": reason},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _error(
            "Proposal rejection needs the SkyN3t server running.\n"
            "Start it with [bold]skyn3t start[/bold] and try again."
        )
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    if not data.get("ok", True):
        _error(data.get("error") or "Proposal rejection failed.")
        raise typer.Exit(1)

    _success(
        "Proposal rejected\n"
        f"• ID: [bold]{proposal_id}[/bold]\n"
        + (f"• Reason: {reason}" if reason else "• Reason: (none)")
    )


# ---------------------------------------------------------------------------
# System commands
# ---------------------------------------------------------------------------


@app.command()
def start(
    host: Optional[str] = typer.Option(None, "--host", "-h", help="Host to bind to"),
    port: Optional[int] = typer.Option(None, "--port", "-p", help="Port to bind to"),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload"),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of worker processes"),
    access_log: bool = typer.Option(
        False,
        "--access-log/--no-access-log",
        help=(
            "Show one log line per HTTP request. Off by default — the "
            "dashboard polls several endpoints per second and the access "
            "log buries the orchestrator's own warning/info messages "
            "(fast-path activations, critique timeouts, etc.)."
        ),
    ),
) -> None:
    """🚀 Start the SkyN3t orchestrator server."""
    import uvicorn

    settings = get_settings()
    host = host if host is not None else settings.web_host
    port = port if port is not None else settings.web_port

    banner = (
        f"[bold cyan]SkyN3t Orchestrator[/bold cyan]  [dim]v{settings.app_version}[/dim]\n"
        f"Starting server on [bold]{host}:{port}[/bold] ..."
    )
    console.print(Panel.fit(banner, title="🚀 Launch", border_style="cyan"))

    uvicorn.run(
        "skyn3t.web.app:app",
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else 1,
        log_level="info",
        access_log=access_log,
    )


@app.command()
def wizard() -> None:
    """🧙 Re-run multi-LLM setup (OpenRouter Studio Quality or local CLI routing)."""
    if not _interactive_cli_ready():
        _error("Setup wizard requires an interactive terminal.")
        raise typer.Exit(1)
    summary = _run_install_wizard()
    if summary:
        _success(summary)
    else:
        console.print("[dim]Setup wizard skipped — no changes written.[/dim]")


@app.command()
def init(
    wizard: bool = typer.Option(
        True,
        "--wizard/--no-wizard",
        help="Run the multi-LLM setup wizard in interactive terminals",
    ),
) -> None:
    """🔧 Initialize the SkyN3t system (directories + database)."""
    from skyn3t.core.models import init_db

    async def _init() -> None:
        settings = get_settings()
        settings.ensure_directories()
        await init_db()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        t = progress.add_task("Initializing SkyN3t...", total=None)
        try:
            asyncio.run(_init())
            progress.update(t, completed=True)
            message = (
                "System initialized successfully!\n"
                f"• Data directory: [cyan]{get_settings().data_dir}[/cyan]\n"
                f"• Logs directory: [cyan]{get_settings().logs_dir}[/cyan]\n"
                f"• Vector DB: [cyan]{get_settings().vector_db_path}[/cyan]"
            )
            if wizard and _interactive_cli_ready():
                wizard_summary = _run_install_wizard()
                if wizard_summary:
                    message += f"\n\n{wizard_summary}"
            _success(message)
        except Exception as exc:
            progress.update(t, completed=True)
            _error(f"Initialization failed: {exc}")
            raise typer.Exit(1)


@models_app.command("sync")
def models_sync(
    force: bool = typer.Option(False, "--force", "-f", help="Ignore cache TTL and refetch"),
) -> None:
    """🔄 Fetch the latest OpenRouter model catalog into data/openrouter_models.json."""
    from skyn3t.core.openrouter_catalog import sync_catalog, validate_tier_models

    async def _run() -> Dict[str, Any]:
        return await sync_catalog(force=force)

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        _error(f"OpenRouter sync failed: {exc}")
        raise typer.Exit(1)

    status = str(result.get("status") or "unknown")
    count = int(result.get("count") or 0)
    source = str(result.get("source") or "")
    if status == "failed":
        _error(str(result.get("error") or "sync failed"))
        raise typer.Exit(1)

    lines = [
        f"Status: [cyan]{status}[/cyan]",
        f"Models: [bold]{count}[/bold]",
        f"Source: {source}",
    ]
    if result.get("synced_at"):
        lines.append(f"Synced at: {result['synced_at']:.0f}")
    if result.get("error"):
        lines.append(f"[yellow]Note:[/yellow] {result['error']}")

    tier_rows = validate_tier_models()
    missing = [r for r in tier_rows if not r.get("exists")]
    if missing:
        lines.append("")
        lines.append("[yellow]Tier models missing from catalog:[/yellow]")
        for row in missing:
            fb = row.get("fallback") or "(none)"
            lines.append(f"  • {row['tier']}: {row['model']} → fallback {fb}")

    console.print(Panel.fit("\n".join(lines), title="OpenRouter catalog", border_style="cyan"))


@app.command()
def cleanup(
    projects: bool = typer.Option(True, "--projects/--no-projects", help="Clean project artifact directories"),
    proposals: bool = typer.Option(True, "--proposals/--no-proposals", help="Clean decided proposals"),
    branches: bool = typer.Option(True, "--branches/--no-branches", help="Delete auto branches"),
    all_: bool = typer.Option(False, "--all", help="All three (overrides defaults)"),
    older_than_days: Optional[int] = typer.Option(None, "--older-than-days", help="Only items older than N days"),
    keep_last: Optional[int] = typer.Option(None, "--keep-last", help="Keep the N most-recent items"),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Preview only by default; pass --apply to actually delete"),
) -> None:
    """🧹 Clean up project artifacts, decided proposals, and auto-branches."""
    from skyn3t.cli.cleanup import execute as exec_plan
    from skyn3t.cli.cleanup import preview
    if all_:
        projects = proposals = branches = True
    plan = preview(projects=projects, proposals=proposals, branches=branches,
                    older_than_days=older_than_days, keep_last=keep_last)
    typer.echo(f"Projects:  {plan['total_projects']}")
    typer.echo(f"Proposals: {plan['total_proposals']}")
    typer.echo(f"Branches:  {plan['total_branches']}")
    typer.echo(f"Total size: {plan['total_bytes']/1024:.1f} KB")
    if not (plan["total_projects"] or plan["total_proposals"] or plan["total_branches"]):
        typer.echo("Nothing to clean.")
        return
    if dry_run:
        typer.echo("\n(dry-run — pass --apply to actually delete)")
        return
    typer.confirm(
        f"Delete {plan['total_projects']} projects, "
        f"{plan['total_proposals']} proposals, "
        f"{plan['total_branches']} branches?",
        abort=True,
    )
    res = exec_plan(plan)
    typer.echo(f"\nRemoved: {res['projects']} projects, {res['proposals']} proposals, {res['branches']} branches")
    if res["errors"]:
        typer.echo("Errors:")
        for e in res["errors"]:
            typer.echo(f"  - {e}")


@app.command()
def status() -> None:
    """📊 Show system status."""
    try:
        with _client() as client:
            resp = client.get("/api/status")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc}")
        raise typer.Exit(1)

    system_running = data.get("running", False)
    status_text = "[bold green]Online[/bold green]" if system_running else "[bold red]Offline[/bold red]"

    info = Table(show_header=False, box=box.SIMPLE)
    info.add_row("Status", status_text)
    info.add_row("Total Agents", str(data.get("total_agents", 0)))
    info.add_row("Running Tasks", str(data.get("running_tasks", 0)))
    info.add_row("Completed Tasks", str(data.get("completed_tasks", 0)))
    info.add_row("Pipelines", str(data.get("pipelines", 0)))

    console.print(
        Panel(
            info,
            title="[bold cyan]📊 System Status[/bold cyan]",
            border_style="cyan",
        )
    )

    agents = data.get("agents", {})
    if agents:
        table = Table(
            title="[bold]👥 Agent Swarm[/bold]",
            box=box.ROUNDED,
            header_style="bold magenta",
        )
        table.add_column("Name", style="cyan")
        table.add_column("Type", style="blue")
        table.add_column("Provider", style="green")
        table.add_column("Mode", style="yellow")
        table.add_column("Status", style="bold")
        table.add_column("Queue", justify="right")
        table.add_column("Errors", justify="right")

        for name, stats in agents.items():
            st = stats.get("status", "idle")
            color = {"idle": "green", "busy": "yellow", "error": "red", "offline": "dim"}.get(st, "white")
            mode = "CLI" if stats.get("cli_agent") else "API"
            table.add_row(
                name,
                stats.get("type", "-"),
                stats.get("provider", "-"),
                mode,
                f"[{color}]{st}[/{color}]",
                str(stats.get("queue_size", 0)),
                str(stats.get("recent_errors", 0)),
            )
        console.print(table)
    else:
        console.print("[dim]No agents registered.[/dim]")


@app.command()
def doctor() -> None:
    """🩺 Run local and remote health checks."""
    from skyn3t.cli.doctor import run_doctor

    report = run_doctor(API_BASE)

    table = Table(title="[bold]🩺 Doctor[/bold]", box=box.SIMPLE, header_style="bold cyan")
    table.add_column("Check", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Summary", style="white")
    for check in report.checks:
        status_style = {"ok": "green", "warn": "yellow", "fail": "red"}.get(check.status, "white")
        table.add_row(check.name, f"[{status_style}]{check.icon}[/{status_style}]", check.summary)
    console.print(table)
    console.print(
        f"[dim]Failures: {report.failed}  Warnings: {report.warned}  API: {API_BASE}[/dim]"
    )

    if report.failed:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Memory commands
# ---------------------------------------------------------------------------


@memory_app.command("summary")
def memory_summary(
    limit: int = typer.Option(5, "--limit", "-l", help="Number of items per section"),
) -> None:
    """🧠 Show memory grouped as session, operator, and project layers."""
    try:
        with _client() as client:
            resp = client.get("/api/memory/layers", params={"limit": limit})
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    if not data.get("enabled"):
        console.print("[dim]Memory is not initialized.[/dim]")
        return

    layers = data.get("layers") or {}
    session = layers.get("session") or {}
    operator = layers.get("operator") or {}
    project = layers.get("project") or {}

    summary = Table(show_header=False, box=box.SIMPLE, pad_edge=False)
    summary.add_row("Session memory", f"{session.get('active_sessions', 0)} active session(s)")
    summary.add_row(
        "Operator memory",
        f"{operator.get('insight_count', 0)} insight(s), {operator.get('skill_summary', {}).get('total', 0)} skill(s)",
    )
    summary.add_row(
        "Project memory",
        f"{project.get('tasks', 0)} tasks, {project.get('knowledge_documents', 0)} knowledge docs",
    )
    console.print(Panel(summary, title="[bold cyan]🧠 Memory Layers[/bold cyan]", border_style="cyan"))

    sessions = session.get("sessions") or []
    if sessions:
        console.print("[bold]Session layer[/bold]")
        for session_id in sessions:
            console.print(f"• {session_id}")

    insights = operator.get("recent_insights") or []
    if insights:
        table = Table(title="Operator memory", box=box.SIMPLE)
        table.add_column("Agent", style="cyan")
        table.add_column("Capability", style="dim")
        table.add_column("Insight")
        for item in insights:
            table.add_row(
                str(item.get("agent") or "—"),
                str(item.get("capability") or "—"),
                str(item.get("insight") or "")[:100],
            )
        console.print(table)

    skills = operator.get("top_skills") or []
    if skills:
        table = Table(title="Top skills", box=box.SIMPLE)
        table.add_column("Name", style="cyan")
        table.add_column("Score", justify="right")
        table.add_column("Tags", style="dim")
        for skill in skills:
            table.add_row(
                str(skill.get("name") or "—"),
                f"{float(skill.get('score') or 0):+.2f}",
                ", ".join(skill.get("tags") or []),
            )
        console.print(table)

    recent_docs = project.get("recent_documents") or []
    if recent_docs:
        table = Table(title="Project knowledge", box=box.SIMPLE)
        table.add_column("Title", style="cyan")
        table.add_column("Type", style="dim")
        table.add_column("Source", style="dim")
        for doc in recent_docs:
            table.add_row(
                str(doc.get("title") or "—"),
                str(doc.get("doc_type") or "—"),
                str(doc.get("source") or "—"),
            )
        console.print(table)


@memory_app.command("sessions")
def memory_sessions() -> None:
    """List active session-memory contexts."""
    try:
        with _client() as client:
            resp = client.get("/api/memory/sessions")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    sessions = data.get("sessions") or []
    if not sessions:
        console.print("[dim]No active memory sessions.[/dim]")
        return
    table = Table(title="Memory sessions", box=box.SIMPLE)
    table.add_column("Session ID", style="cyan")
    for session_id in sessions:
        table.add_row(str(session_id))
    console.print(table)


@memory_app.command("session")
def memory_session(session_id: str) -> None:
    """Show one session-memory context and recent activity."""
    try:
        with _client() as client:
            resp = client.get(f"/api/memory/sessions/{session_id}")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    if data.get("error"):
        _error(str(data["error"]))
        raise typer.Exit(1)

    context = data.get("context") or {}
    info = Table(show_header=False, box=box.SIMPLE, pad_edge=False)
    info.add_row("Session", str(data.get("session_id") or session_id))
    info.add_row("Participants", ", ".join(context.get("participants") or []) or "—")
    info.add_row("History entries", str(len(context.get("history") or [])))
    console.print(Panel(info, title="[bold cyan]Session Memory[/bold cyan]", border_style="cyan"))

    activity = data.get("recent_activity") or []
    if activity:
        table = Table(title="Recent activity", box=box.SIMPLE)
        table.add_column("Type", style="cyan")
        table.add_column("Summary")
        for item in activity:
            summary = item.get("title") or item.get("content") or item.get("description") or "—"
            table.add_row(str(item.get("type") or "—"), str(summary)[:100])
        console.print(table)


@memory_app.command("drafts")
def memory_drafts_cmd(
    limit: int = typer.Option(20, "--limit", "-l", help="Number of drafts to show"),
    doc_type: Optional[str] = typer.Option(None, "--type", help="Optional document type filter"),
) -> None:
    """List pending reviewable memory drafts."""
    params: dict[str, int | str] = {"limit": limit}
    if doc_type:
        params["doc_type"] = doc_type
    try:
        with _client() as client:
            resp = client.get("/api/memory/drafts", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    drafts = data.get("drafts") or []
    if not drafts:
        console.print("[dim]No pending memory drafts.[/dim]")
        return

    table = Table(title="Memory drafts", box=box.SIMPLE)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Type", style="dim")
    table.add_column("Layer", style="dim")
    table.add_column("Source", style="dim")
    table.add_column("Title")
    for draft in drafts:
        meta = draft.get("meta") or {}
        table.add_row(
            str(draft.get("id") or "—"),
            str(draft.get("doc_type") or "—"),
            str(meta.get("memory_layer") or "—"),
            str(draft.get("source") or "—"),
            str(draft.get("title") or "—"),
        )
    console.print(table)


@memory_app.command("approve")
def memory_approve(doc_id: str) -> None:
    """Approve a memory draft and promote it into trusted knowledge."""
    try:
        with _client() as client:
            resp = client.post(f"/api/memory/drafts/{doc_id}/approve")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    draft = data.get("draft") or {}
    console.print(
        f"[green]Approved[/green] {draft.get('id', doc_id)} — {draft.get('title', 'memory draft')}"
    )


@memory_app.command("reject")
def memory_reject(
    doc_id: str,
    reason: str = typer.Option("", "--reason", "-r", help="Optional rejection reason"),
) -> None:
    """Reject a memory draft."""
    try:
        with _client() as client:
            resp = client.post(f"/api/memory/drafts/{doc_id}/reject", json={"reason": reason})
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    draft = data.get("draft") or {}
    console.print(
        f"[yellow]Rejected[/yellow] {draft.get('id', doc_id)} — {draft.get('title', 'memory draft')}"
    )


@memory_app.command("evals")
def memory_evals_cmd(
    status: str = typer.Option("draft", "--status", help="draft, approved, or rejected"),
    limit: int = typer.Option(20, "--limit", "-l", help="Number of evaluation assets to show"),
) -> None:
    """List governed evaluation assets."""
    try:
        with _client() as client:
            resp = client.get("/api/memory/evaluations", params={"status": status, "limit": limit})
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    evaluations = data.get("evaluations") or []
    if not evaluations:
        console.print("[dim]No evaluation assets found.[/dim]")
        return

    table = Table(title="Evaluation assets", box=box.SIMPLE)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Status", style="dim")
    table.add_column("Lane", style="dim")
    table.add_column("Lang", style="dim")
    table.add_column("Signals")
    table.add_column("Title")
    for item in evaluations:
        table.add_row(
            str(item.get("id") or "—"),
            str(item.get("review_status") or "—"),
            str(item.get("lane") or "—"),
            str(item.get("language") or "—"),
            ", ".join(item.get("signals") or []) or "—",
            str(item.get("title") or "—"),
        )
    console.print(table)


@memory_app.command("eval")
def memory_eval_cmd(doc_id: str) -> None:
    """Show one governed evaluation asset."""
    try:
        with _client() as client:
            resp = client.get(f"/api/memory/evaluations/{doc_id}")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    evaluation = data.get("evaluation") or {}
    info = Table(show_header=False, box=box.SIMPLE, pad_edge=False)
    info.add_row("ID", str(evaluation.get("id") or doc_id))
    info.add_row("Status", str(evaluation.get("review_status") or "—"))
    info.add_row("Lane", str(evaluation.get("lane") or "—"))
    info.add_row("Language", str(evaluation.get("language") or "—"))
    info.add_row("Signals", ", ".join(evaluation.get("signals") or []) or "—")
    info.add_row("Repos", ", ".join(evaluation.get("source_repos") or []) or "—")
    console.print(
        Panel(
            info,
            title=f"[bold cyan]{evaluation.get('title') or 'Evaluation asset'}[/bold cyan]",
            border_style="cyan",
        )
    )

    checks = evaluation.get("checks") or []
    if checks:
        checks_table = Table(title="Checks", box=box.SIMPLE)
        checks_table.add_column("Check")
        for check in checks:
            checks_table.add_row(str(check))
        console.print(checks_table)


@memory_app.command("export-eval")
def memory_export_eval_cmd(
    doc_id: str,
    format: str = typer.Option("json", "--format", help="json or jsonl"),
) -> None:
    """Export an approved evaluation asset."""
    try:
        with _client() as client:
            resp = client.get(f"/api/memory/evaluations/{doc_id}/export", params={"format": format})
            resp.raise_for_status()
            if format.strip().lower() == "jsonl":
                console.print(resp.text.rstrip())
            else:
                console.print_json(json.dumps(resp.json()))
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Agent commands
# ---------------------------------------------------------------------------


@agent_app.command("list")
def agent_list() -> None:
    """📋 List all registered agents."""
    try:
        with _client() as client:
            resp = client.get("/api/agents")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)

    agents = data.get("agents", [])
    if not agents:
        console.print("[yellow]No agents registered.[/yellow]")
        return

    table = Table(
        title="[bold]📋 Registered Agents[/bold]",
        box=box.ROUNDED,
        header_style="bold magenta",
    )
    table.add_column("Name", style="cyan")
    table.add_column("Type", style="blue")
    table.add_column("Provider", style="green")
    table.add_column("Mode", style="yellow")
    table.add_column("Status", style="bold")
    table.add_column("Capabilities")
    table.add_column("Queue", justify="right")

    for agent in agents:
        st = agent.get("status", "idle")
        color = {"idle": "green", "busy": "yellow", "error": "red", "offline": "dim"}.get(st, "white")
        caps = ", ".join(agent.get("capabilities", [])) or "-"
        mode = "CLI" if agent.get("cli_agent") else "API"
        table.add_row(
            agent.get("name", "-"),
            agent.get("type", "-"),
            agent.get("provider", "-"),
            mode,
            f"[{color}]{st}[/{color}]",
            caps,
            str(agent.get("queue_size", 0)),
        )
    console.print(table)


@agent_app.command("add")
def agent_add(
    name: str = typer.Argument(..., help="Unique agent name"),
    provider: str = typer.Option("claude", "--provider", "-p", help="Provider (claude, github)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model name (Claude only)"),
    local: bool = typer.Option(False, "--local", "-l", help="Run locally without server"),
) -> None:
    """➕ Add a new agent dynamically."""
    provider = (provider or "claude").lower()
    if provider == "anthropic":
        provider = "claude"
    payload = {
        "name": name,
        "provider": provider,
        "model": model,
    }

    if not local:
        try:
            with _client() as client:
                resp = client.post("/api/agents", json=payload)
                resp.raise_for_status()
                data = resp.json()
            if "error" in data:
                _error(data["error"])
                raise typer.Exit(1)
            _success(
                f"Agent [bold cyan]{name}[/bold cyan] registered on running server!\n"
                f"• Provider: [green]{provider}[/green]\n"
                f"• Status: [bold]{data.get('status', 'registered')}[/bold]"
            )
            return
        except httpx.ConnectError:
            console.print("[yellow]Server not running. Falling back to local mode...[/yellow]\n")
        except httpx.HTTPStatusError as exc:
            _error(f"Server error: {exc.response.text}")
            raise typer.Exit(1)

    # Local mode -----------------------------------------------------------
    from skyn3t.adapters.claude_cli import ClaudeCLIAgent
    from skyn3t.core.events import EventBus
    from skyn3t.core.orchestrator import Orchestrator

    async def _add() -> dict[str, Any]:
        from skyn3t.core.agent import BaseAgent
        event_bus = EventBus()
        orchestrator = Orchestrator(event_bus)
        agent: BaseAgent

        if provider == "claude":
            agent = ClaudeCLIAgent(
                name=name,
                event_bus=event_bus,
                config={"model": model} if model else {},
            )
        elif provider == "github":
            from skyn3t.agents.github_explorer import GitHubExplorerAgent
            agent = GitHubExplorerAgent(name=name, event_bus=event_bus)
        else:
            _error(
                f"Provider '[bold]{provider}[/bold]' not fully implemented.\n"
                "Supported: [cyan]claude[/cyan], [cyan]github[/cyan]"
            )
            raise typer.Exit(1)

        await agent.initialize()
        await agent.start()
        orchestrator.register_agent(agent)
        return orchestrator.get_system_status()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        t = progress.add_task(f"Creating agent [bold cyan]{name}[/bold cyan]...", total=None)
        try:
            result = asyncio.run(_add())
            progress.update(t, completed=True)
            _success(
                f"Agent [bold cyan]{name}[/bold cyan] added locally!\n"
                f"• Provider: [green]{provider}[/green]\n"
                f"• Total agents: {result.get('total_agents', 1)}"
            )
        except typer.Exit:
            raise
        except Exception as exc:
            progress.update(t, completed=True)
            _error(f"Failed to add agent: {exc}")
            raise typer.Exit(1)


@agent_app.command("add-claude")
def agent_add_claude(
    name: str = typer.Argument(..., help="Unique agent name"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Claude model name"),
    local: bool = typer.Option(False, "--local", "-l", help="Run locally without server"),
) -> None:
    """➕ Add a Claude CLI agent."""
    _add_cli_agent(name, "claude", model=model, local=local)


@agent_app.command("add-kimi")
def agent_add_kimi(
    name: str = typer.Argument(..., help="Unique agent name"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Kimi model name"),
    local: bool = typer.Option(False, "--local", "-l", help="Run locally without server"),
) -> None:
    """➕ Add a Kimi CLI agent."""
    _add_cli_agent(name, "kimi", model=model, local=local)


@agent_app.command("add-copilot")
def agent_add_copilot(
    name: str = typer.Argument(..., help="Unique agent name"),
    local: bool = typer.Option(False, "--local", "-l", help="Run locally without server"),
) -> None:
    """➕ Add a Copilot CLI agent."""
    _add_cli_agent(name, "copilot", local=local)


def _add_cli_agent(
    name: str,
    provider: str,
    model: Optional[str] = None,
    local: bool = False,
) -> None:
    """Helper to add a CLI shell agent."""
    payload = {
        "name": name,
        "provider": provider,
        "model": model,
        "cli_agent": True,
    }

    if not local:
        try:
            with _client() as client:
                resp = client.post("/api/agents", json=payload)
                resp.raise_for_status()
                data = resp.json()
            if "error" in data:
                _error(data["error"])
                raise typer.Exit(1)
            _success(
                f"CLI Agent [bold cyan]{name}[/bold cyan] registered on running server!\n"
                f"• Provider: [green]{provider}[/green]\n"
                f"• Mode: [yellow]CLI[/yellow]\n"
                f"• Status: [bold]{data.get('status', 'registered')}[/bold]"
            )
            return
        except httpx.ConnectError:
            console.print("[yellow]Server not running. Falling back to local mode...[/yellow]\n")
        except httpx.HTTPStatusError as exc:
            _error(f"Server error: {exc.response.text}")
            raise typer.Exit(1)

    # Local mode -----------------------------------------------------------
    from skyn3t.adapters.claude_cli import ClaudeCLIAgent
    from skyn3t.adapters.copilot_cli import CopilotCLIAgent
    from skyn3t.adapters.kimi_cli import KimiCLIAgent
    from skyn3t.core.events import EventBus
    from skyn3t.core.orchestrator import Orchestrator

    async def _add() -> dict[str, Any]:
        from skyn3t.core.agent import BaseAgent
        event_bus = EventBus()
        orchestrator = Orchestrator(event_bus)
        agent: BaseAgent
        if provider == "claude":
            agent = ClaudeCLIAgent(
                name=name,
                event_bus=event_bus,
                config={"model": model} if model else {},
            )
        elif provider == "kimi":
            agent = KimiCLIAgent(
                name=name,
                event_bus=event_bus,
                config={"model": model} if model else {},
            )
        elif provider == "copilot":
            agent = CopilotCLIAgent(
                name=name,
                event_bus=event_bus,
            )
        else:
            _error(f"Unknown CLI provider: {provider}")
            raise typer.Exit(1)
        await agent.initialize()
        await agent.start()
        orchestrator.register_agent(agent)
        return orchestrator.get_system_status()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        t = progress.add_task(f"Creating CLI agent [bold cyan]{name}[/bold cyan]...", total=None)
        try:
            result = asyncio.run(_add())
            progress.update(t, completed=True)
            _success(
                f"CLI Agent [bold cyan]{name}[/bold cyan] added locally!\n"
                f"• Provider: [green]{provider}[/green]\n"
                f"• Mode: [yellow]CLI[/yellow]\n"
                f"• Total agents: {result.get('total_agents', 1)}"
            )
        except typer.Exit:
            raise
        except Exception as exc:
            progress.update(t, completed=True)
            _error(f"Failed to add CLI agent: {exc}")
            raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Task commands
# ---------------------------------------------------------------------------


@task_app.command("submit")
def task_submit(
    agent_name: str = typer.Argument(..., help="Target agent name"),
    title: str = typer.Argument(..., help="Task title"),
    description: str = typer.Option("", "--description", "-d", help="Task description"),
    priority: int = typer.Option(0, "--priority", "-p", help="Task priority (0-10)"),
    input_json: Optional[str] = typer.Option(None, "--input", "-i", help="Task input as JSON string"),
    stdin_from: Optional[str] = typer.Option(None, "--stdin-from", help="Task ID to pipe output from as stdin"),
    pipe_to: Optional[str] = typer.Option(None, "--pipe-to", help="Agent name to pipe output to after completion"),
) -> None:
    """📤 Submit a task to an agent."""
    payload: dict = {
        "title": title,
        "description": description,
        "priority": priority,
        "input": {},
    }
    if input_json:
        try:
            payload["input"] = json.loads(input_json)
        except json.JSONDecodeError:
            _error("Invalid JSON in --input")
            raise typer.Exit(1)

    # Handle stdin-from: fetch previous task output
    if stdin_from:
        try:
            with _client() as client:
                resp = client.get(f"/api/tasks/{stdin_from}/result")
                resp.raise_for_status()
                prev_data = resp.json()
                if prev_data.get("status") == "pending":
                    _error(f"Task {stdin_from} is still pending. Wait for it to complete.")
                    raise typer.Exit(1)
                prev_output = prev_data.get("output", {})
                stdin_text = _extract_text(prev_output)
                payload["input"]["stdin"] = stdin_text
        except httpx.ConnectError:
            _server_unavailable()
            raise typer.Exit(1)
        except httpx.HTTPStatusError as exc:
            _error(f"Server error fetching previous task: {exc.response.text}")
            raise typer.Exit(1)

    try:
        with _client() as client:
            resp = client.post(f"/api/agents/{agent_name}/task", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    task_id = data.get("task_id")
    _success(
        f"Task submitted to [bold cyan]{agent_name}[/bold cyan]\n"
        f"• Task ID: [bold]{task_id}[/bold]\n"
        f"• Status: {data.get('status', 'submitted')}\n"
        f"{f'• Piped from: [cyan]{stdin_from}[/cyan]' if stdin_from else ''}\n"
        f"{f'• Will pipe to: [cyan]{pipe_to}[/cyan]' if pipe_to else ''}\n\n"
        f"Check status: [bold]skyn3t task status {task_id}[/bold]"
    )

    # Handle pipe-to: submit follow-up task automatically
    if pipe_to:
        try:
            with _client() as client:
                # Poll for task completion
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                    transient=True,
                ) as progress:
                    t = progress.add_task(f"Waiting for {agent_name} to complete...", total=None)
                    while True:
                        resp = client.get(f"/api/tasks/{task_id}/result")
                        resp.raise_for_status()
                        result_data = resp.json()
                        if result_data.get("status") != "pending":
                            progress.update(t, completed=True)
                            break
                        time_mod.sleep(1)
                        progress.update(t, advance=0)

                output = result_data.get("output", {})
                stdin_text = _extract_text(output)
                follow_payload = {
                    "title": f"Follow-up: {title}",
                    "description": f"Piped output from task {task_id}",
                    "priority": priority,
                    "input": {"stdin": stdin_text},
                }
                resp = client.post(f"/api/agents/{pipe_to}/task", json=follow_payload)
                resp.raise_for_status()
                pipe_data = resp.json()
                _success(
                    f"Piped task submitted to [bold cyan]{pipe_to}[/bold cyan]\n"
                    f"• Task ID: [bold]{pipe_data.get('task_id')}[/bold]\n"
                    f"• Status: {pipe_data.get('status', 'submitted')}"
                )
        except httpx.ConnectError:
            _server_unavailable()
            raise typer.Exit(1)
        except httpx.HTTPStatusError as exc:
            _error(f"Pipe error: {exc.response.text}")
            raise typer.Exit(1)


@task_app.command("status")
def task_status(
    task_id: str = typer.Argument(..., help="Task ID to check"),
    watch: bool = typer.Option(False, "--watch", "-w", help="Watch for completion"),
    interval: float = typer.Option(2.0, "--interval", help="Poll interval in seconds"),
) -> None:
    """🔍 Check task status and results."""

    def _fetch() -> dict[str, Any]:
        with _client() as client:
            resp = client.get(f"/api/tasks/{task_id}/result")
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}

    try:
        data = _fetch()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)

    if watch and data.get("status") == "pending":
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            t = progress.add_task("Waiting for task completion...", total=None)
            while data.get("status") == "pending":
                time_mod.sleep(interval)
                data = _fetch()
                progress.update(t, advance=0)
            progress.update(t, completed=True)

    success = data.get("success")
    if success is True:
        color = "green"
        label = "✅ Completed"
    elif success is False:
        color = "red"
        label = "❌ Failed"
    else:
        color = "yellow"
        label = "⏳ Pending"

    table = Table(show_header=False, box=box.SIMPLE)
    table.add_row("Task ID", task_id)
    table.add_row("Status", f"[bold {color}]{label}[/bold {color}]")
    if "execution_time_ms" in data:
        table.add_row("Execution Time", f"{data['execution_time_ms']:.2f} ms")
    if data.get("output"):
        table.add_row("Output", "")
        out = json.dumps(data["output"], indent=2, default=str)
        table.add_row("", Syntax(out, "json", theme="monokai", line_numbers=False))
    if data.get("error"):
        table.add_row("Error", f"[red]{data['error']}[/red]")

    console.print(
        Panel(table, title="[bold]🔍 Task Status[/bold]", border_style=color)
    )


# ---------------------------------------------------------------------------
# Pipeline commands
# ---------------------------------------------------------------------------


@pipeline_app.command("create")
def pipeline_create(
    name: str = typer.Option("pipeline", "--name", "-n", help="Pipeline name"),
    agents: str = typer.Option(..., "--agents", "-a", help="Comma-separated agent names (e.g., 'claude,kimi')"),
    prompts: str = typer.Option(..., "--prompts", "-p", help="Comma-separated prompts, quoted (e.g., \"'write a function','review it'\")"),
    run_now: bool = typer.Option(False, "--run", "-r", help="Run the pipeline immediately after creation"),
) -> None:
    """🔄 Create a pipeline of tasks."""
    agent_list = [a.strip() for a in agents.split(",")]
    # Parse prompts: split by comma but respect quoted strings
    prompt_list = _parse_prompts(prompts)

    if len(agent_list) != len(prompt_list):
        _error(
            f"Number of agents ({len(agent_list)}) must match number of prompts ({len(prompt_list)}).\n"
            "Usage: skyn3t pipeline create --agents \"claude,kimi\" --prompts \"'write a function','review it'\""
        )
        raise typer.Exit(1)

    payload = {
        "name": name,
        "agents": agent_list,
        "prompts": prompt_list,
        "run": run_now,
    }

    try:
        with _client() as client:
            resp = client.post("/api/pipeline", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    pipeline_id = data.get("pipeline_id")
    _success(
        f"Pipeline created\n"
        f"• Pipeline ID: [bold]{pipeline_id}[/bold]\n"
        f"• Name: [cyan]{name}[/cyan]\n"
        f"• Stages: {len(agent_list)}\n"
        f"• Status: {data.get('status', 'created')}\n\n"
        f"{'Pipeline is running...' if run_now else f'Run it: [bold]skyn3t pipeline run {pipeline_id}[/bold]' }"
    )

    if run_now:
        console.print(f"\n[dim]Watching pipeline {pipeline_id}...[/dim]")
        _watch_pipeline(pipeline_id)


@pipeline_app.command("run")
def pipeline_run(
    pipeline_id: str = typer.Argument(..., help="Pipeline ID to run"),
    watch: bool = typer.Option(False, "--watch", "-w", help="Watch for completion"),
) -> None:
    """▶️  Run a pipeline."""
    try:
        with _client() as client:
            resp = client.post(f"/api/pipeline/{pipeline_id}/run")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    _success(
        f"Pipeline execution started\n"
        f"• Pipeline ID: [bold]{pipeline_id}[/bold]\n"
        f"• Status: {data.get('status', 'running')}"
    )

    if watch:
        _watch_pipeline(pipeline_id)


@pipeline_app.command("status")
def pipeline_status(
    pipeline_id: str = typer.Argument(..., help="Pipeline ID to check"),
    watch: bool = typer.Option(False, "--watch", "-w", help="Watch for completion"),
    interval: float = typer.Option(2.0, "--interval", help="Poll interval in seconds"),
) -> None:
    """🔍 Check pipeline status."""
    if watch:
        _watch_pipeline(pipeline_id, interval=interval)
    else:
        _show_pipeline_status(pipeline_id)


def _show_pipeline_status(pipeline_id: str) -> None:
    try:
        with _client() as client:
            resp = client.get(f"/api/pipeline/{pipeline_id}")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    if data.get("error"):
        _error(data["error"])
        raise typer.Exit(1)

    status_color = {
        "completed": "green",
        "failed": "red",
        "running": "yellow",
        "pending": "dim",
    }.get(data.get("status", "pending"), "white")

    table = Table(show_header=False, box=box.SIMPLE)
    table.add_row("Pipeline ID", pipeline_id)
    table.add_row("Name", data.get("name", "-"))
    table.add_row("Status", f"[bold {status_color}]{data.get('status', 'unknown').upper()}[/bold {status_color}]")
    if data.get("final_output"):
        table.add_row("Final Output", data["final_output"][:500])

    console.print(
        Panel(table, title="[bold]🔍 Pipeline Status[/bold]", border_style=status_color)
    )

    stages = data.get("stages", [])
    if stages:
        stage_table = Table(
            title="[bold]📋 Pipeline Stages[/bold]",
            box=box.ROUNDED,
            header_style="bold magenta",
        )
        stage_table.add_column("Stage", justify="right")
        stage_table.add_column("Agent", style="cyan")
        stage_table.add_column("Status", style="bold")
        stage_table.add_column("Output")

        for stage in stages:
            st = stage.get("status", "pending")
            sc = {"completed": "green", "failed": "red", "running": "yellow", "pending": "dim"}.get(st, "white")
            out = (stage.get("output") or "-")[:60] + "..." if stage.get("output") else "-"
            stage_table.add_row(
                str(stage.get("stage_index", 0) + 1),
                stage.get("agent_name", "-"),
                f"[{sc}]{st}[/{sc}]",
                out,
            )
        console.print(stage_table)


def _watch_pipeline(pipeline_id: str, interval: float = 2.0) -> None:
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        t = progress.add_task("Running pipeline...", total=None)
        while True:
            try:
                with _client() as client:
                    resp = client.get(f"/api/pipeline/{pipeline_id}")
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.ConnectError:
                _server_unavailable()
                raise typer.Exit(1)

            status = data.get("status", "pending")
            if status in ("completed", "failed"):
                progress.update(t, completed=True)
                break
            time_mod.sleep(interval)
            progress.update(t, advance=0)

    _show_pipeline_status(pipeline_id)


def _parse_prompts(prompts_str: str) -> List[str]:
    """Parse a comma-separated string of prompts, respecting quoted segments."""
    lex = shlex.shlex(prompts_str, posix=True)
    lex.whitespace = ","
    lex.whitespace_split = True
    return [token.strip() for token in lex if token.strip()]


# ---------------------------------------------------------------------------
# RAG commands
# ---------------------------------------------------------------------------


@rag_app.command("add")
def rag_add(
    content: str = typer.Argument(..., help="Document content"),
    title: str = typer.Option("Untitled", "--title", "-t", help="Document title"),
    source: str = typer.Option("", "--source", "-s", help="Document source"),
    doc_type: str = typer.Option("text", "--type", help="Document type"),
) -> None:
    """📚 Add a document to the RAG knowledge base."""
    payload = {
        "content": content,
        "title": title,
        "source": source,
        "doc_type": doc_type,
    }

    try:
        with _client() as client:
            resp = client.post("/api/rag/add", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    ids = data.get("ids", [])
    _success(
        f"Document added to knowledge base\n"
        f"• Title: [bold]{title}[/bold]\n"
        f"• Type: {doc_type}\n"
        f"• Chunks: {len(ids)}\n"
        f"• IDs: {', '.join(str(i) for i in ids[:3])}{'...' if len(ids) > 3 else ''}"
    )


@rag_app.command("query")
def rag_query(
    query: str = typer.Argument(..., help="Query string"),
    n_results: int = typer.Option(5, "--results", "-n", help="Number of results"),
) -> None:
    """🔎 Query the RAG knowledge base."""
    payload = {"query": query, "n_results": n_results}

    try:
        with _client() as client:
            resp = client.post("/api/rag/query", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    answer = data.get("answer", "No answer returned.")
    sources = data.get("sources", [])

    console.print(
        Panel(answer, title="[bold cyan]🔎 RAG Answer[/bold cyan]", border_style="cyan")
    )

    if sources:
        table = Table(
            title="[bold]📚 Sources[/bold]",
            box=box.ROUNDED,
            header_style="bold magenta",
        )
        table.add_column("Title", style="cyan")
        table.add_column("Source", style="blue")
        table.add_column("Relevance", justify="right")
        for src in sources:
            table.add_row(
                src.get("title", "-"),
                src.get("source", "-"),
                f"{src.get('score', 0):.3f}",
            )
        console.print(table)


# ---------------------------------------------------------------------------
# GitHub commands
# ---------------------------------------------------------------------------


@github_app.command("explore")
def github_explore(
    repo: str = typer.Argument(..., help="Repository in owner/repo format"),
    task_type: str = typer.Option("repo_analysis", "--type", "-t", help="Task type"),
) -> None:
    """🐙 Explore a GitHub repository."""
    try:
        owner, repo_name = repo.split("/", 1)
    except ValueError:
        _error("Repository must be in 'owner/repo' format (e.g. 'torvalds/linux')")
        raise typer.Exit(1)

    payload = {
        "title": f"Analyze {repo}",
        "description": f"GitHub exploration of {repo}",
        "input": {
            "task_type": task_type,
            "owner": owner,
            "repo": repo_name,
        },
        "priority": 1,
    }

    try:
        with _client() as client:
            resp = client.post("/api/agents/github_explorer/task", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    task_id = data.get("task_id")
    _success(
        f"GitHub exploration task submitted\n"
        f"• Repository: [bold cyan]{repo}[/bold cyan]\n"
        f"• Task Type: {task_type}\n"
        f"• Task ID: [bold]{task_id}[/bold]\n\n"
        f"Check result: [bold]skyn3t task status {task_id}[/bold]"
    )


def _run_repo_scout_command(
    *,
    cadence: str,
    limit: int,
    queries: str,
    every: Optional[str],
    platforms: List[str],
    run_path: str,
    schedule_path: str,
    title: str,
) -> None:
    payload: Dict[str, Any] = {
        "cadence": cadence,
        "limit": limit,
        "queries": [item.strip() for item in queries.split(",") if item.strip()],
    }
    if platforms:
        payload["platforms"] = platforms

    path = run_path
    if every:
        payload["schedule_expr"] = every
        path = schedule_path

    try:
        with _client() as client:
            resp = client.post(path, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    if every:
        _success(
            f"Scheduled {title}\n"
            f"• Job ID: [bold]{data.get('job_id', 'unknown')}[/bold]\n"
            f"• Name: [cyan]{data.get('name', title.lower())}[/cyan]\n"
            f"• Schedule: {every}"
        )
        return

    if data.get("started") and not data.get("proposals"):
        _success(
            f"{title} started in background\n"
            f"• Poll: [cyan]GET /api/repo-scout/status[/cyan]\n"
            f"• State: {data.get('state', 'running')}"
        )
        return

    proposals = data.get("proposals") or []
    tbl = Table(title=f"{title} result", box=box.SIMPLE)
    tbl.add_column("Platform", style="dim")
    tbl.add_column("Repo", style="cyan")
    tbl.add_column("Lane", style="dim")
    tbl.add_column("License", style="dim")
    tbl.add_column("Proposal", style="dim")
    for item in proposals:
        tbl.add_row(
            str(item.get("platform") or ("github" if title == "GitHub scout" else "—")),
            str(item.get("repo") or "—"),
            str(item.get("lane") or "—"),
            str(item.get("license") or "—"),
            str(item.get("proposal_id") or "—"),
        )
    console.print(tbl)
    console.print(
        f"[dim]Filed {data.get('filed', 0)} proposal(s) from {data.get('candidates_seen', 0)} candidate repo(s).[/dim]"
    )


@github_app.command("scout")
def github_scout(
    cadence: str = typer.Option("daily", "--cadence", help="Scout cadence label"),
    limit: int = typer.Option(4, "--limit", "-l", help="Max proposals to file per run"),
    queries: str = typer.Option("", "--queries", help="Comma-separated fit-lane queries"),
    platforms: str = typer.Option(
        "",
        "--platforms",
        help="Optional comma-separated source platforms (github, gitlab, bitbucket)",
    ),
    every: Optional[str] = typer.Option(
        None,
        "--every",
        help="Schedule expression to make the scout recurring instead of running now",
    ),
) -> None:
    """Run or schedule the GitHub scout."""
    platform_list = [item.strip() for item in platforms.split(",") if item.strip()]
    multi_platform = bool(platform_list)
    _run_repo_scout_command(
        cadence=cadence,
        limit=limit,
        queries=queries,
        every=every,
        platforms=platform_list,
        run_path="/api/repo-scout/run" if multi_platform else "/api/github/scout/run",
        schedule_path="/api/repo-scout/schedule" if multi_platform else "/api/github/scout/schedule",
        title="Repo scout" if multi_platform else "GitHub scout",
    )


@scout_app.command("run")
def scout_run(
    cadence: str = typer.Option("daily", "--cadence", help="Scout cadence label"),
    limit: int = typer.Option(4, "--limit", "-l", help="Max proposals to file per run"),
    queries: str = typer.Option("", "--queries", help="Comma-separated fit-lane queries"),
    platforms: str = typer.Option(
        "github,gitlab,bitbucket",
        "--platforms",
        help="Comma-separated source platforms (github, gitlab, bitbucket)",
    ),
    every: Optional[str] = typer.Option(
        None,
        "--every",
        help="Schedule expression to make the scout recurring instead of running now",
    ),
) -> None:
    """Run or schedule the multi-source repo scout."""
    _run_repo_scout_command(
        cadence=cadence,
        limit=limit,
        queries=queries,
        every=every,
        platforms=[item.strip() for item in platforms.split(",") if item.strip()],
        run_path="/api/repo-scout/run",
        schedule_path="/api/repo-scout/schedule",
        title="Repo scout",
    )


# ---------------------------------------------------------------------------
# Exec command
# ---------------------------------------------------------------------------


@app.command()
def exec(
    agent: str = typer.Argument(..., help="Agent name to execute"),
    prompt: str = typer.Argument(..., help="Prompt to send (quote it)"),
    stdin_text: Optional[str] = typer.Option(None, "--stdin", "-s", help="Stdin text to pipe in"),
) -> None:
    """⚡ Quick one-off execution on an agent."""
    payload = {
        "prompt": prompt,
        "stdin": stdin_text,
    }

    try:
        with _client() as client:
            resp = client.post(f"/api/agents/{agent}/exec", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    if data.get("error") is not None:
        _error(data["error"])
        raise typer.Exit(1)

    response_text = data.get("output", "")
    console.print(
        Panel(
            response_text,
            title=f"[bold cyan]⚡ {agent}[/bold cyan]",
            border_style="cyan",
        )
    )


# ---------------------------------------------------------------------------
# Conversation command
# ---------------------------------------------------------------------------


@app.command()
def conversation(
    topic: str = typer.Argument(..., help="Conversation topic"),
    initiator: str = typer.Option("user", "--initiator", "-i", help="Initiator name"),
    participants: List[str] = typer.Option([], "--participant", "-p", help="Participant agent names"),
    rounds: int = typer.Option(3, "--rounds", "-r", help="Number of rounds"),
    prefer_cli: bool = typer.Option(False, "--prefer-cli", help="Prefer CLI agents if available"),
) -> None:
    """💬 Run a multi-agent conversation."""
    chosen = list(participants)
    if not chosen:
        try:
            with _client() as client:
                resp = client.get("/api/agents")
                resp.raise_for_status()
                agents_data = resp.json()
                all_agents = agents_data.get("agents", [])
                if prefer_cli:
                    cli_agents = [a["name"] for a in all_agents if a.get("cli_agent")]
                    if cli_agents:
                        chosen = cli_agents
                    else:
                        chosen = [a["name"] for a in all_agents]
                else:
                    chosen = [a["name"] for a in all_agents]
        except Exception:
            chosen = []

    if not chosen:
        _error("No participants specified and no agents found on server.")
        raise typer.Exit(1)

    payload = {
        "initiator": initiator,
        "participants": chosen,
        "topic": topic,
        "rounds": rounds,
    }

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            t = progress.add_task("Running multi-agent conversation...", total=None)
            with _client() as client:
                resp = client.post("/api/conversation", json=payload)
                resp.raise_for_status()
                data = resp.json()
            progress.update(t, completed=True)
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    conversation_data = data.get("conversation", [])

    console.print(
        Panel(
            f"Topic: [bold]{topic}[/bold]\n"
            f"Participants: {', '.join(chosen)}\n"
            f"Rounds: {rounds}",
            title="[bold cyan]💬 Conversation[/bold cyan]",
            border_style="cyan",
        )
    )

    for entry in conversation_data:
        agent = entry.get("agent", "unknown")
        response_text = entry.get("response", "")
        console.print(
            Panel(
                response_text,
                title=f"[bold magenta]Round {entry.get('round', 0)} • {agent}[/bold magenta]",
                border_style="magenta",
            )
        )

    console.print(
        f"\n[green]✅ Conversation completed with {len(conversation_data)} messages.[/green]"
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@schedule_app.command("list")
def schedule_list() -> None:
    """📅 List all scheduled jobs."""
    try:
        with _client() as client:
            resp = client.get("/api/schedule/jobs")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    jobs = data.get("jobs", [])
    if not jobs:
        console.print("[dim]No scheduled jobs[/dim]")
        return

    tbl = Table(title="Scheduled Jobs", box=box.SIMPLE)
    tbl.add_column("Name", style="cyan")
    tbl.add_column("Schedule", style="dim")
    tbl.add_column("Agent", style="dim")
    tbl.add_column("Next Run", style="dim")
    tbl.add_column("Runs", justify="right")
    for job in jobs:
        status_icon = "🟢" if job.get("enabled") else "🔴"
        tbl.add_row(
            f"{status_icon} {job.get('name', '?')}",
            job.get("schedule_expr", "?"),
            job.get("agent_name") or "—",
            job.get("next_run") or "—",
            str(job.get("run_count", 0)),
        )
    console.print(tbl)


@schedule_app.command("add")
def schedule_add(
    name: str = typer.Argument(..., help="Unique job name"),
    schedule_expr: str = typer.Option(..., "--every", help="Schedule expression, e.g. 'daily at 09:00' or 'every 5 minutes'"),
    agent_name: str = typer.Option("", "--agent", help="Agent to trigger"),
    prompt: str = typer.Option("", "--prompt", help="Prompt or topic to send"),
) -> None:
    """➕ Add a new scheduled job."""
    try:
        with _client() as client:
            resp = client.post("/api/schedule/jobs", json={
                "name": name,
                "schedule_expr": schedule_expr,
                "agent_name": agent_name or None,
                "prompt": prompt or None,
            })
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)
    _success(f"Scheduled job '{name}' created with ID {data.get('job_id', '?')}")


@schedule_app.command("remove")
def schedule_remove(
    job_id: str = typer.Argument(..., help="Job ID to delete"),
) -> None:
    """❌ Remove a scheduled job."""
    try:
        with _client() as client:
            resp = client.delete(f"/api/schedule/jobs/{job_id}")
            resp.raise_for_status()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)
    _success(f"Job {job_id} deleted")


@user_app.command("profile")
def user_profile(
    platform: str = typer.Option("cli", "--platform", help="Platform: cli, telegram, discord"),
    platform_id: str = typer.Option("default", "--id", help="User ID on the platform"),
    profile_json: Optional[str] = typer.Option(None, "--set", help="JSON string to merge into profile"),
) -> None:
    """👤 View or update a user profile."""
    try:
        with _client() as client:
            if profile_json:
                resp = client.patch(
                    f"/api/users/{platform}/{platform_id}",
                    json={"profile": json.loads(profile_json)},
                )
                resp.raise_for_status()
                console.print("[green]✅ Profile updated[/green]")
                return
            resp = client.get(f"/api/users/{platform}/{platform_id}")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            console.print(f"[dim]No profile found for {platform}/{platform_id}[/dim]")
            raise typer.Exit(1)
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    console.print(
        Panel(
            f"Platform: [bold]{data.get('platform')}[/bold]\n"
            f"ID: [bold]{data.get('platform_id')}[/bold]\n"
            f"Name: {data.get('display_name') or '—'}\n"
            f"Messages: {data.get('message_count', 0)}  Sessions: {data.get('session_count', 0)}\n"
            f"Profile: {json.dumps(data.get('profile', {}), indent=2)}",
            title="[bold cyan]User Profile[/bold cyan]",
            border_style="cyan",
        )
    )


@export_app.command("trajectories")
def export_trajectories(
    from_date: str = typer.Option("", "--from", help="Start date (YYYY-MM-DD)"),
    to_date: str = typer.Option("", "--to", help="End date (YYYY-MM-DD)"),
    agent: str = typer.Option("", "--agent", help="Filter by agent name"),
    outcome: str = typer.Option("", "--outcome", help="Filter by outcome: success, failure"),
    include_evaluations: bool = typer.Option(
        False,
        "--include-evaluations",
        help="Append approved evaluation assets to the exported JSONL bundle",
    ),
    output: str = typer.Option("trajectories.jsonl", "--output", "-o", help="Output file path"),
) -> None:
    """📤 Export agent trajectories to JSONL for analysis or training."""
    try:
        with _client() as client:
            params: Dict[str, Any] = {}
            if from_date:
                params["from_date"] = from_date
            if to_date:
                params["to_date"] = to_date
            if agent:
                params["agent"] = agent
            if outcome:
                params["outcome"] = outcome
            if include_evaluations:
                params["include_evaluations"] = True
            resp = client.get("/api/trajectories/export", params=params)
            resp.raise_for_status()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    with open(output, "wb") as fh:
        fh.write(resp.content)
    label = "trajectory bundle" if include_evaluations else "trajectories"
    console.print(f"[green]✅ Exported {label} to {output}[/green]")


@export_app.command("penpot")
def export_penpot(
    slug: str = typer.Argument(..., help="Studio project slug"),
    output: str = typer.Option("", "--output", "-o", help="Output zip path"),
) -> None:
    """📦 Export a Penpot-oriented design handoff package for a Studio project."""
    out_path = output or f"{slug}-penpot-handoff.zip"
    package_bytes: Optional[bytes] = None
    used_local_fallback = False
    try:
        with _client() as client:
            resp = client.get(f"/api/studio/projects/{slug}/design-handoff/penpot/package")
            resp.raise_for_status()
            package_bytes = resp.content
    except httpx.ConnectError:
        package_bytes = _local_penpot_package(slug)
        used_local_fallback = package_bytes is not None
        if package_bytes is None:
            _server_unavailable()
            raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            package_bytes = _local_penpot_package(slug)
            used_local_fallback = package_bytes is not None
            if package_bytes is None:
                _error(f"Server error: {exc.response.text}")
                raise typer.Exit(1)
        else:
            _error(f"Server error: {exc.response.text}")
            raise typer.Exit(1)

    with open(out_path, "wb") as fh:
        fh.write(package_bytes or b"")
    if used_local_fallback:
        console.print(
            f"[green]✅ Exported Penpot handoff package to {out_path}[/green]\n"
            "[dim]Used the local project artifacts because the API route was unavailable.[/dim]"
        )
    else:
        console.print(f"[green]✅ Exported Penpot handoff package to {out_path}[/green]")


# ---------------------------------------------------------------------------
# Search & Insights
# ---------------------------------------------------------------------------

@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    table: str = typer.Option("all", "--table", "-t", help="Table to search: messages, tasks, logs, all"),
    limit: int = typer.Option(20, "--limit", "-l", help="Max results"),
) -> None:
    """🔎 Full-text search over messages, tasks, and logs."""
    try:
        with _client() as client:
            resp = client.post("/api/search", json={"query": query, "table": table, "limit": limit})
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    results = data.get("results", [])
    if not results:
        console.print(f"[dim]No results for '{query}'[/dim]")
        return

    tbl = Table(title=f"Search: '{query}' ({data.get('count', 0)} results)", box=box.SIMPLE)
    tbl.add_column("Table", style="cyan", no_wrap=True)
    tbl.add_column("ID", style="dim", no_wrap=True)
    tbl.add_column("Summary", style="white")
    tbl.add_column("When", style="dim")

    for r in results:
        tname = r.get("table", "?")
        rid = r.get("id", "?")[:8]
        when = r.get("created_at", "—")
        if tname == "messages":
            summary = f"{r.get('source_agent', '?')} → {r.get('target_agent', '?')}: {r.get('content', '')[:80]}"
        elif tname == "tasks":
            summary = f"[{r.get('status', '?')}] {r.get('title', '')[:80]}"
        elif tname == "logs":
            summary = f"[{r.get('level', '?')}] {r.get('message', '')[:80]}"
        else:
            summary = str(r)[:80]
        tbl.add_row(tname, rid, summary, when)

    console.print(tbl)


@app.command()
def insights(
    days: int = typer.Option(7, "--days", "-d", help="Lookback period in days"),
) -> None:
    """📊 System insights: token usage, stage latency, task stats, agent health."""
    try:
        with _client() as client:
            resp = client.get("/api/insights", params={"days": days})
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    # Token usage
    tokens = data.get("tokens", {})
    totals = tokens.get("totals", {})
    console.print(
        Panel(
            f"Total tokens: [bold]{totals.get('total_tokens', 0):,}[/bold]  "
            f"Calls: [bold]{totals.get('total_calls', 0):,}[/bold]  "
            f"Agents tracked: {totals.get('agents_tracked', 0)}  "
            f"Projects: {totals.get('projects_tracked', 0)}",
            title="[bold cyan]Token Usage[/bold cyan]",
            border_style="cyan",
        )
    )

    per_agent = tokens.get("per_agent", [])
    if per_agent:
        tbl = Table(title="Per-Agent Tokens", box=box.SIMPLE)
        tbl.add_column("Agent", style="cyan")
        tbl.add_column("Tokens", justify="right")
        tbl.add_column("Calls", justify="right")
        tbl.add_column("Backend", style="dim")
        for a in per_agent[:10]:
            tbl.add_row(
                a.get("agent", "?"),
                f"{a.get('total_tokens', 0):,}",
                f"{a.get('calls', 0):,}",
                a.get("backend", "?"),
            )
        console.print(tbl)

    # Stage latency
    stages = data.get("stages", {})
    if stages:
        tbl = Table(title="Stage Latency", box=box.SIMPLE)
        tbl.add_column("Stage", style="cyan")
        tbl.add_column("Avg", justify="right")
        tbl.add_column("Min", justify="right")
        tbl.add_column("Max", justify="right")
        tbl.add_column("Runs", justify="right")
        for stage, stats in stages.items():
            tbl.add_row(
                stage,
                f"{stats.get('avg', 0):.1f}s",
                f"{stats.get('min', 0):.1f}s",
                f"{stats.get('max', 0):.1f}s",
                f"{stats.get('count', 0):,}",
            )
        console.print(tbl)

    # Tasks
    tasks = data.get("tasks", {})
    console.print(
        Panel(
            f"Success rate: [bold]{'%.1f' % (tasks.get('success_rate', 0) * 100)}%[/bold]  "
            f"Completed: [bold]{tasks.get('total_completed', 0)}[/bold]  "
            f"Failed: [bold]{tasks.get('total_failed', 0)}[/bold]  "
            f"Total: {tasks.get('total_tasks', 0)}",
            title="[bold cyan]Tasks[/bold cyan]",
            border_style="cyan",
        )
    )

    # Agent health
    agents = data.get("agents", [])
    if agents:
        tbl = Table(title="Agent Health", box=box.SIMPLE)
        tbl.add_column("Agent", style="cyan")
        tbl.add_column("Type", style="dim")
        tbl.add_column("Status")
        tbl.add_column("Queue", justify="right")
        for a in agents:
            status = a.get("status", "?")
            status_style = "green" if status == "idle" else "yellow" if status == "busy" else "red"
            tbl.add_row(
                a.get("name", "?"),
                a.get("type", "?"),
                f"[{status_style}]{status}[/{status_style}]",
                str(a.get("queue_size", 0)),
            )
        console.print(tbl)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@skills_app.command("list")
def skills_list() -> None:
    """List installed skills."""
    from skyn3t.intelligence.skill_library import get_default_library

    lib = get_default_library()
    skills = lib.all()
    if not skills:
        console.print("[dim]No skills installed.[/dim]")
        return
    tbl = Table(title="Skills", box=box.SIMPLE)
    tbl.add_column("Name", style="cyan")
    tbl.add_column("Score", justify="right")
    tbl.add_column("Tags", style="dim")
    tbl.add_column("Source")
    for s in skills:
        score_str = f"{s.score:+.2f}"
        score_style = "green" if s.score > 0 else "red" if s.score < 0 else "yellow"
        tbl.add_row(
            s.name,
            f"[{score_style}]{score_str}[/{score_style}]",
            ", ".join(s.tags[:3]),
            s.source,
        )
    console.print(tbl)
    console.print(f"\nTotal: {len(skills)} skill(s)")


@skills_app.command("search")
def skills_search(query: str) -> None:
    """Search skills by relevance."""
    from skyn3t.intelligence.skill_library import get_default_library

    lib = get_default_library()
    results = lib.find_relevant(query, limit=10)
    if not results:
        console.print(f"[dim]No skills match '{query}'.[/dim]")
        return
    tbl = Table(title=f"Search: {query}", box=box.SIMPLE)
    tbl.add_column("Name", style="cyan")
    tbl.add_column("Relevance", justify="right")
    tbl.add_column("Tags", style="dim")
    for s in results:
        rel = s.relevance(query)
        tbl.add_row(s.name, f"{rel:.1f}", ", ".join(s.tags[:3]))
    console.print(tbl)


@skills_app.command("candidates")
def skills_candidates(
    limit: int = typer.Option(20, "--limit", "-l", help="Number of candidates to show"),
) -> None:
    """List approved memory docs that can be turned into skill drafts."""
    try:
        with _client() as client:
            resp = client.get("/api/skills/candidates", params={"limit": limit})
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    candidates = data.get("candidates") or []
    if not candidates:
        console.print("[dim]No approved memory skill candidates.[/dim]")
        return
    tbl = Table(title="Skill candidates", box=box.SIMPLE)
    tbl.add_column("Doc ID", style="cyan", no_wrap=True)
    tbl.add_column("Type", style="dim")
    tbl.add_column("Confidence", justify="right")
    tbl.add_column("Title")
    for doc in candidates:
        meta = doc.get("meta") or {}
        confidence = meta.get("confidence")
        try:
            confidence_value = confidence if confidence is not None else 0.0
            confidence_str = f"{float(confidence_value):.2f}"
        except (TypeError, ValueError):
            confidence_str = "—"
        tbl.add_row(
            str(doc.get("id") or "—"),
            str(doc.get("doc_type") or "—"),
            confidence_str,
            str(doc.get("title") or "—"),
        )
    console.print(tbl)


@skills_app.command("drafts")
def skills_drafts() -> None:
    """List pending skill drafts."""
    try:
        with _client() as client:
            resp = client.get("/api/skills/drafts")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    drafts = data.get("drafts") or []
    if not drafts:
        console.print("[dim]No pending skill drafts.[/dim]")
        return
    tbl = Table(title="Skill drafts", box=box.SIMPLE)
    tbl.add_column("Slug", style="cyan")
    tbl.add_column("Memory doc", style="dim")
    tbl.add_column("Tags", style="dim")
    tbl.add_column("Name")
    for draft in drafts:
        tbl.add_row(
            str(draft.get("slug") or "—"),
            str(draft.get("memory_doc_id") or "—"),
            ", ".join(draft.get("tags") or []),
            str(draft.get("name") or "—"),
        )
    console.print(tbl)


@skills_app.command("draft")
def skills_draft(doc_id: str) -> None:
    """Create a pending skill draft from an approved memory document."""
    try:
        with _client() as client:
            resp = client.post(f"/api/skills/drafts/from-memory/{doc_id}")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    draft = data.get("draft") or {}
    _success(
        f"Created skill draft\n"
        f"• Memory doc: [bold]{doc_id}[/bold]\n"
        f"• Draft slug: [cyan]{draft.get('slug', 'unknown')}[/cyan]\n"
        f"• Draft name: {draft.get('name', 'unknown')}"
    )


@skills_app.command("approve-draft")
def skills_approve_draft(slug: str) -> None:
    """Promote a pending skill draft into the live library."""
    try:
        with _client() as client:
            resp = client.post(f"/api/skills/drafts/{slug}/approve")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    _success(
        f"Installed skill draft\n"
        f"• Skill: [cyan]{data.get('installed', slug)}[/cyan]"
    )


@skills_app.command("reject-draft")
def skills_reject_draft(
    slug: str,
    reason: str = typer.Option("", "--reason", "-r", help="Optional rejection reason"),
) -> None:
    """Reject and delete a pending skill draft."""
    try:
        with _client() as client:
            resp = client.post(f"/api/skills/drafts/{slug}/reject", json={"reason": reason})
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        _server_unavailable()
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        _error(f"Server error: {exc.response.text}")
        raise typer.Exit(1)

    draft = data.get("draft") or {}
    _success(
        f"Rejected skill draft\n"
        f"• Draft: [cyan]{draft.get('slug', slug)}[/cyan]"
    )


@skills_app.command("install")
def skills_install(source: str) -> None:
    """Install a skill from a local path or URL.

    SOURCE can be a local directory containing SKILL.md or a git URL.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from skyn3t.intelligence.skill_library import get_default_library

    lib = get_default_library()
    source_path = Path(source)

    if source_path.exists() and source_path.is_dir():
        path, findings = lib.import_agent_skill(source_path)
        if path:
            _success(f"Installed skill from {source}")
            if findings:
                console.print(f"[yellow]Warnings: {', '.join(findings)}[/yellow]")
        else:
            _error(f"Could not install skill from {source}")
            if findings:
                console.print(f"[red]Flagged: {', '.join(findings)}[/red]")
        return

    # Try git clone
    if source.startswith(("http://", "https://", "git@")):
        with tempfile.TemporaryDirectory() as tmp:
            console.print(f"Cloning {source} ...")
            result = shutil.which("git")
            if not result:
                _error("git is not installed")
                raise typer.Exit(1)
            import subprocess
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", source, tmp],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                _error(f"git clone failed: {proc.stderr}")
                raise typer.Exit(1)
            # Look for SKILL.md in the clone root or first subdir
            skill_md = Path(tmp) / "SKILL.md"
            if not skill_md.exists():
                subdirs = [d for d in Path(tmp).iterdir() if d.is_dir()]
                if subdirs:
                    skill_md = subdirs[0] / "SKILL.md"
            if not skill_md.exists():
                _error("No SKILL.md found in cloned repository")
                raise typer.Exit(1)
            path, findings = lib.import_agent_skill(skill_md.parent)
            if path:
                _success(f"Installed skill from {source}")
                if findings:
                    console.print(f"[yellow]Warnings: {', '.join(findings)}[/yellow]")
            else:
                _error("Could not install skill")
        return

    _error(f"Source not found: {source}")


@skills_app.command("remove")
def skills_remove(name: str) -> None:
    """Remove a skill by name."""
    from skyn3t.intelligence.skill_library import get_default_library

    lib = get_default_library()
    if lib.delete(name):
        _success(f"Removed skill '{name}'")
    else:
        _error(f"Skill '{name}' not found")


@skills_app.command("hub")
def skills_hub(
    install: bool = typer.Option(False, "--install", help="Install missing hub skills"),
) -> None:
    """List or install skills from the local Skills Hub (examples/skills_seed, skills/)."""
    from skyn3t.intelligence.skills_hub import install_from_hub, list_hub_entries

    catalog = list_hub_entries()
    if not install:
        console.print("[bold]Skills Hub roots[/bold]")
        for root in catalog.get("roots") or []:
            console.print(f"  • {root}")
        console.print(f"\nMarkdown skills: {len(catalog.get('markdown_skills') or [])}")
        console.print(f"Agent SKILL.md dirs: {len(catalog.get('agent_skill_dirs') or [])}")
        console.print("\nRun [cyan]skyn3t skills hub --install[/cyan] to install missing safe skills.")
        return

    result = install_from_hub(only_missing=True, reject_unsafe=True)
    installed = result.get("installed") or []
    if installed:
        _success(f"Installed {len(installed)} skill(s): {', '.join(installed[:8])}")
    else:
        console.print("[dim]No new hub skills to install.[/dim]")
    flagged = result.get("flagged") or []
    if flagged:
        console.print(f"[yellow]Flagged (unsafe): {len(flagged)}[/yellow]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
