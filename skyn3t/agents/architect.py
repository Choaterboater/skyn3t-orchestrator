"""Architect Agent - scaffolds system architecture and tech stack docs.

Tries the configured LLM first to produce brief-aware architecture content;
falls back to the deterministic templates below when the LLM is unavailable
or returns a stub. Picks a stack based on ``target`` and fills out an
``architecture.md`` plus ``tech_stack.json`` in the caller-provided artifact
directory.
"""

from __future__ import annotations

import json
import re
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
        event_bus: EventBus | None = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="architect",
            provider="local",
            event_bus=event_bus or EventBus(),
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

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        await self.think(f"{self.name} starting on {task.task_id}")

        data = task.input_data or {}
        brief: str = (data.get("brief") or "").strip() or "Untitled project"
        target: str = (data.get("target") or "saas").lower()
        if target not in _STACKS:
            target = "saas"
        artifact_dir = self.resolve_artifact_dir(data.get("artifact_dir"))
        next_agent: Optional[str] = data.get("next_agent")

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=f"artifact_dir error: {e}")

        stack = _STACKS[target]
        await self.think(f"selected stack profile '{target}'")

        # STEP 0: try LLM for architecture.md, fall back to deterministic template.
        arch_role_prompt = (
            "You are a senior software architect. Given the user's brief, produce a "
            "concise markdown architecture document with these sections (## headings):\n"
            "- Overview (2-3 sentences)\n"
            "- Components (bullet list of services/modules with one-sentence purpose each)\n"
            "- Data model (key entities and relationships)\n"
            "- APIs (key endpoints if applicable)\n"
            "- Deployment (suggested infra, focused on what's needed for THIS specific project)\n"
            "- Risks (3-5 specific risks tied to the brief)\n\n"
            "Match the brief's actual scope - if it's a static HTML file, don't propose a "
            "SaaS backend. If it's a marketing campaign, focus on content infrastructure "
            "not microservices.\n\n"
            f"Target profile hint: {target}."
        )
        fallback_arch_md = self._render_architecture_md(brief, target, stack)
        arch_md = await self._llm_generate(
            role_prompt=arch_role_prompt,
            brief=brief,
            fallback=fallback_arch_md,
        )
        arch_path = artifact_dir / "architecture.md"
        arch_path.write_text(arch_md, encoding="utf-8")
        await self.think(f"wrote {arch_path.name}")

        # STEP 0: try LLM for tech_stack.json, fall back to deterministic template.
        stack_role_prompt = (
            "Given the brief, return a JSON object with keys: frontend, backend, db, infra, ci. "
            "Pick choices that match the actual scope of the brief. "
            "Return ONLY valid JSON, nothing else. No code fences, no commentary."
        )
        llm_stack = await self._llm_generate_json(
            role_prompt=stack_role_prompt,
            brief=brief,
            fallback=stack,
        )
        # Sanity-check: must be a dict with the expected keys; otherwise fall back.
        if isinstance(llm_stack, dict) and all(
            k in llm_stack for k in ("frontend", "backend", "db", "infra", "ci")
        ):
            stack = llm_stack

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

    async def _llm_generate(self, *, role_prompt: str, brief: str, fallback: str) -> str:
        """Ask the configured LLM for a markdown artifact.

        Returns the LLM output, or the deterministic ``fallback`` if the LLM is
        unavailable / returned a stub.
        """
        try:
            client = self.get_llm() if hasattr(self, "get_llm") else None
            if client is None:
                from skyn3t.adapters import LLMClient
                client = LLMClient(
                    default_model=self.config.get("model"),
                    backend=self.config.get("backend"),
                    event_bus=self.event_bus,
                    caller_name=self.name,
                )
            cot_preamble = (
                "Think step-by-step before writing:\n"
                "1. What's the actual scope here? (web app vs. static page vs. service)\n"
                "2. What's the bare minimum set of components needed?\n"
                "3. What's the data flow?\n"
                "4. What's the riskiest dependency?\n"
                "THEN produce the architecture document.\n\n"
            )
            prompt = (
                f"{cot_preamble}{role_prompt}\n\nBrief from user:\n{brief}\n\n"
                "Produce ONLY the markdown content for the artifact. "
                "No code fences, no preamble, no commentary."
            )
            try:
                skills_block = self.load_skills_for_prompt(
                    tags=["architect", "system-design", "integration", "backend"],
                    limit=3,
                )
                if skills_block:
                    prompt = prompt + skills_block
            except Exception:
                pass
            out = await client.complete(prompt, max_tokens=2500, temperature=0.4)
            if out and "[deterministic-stub]" not in out and len(out.strip()) > 80:
                return out.strip()
        except Exception:
            pass
        return fallback

    async def _llm_generate_json(
        self, *, role_prompt: str, brief: str, fallback: Any
    ) -> Any:
        """Ask the LLM for a JSON object; fall back if parsing fails or stub."""
        try:
            client = self.get_llm() if hasattr(self, "get_llm") else None
            if client is None:
                from skyn3t.adapters import LLMClient
                client = LLMClient(
                    default_model=self.config.get("model"),
                    backend=self.config.get("backend"),
                    event_bus=self.event_bus,
                    caller_name=self.name,
                )
            prompt = (
                f"{role_prompt}\n\nBrief from user:\n{brief}\n\n"
                "Return ONLY a valid JSON object. No code fences, no commentary."
            )
            out = await client.complete(prompt, max_tokens=800, temperature=0.4)
            if not out or "[deterministic-stub]" in out:
                return fallback
            text = out.strip()
            # Strip surrounding code fences if the model added them anyway.
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            # Best-effort: extract first {...} block if there's prose around it.
            if not text.lstrip().startswith("{"):
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if m:
                    text = m.group(0)
            parsed = json.loads(text)
            return parsed
        except Exception:
            return fallback
