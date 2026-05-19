"""Stack scaffold templates — known-good file trees per ecosystem.

CodeAgent's two-phase scaffold used to ask the LLM to invent the file
tree on every project. The LLM is fine at writing file contents but
unreliable at picking the right shape (e.g. Next.js 13+ uses ``app/``,
not ``pages/``; FastAPI projects usually need a ``src/`` layout + tests
folder; iOS demands an Xcode .xcodeproj wrapper that an LLM never
produces correctly).

This module gives CodeAgent a deterministic skeleton per stack. The LLM
still writes each file's body, but starts from a known-good plan.

Each template returns a list of `(relative_path, one-line_purpose)`
tuples. ``detect_stack(brief)`` picks the right template from the brief
text using simple keyword + verb heuristics (no regex gymnastics).

Stacks supported today:
    - static_site          : index.html + style.css + script.js + README
    - python_cli           : main.py + requirements.txt + README
    - fastapi              : src/main.py + src/__init__.py + requirements.txt
                             + tests/test_health.py + README + .env.example
    - flask                : app.py + requirements.txt + templates/index.html
                             + static/style.css + README
    - node_cli             : index.js + package.json + README
    - react_vite           : index.html + src/main.jsx + src/App.jsx
                             + package.json + vite.config.js + README
    - next                 : app/page.tsx + app/layout.tsx + package.json
                             + tsconfig.json + next.config.js + README

When the brief signals integrations with secrets (API keys, env vars,
named services), browser-first stacks (react_vite, next) are augmented
with a Node/Express backend proxy tier: server/index.js + per-service
adapters under server/adapters/ + .env.example + docker-compose.yml.
The browser can't safely hold an API key, so a frontend-only scaffold
for these briefs can't run — augmentation makes it runnable.

A second tier — the "configurable" tier — adds a settings UI when the
brief asks the user to edit config from the browser. Adds 5 files: a
disk-backed config store, GET/PUT/test routes, a settings modal,
per-service editor, and a useConfig hook. Saves the user from
hand-editing .env every time they change a key.

A third tier — the "extensibility" tier (opt-in only) — adds the plugin
registry / marketplace machinery for products that want third parties
to drop in services at runtime.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple, cast

# A file plan entry: (relative path, one-line purpose).
FilePlan = List[Tuple[str, str]]

# Catalog of stack → file plan. Each plan is intentionally small (5-9
# files) so a single sweep through CodeAgent's Phase 2 loop completes in
# a few model calls.
STACK_TEMPLATES: Dict[str, FilePlan] = {
    "static_site": [
        ("index.html", "Single-page HTML entry with the app's UI markup."),
        ("style.css", "Stylesheet for index.html."),
        ("script.js", "Client-side JS that implements the interactive behavior."),
        ("README.md", "How to open and use the site."),
    ],
    "python_cli": [
        ("main.py", "Entry script — argparse-driven CLI exposing the core behavior."),
        ("requirements.txt", "Pinned runtime dependencies."),
        ("README.md", "Usage: how to install and run from the command line."),
    ],
    "fastapi": [
        ("src/__init__.py", "Package marker."),
        ("src/main.py", "FastAPI app with /health endpoint and the feature route(s)."),
        ("requirements.txt", "fastapi + uvicorn + any other pinned deps."),
        ("tests/__init__.py", "Tests package marker."),
        ("tests/test_health.py", "Smoke test that /health returns 200."),
        (".env.example", "Documented env vars (no secrets)."),
        ("README.md", "How to run: `uvicorn src.main:app --reload`."),
    ],
    "flask": [
        ("app.py", "Flask app with one or more routes implementing the brief."),
        ("templates/index.html", "Server-rendered home template."),
        ("static/style.css", "Stylesheet."),
        ("requirements.txt", "Pinned Flask + any other deps."),
        ("README.md", "How to run: `flask --app app run`."),
    ],
    "node_cli": [
        ("index.js", "Node entry — uses commander or process.argv to parse args."),
        ("package.json", "name/version/bin/main, dependencies pinned, scripts.start defined."),
        ("README.md", "Install + run: `npm install && node index.js`."),
    ],
    "react_vite": [
        ("index.html", "Vite entrypoint — root div + module script tag."),
        ("src/main.jsx", "React + ReactDOM mount point that renders <App />."),
        ("src/App.jsx", "Top-level component implementing the brief."),
        ("src/styles.css", "Global styles."),
        ("package.json", "react, react-dom, vite pinned; scripts: dev/build/preview."),
        ("vite.config.js", "Standard Vite + React plugin config."),
        ("README.md", "Install + run: `npm install && npm run dev`."),
    ],
    "next": [
        ("app/page.tsx", "Default route — renders the brief's home view."),
        ("app/layout.tsx", "Root layout with metadata, fonts, global wrappers."),
        ("app/globals.css", "Global styles, Tailwind base or custom resets."),
        ("package.json", "next, react, react-dom, typescript pinned; scripts: dev/build/start."),
        ("tsconfig.json", "Strict TypeScript config compatible with Next 14+."),
        ("next.config.js", "Minimal Next config; experimental flags only if needed."),
        ("README.md", "Install + run: `npm install && npm run dev`."),
    ],
    # iOS apps use the Swift Package Manager executable shape because a
    # real .xcodeproj is a binary-ish directory bundle the LLM can't
    # produce correctly. SwiftPM is the supported alternative — you can
    # open `Package.swift` in Xcode and it'll treat the package as an app
    # target. `swift build` works on the command line. SwiftUI is the
    # default UI framework since iOS 13.
    "ios_app": [
        ("Package.swift", "Swift package manifest — executable target with iOS platform requirement."),
        ("Sources/App/App.swift", "@main App struct conforming to App protocol; WindowGroup with ContentView."),
        ("Sources/App/ContentView.swift", "SwiftUI ContentView implementing the brief's UI."),
        ("README.md", "How to open in Xcode + how to swift build on CLI."),
    ],
}


# Detection: each stack key maps to a list of trigger phrases. First
# match wins, in declaration order (so more specific patterns come
# before more general ones — e.g. "next.js" before "react").
_STACK_TRIGGERS: List[Tuple[str, Tuple[str, ...]]] = [
    # iOS first — its trigger phrases are very specific and we don't want
    # "swift" or "swiftui" to accidentally match a non-iOS Swift project.
    ("ios_app", (
        "ios app", "ios application", "iphone app", "ipad app",
        "swiftui app", "swift package", "swift ios",
    )),
    ("next", ("next.js", "nextjs", "next 14", "next 13")),
    ("react_vite", ("react", "vite", "spa", "single-page app")),
    ("fastapi", ("fastapi", "fast-api", "fast api")),
    ("flask", ("flask",)),
    ("node_cli", ("node cli", "node.js cli", "node command-line", "express cli")),
    ("python_cli", (
        "python cli", "python command-line", "python script", "command-line tool",
        "argparse", "click cli",
    )),
    ("static_site", (
        "static site", "single-page html", "html + js", "browser game",
        "tic-tac-toe", "tictactoe", "snake game", "todo app", "static html",
        "landing page",
    )),
]


@lru_cache(maxsize=256)
def detect_stack(brief: str) -> Optional[str]:
    """Pick a stack template key from the brief. None when no match.

    Conservative on purpose: when no signal is found, returns None so
    CodeAgent falls back to its LLM-only planning path. A wrong template
    is worse than no template.

    Cached: called from many sites in one pipeline (planner, runner,
    code_agent's scoreboard branch) with the same brief. The trigger
    table is module-level and immutable, so the function is pure for
    a given brief string.
    """
    if not brief:
        return None
    text = brief.lower()
    for stack, phrases in _STACK_TRIGGERS:
        for phrase in phrases:
            if phrase in text:
                return stack
    return None


def detect_stack_from_handoff(
    brief: str,
    *,
    architecture_text: str = "",
    tech_stack: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Pick a stack template using the brief plus upstream architect hints.

    CodeAgent used to detect the scaffold stack from the raw brief only. That
    fails on generic product briefs ("service dashboard", "SaaS app") even when
    ArchitectAgent has already written concrete stack guidance in
    ``architecture.md`` / ``tech_stack.json``. When the brief itself is too
    vague, prefer explicit architect handoff before falling back to the LLM
    planner.
    """
    direct = detect_stack(brief)
    if direct:
        return direct

    signal_chunks: List[str] = []
    if architecture_text:
        signal_chunks.append(architecture_text.lower())
    if tech_stack:
        for key in ("frontend", "backend", "db", "infra"):
            value = tech_stack.get(key)
            if isinstance(value, str) and value.strip():
                signal_chunks.append(value.lower())
    signal_text = "\n".join(signal_chunks)
    if signal_text:
        # Prefer the explicit Vite/SPA shape over Next if the architect output
        # mentions both. This happens when tech_stack.json drifts but the
        # architecture overview still says "Vite + React SPA + Express".
        if any(token in signal_text for token in ("vite", "react spa", "single-page app", "single page app")):
            return "react_vite"
        if any(token in signal_text for token in ("next.js", "nextjs", "app router", "route handlers")):
            return "next"
        inferred = detect_stack(signal_text)
        if inferred:
            return inferred

    # Dashboard-class briefs without an explicit framework still map much more
    # often to a SPA scaffold than to a static site. This keeps the deterministic
    # browser-first template path available for homelab/service-board briefs.
    if _needs_design_system(brief):
        return "react_vite"
    return None


def plan_for_stack(
    stack: str,
    brief: str = "",
    *,
    decisions: Optional[Dict[str, Any]] = None,
) -> Optional[FilePlan]:
    """Return the file plan for ``stack``, augmented for the brief.

    Three layered augmentations on top of the base template:

    1. **Backend tier** — when the brief signals integrations with secrets
       (API keys, named services, env vars), browser-first stacks get a
       server process, per-service adapters, ``.env.example``, and
       ``docker-compose.yml``. Without this, the browser-only scaffold
       can't hold credentials and the program can't run.

       The architect's ``decisions.json`` (when present) is a stronger
       signal than brief detection: if the architect committed to a
       Node backend framework (``express``, ``hono-node``), the
       backend tier ships regardless of brief signals. This stops the
       "architect said Express + port 3000 but no server code shipped"
       class of consistency-reviewer finding that dominated tactrax /
       crack-track / e79bc0 reviews.

    2. **Configurable tier** — when the brief signals "the user should be
       able to edit config from the UI" (set API keys, change host/port,
       enable/disable services, test connection), adds the small set of
       files needed to persist user config to disk, expose it over the
       backend, and edit it from a settings UI. Without this, the user
       has to hand-edit ``.env`` and restart the server every time they
       want to change a credential.

    3. **Extensibility tier** — when the brief signals user-extensible
       scope (plugin registry, marketplace, plugin system), the scaffold
       gets the customization machinery: services.json registry, a
       generic API-card component, plugin contract types. Opt-in only —
       see ``_needs_extensibility`` for the explicit signals.
    """
    base = STACK_TEMPLATES.get(stack)
    if not base:
        return None
    plan: FilePlan = list(base)
    if stack in _BROWSER_FIRST_STACKS:
        if _needs_backend(brief) or _decisions_pin_node_backend(decisions):
            services = _detect_services(brief)
            plan = plan + _backend_tier_files(services)
            # Configurable tier rides on backend tier — pointless to add
            # a settings UI for credentials that don't get used (there
            # IS no backend).
            if _needs_configurable_ui(brief):
                plan = plan + _configurable_tier_files(services)
        if _needs_extensibility(brief):
            plan = plan + _extensibility_tier_files(stack, brief)
        # Design-system primitives — every dashboard-class scaffold
        # gets StatusPill, Sparkline, KpiTile reserved. These are
        # deterministic-manifest-driven so the LLM never has to
        # invent them. Triggered for any "dashboard" brief because
        # v15-v28 all needed these and didn't get them.
        if _needs_design_system(brief):
            plan = plan + _design_system_files()
    return plan


def files_target_for(brief: str) -> Tuple[int, int]:
    """Suggested (min, max) file count for the LLM planner's prompt.

    The default LLM-only planner asks for "3-12 files" which is right
    for a tiny CLI or game but wildly wrong for an extensible product.
    When the brief asks for marketplace/plugin/registry/drag-and-drop
    machinery, we let the planner emit substantially more.
    """
    if _needs_extensibility(brief):
        return (15, 60)
    if _needs_backend(brief):
        return (8, 25)
    return (3, 12)


def max_files_for(brief: str) -> int:
    """Hard cap on plan size. The planner can't generate more files
    than this even if it asks. Default 25; ambitious briefs get 80 so
    extensibility / marketplace shapes have room.

    Bumped to 40 for any dashboard brief — the design-system tier
    adds 3 primitives + the configurable tier adds 6+, easily
    pushing past 25. v31 hit this exact bug: plan had 28 files,
    cap was 25, the last 3 (StatusPill / Sparkline / KpiTile) got
    truncated and App.jsx then imported files that didn't exist.
    """
    if _needs_extensibility(brief):
        return 80
    if _needs_design_system(brief):
        return 40
    return 25


def template_keys() -> List[str]:
    """All known stack keys, sorted for stable test assertions."""
    return sorted(STACK_TEMPLATES.keys())


# ── Integration / backend-tier detection ───────────────────────────────
#
# Browser-first stacks (react_vite, next) can't hold API secrets — when
# a brief asks for real integrations with named services and env-var
# keys, the scaffold MUST include a backend proxy or it can't run. The
# heuristics below detect that case and the named services involved.

_BROWSER_FIRST_STACKS: set = {"react_vite", "next"}

# Strong "this brief needs a backend tier" signals. Single match is
# enough — if a brief mentions any of these phrases, the frontend alone
# isn't enough.
_BACKEND_SIGNALS: Tuple[str, ...] = (
    "api key", "api_key", "api keys",
    "env var", "env-var", "env vars", "environment variable",
    "real api", "real api calls", "not hardcoded", "not mock",
    "no mock data", "no fake data", "no demo data",
    "docker socket", "docker container", "docker monitor",
    "credentials", "secret", "secrets",
    "proxy", "backend proxy",
    "subscription", "auth header", "bearer token",
    "backend api", "server-side crud", "crud api",
    "crud endpoint", "health endpoint", "health check endpoint",
    "api endpoint", "api route", "api routes", "/health",
)

_BACKEND_CONTEXT_SIGNALS: Tuple[str, ...] = (
    "backend",
    "server-side",
    "server side",
    "health endpoint",
    "health check endpoint",
    "crud",
    "api endpoint",
    "api route",
    "api routes",
    "/health",
)

_BACKEND_PERSISTENCE_SIGNALS: Tuple[str, ...] = (
    "persistent backend config",
    "persistent config",
    "persist config",
    "persist configuration",
    "persisted config",
    "config store",
    "save configuration",
    "saved configuration",
    "survive restart",
    "survives restart",
    "across restarts",
    "persist to disk",
    "save to disk",
)

# Named-service triggers → service slug. Slug becomes the adapter
# filename: server/adapters/<slug>.js (or .py). Order matters only for
# readability; lookup is membership-based.
_SERVICE_TRIGGERS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("sonarr",        ("sonarr",)),
    ("radarr",        ("radarr",)),
    ("prowlarr",      ("prowlarr",)),
    ("qbittorrent",   ("qbittorrent", "qbit ", "qbit,")),
    ("emby",          ("emby",)),
    ("jellyfin",      ("jellyfin",)),
    ("plex",          ("plex",)),
    ("sonos",         ("sonos", "soco")),
    ("docker",        ("docker", "docker socket", "docker container", "docker stats",
                       "docker monitor", "docker monitoring")),
    ("home_assistant", ("home assistant", "home-assistant", "homeassistant")),
    ("pihole",        ("pi-hole", "pihole")),
    ("unifi",         ("unifi", "ubiquiti")),
    ("transmission",  ("transmission",)),
    ("nzbget",        ("nzbget",)),
    ("sabnzbd",       ("sabnzbd",)),
    ("overseerr",     ("overseerr",)),
    ("tautulli",      ("tautulli",)),
)


def _needs_backend(brief: str) -> bool:
    if not brief:
        return False
    text = brief.lower()
    for sig in _BACKEND_SIGNALS:
        if sig in text:
            return True
    if (
        any(sig in text for sig in _BACKEND_PERSISTENCE_SIGNALS)
        and any(sig in text for sig in _BACKEND_CONTEXT_SIGNALS)
    ):
        return True
    # Fallback: 2+ named services in a single brief is itself a signal
    # — a real homelab integration UI never talks to one service.
    if len(_detect_services(brief)) >= 2:
        return True
    return False


# Decisions-contract bundle backend slots that mean "ship a Node
# backend tier." Kept narrow on purpose — `next` has its own
# fullstack template so it doesn't need the Express tier appended,
# and `none` is explicit no-backend.
_NODE_BACKEND_FRAMEWORKS = {"express", "hono-node", "hono"}


def _decisions_pin_node_backend(decisions: Optional[Dict[str, Any]]) -> bool:
    """True when the architect's decisions.json committed to a Node
    backend framework that needs the backend tier shipped.

    Honors the architect contract (PR #23) — when decisions.framework
    is express / hono-node, the scaffold ships the server even if the
    brief itself didn't trigger _needs_backend. Without this, the
    architect promises Express + port 3000 in decisions.json and the
    consistency reviewer correctly flags the missing backend on
    every build.
    """
    if not decisions:
        return False
    framework = str(decisions.get("framework") or "").strip().lower()
    if framework in _NODE_BACKEND_FRAMEWORKS:
        return True
    # backend_language as a secondary signal in case `framework`
    # is unset / unknown but the decisions still committed to Node.
    language = str(decisions.get("backend_language") or "").strip().lower()
    backend_port = decisions.get("backend_port")
    if language == "node" and isinstance(backend_port, int):
        return True
    return False


def _detect_services(brief: str) -> List[str]:
    """Return the service slugs the brief wants pre-built adapters for.

    Distinguishes SEED services (the brief asks the scaffold to ship
    adapters for these) from EXAMPLE services (the brief lists these as
    "the user could also add X, Y, Z" — extensibility examples, NOT
    pre-build requests). Without this split, an extensible-dashboard
    brief explodes the adapter cluster: e.g. v17's brief mentioned 7
    seeds plus 7 examples in a "user can add Pi-hole, Plex, Jellyfin,
    Tautulli, Overseerr, AdGuard, Home Assistant" parenthetical, and
    we naively planned 13+ adapter files when only 7 should have been
    seeded.

    Strategy:
      1. Mask out the brief regions that introduce "add your own"
         examples (parentheticals after phrases like "adding a new
         service", "like X", "anything with an HTTP API", etc).
      2. Run the existing trigger match on the masked text only.
      3. Cap at 8 seed services so a still-noisy brief can't blow up
         the cluster — anything beyond 8 stops being a "seed" and
         should be added through the runtime registry.
    """
    if not brief:
        return []
    text = brief.lower()
    masked = _mask_extensibility_examples(text)
    found: List[str] = []
    for slug, triggers in _SERVICE_TRIGGERS:
        for t in triggers:
            if t in masked and slug not in found:
                found.append(slug)
                break
    # Hard cap: anything beyond 8 named seeds is over-detection; the
    # rest belongs in services.json via the registry.
    return found[:8]


# Phrases that introduce a list of EXAMPLE services the user "could
# add" — not seed services to ship adapters for. When any of these is
# in the brief, we mask out the following parenthetical (or up to the
# next sentence end) before running service detection. Order: longer
# phrases first so they match before shorter substrings.
_EXAMPLE_INTRODUCERS: Tuple[str, ...] = (
    "adding a new service",
    "add a new service",
    "add your own",
    "bring your own",
    "add custom",
    "such as",
    "examples include",
    "for example",
    "anything with an http api",
    "anything with a rest api",
    "any service with an api",
    "any http api",
    "could add",
    "can add",
    "users can add",
    "user could add",
    "like ",
    "e.g.",
    "e.g ",
    "etc",
    # Default-features template uses quoted examples in parentheticals
    # after "recent events" — e.g. "('Plex Media Server started', ...)".
    # Without this, _detect_services falsely seeds a plex adapter.
    "recent events",
    "example events",
    "sample events",
)


def _mask_extensibility_examples(text: str) -> str:
    """Replace the example-list regions in ``text`` with spaces so the
    service-trigger pass doesn't see them. Keeps line/column positions
    so any downstream regex stays consistent. Conservative — if we
    can't identify a clean end of an example block, we mask only up
    to the nearest sentence-ending punctuation.
    """
    if not text:
        return text
    result = list(text)
    n = len(text)
    for introducer in _EXAMPLE_INTRODUCERS:
        start = 0
        while True:
            idx = text.find(introducer, start)
            if idx < 0:
                break
            # Find the end of the example block. Three heuristics in
            # priority order:
            #   1. If followed by '(', mask to the matching ')'.
            #   2. Otherwise mask to the next sentence boundary
            #      (., ?, !, newline, or up to ~200 chars max).
            after = idx + len(introducer)
            end = after
            # Skip whitespace right after the introducer.
            while end < n and text[end] in " \t":
                end += 1
            if end < n and text[end] == "(":
                depth = 1
                end += 1
                while end < n and depth > 0:
                    if text[end] == "(":
                        depth += 1
                    elif text[end] == ")":
                        depth -= 1
                    end += 1
            else:
                hardstop = min(n, after + 200)
                sentence_end = after
                while sentence_end < hardstop:
                    if text[sentence_end] in ".?!\n":
                        sentence_end += 1
                        break
                    sentence_end += 1
                end = sentence_end
            # Mask the region (keeping length so positions don't shift).
            for i in range(after, min(end, n)):
                result[i] = " "
            start = end
    return "".join(result)


# Signals for the homelab/service-status template tier specifically.
# These are *not* generic "any dashboard" signals — earlier versions
# triggered on the bare word "dashboard" which caused Excel-upload,
# finance, and analytics dashboards to all get the homelab scaffold
# (Plex/Sonarr/Radarr tiles), wrong-product disasters that tanked
# review scores. The template tier should only kick in when the brief
# clearly describes a multi-service status board.
_HOMELAB_SIGNALS: Tuple[str, ...] = (
    "homelab", "home lab", "home-lab",
    "self-hosted dashboard", "self hosted dashboard",
    "service status dashboard", "service status board",
    "status board", "status dashboard",
    "service dashboard", "services dashboard",
    "media stack dashboard",
    "homarr", "heimdall", "dashy",
    # Explicit dashboard component asks — when the brief mentions a
    # command palette, activity feed, or service detail drawer, the
    # design-system primitives are the intended output even if the
    # brief doesn't mention "homelab". The backfill path falls back
    # to a placeholder stub otherwise.
    "command palette", "activity feed", "service detail",
    "monitor my services", "monitor my homelab",
    "ops dashboard", "noc dashboard",
)


# Briefs that explicitly disqualify the homelab path even if they
# happen to contain "dashboard" — these describe other domains.
_HOMELAB_DISQUALIFIERS: Tuple[str, ...] = (
    "excel", "spreadsheet", "csv", "xlsx",
    "finance", "expense", "budget", "transaction",
    "kanban", "todo", "habit", "recipe",
    "notes app", "markdown notes",
    "bookmark", "rss", "feed reader",
    "journal", "workout", "fitness",
    "crm", "invoice", "billing",
    "real estate", "listing",
)


def _needs_design_system(brief: str) -> bool:
    """True when the brief specifically describes a homelab-style
    service status dashboard.

    Narrower than the previous "anything with the word dashboard" logic
    — that was the root cause of CodeAgent producing homelab Plex/Sonarr
    scaffolds for Excel uploaders and finance trackers.
    """
    if not brief:
        return False
    text = brief.lower()
    if any(d in text for d in _HOMELAB_DISQUALIFIERS):
        return False
    return any(sig in text for sig in _HOMELAB_SIGNALS)


def _design_system_files() -> FilePlan:
    """The 3 deterministic UI primitives every dashboard needs.

    These files have deterministic manifest generators registered in
    _MANIFEST_GENERATORS so CodeAgent's Phase 2 loop writes them
    instantly with no LLM call. The LLM uses them for composition
    (App.jsx imports StatusPill, ServiceCard wraps a Sparkline,
    topbar uses KpiTile)."""
    return [
        ("src/components/StatusPill.jsx",
         "Universal status pill: dot + uppercase label, rounded-full, "
         "tinted background. Props: tone (ok|warn|err|info|idle) + "
         "children. Deterministic shape — don't reinvent."),
        ("src/components/Sparkline.jsx",
         "Tiny 60×24 SVG sparkline for inline trend lines on service "
         "cards. Props: values, color, height, width. Hand-rolled SVG, "
         "no recharts dep. Empty state: dashed neutral line."),
        ("src/components/KpiTile.jsx",
         "Topbar KPI tile: label / value+unit / optional delta with a "
         "tinted left border. Props: label, value, unit, delta, tone. "
         "Use these in the top-of-dashboard summary strip, NOT in "
         "service cards."),
    ]


def _backend_tier_files(services: List[str]) -> FilePlan:
    """Files to append for a Node/Express backend proxy tier.

    Kept small on purpose — the per-service adapter is the variable
    part. Everything else (server entry, env example, docker-compose,
    server README) is fixed. Order matters: server entry first so the
    LLM has it as context when writing each adapter.
    """
    # Default port plan (reserved range to avoid collisions with the
    # SkyN3t studio itself on 5173/6660):
    #   Generated frontend (Vite): 5180
    #   Generated backend (Express): 3100
    #   CORS allowed origin: http://localhost:5180
    # Briefs that name a different port still win — these are defaults
    # only, threaded through the per-file LLM prompt as a hint.
    files: FilePlan = [
        ("server/index.js",
         "Node + Express proxy server. Loads env vars, mounts each adapter "
         "under /api/<service>, returns JSON. Reads PORT from env, "
         "defaults 3100. Adds CORS for http://localhost:5180 (the "
         "Vite dev origin). Use ESM throughout — `import` not "
         "`require`, matching package.json type: module."),
        ("server/package.json",
         "Server-side dependencies: express, dotenv, node-fetch (or built-in "
         "fetch on Node 20+), cors. scripts: start, dev (node --watch). "
         "Set \"type\": \"module\" so adapter ESM exports work."),
        (".env.example",
         "Documented env vars for every integration: API keys, hosts, ports, "
         "usernames, passwords. The top section sets PORT=3100 (backend), "
         "CORS_ORIGIN=http://localhost:5180 (frontend dev origin). "
         "No real secrets — placeholders only. Copy to .env to run."),
        ("docker-compose.yml",
         "Two services: 'web' (Vite preview build) and 'api' (Node server). "
         "Wires env_file: .env so the proxy server gets real credentials. "
         "Exposes web on 5180 and api on 3100 on the host."),
        ("server/README.md",
         "Server-side run + config: copy .env.example to .env, fill keys, "
         "`npm install && npm run dev` in server/. Notes on each adapter's "
         "expected env vars. Default ports: backend 3100, frontend 5180."),
    ]
    for slug in services:
        files.append((
            f"server/adapters/{slug}.js",
            f"Adapter for {slug.replace('_', ' ').title()} — module exports an Express "
            f"router. Reads its host/port/key from env vars (see .env.example). "
            f"Translates the service's native API into a stable JSON shape the frontend "
            f"can consume. Surfaces errors as proper HTTP status codes, not silent empty arrays.",
        ))
    return files


# ── Extensibility-tier detection ───────────────────────────────────────
#
# When a brief uses any of these phrases, the user expects a product
# that can be *configured at runtime* — adding a new card, rearranging
# layout, customizing without editing source. The default react_vite
# template plus a backend tier produces a static panel, which always
# disappoints. The extensibility-tier files give the planner explicit
# slots for the customization machinery.

_EXTENSIBILITY_SIGNALS: Tuple[str, ...] = (
    "plugin", "plug-in", "plug in",
    "marketplace", "market place",
    "extensible", "extensibility", "extendable",
    "user-extensible", "user extensible",
    "drag and drop", "drag-and-drop", "draggable",
    "settings panel", "settings ui", "settings page",
    "customizable", "customize", "customization",
    "user-configurable", "user configurable",
    "services.json", "service registry", "service-registry",
    "card system", "card framework", "pluggable card",
    "bring your own", "byo", "add your own", "add custom",
    "homarr", "heimdall", "dashy",
    "theme picker", "theme switch", "switch theme",
    "omnibox", "cmd+k", "command palette", "command-k",
    "registry", "manifest-driven", "config-driven",
)


# ── Configurable-UI tier detection ─────────────────────────────────────
#
# Signals that the user expects to edit per-service settings (URL, API
# key, credentials, enable/disable, refresh interval) from the running
# UI — not by hand-editing .env and restarting. Without this tier the
# scaffold renders cards but every change requires shell work.

_CONFIGURABLE_SIGNALS: Tuple[str, ...] = (
    "edit each", "edit per service", "edit per-service",
    "configure each", "configure per service",
    "change api key", "change the api key", "add api key",
    "set api key", "edit api key", "configure api key",
    "set credentials", "edit credentials",
    "choose which api", "pick which api", "select api",
    "swap out", "swap services",
    "settings ui", "settings page", "settings panel",
    "config ui", "config page", "config panel",
    "configuration ui", "configuration panel",
    "save settings", "save config",
    "test connection", "test the connection",
    "enable/disable", "enable or disable",
    "toggle service",
    "edit from the ui", "edit from ui",
    "set host", "change host", "set port", "change port",
    "edit url",
    "user configurable", "user-configurable",
    "persistent backend config", "persistent config",
    "persist config", "persist configuration", "persisted config",
    "config store", "save configuration", "saved configuration",
    "settings saved", "survive restart", "survives restart",
    "across restarts",
)


def _needs_configurable_ui(brief: str) -> bool:
    """True when the brief says the user should edit config from the UI.

    Falls back to True any time the BACKEND tier is needed AND the
    brief mentions "settings" — that's a strong implicit signal: a
    scaffold with credentials and a settings panel almost always
    means "let me set the credentials from the settings panel."
    """
    if not brief:
        return False
    text = brief.lower()
    for sig in _CONFIGURABLE_SIGNALS:
        if sig in text:
            return True
    if _needs_backend(brief) and "settings" in text:
        return True
    return False


def _configurable_tier_files(services: List[str]) -> FilePlan:
    """Files to append so the user can edit service config from the UI.

    Adds 5 files:
      - ``server/config-store.js`` — JSON-on-disk store for the
        user's saved service config; survives restart.
      - ``server/routes/config.js`` — GET/PUT routes the frontend
        calls to read & update config; routes also expose a
        ``POST /api/config/:slug/test`` for connection checks.
      - ``src/components/SettingsModal.jsx`` — top-level modal
        with one row per service; click "Edit" to open editor.
      - ``src/components/ServiceEditor.jsx`` — per-service form:
        URL, API key, enabled toggle, test-connection button.
      - ``src/hooks/useConfig.js`` — fetches & caches the config
        from the backend, exposes ``updateService(slug, patch)``
        and ``testConnection(slug)``.

    The list is small on purpose. Each file has a focused job; the
    LLM doesn't need to invent the architecture, just fill it in.
    """
    return [
        ("server/config-store.js",
         "Disk-backed config store. Module exports load(), save(patch), "
         "getAll(), get(slug), set(slug, partial). Persists to "
         "data/user-config.json next to the server. On first load, seeds "
         "from environment variables and the per-adapter defaults so the "
         "UI shows sensible starting values; user changes survive restart. "
         "Atomic writes: write to .tmp then rename."),
        ("server/routes/config.js",
         "Express router. GET /api/config returns the full config "
         "(masking secrets — return `***` not the actual API key). "
         "PUT /api/config/:slug updates one service. POST /api/config/"
         ":slug/test makes a real request to the service's healthiest "
         "endpoint with the SUPPLIED credentials and returns {ok, "
         "latencyMs, error}. Routes mounted at /api/config."),
        ("server/data/user-config.json",
         "Empty `{}` JSON file as the initial state. Created so the "
         "config-store doesn't crash on first read. Real values are "
         "written by the user via the settings UI."),
        ("src/components/SettingsModal.jsx",
         "Top-level settings modal opened via gear icon in the topbar. "
         "Lists every configured service (from useConfig) with current "
         "host/port and an Edit button. Drawer or modal overlay; "
         "Escape closes; click-outside closes. Uses CSS variables for "
         "styling, not Tailwind (Tailwind isn't installed)."),
        ("src/components/ServiceEditor.jsx",
         "Per-service edit form. Inputs: protocol (http/https), host, "
         "port, base URL preview, API key (masked input), enabled "
         "toggle. Buttons: Save, Test Connection, Cancel. Test calls "
         "POST /api/config/:slug/test and shows latency/error inline. "
         "Save calls PUT /api/config/:slug and closes on success."),
        ("src/hooks/useConfig.js",
         "React hook. On mount, GET /api/config and stash in module-"
         "level cache (so multiple components share it). Returns "
         "{config, isLoading, error, updateService(slug, patch), "
         "testConnection(slug, overrides)}. updateService PUTs then "
         "refreshes the cache. testConnection POSTs with optional "
         "patch (so user can test before saving)."),
    ]


def _needs_extensibility(brief: str) -> bool:
    """Opt-in only: only the most explicit "I want a plugin system /
    marketplace / dynamic-service-registry" phrases trigger the
    extensibility tier.

    Why opt-in: the tier adds 14 files to the plan, which on CLI
    backends at ~3 min/file × concurrency 4 = ~12 extra minutes of
    wall time. That's enough to push a 19-file run from ~14 min code
    stage to ~25 min code stage, which hits the 30-min timeout on
    long-tail unlucky days (v17-v21 all timed out on extensibility
    tier).

    A brief that mentions "drag-and-drop" or "settings panel" gets
    those features written by the LLM into App.jsx — it doesn't
    NEED 14 separate scaffolded files for them. The tier is only
    worth it when the user explicitly wants a marketplace-class
    product where third parties can drop in services.
    """
    if not brief:
        return False
    text = brief.lower()
    for sig in _EXTENSIBILITY_OPTIN_SIGNALS:
        if sig in text:
            return True
    return False


# EXPLICIT opt-in phrases only. These are unambiguous "I want a
# pluggable / marketplace / extensible-by-third-parties product"
# signals. Anything softer (drag-and-drop, settings panel, theme
# picker) gets handled inside App.jsx by the LLM, no extra files.
_EXTENSIBILITY_OPTIN_SIGNALS: Tuple[str, ...] = (
    "extensibility tier",       # literal opt-in
    "plugin marketplace",
    "plugin registry",
    "plugin system",
    "marketplace tier",
    "services.json registry",
    "third-party plugins",
    "third party plugins",
    "pluggable architecture",
    "byo plugins", "byo cards",
    "user-contributed plugins",
    "addon system", "add-on system",
)


def _extensibility_tier_files(stack: str, brief: str) -> FilePlan:
    """Files to append for a user-extensible dashboard tier.

    The planner gets explicit slots for: a runtime service registry
    that backend reads at startup, a generic API-card component, a
    settings UI, a layout engine with localStorage persistence, and
    a plugin contract types file. Without these slots the model
    can't allocate room for the customization surface — it spends
    every file on the named-service adapters.

    Tailored for ``react_vite`` / ``next`` because that's where the
    augmentation makes sense; CLI projects don't need this.
    """
    files: FilePlan = [
        ("server/services.json",
         "Runtime service registry — JSON manifest the backend reads at startup. "
         "One entry per service: {slug, name, type, baseUrl, auth, endpoints, "
         "envVars}. The 7 named services ship as seeds; users add more by "
         "appending entries here (no source edits needed). This file is the "
         "extensibility contract — without it the dashboard is static."),
        ("server/registry.js",
         "Loads services.json, validates each entry against the plugin schema, "
         "instantiates a generic adapter per service that fetches `baseUrl + "
         "endpoints[*].path` with `auth` headers from env. The default fan-out "
         "so users don't have to write JS for every new integration."),
        ("server/adapters/generic.js",
         "Schema-driven adapter — given a service entry from services.json, "
         "builds an Express router that proxies the documented endpoints, "
         "injects auth headers from env, and normalizes the response into the "
         "dashboard's card-data shape. Lets new services join with config only."),
        ("src/components/Card.jsx",
         "Generic card component — renders ANY service's data given a "
         "{title, items, status, error, isLoading} shape. Service-specific "
         "cards reuse this; user-added API cards use it directly."),
        ("src/components/ApiCard.jsx",
         "Configurable API card — user provides URL + headers + JSONPath/template "
         "for what to display; component fetches, applies the template, renders. "
         "The 'bring your own API' surface, used by the settings UI's 'Add card' flow."),
        ("src/components/Grid.jsx",
         "Draggable, resizable grid using react-grid-layout. Each card is a grid "
         "item; layout persists to localStorage on every change. Reads card "
         "list + layout from the app store."),
        ("src/components/Settings.jsx",
         "Settings panel/drawer — toggle card visibility, add a new API card "
         "(URL + headers + display template), change refresh interval, switch "
         "theme accent. Persists every change to localStorage."),
        ("src/components/Omnibox.jsx",
         "Command palette / search omnibox. Cmd+K (or Ctrl+K) opens a fuzzy "
         "filter over card titles; pressing Enter jumps focus to that card. "
         "Filter-as-you-type also dims non-matching cards on the grid."),
        ("src/store.js",
         "Tiny client state store (Zustand or a vanilla useSyncExternalStore "
         "module) holding: card list, per-card visibility, layout, theme accent, "
         "refresh interval, custom API cards. Reads/writes localStorage."),
        ("src/lib/plugin.js",
         "Plugin contract — JS types/JSDoc describing the ServiceEntry shape, "
         "the CardData shape, and helper functions for validating user-added "
         "API cards before they're saved to the store."),
        ("src/hooks/usePolling.js",
         "Shared polling hook — fetches `/api/<slug>` at the configured "
         "interval, returns {data, isLoading, error, lastUpdated}. Pauses "
         "when the tab is hidden. Used by every card."),
        ("src/theme.js",
         "Theme accent system — exports a small set of accent palettes "
         "(blue, purple, emerald, amber) and the swap helper the settings "
         "UI calls. CSS custom properties driven; no full restyle needed."),
        ("server/routes/cards.js",
         "POST /api/cards (save a user-added API card definition), GET /api/cards "
         "(list saved cards). Persists to data/user_cards.json so user-added "
         "cards survive backend restart even if the browser cache clears."),
        ("server/data/user_cards.json",
         "Initial empty user-added cards file. Created with `[]` so first-run "
         "doesn't crash on missing-file read."),
    ]
    return files


# ── Per-stack idiom hints ───────────────────────────────────────────────
#
# CodeAgent's Phase 2 sends a generic "write this file" prompt. Without
# stack-specific guidance the model defaults to outdated idioms (Next.js
# pages/ router, FastAPI app.py in repo root, Vite with deprecated config
# shapes). Each stack here adds a short instruction block appended to the
# build system prompt — enough to anchor the model to the modern shape
# without bloating the prompt.

STACK_BUILD_HINTS: Dict[str, str] = {
    "static_site": (
        "Idiom: vanilla HTML/CSS/JS, no build step. No external CDN scripts "
        "unless absolutely required. Link script.js with `defer`. Inline "
        "any small init logic inside script.js, not in index.html. Keep "
        "the page accessible — semantic tags, alt text on images."
    ),
    "python_cli": (
        "Idiom: a single-file CLI driven by argparse. The `main()` function "
        "wires the parser, calls into helper functions, and returns an int "
        "exit code. Guard with `if __name__ == \"__main__\": sys.exit(main())`. "
        "Pin runtime deps in requirements.txt with `>=` only, no upper bounds."
    ),
    "fastapi": (
        "Idiom: FastAPI 0.110+. Import `FastAPI` from fastapi. Define request/"
        "response models with pydantic v2 (`BaseModel`, `Field`). The /health "
        "route returns `{\"status\": \"ok\"}` with status_code=200. Tests use "
        "fastapi.testclient.TestClient. Run with `uvicorn src.main:app "
        "--reload`. requirements.txt pins fastapi, uvicorn[standard], pydantic."
    ),
    "flask": (
        "Idiom: Flask 3+. `from flask import Flask, render_template`. The "
        "app object is `app = Flask(__name__)`. Routes return either "
        "render_template('index.html') for HTML or a JSON dict (Flask "
        "auto-serializes). Static files live in static/, templates in "
        "templates/. requirements.txt pins flask."
    ),
    "node_cli": (
        "Idiom: Node 20+, no TypeScript build step. Use `process.argv` "
        "directly or commander if more than two args. package.json sets "
        "\"type\": \"module\" and \"bin\": {\"<name>\": \"./index.js\"}. "
        "Add a `start` script. Shebang on index.js: `#!/usr/bin/env node`."
    ),
    "react_vite": (
        "Idiom: Vite 5+ with React 18+. Use functional components and "
        "hooks — no class components. main.jsx imports React from 'react' "
        "and ReactDOM from 'react-dom/client', then `createRoot(...).render"
        "(<App />)`. vite.config.js uses `defineConfig({ plugins: "
        "[react()] })` with `@vitejs/plugin-react`. "
        "CRITICAL: this is a VITE project, NOT Next.js. "
        "Do NOT use `app/page.tsx`, `app/layout.tsx`, `next.config.js`, "
        "`next/font`, `next/head`, or `import type { Metadata } from 'next'`. "
        "Do NOT use the Next.js App Router. The entry is `index.html` + "
        "`src/main.jsx`, not `app/page.tsx`."
    ),
    "next": (
        "Idiom: Next 14+ with the App Router (app/ directory). NEVER use "
        "pages/. Default export functional components from app/*.tsx. "
        "app/layout.tsx exports a default RootLayout with `children`. "
        "app/page.tsx is the index route. metadata is exported from "
        "layout. tsconfig.json sets `strict: true` and "
        "`moduleResolution: \"bundler\"`."
    ),
    "ios_app": (
        "Idiom: SwiftUI on iOS 16+, no UIKit unless strictly required. "
        "App entry is `@main struct AppName: App` with a `WindowGroup` "
        "containing the root view. Use `@State` / `@Binding` / "
        "`@Observable` (Swift 5.9+) for view state — not legacy "
        "`ObservableObject`. Package.swift declares an executable "
        "product with `.iOS(.v16)` platform requirement. Open the "
        "package directory in Xcode to run on a simulator."
    ),
}


def hint_for_stack(stack: Optional[str]) -> str:
    """Return the per-stack idiom hint, or empty string when none."""
    if not stack:
        return ""
    return STACK_BUILD_HINTS.get(stack, "")


# ── Per-stack README starters ──────────────────────────────────────────
#
# Every scaffold's README has the same shape (Install / Run / How it
# works / License). Content differs per stack but the LLM writes nearly
# the same boilerplate every time — wasted tokens. Generate it locally.
# CodeAgent can use these as the README body directly, or pass them to
# the LLM as a starting point for a brief-specific README.

def _readme_static(brief: str) -> str:
    return (
        f"# Project\n\n"
        f"{brief.strip()}\n\n"
        "## Run\n\n"
        "Open `index.html` in any modern browser. No build step.\n\n"
        "```bash\n"
        "open index.html   # macOS\n"
        "xdg-open index.html  # Linux\n"
        "```\n\n"
        "## Files\n\n"
        "- `index.html` — entry markup\n"
        "- `style.css` — styles\n"
        "- `script.js` — interactive behavior (loaded with `defer`)\n"
    )


def _readme_python_cli(brief: str) -> str:
    return (
        f"# Project\n\n"
        f"{brief.strip()}\n\n"
        "## Install\n\n"
        "```bash\n"
        "python3 -m venv .venv && source .venv/bin/activate\n"
        "pip install -r requirements.txt\n"
        "```\n\n"
        "## Run\n\n"
        "```bash\n"
        "python main.py --help\n"
        "```\n"
    )


def _readme_fastapi(brief: str) -> str:
    return (
        f"# Project\n\n"
        f"{brief.strip()}\n\n"
        "## Install\n\n"
        "```bash\n"
        "python3 -m venv .venv && source .venv/bin/activate\n"
        "pip install -r requirements.txt\n"
        "cp .env.example .env  # then edit\n"
        "```\n\n"
        "## Run\n\n"
        "```bash\n"
        "uvicorn src.main:app --reload\n"
        "```\n\n"
        "Smoke: `curl http://127.0.0.1:8000/health` should return "
        "`{\"status\":\"ok\"}`.\n\n"
        "## Test\n\n"
        "```bash\n"
        "pytest tests/\n"
        "```\n"
    )


def _readme_flask(brief: str) -> str:
    return (
        f"# Project\n\n"
        f"{brief.strip()}\n\n"
        "## Install\n\n"
        "```bash\n"
        "python3 -m venv .venv && source .venv/bin/activate\n"
        "pip install -r requirements.txt\n"
        "```\n\n"
        "## Run\n\n"
        "```bash\n"
        "flask --app app run\n"
        "```\n\n"
        "Then open http://127.0.0.1:5000.\n"
    )


def _readme_node_cli(brief: str) -> str:
    return (
        f"# Project\n\n"
        f"{brief.strip()}\n\n"
        "## Install\n\n"
        "```bash\n"
        "npm install\n"
        "```\n\n"
        "## Run\n\n"
        "```bash\n"
        "node index.js --help\n"
        "```\n"
    )


def _readme_react_vite(brief: str) -> str:
    return (
        f"# Project\n\n"
        f"{brief.strip()}\n\n"
        "## Install\n\n"
        "```bash\n"
        "npm install\n"
        "```\n\n"
        "## Run\n\n"
        "```bash\n"
        "npm run dev\n"
        "```\n\n"
        "Then open http://127.0.0.1:5173.\n\n"
        "## Build\n\n"
        "```bash\n"
        "npm run build && npm run preview\n"
        "```\n"
    )


def _readme_next(brief: str) -> str:
    return (
        f"# Project\n\n"
        f"{brief.strip()}\n\n"
        "## Install\n\n"
        "```bash\n"
        "npm install\n"
        "```\n\n"
        "## Run\n\n"
        "```bash\n"
        "npm run dev\n"
        "```\n\n"
        "Then open http://127.0.0.1:3000.\n\n"
        "## Build\n\n"
        "```bash\n"
        "npm run build && npm start\n"
        "```\n"
    )


def _readme_ios_app(brief: str) -> str:
    return (
        f"# Project\n\n"
        f"{brief.strip()}\n\n"
        "## Requirements\n\n"
        "- macOS with Xcode 15+ installed\n"
        "- iOS 16+ deployment target\n\n"
        "## Open in Xcode\n\n"
        "Double-click `Package.swift`. Xcode will resolve dependencies "
        "and let you pick a simulator from the toolbar.\n\n"
        "## Build from CLI\n\n"
        "```bash\n"
        "swift build\n"
        "```\n\n"
        "Note: `swift build` validates that the code compiles. To "
        "actually run on a simulator you must use Xcode or `xcodebuild`.\n"
    )


_README_GENERATORS = {
    "static_site": _readme_static,
    "python_cli": _readme_python_cli,
    "fastapi": _readme_fastapi,
    "flask": _readme_flask,
    "node_cli": _readme_node_cli,
    "react_vite": _readme_react_vite,
    "next": _readme_next,
    "ios_app": _readme_ios_app,
}


# ── Per-stack deterministic manifests ──────────────────────────────────
#
# requirements.txt / package.json contents the LLM keeps re-deriving (and
# sometimes drifting to outdated versions). Locking them deterministically:
#   - kills the "model pinned react ^17 but used hooks API" failure class
#   - eliminates one LLM call per project (~8000-token budget freed up)
#   - lets the verifier rely on known shapes (npm-shape gate no longer
#     guesses what was meant)
#
# Versions chosen to match the idiom hints (Next 14, React 18, FastAPI
# 0.110+, etc.). Use `>=` floors so newer minor/patch releases land
# without forcing a regenerate.


def _manifest_python_cli(brief: str) -> str:
    return (
        "# Pinned runtime dependencies. Edit as needed.\n"
        "# No external deps by default — argparse + stdlib are enough\n"
        "# for most CLIs. Add lines below as you need them.\n"
    )


def _manifest_fastapi(brief: str) -> str:
    return (
        "fastapi>=0.110.0\n"
        "uvicorn[standard]>=0.27.0\n"
        "pydantic>=2.6.0\n"
        "# Dev/test deps:\n"
        "httpx>=0.27.0\n"
        "pytest>=8.0.0\n"
    )


def _manifest_flask(brief: str) -> str:
    return (
        "flask>=3.0.0\n"
        "# Dev/test deps:\n"
        "pytest>=8.0.0\n"
    )


def _manifest_node_cli(brief: str) -> str:
    # Note: package.json is JSON, not a free-form file — keep it minimal
    # and valid. The model writes the bin name + main file but the shape
    # is fixed here.
    return _json_dumps_pretty({
        "name": "scaffold",
        "version": "0.1.0",
        "private": True,
        "type": "module",
        "bin": {"app": "./index.js"},
        "main": "index.js",
        "scripts": {
            "start": "node index.js",
        },
        "engines": {"node": ">=20"},
        "dependencies": {},
    })


def _manifest_react_vite(brief: str) -> str:
    return _json_dumps_pretty({
        "name": "scaffold",
        "version": "0.1.0",
        "private": True,
        "type": "module",
        "scripts": {
            "dev": "vite",
            "build": "vite build",
            "preview": "vite preview",
        },
        "dependencies": {
            "react": "^18.3.0",
            "react-dom": "^18.3.0",
        },
        "devDependencies": {
            "@vitejs/plugin-react": "^4.3.0",
            "vite": "^5.4.0",
        },
    })


def _manifest_index_html(brief: str) -> str:
    """Deterministic Vite entry HTML."""
    return (
        '<!doctype html>\n'
        '<html lang="en">\n'
        '  <head>\n'
        '    <meta charset="UTF-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        '    <title>Homelab Dashboard</title>\n'
        '  </head>\n'
        '  <body>\n'
        '    <div id="root"></div>\n'
        '    <script type="module" src="/src/main.jsx"></script>\n'
        '  </body>\n'
        '</html>\n'
    )


def _manifest_vite_config(brief: str) -> str:
    """Deterministic vite.config.js for React."""
    return (
        "import { defineConfig } from 'vite';\n"
        "import react from '@vitejs/plugin-react';\n"
        "\n"
        "export default defineConfig({\n"
        "  plugins: [react()],\n"
        "  server: {\n"
        "    port: 5180,\n"
        "    proxy: {\n"
        "      '/api': {\n"
        "        target: 'http://localhost:3100',\n"
        "        changeOrigin: true,\n"
        "      },\n"
        "    },\n"
        "  },\n"
        "});\n"
    )


def _manifest_main_jsx(brief: str) -> str:
    """Deterministic React 18 mount point."""
    return (
        "import React from 'react';\n"
        "import ReactDOM from 'react-dom/client';\n"
        "import App from './App.jsx';\n"
        "import './styles.css';\n"
        "\n"
        "ReactDOM.createRoot(document.getElementById('root')).render(\n"
        "  <React.StrictMode>\n"
        "    <App />\n"
        "  </React.StrictMode>\n"
        ");\n"
    )


def _manifest_next(brief: str) -> str:
    return _json_dumps_pretty({
        "name": "scaffold",
        "version": "0.1.0",
        "private": True,
        "scripts": {
            "dev": "next dev",
            "build": "next build",
            "start": "next start",
            "lint": "next lint",
        },
        "dependencies": {
            "next": "^14.2.0",
            "react": "^18.3.0",
            "react-dom": "^18.3.0",
        },
        "devDependencies": {
            "@types/node": "^20.12.0",
            "@types/react": "^18.3.0",
            "@types/react-dom": "^18.3.0",
            "typescript": "^5.4.0",
        },
    })


def _manifest_ios_app(brief: str) -> str:
    return (
        "// swift-tools-version:5.9\n"
        "import PackageDescription\n"
        "\n"
        "let package = Package(\n"
        '    name: "App",\n'
        "    platforms: [\n"
        "        .iOS(.v16),\n"
        "    ],\n"
        "    products: [\n"
        '        .executable(name: "App", targets: ["App"]),\n'
        "    ],\n"
        "    targets: [\n"
        "        .executableTarget(\n"
        '            name: "App",\n'
        '            path: "Sources/App"\n'
        "        ),\n"
        "    ]\n"
        ")\n"
    )


def _json_dumps_pretty(obj) -> str:
    """package.json convention: 2-space indent, trailing newline."""
    import json as _json
    return _json.dumps(obj, indent=2) + "\n"


def _env_example_for_services(brief: str) -> str:
    """Deterministic .env.example for backend-tier scaffolds.

    Locks the port plan (PORT=3100, CORS_ORIGIN=http://localhost:5180)
    so the LLM can't drift back to 3001/5173 and cause the CORS
    rejections we keep hitting (v15, v24). Per-service blocks are
    emitted only for services the brief explicitly mentioned (the
    detected SEED list), avoiding noise from extensibility examples.
    """
    services = _detect_services(brief)
    lines: List[str] = [
        "# Copy this file to .env. Real secrets go here — do not commit .env.",
        "# Default ports match the SkyN3t generated-app plan:",
        "#   backend  → PORT=3100",
        "#   frontend → http://localhost:5180 (Vite dev)",
        "# These avoid colliding with the SkyN3t studio on 5173/6660.",
        "",
        "NODE_ENV=development",
        "PORT=3100",
        "CORS_ORIGIN=http://localhost:5180",
        "CORS_ORIGINS=http://localhost:5180,http://localhost:5173",
        "",
    ]
    # Per-service env blocks. Each service has its known default port
    # and the env-var names the adapter expects.
    _SERVICE_DEFAULTS: dict = {
        "sonarr":         (8989, ("SONARR_URL", "SONARR_API_KEY")),
        "radarr":         (7878, ("RADARR_URL", "RADARR_API_KEY")),
        "prowlarr":       (9696, ("PROWLARR_URL", "PROWLARR_API_KEY")),
        "qbittorrent":    (8080, ("QBITTORRENT_URL", "QBITTORRENT_USER", "QBITTORRENT_PASS")),
        "emby":           (8096, ("EMBY_URL", "EMBY_API_KEY")),
        "jellyfin":       (8096, ("JELLYFIN_URL", "JELLYFIN_API_KEY")),
        "plex":           (32400, ("PLEX_URL", "PLEX_TOKEN")),
        "sonos":          (5005, ("SONOS_HTTP_API_URL",)),
        "docker":         (2375, ("DOCKER_SOCKET_PATH",)),
        "home_assistant": (8123, ("HOME_ASSISTANT_URL", "HOME_ASSISTANT_TOKEN")),
        "pihole":         (80, ("PIHOLE_URL", "PIHOLE_API_KEY")),
        "unifi":          (8443, ("UNIFI_URL", "UNIFI_USER", "UNIFI_PASS")),
        "transmission":   (9091, ("TRANSMISSION_URL", "TRANSMISSION_USER", "TRANSMISSION_PASS")),
        "nzbget":         (6789, ("NZBGET_URL", "NZBGET_USER", "NZBGET_PASS")),
        "sabnzbd":        (8080, ("SABNZBD_URL", "SABNZBD_API_KEY")),
        "overseerr":      (5055, ("OVERSEERR_URL", "OVERSEERR_API_KEY")),
        "tautulli":       (8181, ("TAUTULLI_URL", "TAUTULLI_API_KEY")),
    }
    for slug in services:
        port, vars_ = _SERVICE_DEFAULTS.get(slug, (0, ()))
        if not vars_:
            continue
        lines.append(f"# {slug.replace('_', ' ').title()}"
                     + (f" (default port {port})" if port else ""))
        for v in vars_:
            if v.endswith("_URL"):
                lines.append(f"{v}=http://localhost:{port}")
            elif v.endswith(("_USER", "_USERNAME")):
                lines.append(f"{v}=admin")
            elif v.endswith(("_PASS", "_PASSWORD")):
                lines.append(f"{v}=changeme")
            elif v.endswith("_SOCKET_PATH"):
                lines.append(f"{v}=/var/run/docker.sock")
            else:
                lines.append(f"{v}=replace_with_real_value")
        lines.append("")
    return "\n".join(lines)


def _docker_compose_for_services(brief: str) -> str:
    """Deterministic docker-compose.yml — locks the port plan so
    `docker compose up` matches the .env-file expectations."""
    return (
        "# docker-compose for the generated homelab dashboard.\n"
        "# Default ports match the SkyN3t plan (no studio collision):\n"
        "#   web → 5180, api → 3100\n"
        "version: \"3.9\"\n"
        "services:\n"
        "  api:\n"
        "    build: ./server\n"
        "    env_file: .env\n"
        "    environment:\n"
        "      - PORT=3100\n"
        "      - CORS_ORIGIN=http://localhost:5180\n"
        "    ports:\n"
        "      - \"3100:3100\"\n"
        "    restart: unless-stopped\n"
        "  web:\n"
        "    build: .\n"
        "    environment:\n"
        "      - VITE_API_BASE_URL=http://localhost:3100\n"
        "    ports:\n"
        "      - \"5180:5180\"\n"
        "    depends_on:\n"
        "      - api\n"
        "    restart: unless-stopped\n"
    )


def _server_package_json_for_services(brief: str) -> str:
    """Deterministic server/package.json — locks "type": "module" so
    the LLM can't ship an index.js that mixes CJS imports of ESM
    adapters (the v15 manual-fix bug). Includes the standard server
    deps; the LLM can still extend in the per-adapter prompt."""
    return (
        "{\n"
        '  "name": "homelab-dashboard-server",\n'
        '  "version": "1.0.0",\n'
        '  "private": true,\n'
        '  "type": "module",\n'
        '  "main": "index.js",\n'
        '  "engines": {\n'
        '    "node": ">=20.0.0"\n'
        '  },\n'
        '  "scripts": {\n'
        '    "start": "node index.js",\n'
        '    "dev": "node --watch index.js"\n'
        '  },\n'
        '  "dependencies": {\n'
        '    "express": "^4.21.2",\n'
        '    "cors": "^2.8.5",\n'
        '    "dotenv": "^16.4.7"\n'
        "  }\n"
        "}\n"
    )


def _component_status_pill(brief: str) -> str:
    """Deterministic StatusPill — universal 'is it OK' indicator.

    The LLM keeps reinventing this with subtly different shapes
    (different padding, different colors, sometimes only colored
    text with no dot). Ship the canonical shape so every card uses
    the same one.
    """
    return (
        "// Universal status pill. Shape locked by SkyN3t design system:\n"
        "// dot + uppercase label, rounded-full, tinted background. Don't\n"
        "// invent variants — pass tone='ok'|'warn'|'err'|'info'|'idle'.\n"
        "\n"
        "const TONES = {\n"
        "  ok:   { color: '#10B981', bg: 'rgba(16,185,129,0.12)' },\n"
        "  warn: { color: '#F59E0B', bg: 'rgba(245,158,11,0.12)' },\n"
        "  err:  { color: '#EF4444', bg: 'rgba(239,68,68,0.12)' },\n"
        "  info: { color: '#3B82F6', bg: 'rgba(59,130,246,0.12)' },\n"
        "  idle: { color: '#6B7280', bg: 'rgba(107,114,128,0.12)' },\n"
        "};\n"
        "\n"
        "export default function StatusPill({ tone = 'ok', children }) {\n"
        "  const t = TONES[tone] || TONES.idle;\n"
        "  return (\n"
        "    <span\n"
        "      style={{\n"
        "        display: 'inline-flex',\n"
        "        alignItems: 'center',\n"
        "        gap: 6,\n"
        "        padding: '2px 8px',\n"
        "        borderRadius: 9999,\n"
        "        fontSize: 10,\n"
        "        fontWeight: 500,\n"
        "        textTransform: 'uppercase',\n"
        "        letterSpacing: '0.05em',\n"
        "        color: t.color,\n"
        "        background: t.bg,\n"
        "      }}\n"
        "    >\n"
        "      <span style={{\n"
        "        width: 6, height: 6, borderRadius: '50%',\n"
        "        background: t.color,\n"
        "      }} />\n"
        "      {children}\n"
        "    </span>\n"
        "  );\n"
        "}\n"
    )


def _component_sparkline(brief: str) -> str:
    """Deterministic Sparkline — tiny inline time-series SVG.

    Hand-rolled (no recharts dep). The LLM keeps either pulling in
    recharts for trivial sparklines (heavy) or rendering a flat
    line. Ship the canonical 60×24 SVG version.
    """
    return (
        "// Tiny inline time-series sparkline. Hand-rolled SVG — no\n"
        "// recharts/d3 dependency. For ~60 samples over 1h, this\n"
        "// renders at 60×24 inside a service card.\n"
        "\n"
        "export default function Sparkline({\n"
        "  values,\n"
        "  color = '#3B82F6',\n"
        "  height = 24,\n"
        "  width = 60,\n"
        "}) {\n"
        "  if (!Array.isArray(values) || values.length < 2) {\n"
        "    return (\n"
        "      <svg width={width} height={height} role=\"img\" aria-label=\"no data\">\n"
        "        <line\n"
        "          x1={0} y1={height / 2} x2={width} y2={height / 2}\n"
        "          stroke=\"currentColor\" strokeWidth={1}\n"
        "          strokeDasharray=\"3 3\" opacity={0.3}\n"
        "        />\n"
        "      </svg>\n"
        "    );\n"
        "  }\n"
        "  const max = Math.max(...values, 1);\n"
        "  const min = Math.min(...values, 0);\n"
        "  const range = max - min || 1;\n"
        "  const step = width / (values.length - 1);\n"
        "  const points = values\n"
        "    .map((v, i) => `${i * step},${height - ((v - min) / range) * height}`)\n"
        "    .join(' ');\n"
        "  return (\n"
        "    <svg width={width} height={height} role=\"img\" aria-label=\"trend\">\n"
        "      <polyline\n"
        "        points={points} fill=\"none\" stroke={color}\n"
        "        strokeWidth={1.5} strokeLinecap=\"round\" strokeLinejoin=\"round\"\n"
        "      />\n"
        "    </svg>\n"
        "  );\n"
        "}\n"
    )


def _component_kpi_tile(brief: str) -> str:
    """Deterministic KpiTile — top-of-dashboard summary tile."""
    return (
        "// KPI tile for the top-of-dashboard summary strip. Locked\n"
        "// shape per the SkyN3t design system: label / value+unit /\n"
        "// optional delta. Tones tint the left border.\n"
        "\n"
        "const TONES = {\n"
        "  ok:   '#10B981',\n"
        "  warn: '#F59E0B',\n"
        "  err:  '#EF4444',\n"
        "  info: '#3B82F6',\n"
        "  idle: '#6B7280',\n"
        "};\n"
        "\n"
        "export default function KpiTile({\n"
        "  label,\n"
        "  value,\n"
        "  unit,\n"
        "  delta,\n"
        "  tone = 'ok',\n"
        "}) {\n"
        "  const accent = TONES[tone] || TONES.idle;\n"
        "  return (\n"
        "    <div\n"
        "      style={{\n"
        "        background: 'var(--panel, #161b22)',\n"
        "        border: '1px solid var(--border, #2a3142)',\n"
        "        borderLeft: `2px solid ${accent}`,\n"
        "        borderRadius: 8,\n"
        "        padding: 12,\n"
        "        minWidth: 140,\n"
        "      }}\n"
        "    >\n"
        "      <div style={{\n"
        "        fontSize: 11,\n"
        "        textTransform: 'uppercase',\n"
        "        letterSpacing: '0.05em',\n"
        "        color: 'var(--text-dim, #8d96a7)',\n"
        "        marginBottom: 4,\n"
        "      }}>{label}</div>\n"
        "      <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>\n"
        "        <span style={{\n"
        "          fontSize: 22, fontWeight: 600,\n"
        "          fontVariantNumeric: 'tabular-nums',\n"
        "          color: 'var(--text, #e6edf3)',\n"
        "        }}>{value}</span>\n"
        "        {unit && (\n"
        "          <span style={{\n"
        "            fontSize: 13,\n"
        "            color: 'var(--text-dim, #8d96a7)',\n"
        "          }}>{unit}</span>\n"
        "        )}\n"
        "      </div>\n"
        "      {delta && (\n"
        "        <div style={{\n"
        "          fontSize: 10,\n"
        "          color: 'var(--text-dim, #8d96a7)',\n"
        "          marginTop: 4,\n"
        "        }}>{delta}</div>\n"
        "      )}\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )


# Homelab-specific generators were factored out into stack_templates_homelab
# but that module was deleted during the anti-leak cleanup. The lazy import
# returns None when the module is unavailable; each generator returns None,
# and the dispatch lookup falls through to LLM generation — same behavior
# as a non-dashboard brief.
_homelab: Any = None
_homelab_attempted = False

def _homelab_mod() -> Any:
    global _homelab, _homelab_attempted
    if _homelab is None and not _homelab_attempted:
        _homelab_attempted = True
        try:
            from skyn3t.agents import stack_templates_homelab as _mod  # type: ignore
            _homelab = _mod
        except ImportError:
            _homelab = None
    return _homelab


def _component_app_jsx_homelab(brief: str) -> Optional[str]:
    mod = _homelab_mod()
    if mod is None or not _needs_design_system(brief):
        return None
    return cast(Optional[str], mod.app_jsx(_detect_services(brief)))


def _component_styles_css_homelab(brief: str, palette_hexes: Optional[List[str]] = None) -> Optional[str]:
    mod = _homelab_mod()
    if mod is None or not _needs_design_system(brief):
        return None
    return cast(Optional[str], mod.styles_css(palette_hexes))


def _hook_use_polling(brief: str) -> Optional[str]:
    mod = _homelab_mod()
    if mod is None or not _needs_design_system(brief):
        return None
    return cast(Optional[str], mod.use_polling_hook())


def _component_command_palette(brief: str) -> Optional[str]:
    # No _needs_design_system gate here: this generator is only reached
    # via manifest_for() when the scaffold's App.jsx already imports
    # CommandPalette.jsx — that import IS the signal. The brief-level
    # gate was incorrectly suppressing backfill for briefs like "build
    # a polished dashboard" that ask for these primitives indirectly.
    mod = _homelab_mod()
    if mod is None:
        return None
    return cast(Optional[str], mod.command_palette())


def _component_service_detail(brief: str) -> Optional[str]:
    mod = _homelab_mod()
    if mod is None:
        return None
    return cast(Optional[str], mod.service_detail())


def _component_activity_feed(brief: str) -> Optional[str]:
    mod = _homelab_mod()
    if mod is None:
        return None
    return cast(Optional[str], mod.activity_feed())


def _server_config_store_homelab(brief: str) -> Optional[str]:
    mod = _homelab_mod()
    if mod is None or not _needs_configurable_ui(brief):
        return None
    return cast(Optional[str], mod.config_store_js(_detect_services(brief)))


def _server_index_homelab(brief: str) -> Optional[str]:
    mod = _homelab_mod()
    if mod is None or not _needs_backend(brief):
        return None
    return cast(Optional[str], mod.server_index_js())


def _server_config_route_homelab(brief: str) -> Optional[str]:
    mod = _homelab_mod()
    if mod is None or not _needs_configurable_ui(brief):
        return None
    return cast(Optional[str], mod.config_route_js())


def _component_settings_modal_homelab(brief: str) -> Optional[str]:
    mod = _homelab_mod()
    if mod is None or not _needs_configurable_ui(brief):
        return None
    return cast(Optional[str], mod.settings_modal_jsx())


def _component_service_editor_homelab(brief: str) -> Optional[str]:
    mod = _homelab_mod()
    if mod is None or not _needs_configurable_ui(brief):
        return None
    return cast(Optional[str], mod.service_editor_jsx())


def _hook_use_config_homelab(brief: str) -> Optional[str]:
    mod = _homelab_mod()
    if mod is None or not _needs_configurable_ui(brief):
        return None
    return cast(Optional[str], mod.use_config_js())


_MANIFEST_GENERATORS = {
    # path → generator. Keyed by the SPECIFIC file the generator writes
    # so CodeAgent can match on filename and short-circuit.
    ("python_cli", "requirements.txt"): _manifest_python_cli,
    ("fastapi", "requirements.txt"): _manifest_fastapi,
    ("flask", "requirements.txt"): _manifest_flask,
    ("node_cli", "package.json"): _manifest_node_cli,
    ("react_vite", "package.json"): _manifest_react_vite,
    ("next", "package.json"): _manifest_next,
    ("ios_app", "Package.swift"): _manifest_ios_app,
    # Backend-tier files — deterministic so LLM drift can't reintroduce
    # the recurring port / module-style / CORS bugs.
    ("react_vite", ".env.example"): _env_example_for_services,
    ("next", ".env.example"): _env_example_for_services,
    ("react_vite", "docker-compose.yml"): _docker_compose_for_services,
    ("next", "docker-compose.yml"): _docker_compose_for_services,
    ("react_vite", "server/index.js"): _server_index_homelab,
    ("react_vite", "server/package.json"): _server_package_json_for_services,
    ("next", "server/package.json"): _server_package_json_for_services,
    ("react_vite", "server/config-store.js"): _server_config_store_homelab,
    ("react_vite", "server/routes/config.js"): _server_config_route_homelab,
    ("react_vite", "src/components/SettingsModal.jsx"): _component_settings_modal_homelab,
    ("react_vite", "src/components/ServiceEditor.jsx"): _component_service_editor_homelab,
    ("react_vite", "src/hooks/useConfig.js"): _hook_use_config_homelab,
    # Core Vite scaffolding — these are pure boilerplate; letting the
    # LLM regenerate them wastes tokens and creates a vector for stack
    # drift (v37: kimi rewrote the react_vite plan as Next.js). Locking
    # them down eliminates the "wrong ecosystem" failure mode entirely.
    ("react_vite", "index.html"): _manifest_index_html,
    ("react_vite", "vite.config.js"): _manifest_vite_config,
    ("react_vite", "src/main.jsx"): _manifest_main_jsx,
    # Design-system primitives + App shell — every dashboard reinvents
    # these subtly differently. Locked shapes save the LLM a roundtrip
    # AND eliminate inconsistent variants. For dashboard briefs we ship
    # the deterministic App.jsx/styles.css so the stack can't drift;
    # for non-dashboard briefs these generators return None and the
    # LLM path wins as before.
    ("react_vite", "src/components/StatusPill.jsx"):   _component_status_pill,
    ("react_vite", "src/components/Sparkline.jsx"):    _component_sparkline,
    ("react_vite", "src/components/KpiTile.jsx"):      _component_kpi_tile,
    ("react_vite", "src/App.jsx"):                     _component_app_jsx_homelab,
    ("react_vite", "src/styles.css"):                  _component_styles_css_homelab,
    ("react_vite", "src/hooks/usePolling.js"):         _hook_use_polling,
    ("react_vite", "src/components/CommandPalette.jsx"): _component_command_palette,
    ("react_vite", "src/components/ServiceDetail.jsx"):  _component_service_detail,
    ("react_vite", "src/components/ActivityFeed.jsx"):   _component_activity_feed,
    ("next",       "src/components/StatusPill.jsx"):   _component_status_pill,
    ("next",       "src/components/Sparkline.jsx"):    _component_sparkline,
    ("next",       "src/components/KpiTile.jsx"):      _component_kpi_tile,
    ("next",       "src/hooks/usePolling.js"):         _hook_use_polling,
    ("next",       "src/components/CommandPalette.jsx"): _component_command_palette,
    ("next",       "src/components/ServiceDetail.jsx"):  _component_service_detail,
    ("next",       "src/components/ActivityFeed.jsx"):   _component_activity_feed,
}


def manifest_for(
    stack: Optional[str],
    rel_path: str,
    brief: str = "",
    *,
    palette_hexes: Optional[List[str]] = None,
) -> Optional[str]:
    """Return the deterministic manifest body for a (stack, file path)
    combo, or None when no generator applies.

    Used by CodeAgent's Phase 2 loop to short-circuit dependency files
    (requirements.txt, package.json, Package.swift) — the LLM keeps
    re-deriving these and sometimes drifts to outdated pins.

    ``palette_hexes`` is an optional list of hex colors read from
    ``artifact_dir/palette.json``. When provided, palette-aware
    generators (``styles.css`` today) weave the brand colors into the
    output so the scaffold doesn't ship the default slate palette
    regardless of what DesignerAgent picked.
    """
    if not stack or not rel_path:
        return None
    # Normalize the relative path so a top-level match works regardless
    # of incoming case/slashes.
    key = (stack, rel_path.lstrip("/").strip())
    gen = _MANIFEST_GENERATORS.get(key)
    if gen is None:
        return None
    # Only the palette-aware generators accept the kwarg. Detect by
    # signature so we don't break the legion of `(brief)` generators.
    import inspect
    try:
        sig = inspect.signature(gen)
        if "palette_hexes" in sig.parameters:
            # mypy can't follow the inspect-based signature check; cast to
            # an Any-typed callable so the palette-aware kwarg goes through.
            from typing import Any, cast
            result = cast(Any, gen)(brief or "", palette_hexes=palette_hexes)
            return cast(Optional[str], result)
    except (TypeError, ValueError):
        pass
    return gen(brief or "")


def readme_for_stack(stack: Optional[str], brief: str) -> Optional[str]:
    """Return a deterministic README.md body for the given stack, or None
    when the stack isn't recognized. ``brief`` is interpolated as the
    project description in the first paragraph.
    """
    if not stack:
        return None
    gen = _README_GENERATORS.get(stack)
    return gen(brief or "") if gen else None


def validate_stack_shape(stack: Optional[str], written_files: List[str]) -> List[str]:
    """Return a list of mismatch errors when files on disk contradict ``stack``.

    This is the last line of defense against stack drift: even if
    ``detect_stack`` and ``plan_for_stack`` agreed on ``react_vite``,
    a rogue LLM response or a stale manifest could still write
    ``app/page.tsx`` or ``next.config.js``.  Catching those before the
    build verifier runs saves minutes of misleading "cannot find module
    'next'" errors.
    """
    if not stack or not written_files:
        return []

    # Normalise to scaffold-relative paths with forward slashes.
    rels: set[str] = set()
    for f in written_files:
        p = f.replace("\\", "/").lstrip("/")
        # Strip a leading ``scaffold/`` prefix so the check works whether
        # the caller passes absolute paths or manifest-relative ones.
        if p.startswith("scaffold/"):
            p = p[len("scaffold/"):]
        rels.add(p)

    errors: list[str] = []

    # ── react_vite must NOT contain Next.js boilerplate ──────────────────
    if stack == "react_vite":
        nextjs_signatures = {
            "app/page.tsx", "app/page.jsx",
            "app/layout.tsx", "app/layout.jsx",
            "app/globals.css",
            "next.config.js", "next.config.ts", "next.config.mjs",
            "next.config.cjs",
        }
        for sig in nextjs_signatures:
            if sig in rels:
                errors.append(
                    f"Stack mismatch: '{sig}' is a Next.js file but stack is react_vite"
                )
        # package.json with a "next" dependency is also a smoking gun.
        # We can't parse JSON here (the file may be malformed), but if
        # the path exists we can do a cheap string sniff later in the
        # runner.  For now the path-based check above catches the
        # deterministic-drift cases (v42-retry-retry).

    # ── next must NOT contain Vite boilerplate ───────────────────────────
    elif stack == "next":
        vite_signatures = {
            "vite.config.js", "vite.config.ts", "vite.config.mjs",
            "index.html",       # Next.js doesn't use a root index.html
            "src/main.jsx", "src/main.tsx",
        }
        for sig in vite_signatures:
            if sig in rels:
                errors.append(
                    f"Stack mismatch: '{sig}' is a Vite file but stack is next"
                )

    # ── fastapi must NOT contain Node / Next / Vite files ─────────────────
    elif stack == "fastapi":
        foreign = {
            "package.json", "vite.config.js", "next.config.js",
            "app/page.tsx", "app/layout.tsx", "index.html",
            "src/main.jsx", "src/App.jsx",
        }
        for sig in foreign:
            if sig in rels:
                errors.append(
                    f"Stack mismatch: '{sig}' is a frontend/Node file but stack is fastapi"
                )

    # ── python_cli must NOT contain web-framework files ──────────────────
    elif stack == "python_cli":
        foreign = {
            "package.json", "vite.config.js", "next.config.js",
        }
        for sig in foreign:
            if sig in rels:
                errors.append(
                    f"Stack mismatch: '{sig}' is a web/Node file but stack is python_cli"
                )

    return errors
