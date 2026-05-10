"""SkyN3t CLI — Rich command-line interface for the orchestrator."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import time as time_mod
from typing import Any, Dict, List, Optional

import httpx
import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.syntax import Syntax
from rich.table import Table

from skyn3t.config.settings import get_settings
from skyn3t.studio.mission_setup import mission_setup_labels, normalize_mission_setup
from skyn3t.studio.repo_target import normalize_repo_target, resolve_repo_target

API_BASE = os.environ.get("SKYN3T_API_URL", "http://localhost:6660")

console = Console()
app = typer.Typer(
    name="skyn3t",
    help="🤖 SkyN3t Orchestrator CLI",
    rich_markup_mode="rich",
    no_args_is_help=False,
)


def _print_getting_started() -> None:
    """Show a compact first-run path instead of dropping into the REPL."""
    quickstart = Table(show_header=False, box=box.SIMPLE, pad_edge=False)
    quickstart.add_row("[bold cyan]skyn3t init[/bold cyan]", "Initialize data directories and the database")
    quickstart.add_row("[bold cyan]skyn3t start[/bold cyan]", "Start the API and dashboard on localhost:6660")
    quickstart.add_row("[bold cyan]skyn3t status[/bold cyan]", "Check whether the system is up")
    quickstart.add_row("[bold cyan]skyn3t project \"build a habit tracker\"[/bold cyan]", "Start a Studio run from one brief")
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


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """Show a guided entry screen when invoked with no subcommand."""
    if ctx.invoked_subcommand is None:
        _print_getting_started()


@app.command()
def repl() -> None:
    """💬 Launch the interactive REPL (Claude-Code-style swarm console)."""
    from skyn3t.cli.repl import run as run_repl
    run_repl()


@app.command()
def project(
    brief: str = typer.Argument(..., help="Describe the project you want SkyN3t to build"),
    template: str = typer.Option("auto", "--template", "-t", help="Studio template key; defaults to auto"),
    audience: str = typer.Option(
        "auto",
        "--audience",
        help="Mission audience: auto, general, builders, team, leaders, investors",
    ),
    autonomy: str = typer.Option(
        "move_fast",
        "--autonomy",
        help="Mission mode: balanced, confirm_first, move_fast",
    ),
    repo_path: str = typer.Option(
        "",
        "--repo-path",
        help="Optional local git repo path to target for code work",
    ),
    focus_file: str = typer.Option(
        "",
        "--focus-file",
        help="Optional repo-relative file path to focus code changes on",
    ),
) -> None:
    """🚀 Start a Studio project from one brief."""
    audience_map = {
        "auto": "",
        "general": "general",
        "builders": "builders",
        "team": "team",
        "leaders": "leaders",
        "investors": "investors",
    }
    audience = str(audience or "auto").strip().lower()
    autonomy = str(autonomy or "move_fast").strip().lower()
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
    try:
        with _client() as client:
            resp = client.post(
                "/api/studio/start",
                json={
                    "template": template,
                    "brief": brief,
                    "mission_setup": mission_setup,
                    "repo_target": repo_target,
                },
            )
            resp.raise_for_status()
            data = resp.json()
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
        "Watch it in the dashboard or use [bold]skyn3t repl[/bold] to keep chatting."
    )

agent_app = typer.Typer(help="Agent management commands", no_args_is_help=True)
task_app = typer.Typer(help="Task management commands", no_args_is_help=True)
pipeline_app = typer.Typer(help="Pipeline management commands", no_args_is_help=True)
rag_app = typer.Typer(help="RAG knowledge base commands", no_args_is_help=True)
github_app = typer.Typer(help="GitHub exploration commands", no_args_is_help=True)
proposal_app = typer.Typer(help="Self-update proposal review", no_args_is_help=True)

app.add_typer(agent_app, name="agent")
app.add_typer(task_app, name="task")
app.add_typer(pipeline_app, name="pipeline")
app.add_typer(rag_app, name="rag")
app.add_typer(github_app, name="github")
app.add_typer(proposal_app, name="proposal")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client() -> httpx.Client:
    return httpx.Client(base_url=API_BASE, timeout=30.0)


def _error(message: str) -> None:
    console.print(Panel(str(message) if message is not None else "Unknown error", title="[bold red]Error", border_style="red"))


def _success(message: str) -> None:
    console.print(Panel(message, title="[bold green]Success", border_style="green"))


def _server_unavailable() -> None:
    _error(
        "Could not connect to SkyN3t server at [bold]localhost:6660[/bold].\n"
        "Is the server running? Try: [bold]skyn3t start[/bold]"
    )


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
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="Host to bind to"),
    port: int = typer.Option(6660, "--port", "-p", help="Port to bind to"),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload"),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of worker processes"),
) -> None:
    """🚀 Start the SkyN3t orchestrator server."""
    import uvicorn

    banner = (
        f"[bold cyan]SkyN3t Orchestrator[/bold cyan]  [dim]v{get_settings().app_version}[/dim]\n"
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
    )


@app.command()
def init() -> None:
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
            _success(
                "System initialized successfully!\n"
                f"• Data directory: [cyan]{get_settings().data_dir}[/cyan]\n"
                f"• Logs directory: [cyan]{get_settings().logs_dir}[/cyan]\n"
                f"• Vector DB: [cyan]{get_settings().vector_db_path}[/cyan]"
            )
        except Exception as exc:
            progress.update(t, completed=True)
            _error(f"Initialization failed: {exc}")
            raise typer.Exit(1)


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


def main() -> None:
    app()
