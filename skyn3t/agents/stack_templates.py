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
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

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
}


# Detection: each stack key maps to a list of trigger phrases. First
# match wins, in declaration order (so more specific patterns come
# before more general ones — e.g. "next.js" before "react").
_STACK_TRIGGERS: List[Tuple[str, Tuple[str, ...]]] = [
    ("next", ("next.js", "nextjs", "next 14", "next 13", " next ")),
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


def detect_stack(brief: str) -> Optional[str]:
    """Pick a stack template key from the brief. None when no match.

    Conservative on purpose: when no signal is found, returns None so
    CodeAgent falls back to its LLM-only planning path. A wrong template
    is worse than no template.
    """
    if not brief:
        return None
    text = brief.lower()
    for stack, phrases in _STACK_TRIGGERS:
        for phrase in phrases:
            if phrase in text:
                return stack
    return None


def plan_for_stack(stack: str) -> Optional[FilePlan]:
    """Return the file plan for ``stack``, or None if unknown."""
    return STACK_TEMPLATES.get(stack)


def template_keys() -> List[str]:
    """All known stack keys, sorted for stable test assertions."""
    return sorted(STACK_TEMPLATES.keys())


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
        "[react()] })` with `@vitejs/plugin-react`."
    ),
    "next": (
        "Idiom: Next 14+ with the App Router (app/ directory). NEVER use "
        "pages/. Default export functional components from app/*.tsx. "
        "app/layout.tsx exports a default RootLayout with `children`. "
        "app/page.tsx is the index route. metadata is exported from "
        "layout. tsconfig.json sets `strict: true` and "
        "`moduleResolution: \"bundler\"`."
    ),
}


def hint_for_stack(stack: Optional[str]) -> str:
    """Return the per-stack idiom hint, or empty string when none."""
    if not stack:
        return ""
    return STACK_BUILD_HINTS.get(stack, "")
