"""Curated competitor catalog for Cortex scout and adaptation proposals.

Maps known agent-orchestrator repos to SkyN3t target areas so scout
ingest → adaptation proposals are actionable instead of generic.
Inspired by Hermes, MetaSwarm, Forge, Railyard, Ark, OpenClaw, Paperclip.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# repo slug (lowercase) → metadata. Keep license-safe: patterns only, no code copy.
_COMPETITORS: Dict[str, Dict[str, Any]] = {
    "nousresearch/hermes-agent": {
        "name": "Hermes Agent",
        "patterns": [
            "multi-channel messaging gateway",
            "cron scheduled agent runs",
            "Modal/Daytona/SSH execution backends",
            "Skills Hub with slash commands",
            "persistent external memory providers",
        ],
        "skyn3t_targets": [
            "skyn3t/channels/",
            "skyn3t/skills/",
            "skyn3t/security/sandbox.py",
            "skyn3t/scheduler/",
        ],
        "priority": "high",
    },
    "dsifry/metaswarm": {
        "name": "MetaSwarm",
        "patterns": [
            "BEADS git-native task tracking",
            "TDD enforcement gates",
            "spec-driven SDLC with parallel review",
            "cross-model adversarial review",
        ],
        "skyn3t_targets": [
            "skyn3t/studio/runner.py",
            "skyn3t/agents/reviewer.py",
            "skyn3t/studio/templates.py",
        ],
        "priority": "high",
    },
    "artaeon/forge-ai": {
        "name": "Forge",
        "patterns": [
            "six orchestration modes (parallel, chain, review, swarm)",
            "duo planner+coder pipeline with verify loops",
            "pipeline resume state and rollback",
            "per-agent cost tracking and budget caps",
            "persistent cross-run memory",
        ],
        "skyn3t_targets": [
            "skyn3t/studio/runner.py",
            "skyn3t/observability/token_tracker.py",
            "skyn3t/self_healing/budget.py",
        ],
        "priority": "high",
    },
    "zulandar/railyard": {
        "name": "Railyard",
        "patterns": [
            "git worktree isolation per track",
            "Yardmaster supervisor for stall detection",
            "semantic code search MCP overlay",
            "multi-CLI engine dispatch per track",
        ],
        "skyn3t_targets": [
            "skyn3t/studio/runner.py",
            "skyn3t/cortex/repo_scout.py",
        ],
        "priority": "medium",
    },
    "ytarasova/ark": {
        "name": "Ark",
        "patterns": [
            "DAG SDLC flows with fan-out and verification gates",
            "session resume/fork/clone lifecycle",
            "multi-runtime compute (local, container, K8s)",
            "automatic git worktree per session",
        ],
        "skyn3t_targets": [
            "skyn3t/studio/runner.py",
            "skyn3t/security/sandbox.py",
        ],
        "priority": "medium",
    },
    "flexnetos/atc": {
        "name": "ATC",
        "patterns": [
            "worktree dispatch with SQLite registry",
            "six-signal agent health monitoring",
            "resume dispatches with transcript continuity",
        ],
        "skyn3t_targets": [
            "skyn3t/studio/runner.py",
            "skyn3t/core/self_healing.py",
        ],
        "priority": "medium",
    },
    "ruah-dev/ruah-orch": {
        "name": "Ruah",
        "patterns": [
            "file claim locks before agent execution",
            "DAG workflow with contract enforcement pre-merge",
            "durable task artifacts",
        ],
        "skyn3t_targets": [
            "skyn3t/studio/runner.py",
            "skyn3t/agents/code_agent.py",
        ],
        "priority": "medium",
    },
    "manufosela/karajan-code": {
        "name": "Karajan",
        "patterns": [
            "MCP server exposing pipeline tools",
            "Chrome DevTools UI verification",
            "22-role pipeline with SonarQube audit",
        ],
        "skyn3t_targets": [
            "skyn3t/agents/build_verifier.py",
            "skyn3t/web/acp/",
        ],
        "priority": "medium",
    },
    "openclaw/openclaw": {
        "name": "OpenClaw",
        "patterns": [
            "nested sub-agent spawn with depth limits",
            "SQLite meta-skill DAG runner with pause/resume",
            "TaskFlow durable parent job records",
            "gateway lifecycle and operator cancel",
        ],
        "skyn3t_targets": [
            "skyn3t/core/orchestrator.py",
            "skyn3t/skills/",
            "skyn3t/studio/runner.py",
        ],
        "priority": "high",
    },
    "paperclipai/paperclip": {
        "name": "Paperclip",
        "patterns": [
            "agent company packages with departments",
            "approval-gated autonomous writes",
            "PR automation and release orchestration",
        ],
        "skyn3t_targets": [
            "skyn3t/cortex/",
            "skyn3t/studio/approval_gate.py",
        ],
        "priority": "medium",
    },
    "garrytan/gbrain": {
        "name": "gbrain",
        "patterns": [
            "persistent agent memory graph",
            "lesson attribution across sessions",
        ],
        "skyn3t_targets": [
            "skyn3t/memory/",
            "skyn3t/rag/",
        ],
        "priority": "low",
    },
    "steveyegge/beads": {
        "name": "BEADS",
        "patterns": [
            "git-native issue and knowledge tracking CLI",
            "task DAG with dependency ordering",
        ],
        "skyn3t_targets": [
            "skyn3t/studio/runner.py",
            "skyn3t/memory/store.py",
        ],
        "priority": "medium",
    },
}

# Extra scout queries targeting competitor feature areas (deduped with settings defaults).
_COMPETITIVE_SCOUT_QUERIES: List[str] = [
    "hermes agent multi channel gateway cron skills",
    "metaswarm beads spec driven tdd multi agent",
    "forge ai orchestrator cost tracking resume pipeline",
    "git worktree agent orchestrator parallel coding",
    "openclaw subagent meta skill dag orchestration",
    "paperclip agent company approval autonomous",
    "mcp server coding agent pipeline tools",
]


def normalize_repo_slug(repo: str) -> str:
    return str(repo or "").strip().lower()


def match_competitor(repo: str) -> Optional[Dict[str, Any]]:
    """Return competitor metadata when ``repo`` matches the catalog."""
    slug = normalize_repo_slug(repo)
    if not slug:
        return None
    entry = _COMPETITORS.get(slug)
    if entry is None:
        return None
    return {"repo": slug, **entry}


def competitive_scout_queries() -> List[str]:
    """Scout fit queries aimed at competitor feature discovery."""
    return list(_COMPETITIVE_SCOUT_QUERIES)


def all_competitor_slugs() -> List[str]:
    return sorted(_COMPETITORS.keys())


def build_competitive_adaptation_brief(
    repo: str,
    *,
    description: str = "",
    ingested_paths: Optional[List[str]] = None,
) -> Optional[str]:
    """Structured adaptation brief when ``repo`` is a known competitor."""
    match = match_competitor(repo)
    if match is None:
        return None

    name = str(match.get("name") or repo)
    patterns = list(match.get("patterns") or [])
    targets = list(match.get("skyn3t_targets") or [])
    priority = str(match.get("priority") or "medium")
    paths = ingested_paths or []

    lines = [
        f"Competitive intel: adapt **{name}** (`{repo}`) patterns into SkyN3t.",
        f"Priority: {priority}. Borrow workflow ideas only — do not copy code verbatim.",
        "",
        "## Patterns to evaluate",
    ]
    lines.extend(f"- {pattern}" for pattern in patterns)
    lines.extend(
        [
            "",
            "## SkyN3t integration targets",
        ]
    )
    lines.extend(f"- `{target}`" for target in targets)
    if description.strip():
        lines.extend(["", "## Scout description", description.strip()[:500]])
    if paths:
        lines.extend(["", "## Ingested docs", ", ".join(paths[:8])])
    lines.extend(
        [
            "",
            "## Acceptance",
            "- Propose a minimal diff that closes one concrete gap vs this competitor.",
            "- Preserve SkyN3t verification sandwich and approval gates.",
            "- Add or extend tests for the adopted behavior.",
        ]
    )
    return "\n".join(lines)


def extract_readme_signals(text: str) -> List[str]:
    """Pull competitor-relevant keywords from README text for pattern tagging."""
    if not text:
        return []
    lowered = text.lower()
    signals: List[str] = []
    keyword_map = {
        "worktree": "git worktree isolation",
        "docker": "container sandbox",
        "mcp": "MCP tool surface",
        "cron": "scheduled runs",
        "gateway": "messaging gateway",
        "resume": "pipeline resume",
        "checkpoint": "pipeline checkpoint",
        "budget": "cost/token budget",
        "tdd": "TDD enforcement",
        "dag": "DAG workflow",
        "sub-agent": "nested sub-agents",
        "subagent": "nested sub-agents",
        "skill": "skills ecosystem",
        "rag": "RAG memory",
        "sqlite": "SQLite persistence",
    }
    for token, label in keyword_map.items():
        if token in lowered and label not in signals:
            signals.append(label)
    return signals[:8]


def competitive_practice_brief() -> Optional[str]:
    """Rotate a small Studio brief that exercises a competitor pattern in isolation."""
    import random

    slugs = all_competitor_slugs()
    if not slugs:
        return None
    repo = random.choice(slugs)
    match = match_competitor(repo)
    if match is None:
        return None
    name = str(match.get("name") or repo)
    pattern = (match.get("patterns") or ["workflow automation"])[0]
    return (
        f"Autonomous competitive drill: build a minimal runnable app demonstrating "
        f"«{pattern}» inspired by {name} ({repo}). Ship scaffold/ only — do not modify SkyN3t."
    )


def merge_scout_fit_queries(base_queries: List[str]) -> List[str]:
    """Dedupe base operator queries with competitive discovery queries."""
    seen: set[str] = set()
    merged: List[str] = []
    for query in [*base_queries, *competitive_scout_queries()]:
        normalized = re.sub(r"\s+", " ", str(query or "").strip().lower())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(str(query).strip())
    return merged
