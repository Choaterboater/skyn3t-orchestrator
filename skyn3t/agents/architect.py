"""Architect Agent - scaffolds system architecture and tech stack docs.

Tries the configured LLM first to produce brief-aware architecture content;
falls back to the deterministic templates below when the LLM is unavailable
or returns a stub. Picks a stack based on ``target`` and fills out an
``architecture.md`` plus ``tech_stack.json`` in the caller-provided artifact
directory.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.architect")

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
        # The previous prompt let the LLM over-promise (AES-256-GCM
        # encryption, node-cron schedulers, Next.js + Hono — none of
        # which CodeAgent actually shipped). Result: every canary's
        # reviewer LLM correctly flagged the drift and deducted ~10–20
        # points. This prompt now forbids over-promising and pins the
        # architect to what's in the chosen stack template.
        _stack_picks = ", ".join(
            f"{k}={v}" for k, v in stack.items() if v
        )
        arch_role_prompt = (
            "You are a senior software architect writing an architecture doc that "
            "the CodeAgent will implement verbatim. CodeAgent ships exactly what's "
            "in the chosen stack template — nothing more. So this document MUST be "
            "honest about scope.\n\n"
            "Stack the swarm will actually build with:\n"
            f"  {_stack_picks}\n\n"
            "Produce markdown with these sections (## headings):\n"
            "- Overview (2-3 sentences)\n"
            "- Components (bullet list of services/modules with one-sentence purpose each)\n"
            "- Data model (key entities and relationships)\n"
            "- APIs (key endpoints if applicable)\n"
            "- Deployment (only describe infra that fits the chosen stack)\n"
            "- Risks (3-5 specific risks tied to the brief)\n\n"
            "RULES (non-negotiable — violations cause downstream review failures):\n"
            "- This is a Node/TypeScript scaffold. NEVER mention Python, FastAPI, "
            "Flask, Django, SQLAlchemy, Alembic, Pydantic, Celery, ruff, pytest, "
            "or any other Python tooling. CodeAgent cannot scaffold these and the "
            "reviewer LLM will flag the architecture-vs-scaffold drift every time. "
            "Stack the swarm will actually build with:\n"
            f"  {_stack_picks}\n"
            "- Do NOT mention frameworks/libraries that aren't in the stack list above. "
            "If the stack says backend=express, do NOT promise Hono, Fastify, or Nest. "
            "If db=better-sqlite3, do NOT promise Postgres / Prisma / Drizzle.\n"
            "- Do NOT promise features the stack doesn't deliver — no AES-256-GCM "
            "encryption (write 'plaintext storage; out of scope for this stack'), "
            "no cron/scheduler unless the stack includes a scheduling lib, no auth "
            "flow unless the stack lists an auth lib, no Alembic/migrations unless "
            "the stack lists a migration tool.\n"
            "- Do NOT mention Cloudflare, Fly.io, Render, AWS, Vercel deploy manifests "
            "unless they are in the infra slot. Local docker-compose and 'self-hosted "
            "on user's machine' is the default deployment story.\n"
            "- If the brief asks for a feature the stack lacks, write `(out of scope "
            "for this stack)` next to it rather than pretending it ships.\n"
            "- Match the brief's actual scope — if it's a static HTML file, don't "
            "propose a SaaS backend. If it's a marketing campaign, focus on content "
            "infrastructure not microservices.\n\n"
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

        # STEP 0: try LLM for tech_stack.json. The previous version
        # allowed pairwise-incoherent picks (e.g. backend=fastapi +
        # db=better-sqlite3 → Python framework + Node lib). canary-116/117
        # both produced "fastapi" as a literal npm dependency, breaking
        # `npm install` outright. The fix is to constrain to COHERENT
        # BUNDLES that CodeAgent has stack templates for, not to free
        # pick from per-slot lists.
        stack_role_prompt = (
            "Given the brief, return a JSON object with keys: frontend, backend, db, "
            "infra, ci.\n\n"
            "Pick from EXACTLY one of these coherent bundles (CodeAgent only knows "
            "how to scaffold these — anything else will silently downgrade and the "
            "scaffold will not match the manifest):\n\n"
            "ALLOWED BUNDLES (Node only — CodeAgent's Python scaffold templates "
            "are not yet implemented; FastAPI/Flask picks would silently downgrade "
            "to Express and corrupt package.json):\n"
            "  - {frontend: 'react-vite-tailwind', backend: 'express', db: 'better-sqlite3', infra: 'docker-compose', ci: 'github-actions'}\n"
            "  - {frontend: 'react-vite-tailwind', backend: 'express', db: 'none', infra: 'docker-compose', ci: 'github-actions'}\n"
            "  - {frontend: 'react-vite', backend: 'express', db: 'better-sqlite3', infra: 'local-node', ci: 'github-actions'}\n"
            "  - {frontend: 'react-vite', backend: 'hono-node', db: 'better-sqlite3', infra: 'docker-compose', ci: 'github-actions'}\n"
            "  - {frontend: 'next', backend: 'next', db: 'better-sqlite3', infra: 'vercel', ci: 'github-actions'}\n"
            "  - {frontend: 'vue-vite', backend: 'express', db: 'better-sqlite3', infra: 'local-node', ci: 'github-actions'}\n"
            "  - {frontend: 'vanilla-vite', backend: 'none', db: 'none', infra: 'vercel', ci: 'github-actions'}\n\n"
            "RULES:\n"
            "- NEVER pick fastapi, flask, django, or any Python backend. CodeAgent\n"
            "  cannot scaffold these and the result will be Express with 'fastapi'\n"
            "  listed as a literal npm dep — `npm install` will fail outright.\n"
            "- NEVER pick a backend+db combo from different languages.\n"
            "- If the brief mentions a feature that isn't in any bundle (e.g. Hono\n"
            "  + Postgres), pick the CLOSEST bundle and accept the substitution\n"
            "  rather than inventing new values.\n"
            "- 'none' means the role is genuinely not needed (e.g. a CLI tool sets\n"
            "  frontend=none).\n\n"
            "Return ONLY valid JSON for one bundle, nothing else. No code fences."
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

        # Deterministic sanitizer: Claude Opus consistently ignores the
        # "NEVER mention Python/FastAPI" prompt rule when writing the
        # architecture doc for "homelab dashboard" briefs. The result is
        # a 10-15 point reviewer LLM deduction every canary for
        # architecture↔scaffold drift. We can't out-prompt training data,
        # so rewrite the artifact in-place instead. Stack-mismatched tech
        # mentions get neutralized before any downstream agent reads them.
        try:
            sanitized_md = self._sanitize_architecture_md(arch_md, stack)
            if sanitized_md != arch_md:
                arch_path.write_text(sanitized_md, encoding="utf-8")
                await self.think(
                    f"sanitized {arch_path.name}: stripped stack-mismatched tech mentions"
                )
        except Exception:
            logger.exception("architecture.md sanitization failed (non-fatal)")

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

    # ------------------------------------------------------------------
    # Architecture sanitizer (deterministic post-LLM cleanup)
    # ------------------------------------------------------------------

    # Tech-name terms that mean the architecture is describing a Python
    # stack we don't actually scaffold. When ANY of these appears in a
    # sentence, the ENTIRE sentence gets dropped from the markdown rather
    # than substituted.
    #
    # canary-123 showed why substitution is the wrong approach:
    # case-insensitive word replacement created franken-prose like "ASGI
    # app served by node", "better-sqlite3 2.x via asyncpg", and "eslint
    # + eslint". The reviewer LLM penalized those as harshly as the
    # original FastAPI mentions, so the sanitizer netted zero gain.
    #
    # Sentence-drop is conservative but never invents nonsense. The
    # reviewer sees a slightly thinner architecture doc rather than a
    # cross-language word-salad.
    _NODE_STACK_DROP_TERMS: List[str] = [
        # Python frameworks
        "FastAPI", "Flask", "Django", "Starlette",
        # ORM / migrations / serialization
        "SQLAlchemy", "Alembic", "Pydantic",
        # Async runtime / WSGI/ASGI servers
        "Celery", "uvicorn", "gunicorn", "asgi", "wsgi", "asyncpg",
        # Language / runtime mentions
        "Python 3.11", "Python 3.12", "Python 3", "Python ",
        # Lint/test tooling
        "ruff", "pytest", "mypy",
        # DBs we don't ship
        "PostgreSQL", "Postgres", "asyncpg",
        # Deploy targets we don't ship configs for
        "Cloudflare", "Fly.io", "Render ", "AWS Lambda", "Heroku",
    ]

    @classmethod
    def _sanitize_architecture_md(cls, body: str, stack: Dict[str, Any]) -> str:
        """Strip stack-mismatched tech mentions from architecture.md.

        Used post-LLM because Claude Opus consistently writes FastAPI +
        PostgreSQL + Alembic into homelab-dashboard architectures even
        when the prompt explicitly forbids it. We can't out-prompt the
        training-data prior, so we rewrite the file in place.

        Conservative: only acts when the stack is clearly Node-backed.
        Python scaffolds (when they exist) pass through unchanged.

        Strategy: drop entire sentences that mention any Python-only
        tech rather than substituting words. canary-123 proved that
        case-insensitive substitution creates franken-prose ("ASGI app
        served by node", "eslint + eslint") that the reviewer LLM
        penalizes as harshly as the original FastAPI mentions. The
        sentence-drop approach loses some context but never invents
        nonsense.
        """
        if not body or not isinstance(stack, dict):
            return body

        backend = str(stack.get("backend") or "").lower()
        node_backends = {
            "express", "express-node", "hono", "hono-node",
            "fastify", "koa", "nestjs", "next",
        }
        if backend not in node_backends:
            return body  # not a Node stack — don't touch

        out = body
        for needle in cls._NODE_STACK_DROP_TERMS:
            # `_strip_sentences_mentioning` is a no-op if the needle
            # isn't present, so we don't need an outer guard.
            out = cls._strip_sentences_mentioning(out, needle)

        # Collapse extra blank lines that the sentence drops leave behind.
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out

    # Sentence end: `.`, `!`, `?` followed by space + capital letter, OR
    # end of line. Crucially, `Python 3.11` does NOT match this — the
    # period is inside a version number, not a sentence boundary.
    _SENTENCE_END_RE = re.compile(r"[.!?](?=\s+[A-Z]|\s*$|\s*\n)")

    @classmethod
    def _strip_sentences_mentioning(cls, body: str, needle: str) -> str:
        """Drop sentences (NOT lines) containing ``needle``.

        canary-123 lesson: substitution creates franken-prose. Naive
        period-based sentence drop misfires on ``Python 3.11`` (the
        version-number dot reads as a sentence end). Line-drop is too
        aggressive and loses unrelated content on the same line.

        Solution: split on sentence boundaries that REQUIRE space + capital
        after the punctuation, so version numbers and acronyms don't split
        the sentence prematurely. Then drop only the sentences containing
        the needle.
        """
        if not body or not needle:
            return body
        nlower = needle.lower()
        # Process paragraph-by-paragraph to preserve markdown structure
        # (headings, list bullets, blank lines).
        paragraphs = body.split("\n\n")
        kept_paragraphs: List[str] = []
        for para in paragraphs:
            if nlower not in para.lower():
                kept_paragraphs.append(para)
                continue
            # Split into sentences. _SENTENCE_END_RE matches just the
            # boundary punctuation, not the following whitespace, so we
            # use re.split with a capturing group to keep the punctuation
            # attached to each piece.
            pieces = re.split(r"(?<=[.!?])(?=\s+[A-Z])", para)
            kept_pieces = [p for p in pieces if nlower not in p.lower()]
            # If we dropped everything, leave the paragraph out.
            if not kept_pieces:
                continue
            # If we kept the same number, the needle was inside a piece
            # that the splitter merged — try a coarser drop: split on
            # any `. ` boundary and accept the version-number false-pos.
            if len(kept_pieces) == len(pieces):
                pieces = re.split(r"(?<=[.!?])\s+", para)
                kept_pieces = [p for p in pieces if nlower not in p.lower()]
                if not kept_pieces:
                    continue
            kept_paragraphs.append(" ".join(p.strip() for p in kept_pieces if p.strip()))
        return "\n\n".join(kept_paragraphs)

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
