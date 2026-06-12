"""Project-type → OpenRouter model routing.

Detects the *kind* of product the user is asking for (UI-heavy, backend,
CLI, data-viz, game, docs) from the brief and picks an OpenRouter model
ladder tuned to that kind of work.

Why per-type ladders matter: a single ladder optimized for React UI
will pick the wrong model for a Python CLI or a marketing site. By
routing based on classification, we let each model play to its strength
instead of forcing one tool through every domain.

Empirical baseline (from OpenRouter docs + my own testing):

* **Owl Alpha** — free, 1M ctx, agentic + general code. Strong default.
* **Qwen3-Coder** — explicit code specialist. Good for backend/CLI/types.
* **DeepSeek v3.2** — reliable everyday. Bigger files, less drift.
* **Mimo v2-Flash** — fast paid option for visual / UI work.
* **Mimo v2.5 Pro** — SWE-bench Pro leader. Premium fallback for the
  hardest single-file tasks.
* **Hunyuan-3 Preview** — Tencent's agentic model, strong reasoning.
* **GPT-5 Mini** — strict logic, predictable JSON output.

Classifier is regex-first (cheap, fast); falls back to a generic ladder
if nothing matches. No LLM call to classify — that would defeat the
point of routing in the first place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence, Tuple


@dataclass(frozen=True)
class RoutingProfile:
    """A named project type and the model ladder we route it to.

    The ladder is ordered QUALITY-FIRST (owner, 2026-06-12: build output
    quality over marginal cost — the old cheapest-first ladders meant
    "most calls land on the free model", which is exactly how output
    stayed flat). Free models remain as tail fallbacks for rate-limit
    escapes. The CodeAgent fourth-tier retry walks the ladder until a
    model returns valid output.
    """
    project_type: str
    ladder: Tuple[str, ...]
    notes: str


UI_HEAVY = RoutingProfile(
    project_type="ui_heavy",
    ladder=(
        "qwen/qwen3.6-flash",
        "qwen/qwen3-coder-next",
        "deepseek/deepseek-v3.2",
        "openrouter/owl-alpha",
    ),
    notes="React/Vue/Svelte SPAs, dashboards, visual-first apps",
)

BACKEND = RoutingProfile(
    project_type="backend",
    ladder=(
        "qwen/qwen3-coder-next",
        "deepseek/deepseek-v3.2",
        "openai/gpt-5-mini",
        "openrouter/owl-alpha",
    ),
    notes="Express/FastAPI/CLI/scripts, types and error handling matter",
)

DATA_VIZ = RoutingProfile(
    project_type="data_viz",
    ladder=(
        "qwen/qwen3.6-flash",
        "qwen/qwen3-coder-next",
        "deepseek/deepseek-v3.2",
        "openrouter/owl-alpha",
    ),
    notes="Charts, dashboards w/ live data, analytics views",
)

GAME = RoutingProfile(
    project_type="game",
    ladder=(
        "qwen/qwen3-coder-next",
        "deepseek/deepseek-v3.2",
        "tencent/hy3-preview",
        "openrouter/owl-alpha",
    ),
    notes="Canvas games, state machines, event-loop heavy",
)

DOCS = RoutingProfile(
    project_type="docs",
    ladder=(
        "openai/gpt-oss-120b:free",
        "deepseek/deepseek-v3.2",
        "openrouter/owl-alpha",
    ),
    notes="READMEs, docs sites, markdown content, blog-style copy",
)

GENERIC = RoutingProfile(
    project_type="generic",
    ladder=(
        "qwen/qwen3-coder-next",
        "qwen/qwen3.6-flash",
        "deepseek/deepseek-v3.2",
        "openrouter/owl-alpha",
    ),
    notes="Default ladder when project type isn't classified",
)


_PROFILES_BY_TYPE = {
    p.project_type: p for p in (UI_HEAVY, BACKEND, DATA_VIZ, GAME, DOCS, GENERIC)
}

_PROFILE_TIER = {
    "ui_heavy": "or_ui",
    "data_viz": "or_ui",
    "backend": "or_backend",
    "docs": "or_docs",
    "game": "or_cheap",
    "generic": "or_cheap",
}

_PROFILE_TASK_KIND = {
    "ui_heavy": "ui",
    "data_viz": "ui",
    "backend": "backend",
    "docs": "docs",
    "game": "general",
    "generic": "general",
}


_TYPE_PATTERNS: Sequence[Tuple[str, str]] = (
    ("game",      r"\b(?:game|puzzle|arcade|platformer|sudoku|chess|tic[-\s]?tac[-\s]?toe|"
                  r"snake|tetris|breakout|pong|wordle|connect\s*four|2048|"
                  r"canvas\s+game|interactive\s+(?:demo|sim|sketch))\b"),

    ("data_viz",  r"\b(?:analytics\s+(?:dashboard|tool|platform)|"
                  r"data\s+visuali[sz]ation|metrics\s+dashboard|chart(?:ing)?\s+(?:app|tool)|"
                  r"observability|grafana[-\s]?style|kpi\s+(?:dashboard|tracker)|"
                  r"financial\s+(?:dashboard|charts?))\b"),

    ("docs",      r"\b(?:documentation\s+site|docs\s+site|knowledge\s+base|"
                  r"readme\s+(?:generator|template)|marketing\s+(?:page|site|landing)|"
                  r"blog(?:\s+platform)?|landing\s+page|wiki)\b"),

    ("backend",   r"\b(?:cli\s+tool|command[-\s]?line(?:\s+(?:tool|app|utility))?|"
                  r"api\s+(?:server|service|gateway|for|to)|backend\s+service|"
                  r"rest\s+api|graphql\s+(?:api|server)|microservice|"
                  r"node[-\s]?cron|cron\s+(?:job|service)|"
                  r"script\s+(?:that|to|for)|automation\s+script|"
                  r"webhook\s+(?:handler|receiver|service)|"
                  r"python\s+(?:cli|script|tool)|"
                  r"express\s+(?:api|app|server|service)|"
                  r"fastapi\s+(?:api|app|server|service)|"
                  r"flask\s+(?:api|app|server|service)|"
                  r"nest(?:js)?\s+(?:api|app))\b"),

    ("ui_heavy",  r"\b(?:todo|task|habit(?:\s+tracker)?|kanban|notes?|recipe|"
                  r"finance\s+(?:tracker|app)|expense|budget|workout|fitness|"
                  r"journal|mood|reading\s+list|bookmark|crm|invoice|"
                  r"e[-\s]?commerce|shop(?:ping)?\s+(?:cart|app)|"
                  r"social\s+(?:network|feed|app)|chat\s+(?:app|ui)|"
                  r"dashboard|admin\s+panel|web\s+app|mobile\s+app|"
                  r"react(?:\s+(?:app|spa|component|page))?|vue\s+app|svelte\s+app|"
                  r"single[-\s]?page\s+app|spa\s+(?:for|with))\b"),
)


def classify_project(brief: str) -> str:
    """Classify the brief into one of the ``RoutingProfile`` project types.

    Returns the project_type string (``"ui_heavy"``, ``"backend"``, etc.)
    or ``"generic"`` if nothing matches.
    """
    if not brief:
        return GENERIC.project_type
    text = brief.lower()
    for project_type, pattern in _TYPE_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return project_type
    return GENERIC.project_type


def ladder_for_brief(brief: str) -> Tuple[str, ...]:
    """Convenience: classify and return the matching ladder in one call."""
    project_type = classify_project(brief)
    profile = _PROFILES_BY_TYPE.get(project_type, GENERIC)
    return _catalog_aware_ladder(profile.project_type, profile.ladder)


def ladder_for_file_and_brief(rel_path: str, brief: str) -> Tuple[str, ...]:
    """File-aware override: a backend file in a UI-heavy project still
    benefits from the backend ladder for that specific file. Mirrors
    how the existing CLI model router overrides per file type, but for
    OpenRouter models.

    Rules:
    * ``server/`` or ``api/`` paths or backend extensions → backend ladder
    * Markdown / docs files → docs ladder
    * Everything else → whatever the brief classifies as
    """
    rl = (rel_path or "").lower().replace("\\", "/")
    if not rl:
        return ladder_for_brief(brief)
    if rl.startswith(("server/", "api/")) or "/server/" in rl or "/api/" in rl:
        return _catalog_aware_ladder(BACKEND.project_type, BACKEND.ladder)
    if rl.endswith((".py", ".sh")):
        return _catalog_aware_ladder(BACKEND.project_type, BACKEND.ladder)
    if rl.endswith((".md", ".markdown")):
        return _catalog_aware_ladder(DOCS.project_type, DOCS.ladder)
    return ladder_for_brief(brief)


def profile_for_brief(brief: str) -> RoutingProfile:
    """Public lookup used by anything that wants the full profile (notes,
    type, ladder), not just the ladder tuple."""
    return _PROFILES_BY_TYPE.get(classify_project(brief), GENERIC)


def _catalog_aware_ladder(project_type: str, ladder: Tuple[str, ...]) -> Tuple[str, ...]:
    """Resolve a static ladder through the live OpenRouter catalog.

    OpenRouter's model list changes frequently. Keep the hand-curated
    ladder as a stable fallback, but prepend the current catalog/evolution
    winner for this project type and replace missing model ids when the
    local catalog knows a better fit.
    """
    resolved: list[str] = []
    tier = _PROFILE_TIER.get(project_type, "or_cheap")
    task_kind = _PROFILE_TASK_KIND.get(project_type, "general")
    try:
        from skyn3t.core.openrouter_catalog import (
            pick_best_model_for_task,
            resolve_openrouter_model,
        )

        best = pick_best_model_for_task(
            tier,
            task_kind,
            prefer_evolution=True,
        )
        if best:
            resolved.append(best)
        for model in ladder:
            candidate = resolve_openrouter_model(tier, model) or model
            resolved.append(candidate)
    except Exception:
        resolved.extend(ladder)

    deduped: list[str] = []
    for model in resolved:
        if model and model not in deduped:
            deduped.append(model)
    return tuple(deduped) or ladder
