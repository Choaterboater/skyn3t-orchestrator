"""Product-category detection + default-feature manifests.

The system used to require the user to spell out every feature in
the brief: "needs Cmd+K", "needs settings panel", "needs sparklines",
"needs dark theme", etc. That's the system asking the user to do the
designer's job.

This module fixes that by:

  1. **Detecting product category** from the brief — homelab
     dashboard, status dashboard, SaaS landing, CRUD app, etc.
  2. **Loading a defaults manifest per category** — the features a
     user IMPLICITLY wants for that product type, that competing
     products in the same category ship by default.

The detector returns a category slug. The defaults manifest returns
a list of "implicit features" the planner can use to auto-enrich
the brief before downstream agents see it. The user's explicit
asks always win — defaults only fill the gaps.

Why this is the right shape:
- It's keyed by what KIND of product, not by what the user typed.
- The defaults catalog is editable per category; can be extended
  without touching agent code.
- It's transparent: the auto-enriched features are logged so the
  user can see what got added.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

_SPARSE_EXPANSION_MARKER = "## Auto-expanded product baseline"


@dataclass(frozen=True)
class ProductCategoryDefaults:
    """Default feature set for a product category."""

    slug: str
    label: str
    # Features the user almost certainly wants. Each is a short
    # sentence that gets appended to the brief verbatim. Phrased as
    # "the product has X" — not as bullet points — so the LLM reads
    # them as already-decided requirements.
    implicit_features: List[str] = field(default_factory=list)
    # Aesthetic baseline. Same shape as implicit_features but
    # specifically about look-and-feel.
    aesthetic_baseline: List[str] = field(default_factory=list)


# Trigger phrases per category. Order matters — the first category
# whose triggers all match wins. Most-specific first.
_CATEGORY_TRIGGERS: List[Tuple[str, Tuple[str, ...]]] = [
    # Homelab status dashboard — the v15-v28 use case
    ("homelab_dashboard", (
        "homelab", "home lab", "home-lab", "self-hosted dashboard",
        "self hosted dashboard", "homarr", "heimdall", "dashy",
        "service status dashboard", "media stack dashboard",
    )),
    # Generic operations / status dashboard
    ("status_dashboard", (
        "status dashboard", "uptime dashboard", "monitoring dashboard",
        "ops dashboard", "noc dashboard", "service health",
    )),
    # Analytics dashboard (charts-heavy, metrics-driven)
    ("analytics_dashboard", (
        "analytics dashboard", "metrics dashboard", "kpi dashboard",
        "telemetry dashboard", "bi dashboard", "business intelligence",
    )),
    # SaaS marketing landing page
    ("saas_landing", (
        "saas landing", "marketing site", "marketing page",
        "landing page for a", "product landing page", "pricing page",
    )),
    # Internal CRUD admin
    ("crud_admin", (
        "admin panel", "crud admin", "internal tool",
        "back office", "back-office", "data management ui",
    )),
    # Documentation site
    ("docs_site", (
        "documentation site", "docs site", "knowledge base",
        "developer docs", "api documentation",
    )),
]


_CATEGORY_DEFAULTS: Dict[str, ProductCategoryDefaults] = {
    "homelab_dashboard": ProductCategoryDefaults(
        slug="homelab_dashboard",
        label="Homelab status dashboard",
        implicit_features=[
            "Cmd+K (Ctrl+K on Windows/Linux) opens a global command palette that "
            "filters across services, devices, and settings; pressing Enter on a "
            "result jumps to that service's drawer.",
            "Top-of-page strip with a time-aware greeting ('Good morning/afternoon/"
            "evening, <user>') and a horizontal row of 3-5 KPI tiles (services online, "
            "total bandwidth, storage used, active alerts).",
            "Each service is rendered as a CARD with its real brand icon (SVG from "
            "dashboard-icons or simple-icons CDN), brand color accents, a status pill "
            "(Online/Warning/Offline with colored dot), one line of concrete numbers "
            "(NOT a JSON dump), an optional inline sparkline for recent activity, and "
            "an action row with open/refresh/settings icon buttons + a 'X ago' last-"
            "checked stamp.",
            "Clicking a card opens a RIGHT-SIDE DRAWER (slide-in, 480px wide, "
            "Esc/click-outside closes) showing per-service detail: stats grid, "
            "recent items list or poster grid, action buttons.",
            "Settings panel: per-service editor with URL/host/port/API-key fields, "
            "Test Connection button, enable/disable toggle. Settings persist to disk "
            "so changes survive a server restart.",
            "Sidebar navigation between dashboard pages (Overview, Analytics, "
            "Integrations marketplace, Settings).",
            "Activity feed: a small right-rail list of recent events ('Plex Media "
            "Server started', 'qBittorrent connection lost', 'High CPU usage') with "
            "timestamps.",
            "Service category filters (All / Media / Network / Smart Home / "
            "Monitoring / Utilities) as pill buttons above the service grid.",
            "Mobile responsive — cards stack to a single column at <768px, drawer "
            "becomes full-screen.",
        ],
        aesthetic_baseline=[
            "Dark theme by default (slate-950 background, slate-900 cards, "
            "slate-700 borders). Optional light theme toggle.",
            "Inter (or comparable modern sans-serif) for body and headings. "
            "JetBrains Mono / ui-monospace for numbers, code, paths.",
            "Tabular numerals everywhere a value is shown so columns align.",
            "12px border-radius on cards, 8px on pills and pills, soft shadows "
            "(0 1px 3px rgba(0,0,0,0.3)).",
            "Per-service brand color used SPARINGLY: status dot, sparkline stroke, "
            "icon background tint at ~8% opacity. Card chrome stays neutral.",
            "Aesthetic reference points: Homarr, Heimdall, Linear, Vercel. "
            "Avoid: cyberpunk, matrix terminal, 'NOC console', monospace as the "
            "primary body font.",
        ],
    ),

    "status_dashboard": ProductCategoryDefaults(
        slug="status_dashboard",
        label="Service status / uptime dashboard",
        implicit_features=[
            "Service grid where each card shows status (Online/Degraded/Offline), "
            "latency, uptime %, and a small sparkline of response times.",
            "Color-coded health: green/amber/red dots paired with text labels (never "
            "color-only — colorblind-safe).",
            "Incident timeline / history view per service.",
            "Big top-level health summary tile (overall % of services up).",
        ],
        aesthetic_baseline=[
            "Dark theme, dense tabular layout, monospace for latency numbers.",
            "Inter or similar sans for the rest.",
        ],
    ),

    "analytics_dashboard": ProductCategoryDefaults(
        slug="analytics_dashboard",
        label="Analytics / KPI dashboard",
        implicit_features=[
            "Top strip of KPI tiles (4-6 metric tiles) with the most important "
            "numbers + delta-vs-last-period.",
            "Time-range selector (1H / 6H / 24H / 7D / 30D) that drives every chart.",
            "Big primary time-series chart (recharts area chart, dark theme).",
            "Smaller secondary charts: bar charts for breakdowns, donut for share-of.",
            "Filterable data table beneath the charts.",
        ],
        aesthetic_baseline=[
            "Dark theme, clean sans-serif, chart colors from a 4-6 color brand palette.",
            "Tabular numerals in tables and KPI values.",
        ],
    ),

    "saas_landing": ProductCategoryDefaults(
        slug="saas_landing",
        label="SaaS marketing landing page",
        implicit_features=[
            "Hero with headline + subheadline + primary CTA button.",
            "Logo bar (3-6 customer logos).",
            "3-column feature grid with icons.",
            "Pricing table (3 tiers, recommended one highlighted).",
            "Testimonial section.",
            "FAQ accordion.",
            "Footer with nav + social links.",
            "Sticky navbar that becomes solid on scroll.",
        ],
        aesthetic_baseline=[
            "Modern sans-serif (Inter or Geist). Generous whitespace. Subtle "
            "gradient accents. Light theme by default.",
            "Reference points: Linear, Stripe, Vercel landing pages.",
        ],
    ),

    "crud_admin": ProductCategoryDefaults(
        slug="crud_admin",
        label="Internal CRUD admin",
        implicit_features=[
            "Sidebar nav with resource list (Users, Orders, etc).",
            "Per-resource: filterable + sortable table, row actions (edit/delete), "
            "create-new button, detail/edit drawer or modal.",
            "Search across resources.",
            "Bulk actions on selected rows.",
            "Login / auth screen.",
        ],
        aesthetic_baseline=[
            "Dense, table-first, minimal chrome. Dark or light theme — match the "
            "user's existing tools.",
        ],
    ),

    "docs_site": ProductCategoryDefaults(
        slug="docs_site",
        label="Documentation site",
        implicit_features=[
            "Left nav with sections + subsections, collapsible.",
            "Search across all docs.",
            "Code blocks with syntax highlighting + copy button.",
            "Versioned content (vN.N selector).",
            "Edit-on-github link per page.",
            "Mobile responsive — nav becomes hamburger.",
        ],
        aesthetic_baseline=[
            "Light theme with dark-theme toggle. Serif or quality sans for body. "
            "Reference points: Stripe docs, Linear docs, Mintlify.",
        ],
    ),
}


def detect_category(brief: str) -> str:
    """Return the slug of the best-matching product category, or 'unknown'.

    Conservative: returns 'unknown' when no trigger matches — caller
    should treat that as 'no defaults to apply'.
    """
    if not brief:
        return "unknown"
    text = brief.lower()
    for slug, triggers in _CATEGORY_TRIGGERS:
        for t in triggers:
            if t in text:
                return slug
    return "unknown"


def defaults_for(category: str) -> ProductCategoryDefaults:
    """Return the defaults manifest for a category. Empty manifest
    for 'unknown' so callers can chain without conditionals."""
    if category in _CATEGORY_DEFAULTS:
        return _CATEGORY_DEFAULTS[category]
    return ProductCategoryDefaults(slug="unknown", label="Unknown")


def enrich_brief(brief: str) -> Tuple[str, ProductCategoryDefaults]:
    """Append category-default features to the brief.

    Returns (enriched_brief, defaults_used). The user's original
    brief is preserved verbatim at the top; defaults are appended in
    a clearly-labeled section so downstream agents see them but
    nothing is hidden from the user when they look at project.json.

    If category is 'unknown' the brief is returned unchanged.
    """
    cat = detect_category(brief)
    defaults = defaults_for(cat)
    if cat == "unknown" or (not defaults.implicit_features and not defaults.aesthetic_baseline):
        return brief, defaults
    lines: List[str] = [
        brief.rstrip(),
        "",
        "---",
        "",
        f"## Default features for this product category ({defaults.label})",
        "",
        "These are features users IMPLICITLY expect for this product type. "
        "The user did not have to list them; they're added automatically "
        "based on the product category. Treat them as part of the brief.",
        "",
    ]
    if defaults.implicit_features:
        lines.append("### Features")
        for f in defaults.implicit_features:
            lines.append(f"- {f}")
        lines.append("")
    if defaults.aesthetic_baseline:
        lines.append("### Aesthetic baseline")
        for f in defaults.aesthetic_baseline:
            lines.append(f"- {f}")
        lines.append("")
    return "\n".join(lines), defaults


def expand_sparse_brief(brief: str) -> str:
    """Expand one-line briefs into a minimal product baseline.

    Two paths:
    1. **LLM expansion (OpenRouter)** — when ``OPENROUTER_API_KEY`` is
       configured, ask a cheap model to write a real 150-200 word
       product spec tailored to this specific brief. Better signal for
       the architect downstream.
    2. **Template expansion (offline fallback)** — appends a static
       baseline so the architect always sees production-grade hints.

    The expansion is marker-guarded to stay idempotent across retries.
    """
    text = (brief or "").strip()
    if not text:
        return text
    if _SPARSE_EXPANSION_MARKER in text:
        return text

    words = [w for w in text.split() if w.strip()]
    # Don't expand briefs the user already wrote in detail.
    if len(words) > 14:
        return text

    # Try LLM expansion first; fall through to template on any error.
    llm_text = _llm_expand_brief(text)
    if llm_text:
        return f"{text}\n\n{_SPARSE_EXPANSION_MARKER}\n{llm_text}"

    baseline = [
        "Primary user flow is complete end-to-end (create/read/update/delete where applicable).",
        "Ship auth-ready foundations (session handling, protected routes, and profile/settings entry points).",
        "Include production basics: input validation, clear error states, loading/empty states, and structured logs.",
        "Generate API + UI with matching contracts (request/response shapes must align).",
        "Include runnable setup docs and an environment template so the project boots without manual guesswork.",
    ]
    return (
        f"{text}\n\n"
        f"{_SPARSE_EXPANSION_MARKER}\n"
        + "\n".join(f"- {line}" for line in baseline)
    )


def _llm_expand_brief(brief: str) -> str:
    """Ask OpenRouter to expand a terse brief into a 150-200 word spec.

    Returns the expansion body (no header markers — the caller wraps it).
    Returns empty string on any failure so the template path can take over.
    Uses ``openrouter/owl-alpha`` — free, 1M context, agentic-focused.
    """
    import os
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        try:
            from skyn3t.config.settings import get_settings
            api_key = getattr(get_settings(), "openrouter_api_key", None)
            if api_key:
                os.environ.setdefault("OPENROUTER_API_KEY", api_key)
        except Exception:  # noqa: BLE001
            api_key = None
    if not api_key:
        return ""

    prompt = f"""User brief: "{brief.strip()}"

Expand this terse brief into a 150-200 word product specification. Output ONLY a structured list (no preamble, no headings). Include exactly these sections, in this order:

- **Target user**: one sentence describing who uses this
- **Primary flow**: the single most important user journey, end-to-end, in 2-3 sentences
- **Core features**: 4-6 bullets, each one concrete and testable (e.g. "user can mark a habit complete with one tap on the day's cell")
- **Out of scope**: 2-3 things this version explicitly does NOT include (helps architect bound the work)
- **Success criteria**: 2 measurable signals (e.g. "user adds a habit in <10 seconds", "streak count visible on every habit card")

Output format: plain markdown bullets only. Each section starts with `**Label**:` on its own line, followed by the content.
Do not include the verbatim brief. Do not add commentary. Do not wrap output in code fences."""

    try:
        import asyncio

        from skyn3t.adapters import LLMClient

        async def _run() -> str:
            from skyn3t.core.model_router import resolve_model

            backend, model = resolve_model("planner")
            client = LLMClient(
                default_model=model,
                backend=backend,
                caller_name="product_categories",
                backend_is_policy=bool(backend),
            )
            try:
                out = await client.complete(
                    prompt,
                    system=(
                        "You are a senior product manager turning vague briefs into "
                        "actionable specs. Be concrete and specific. No marketing fluff."
                    ),
                    max_tokens=600,
                    temperature=0.2,
                    timeout=45.0,
                )
                return (out or "").strip()
            finally:
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001
                    pass

        # Run synchronously — this function is called from a sync code path.
        try:
            # Probe for a running loop. We don't need the loop object, just
            # the RuntimeError it raises when there isn't one.
            asyncio.get_running_loop()
            # Already inside an async context — run the coro in a thread
            # so we don't deadlock on the existing loop.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _run())
                return future.result(timeout=60) or ""
        except RuntimeError:
            # No running loop — safe to call asyncio.run directly.
            return asyncio.run(_run()) or ""
    except Exception:  # noqa: BLE001
        logger.debug("LLM brief expansion failed; falling back to template", exc_info=True)
        return ""
