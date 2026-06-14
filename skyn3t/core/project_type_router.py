"""Project-type → OpenRouter model routing.

Detects the *kind* of product the user is asking for (UI-heavy, backend,
CLI, data-viz, game, docs) from the brief and picks an OpenRouter model
ladder tuned to that kind of work.

Why per-type ladders matter: a single ladder optimized for React UI
will pick the wrong model for a Python CLI or a marketing site. By
routing based on classification, we let each model play to its strength
instead of forcing one tool through every domain.

Empirical baseline (refreshed against the current OpenRouter catalog —
newer + cheaper preferred over entrenched older ids):

* **DeepSeek V4 Flash** — newest DeepSeek, 1M ctx, ~$0.10/1M in. Cheapest
  capable everyday driver; replaced the older v3.2.
* **Qwen3.7 Plus** — current Qwen general flagship, 1M ctx, ~$0.32/1M in.
  Cheaper than the older qwen3-coder-plus and a newer point release.
* **Qwen3 Coder Flash** — fast, cheap code-leaning option for visual/UI work.
* **Owl Alpha** — free, 1M ctx, agentic + general code. Tail fallback.
* **gpt-oss-120b:free** — free docs/long-form fallback.

NOTE: the static ladders below are only the fast-path fallback. The live
path (``_catalog_aware_ladder``) scores the on-disk catalog via the model
evolution scorer, which now prefers newer + cheaper models automatically.

Classifier is regex-first (cheap, fast); falls back to a generic ladder
if nothing matches. No LLM call to classify — that would defeat the
point of routing in the first place.
"""

from __future__ import annotations

import os
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


# NOTE: these static ladders are the FAST-PATH FALLBACK only — the primary
# path is ``_catalog_aware_ladder`` which scores the live catalog. Every id
# below is validated present in the on-disk catalog cache
# (data/openrouter_models.json) and refreshed to current, newer+cheaper
# models (qwen3.7-plus / deepseek-v4-flash class) rather than the entrenched
# qwen3.6-flash / qwen3-coder-next / deepseek-v3.2 ids. A :free model is the
# tail fallback so rate-limited keys still make progress.
UI_HEAVY = RoutingProfile(
    project_type="ui_heavy",
    ladder=(
        # deepseek-v4-flash first: ~5s + reliable. qwen3.7-plus was first here
        # and timed out (~43s) / returned truncated bodies on real builds,
        # stalling every UI file before the fallthrough. Demoted to fallback.
        "deepseek/deepseek-v4-flash",
        "qwen/qwen3.7-plus",
        "qwen/qwen3-coder-flash",
        "openrouter/owl-alpha",
    ),
    notes="React/Vue/Svelte SPAs, dashboards, visual-first apps",
)

BACKEND = RoutingProfile(
    project_type="backend",
    ladder=(
        "deepseek/deepseek-v4-flash",
        "qwen/qwen3.7-plus",
        "qwen/qwen3-coder-flash",
        "openrouter/owl-alpha",
    ),
    notes="Express/FastAPI/CLI/scripts, types and error handling matter",
)

DATA_VIZ = RoutingProfile(
    project_type="data_viz",
    ladder=(
        # deepseek-v4-flash first (fast + reliable); qwen3.7-plus demoted to
        # fallback — it timed out / truncated on real builds (see ui_heavy).
        "deepseek/deepseek-v4-flash",
        "qwen/qwen3.7-plus",
        "qwen/qwen3-coder-flash",
        "openrouter/owl-alpha",
    ),
    notes="Charts, dashboards w/ live data, analytics views",
)

GAME = RoutingProfile(
    project_type="game",
    ladder=(
        "deepseek/deepseek-v4-flash",
        "qwen/qwen3.7-plus",
        "qwen/qwen3-coder-flash",
        "openrouter/owl-alpha",
    ),
    notes="Canvas games, state machines, event-loop heavy",
)

DOCS = RoutingProfile(
    project_type="docs",
    ladder=(
        "deepseek/deepseek-v4-flash",
        "qwen/qwen3.7-plus",
        "openai/gpt-oss-120b:free",
        "openrouter/owl-alpha",
    ),
    notes="READMEs, docs sites, markdown content, blog-style copy",
)

GENERIC = RoutingProfile(
    project_type="generic",
    ladder=(
        "deepseek/deepseek-v4-flash",
        "qwen/qwen3.7-plus",
        "qwen/qwen3-coder-flash",
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
            free_first_ladder,
            list_free_models,
            pick_best_model_for_task,
            resolve_openrouter_model,
        )

        free_only = os.environ.get("SKYN3T_FREE_ONLY", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        best = pick_best_model_for_task(tier, task_kind, prefer_evolution=True)
        if free_only:
            # $0-key policy: free models first (rotate on rate-limits), then the
            # scorer winner + curated ladder as fallbacks (deduped below).
            resolved.extend(free_first_ladder(task_kind))
            if best:
                resolved.append(best)
            for model in ladder:
                resolved.append(resolve_openrouter_model(tier, model) or model)
        else:
            # Funded: QUALITY-FIRST. Lead with the curated reliable ladder
            # (deepseek-v4-flash / qwen3.7-plus class) + the scorer winner; free
            # models only as a rate-limit fallback. Leading with free OR the
            # absolute-cheapest paid models churns on truncated "unusable body"
            # responses and ships TODO-stub files (regressed a build to 9 stubs).
            for model in ladder:
                resolved.append(resolve_openrouter_model(tier, model) or model)
            if best:
                resolved.append(best)
            resolved.extend(list_free_models()[:3])
    except Exception:
        resolved.extend(ladder)

    deduped: list[str] = []
    for model in resolved:
        if model and model not in deduped:
            deduped.append(model)
    return tuple(deduped) or ladder
