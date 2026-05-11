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
}


def manifest_for(stack: Optional[str], rel_path: str, brief: str = "") -> Optional[str]:
    """Return the deterministic manifest body for a (stack, file path)
    combo, or None when no generator applies.

    Used by CodeAgent's Phase 2 loop to short-circuit dependency files
    (requirements.txt, package.json, Package.swift) — the LLM keeps
    re-deriving these and sometimes drifts to outdated pins.
    """
    if not stack or not rel_path:
        return None
    # Normalize the relative path so a top-level match works regardless
    # of incoming case/slashes.
    key = (stack, rel_path.lstrip("/").strip())
    gen = _MANIFEST_GENERATORS.get(key)
    return gen(brief or "") if gen else None


def readme_for_stack(stack: Optional[str], brief: str) -> Optional[str]:
    """Return a deterministic README.md body for the given stack, or None
    when the stack isn't recognized. ``brief`` is interpolated as the
    project description in the first paragraph.
    """
    if not stack:
        return None
    gen = _README_GENERATORS.get(stack)
    return gen(brief or "") if gen else None
