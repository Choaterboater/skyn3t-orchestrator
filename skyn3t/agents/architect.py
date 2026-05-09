"""Architect Agent - scaffolds system architecture and tech stack docs.

LLM-free deterministic generator. Picks a stack based on ``target`` and fills
out an ``architecture.md`` plus ``tech_stack.json`` in the caller-provided
artifact directory. Future LLM-powered variants can subclass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus


_STACKS: Dict[str, Dict[str, Any]] = {
    "saas": {
        "frontend": "React + Vite + TypeScript + TailwindCSS",
        "backend": "FastAPI (Python 3.11) with async SQLAlchemy",
        "db": "PostgreSQL 16 with Alembic migrations",
        "infra": "Docker + Fly.io / Render; Cloudflare in front",
        "ci": "GitHub Actions: lint, test, build, deploy",
    },
    "site": {
        "frontend": "Next.js (App Router) static export",
        "backend": "None (static); contact form via Formspree",
        "db": "None (Markdown content collection)",
        "infra": "Cloudflare Pages or Vercel",
        "ci": "GitHub Actions: build + Lighthouse",
    },
    "mobile": {
        "frontend": "Expo + React Native + TypeScript",
        "backend": "Supabase (Postgres + Auth + Storage)",
        "db": "Supabase Postgres",
        "infra": "EAS Build + TestFlight + Play Internal",
        "ci": "GitHub Actions + EAS submit",
    },
    "cli": {
        "frontend": "Typer (Python) with Rich for output",
        "backend": "Local SQLite + JSON config in ~/.config",
        "db": "SQLite",
        "infra": "PyPI release; Homebrew tap optional",
        "ci": "GitHub Actions: pytest, mypy, build wheel",
    },
}


_RISKS_BY_TARGET: Dict[str, List[str]] = {
    "saas": [
        "Multi-tenant data isolation must be enforced at the query layer.",
        "Background jobs (emails, billing webhooks) need a durable queue.",
        "Auth misconfiguration is the most common day-1 incident.",
    ],
    "site": [
        "Content drift between code and CMS without a single source of truth.",
        "Image weight kills Core Web Vitals if not pre-optimized.",
        "Forms without spam protection get abused immediately.",
    ],
    "mobile": [
        "Store review timelines (1-3 days) gate every release.",
        "Offline-first sync conflicts need a defined merge strategy.",
        "Push notification permissions are increasingly opt-in only.",
    ],
    "cli": [
        "Cross-platform path handling (Windows vs POSIX) breaks naive code.",
        "Long-running commands need clear progress + cancel semantics.",
        "Auto-update is a footgun; prefer package managers.",
    ],
}


class ArchitectAgent(BaseAgent):
    """Scaffolds architecture artifacts for a project brief."""

    def __init__(
        self,
        name: str = "architect",
        event_bus: EventBus = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="architect",
            provider="local",
            event_bus=event_bus,
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="architecture",
                description="Produce architecture.md describing components, data, APIs, deployment.",
                parameters={"brief": "str", "target": "str", "artifact_dir": "str"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="system_design",
                description="Pick a tech stack and emit tech_stack.json keyed by target.",
                parameters={"target": "str", "artifact_dir": "str"},
            )
        )

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return bool(_STACKS)

    async def execute(self, task: TaskRequest) -> TaskResult:
        await self.think(f"{self.name} starting on {task.task_id}")

        data = task.input_data or {}
        brief: str = (data.get("brief") or "").strip() or "Untitled project"
        target: str = (data.get("target") or "saas").lower()
        if target not in _STACKS:
            target = "saas"
        artifact_dir = Path(data.get("artifact_dir") or ".")
        next_agent: Optional[str] = data.get("next_agent")

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=f"artifact_dir error: {e}")

        stack = _STACKS[target]
        await self.think(f"selected stack profile '{target}'")

        arch_md = self._render_architecture_md(brief, target, stack)
        arch_path = artifact_dir / "architecture.md"
        arch_path.write_text(arch_md, encoding="utf-8")
        await self.think(f"wrote {arch_path.name}")

        stack_path = artifact_dir / "tech_stack.json"
        stack_path.write_text(json.dumps(stack, indent=2), encoding="utf-8")
        await self.think(f"wrote {stack_path.name}")

        files = [str(arch_path), str(stack_path)]
        summary = f"Architecture for '{brief[:60]}' on {target} stack drafted."

        if next_agent:
            await self.send_message(
                to=next_agent,
                kind="info",
                content=f"{self.name} done; artifacts in {artifact_dir}",
                payload={"files": files, "stack": stack, "target": target},
            )

        await self.share_learning(
            f"Architect scaffold for target={target} works best with explicit risks section.",
            scope="global",
            target=target,
        )

        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"files": files, "stack": stack, "summary": summary},
        )

    def _render_architecture_md(self, brief: str, target: str, stack: Dict[str, Any]) -> str:
        words = [w for w in brief.replace(",", " ").split() if w]
        keywords = [w.strip(".").lower() for w in words[:8]] or ["product"]
        kw_line = ", ".join(keywords)

        components = self._components_for(target)
        data_model = self._data_model_for(target, keywords)
        apis = self._apis_for(target)
        deployment = self._deployment_for(target, stack)
        risks = _RISKS_BY_TARGET.get(target, [])

        lines: List[str] = []
        lines.append(f"# Architecture - {brief}\n")
        lines.append(f"_Target profile: **{target}**_\n")
        lines.append("## Overview\n")
        lines.append(
            f"This document scaffolds the architecture for: {brief}. "
            f"Keywords distilled from the brief: {kw_line}. "
            f"The chosen profile is `{target}`, which favors a {stack['frontend']} client "
            f"talking to a {stack['backend']} backend.\n"
        )

        lines.append("## Components\n")
        for c in components:
            lines.append(f"- **{c['name']}** - {c['desc']}")
        lines.append("")

        lines.append("## Data model\n")
        lines.append("Initial entities (rename to fit the domain):\n")
        for entity in data_model:
            lines.append(f"- `{entity['name']}` - {entity['fields']}")
        lines.append("")

        lines.append("## APIs\n")
        for api in apis:
            lines.append(f"- `{api['method']} {api['path']}` - {api['desc']}")
        lines.append("")

        lines.append("## Deployment\n")
        lines.append(deployment + "\n")

        lines.append("## Risks\n")
        for r in risks:
            lines.append(f"- {r}")
        lines.append("")

        lines.append("## Open questions\n")
        lines.append("- What is the single most important user outcome in week 1?")
        lines.append("- What measurable metric proves the system is working?")
        lines.append("- What are the hard scale targets (users, RPS, storage) for the first 90 days?\n")

        return "\n".join(lines)

    def _components_for(self, target: str) -> List[Dict[str, str]]:
        if target == "saas":
            return [
                {"name": "Web client", "desc": "React SPA, calls JSON API, handles auth state."},
                {"name": "API gateway", "desc": "FastAPI app exposing REST + OpenAPI schema."},
                {"name": "Domain services", "desc": "Modular business logic, async DB access."},
                {"name": "Worker", "desc": "Background jobs (email, billing, exports)."},
                {"name": "Database", "desc": "PostgreSQL with row-level tenant scoping."},
            ]
        if target == "site":
            return [
                {"name": "Static pages", "desc": "Next.js App Router output, CDN-served."},
                {"name": "Content collection", "desc": "Markdown/MDX in repo, typed front-matter."},
                {"name": "Forms relay", "desc": "Formspree or Cloudflare Workers endpoint."},
                {"name": "Analytics", "desc": "Plausible or Cloudflare Web Analytics."},
            ]
        if target == "mobile":
            return [
                {"name": "Expo app", "desc": "React Native screens, navigation, state."},
                {"name": "Supabase backend", "desc": "Auth, Postgres, storage, realtime."},
                {"name": "Push service", "desc": "Expo push tokens persisted server-side."},
                {"name": "Build pipeline", "desc": "EAS Build + submit for iOS/Android."},
            ]
        return [
            {"name": "CLI entry", "desc": "Typer app exposing subcommands."},
            {"name": "Config store", "desc": "JSON in ~/.config plus env overrides."},
            {"name": "Local DB", "desc": "SQLite for state and history."},
            {"name": "Plugin loader", "desc": "Entry-points group for third-party extensions."},
        ]

    def _data_model_for(self, target: str, keywords: List[str]) -> List[Dict[str, str]]:
        domain = keywords[0] if keywords else "item"
        if target == "saas":
            return [
                {"name": "User", "fields": "id, email, password_hash, created_at, tenant_id"},
                {"name": "Tenant", "fields": "id, name, plan, created_at"},
                {"name": domain.capitalize(), "fields": "id, tenant_id, title, body, created_at"},
                {"name": "Subscription", "fields": "id, tenant_id, stripe_id, status, current_period_end"},
            ]
        if target == "site":
            return [
                {"name": "Page", "fields": "slug, title, body_md, published_at"},
                {"name": "Post", "fields": "slug, title, body_md, tags, published_at"},
                {"name": "Submission", "fields": "id, form, payload_json, received_at"},
            ]
        if target == "mobile":
            return [
                {"name": "User", "fields": "id, email, push_token, created_at"},
                {"name": domain.capitalize(), "fields": "id, owner_id, payload_json, updated_at"},
                {"name": "Device", "fields": "id, user_id, platform, app_version, last_seen"},
            ]
        return [
            {"name": "Run", "fields": "id, command, args_json, started_at, finished_at, exit_code"},
            {"name": "Config", "fields": "key, value, scope"},
            {"name": "Cache", "fields": "key, value, expires_at"},
        ]

    def _apis_for(self, target: str) -> List[Dict[str, str]]:
        if target == "saas":
            return [
                {"method": "POST", "path": "/auth/login", "desc": "Issue session token."},
                {"method": "GET", "path": "/v1/items", "desc": "List entities for current tenant."},
                {"method": "POST", "path": "/v1/items", "desc": "Create entity."},
                {"method": "POST", "path": "/billing/webhook", "desc": "Stripe webhook receiver."},
            ]
        if target == "site":
            return [
                {"method": "GET", "path": "/", "desc": "Landing page."},
                {"method": "GET", "path": "/blog/[slug]", "desc": "Render markdown post."},
                {"method": "POST", "path": "/api/contact", "desc": "Forward to form relay."},
            ]
        if target == "mobile":
            return [
                {"method": "POST", "path": "/auth/otp", "desc": "Send one-time login code."},
                {"method": "GET", "path": "/sync", "desc": "Pull deltas since last sync token."},
                {"method": "POST", "path": "/sync", "desc": "Push local changes."},
            ]
        return [
            {"method": "CLI", "path": "init", "desc": "Create config + DB in ~/.config."},
            {"method": "CLI", "path": "run", "desc": "Execute the primary action."},
            {"method": "CLI", "path": "status", "desc": "Show recent runs."},
        ]

    def _deployment_for(self, target: str, stack: Dict[str, Any]) -> str:
        return (
            f"Deploy via {stack['infra']}. CI: {stack['ci']}. "
            "Promotion is trunk-based: every merge to main builds, runs tests, and "
            "deploys to staging; production is a manual approval step."
        )
