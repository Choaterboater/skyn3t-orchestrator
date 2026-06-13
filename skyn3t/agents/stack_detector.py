"""Detect a project's stack family + runtimes by reading manifest files.

Used by PackagingAgent to pick the right packaging strategy (web vs
docker vs fullstack). No LLM calls — pure manifest inspection so the
detection is deterministic and fast.

Reads:
- ``package.json``                 — Node/JS frameworks + runtime version
- ``pyproject.toml`` / ``requirements.txt`` — Python frameworks
- ``Dockerfile``                   — containerization signal
- ``docker-compose.yml`` / ``compose.yaml`` — service topology
- ``vite.config.*`` / ``next.config.*`` / ``svelte.config.*`` — bundler hints

Returns a typed ``StackDetection`` that downstream code matches on
``family`` to dispatch packaging strategies. Falls back to
``family="unknown"`` cleanly when nothing recognizable is found.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

# Packaging strategies. "fullstack" means web + server in one project tree.
# "cli", "mobile", and "desktop" are recognized families even though the
# built-in packaging templates currently only ship for web/server/fullstack;
# detecting them lets the pipeline fail loudly instead of silently treating
# a React Native or Tauri app as a generic web project.
Family = Literal["web", "server", "fullstack", "cli", "mobile", "desktop", "unknown"]


@dataclass
class Runtime:
    """One language runtime the project needs at install/run time."""

    name: str                    # "node" | "python" | "deno" | "bun"
    min_version: Optional[str] = None
    install_url: Optional[str] = None


@dataclass
class StackDetection:
    """Aggregated detection result."""

    family: Family = "unknown"
    # Specific stack the architect / code agent picked. Use this to pick
    # per-stack templates inside PackagingAgent.
    stack: Optional[str] = None  # "react_vite" | "next" | "fastapi" | "flask" | "express" | "hono" | ...
    runtimes: List[Runtime] = field(default_factory=list)
    # External services declared in compose or implied by deps — the docker
    # strategy needs this list to write the right compose stanzas.
    services: List[str] = field(default_factory=list)  # "postgres", "redis", "mongodb", ...
    has_dockerfile: bool = False
    has_compose: bool = False
    # Useful for downstream messaging — populated when we can find a
    # specific marker file but couldn't fully classify.
    confidence_notes: List[str] = field(default_factory=list)
    # Architect-declared port override from decisions.json. When set,
    # _server_port returns this instead of looking up the per-stack
    # default — keeping Dockerfile/compose/READMEs in sync with the
    # architect's contract.
    port_override: Optional[int] = None

    @property
    def is_web(self) -> bool:
        return self.family in ("web", "fullstack")

    @property
    def is_server(self) -> bool:
        return self.family in ("server", "fullstack")

    @property
    def is_packaged(self) -> bool:
        """True when the project family has built-in packaging support."""
        return self.family in ("web", "server", "fullstack")


# ---------------------------------------------------------------------------
# Manifest heuristics
# ---------------------------------------------------------------------------

# Canonical name aliases. The scoreboard (build_patterns.json) historically
# split the same logical stack across two keys ("vite_react" vs "react_vite")
# because different record sites named it differently. ``detect()`` already
# emits "react_vite", but normalize any legacy/alias names through here so
# every caller — and the backfill script — converges on one canonical bucket.
_CANONICAL_STACK_NAMES: Dict[str, str] = {
    "vite_react": "react_vite",
}

# CLI-stack signatures by package.json dependency.
_CLI_STACK_BY_NODE_DEP: List[tuple[str, str]] = [
    ("commander", "node_cli"),
    ("yargs", "node_cli"),
    ("minimist", "node_cli"),
    ("inquirer", "node_cli"),
    ("oclif", "node_cli"),
    ("ink", "node_cli"),
]

_CLI_STACK_BY_PY_DEP: List[tuple[str, str]] = [
    ("click", "python_cli"),
    ("typer", "python_cli"),
    ("rich", "python_cli"),
]

# Mobile-stack signatures.
_MOBILE_STACK_BY_DEP: List[tuple[str, str]] = [
    ("react-native", "react_native"),
    ("expo", "expo"),
    ("@capacitor/core", "capacitor"),
]

# Desktop-stack signatures.
_DESKTOP_STACK_BY_DEP: List[tuple[str, str]] = [
    ("electron", "electron"),
    ("@tauri-apps/cli", "tauri"),
    ("@tauri-apps/api", "tauri"),
]

# Web-stack signatures by package.json dep. Order matters — more specific
# (next > react_vite > generic_react) so the most-informative one wins.
_WEB_STACK_BY_DEP: List[tuple[str, str]] = [
    ("next", "next"),
    ("@sveltejs/kit", "sveltekit"),
    ("astro", "astro"),
    ("nuxt", "nuxt"),
    ("@remix-run/react", "remix"),
    ("vite", "react_vite"),       # vite alone classifies as react_vite by default;
                                  # specific framework deps above override it.
]

# Server-stack signatures by package.json dep.
_SERVER_STACK_BY_NODE_DEP: List[tuple[str, str]] = [
    ("@hono/node-server", "hono"),
    ("hono", "hono"),
    ("fastify", "fastify"),
    ("@koa/router", "koa"),
    ("koa", "koa"),
    ("express", "express"),
]

# Server-stack signatures by Python dep. Matched against the union of
# pyproject.toml [project.dependencies] and requirements.txt names.
_SERVER_STACK_BY_PY_DEP: List[tuple[str, str]] = [
    ("fastapi", "fastapi"),
    ("flask", "flask"),
    ("django", "django"),
    ("starlette", "starlette"),
    ("aiohttp", "aiohttp"),
    ("bottle", "bottle"),
]

# Database / cache / queue services we know how to seed in docker-compose.
# Detected from deps that imply them (psycopg2 → postgres, redis → redis,
# pymongo → mongodb), or directly from compose service names.
_SERVICE_BY_DEP: Dict[str, str] = {
    # Python
    "psycopg2": "postgres",
    "psycopg2-binary": "postgres",
    "psycopg": "postgres",
    "asyncpg": "postgres",
    "sqlalchemy": "postgres",      # ambiguous; treated as postgres hint
    "redis": "redis",
    "pymongo": "mongodb",
    "motor": "mongodb",
    "elasticsearch": "elasticsearch",
    "celery": "redis",             # celery defaults to redis broker
    # Node
    "pg": "postgres",
    "postgres": "postgres",
    "mongodb": "mongodb",
    "mongoose": "mongodb",
    "ioredis": "redis",
}


# ---------------------------------------------------------------------------
# Manifest readers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Optional[Dict[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items()}
        return None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _node_deps(pkg: Dict[str, object]) -> Dict[str, str]:
    """Union of dependencies + devDependencies + peerDependencies."""
    out: Dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = pkg.get(key)
        if isinstance(section, dict):
            for name, ver in section.items():
                if isinstance(name, str) and isinstance(ver, str):
                    out.setdefault(name, ver)
    return out


def _parse_requirements_txt(text: str) -> List[str]:
    """Pull bare package names out of a requirements.txt-style file.

    Ignores comments, blank lines, version specifiers, and editable installs.
    """
    out: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Take the part before any version specifier or extras.
        name = re.split(r"[<>=!~\[;]", line, maxsplit=1)[0].strip()
        if name:
            out.append(name.lower())
    return out


def _parse_pyproject_deps(pyproject_text: str) -> List[str]:
    """Extract dependency names from pyproject.toml without needing tomllib<3.11.

    Looks for ``dependencies = [ ... ]`` blocks (PEP 621) and the
    poetry-style ``[tool.poetry.dependencies]`` table. Regex-based because
    we only need names, not version constraints — keeps the agent dep-free.
    """
    names: List[str] = []

    # PEP 621 style: dependencies = ["fastapi >=0.100", "uvicorn[standard]", ...]
    m = re.search(
        r"\bdependencies\s*=\s*\[([^\]]*)\]",
        pyproject_text,
        re.DOTALL,
    )
    if m:
        for entry in re.findall(r"['\"]([^'\"\n]+)['\"]", m.group(1)):
            name = re.split(r"[<>=!~\[;]", entry, maxsplit=1)[0].strip()
            if name:
                names.append(name.lower())

    # Poetry style: [tool.poetry.dependencies] then key = "^1.0"
    poetry_block = re.search(
        r"\[tool\.poetry\.dependencies\](.*?)(?:\n\[|\Z)",
        pyproject_text,
        re.DOTALL,
    )
    if poetry_block:
        for line in poetry_block.group(1).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m2 = re.match(r"([A-Za-z0-9_\-\.]+)\s*=", stripped)
            if m2:
                name = m2.group(1).lower()
                # poetry uses "python" to declare the runtime requirement; not a real dep
                if name != "python":
                    names.append(name)

    return names


def _parse_compose_services(text: str) -> List[str]:
    """Pull top-level service NAMES out of a compose YAML by indentation.

    Doesn't need a YAML parser. Matches lines that are exactly two-space
    indented under a ``services:`` block and look like ``name:``.
    """
    names: List[str] = []
    in_services = False
    services_indent: Optional[int] = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        stripped = raw.lstrip()
        indent = len(raw) - len(stripped)
        if stripped.rstrip().rstrip(":") == "services" and not in_services:
            in_services = True
            services_indent = indent
            continue
        if in_services:
            if services_indent is not None and indent <= services_indent and stripped.endswith(":"):
                # Hit a sibling top-level key like "volumes:" — done with services.
                in_services = False
                continue
            # A service entry is indented one level under "services:" and ends with ":"
            if services_indent is not None and indent == services_indent + 2 and stripped.endswith(":"):
                name = stripped.rstrip().rstrip(":").strip()
                # Sub-keys like "image:" or "ports:" also end in ":" but live
                # at services_indent+4 — filter by indent guarantees that.
                # Reject obviously-not-a-service-name values.
                if name and re.match(r"^[A-Za-z0-9_\-]+$", name):
                    names.append(name)
    return names


# ---------------------------------------------------------------------------
# Runtime version pulls
# ---------------------------------------------------------------------------

def _node_runtime_from_pkg(pkg: dict) -> Runtime:
    engines = pkg.get("engines", {}) if isinstance(pkg.get("engines"), dict) else {}
    node_req = engines.get("node") if isinstance(engines.get("node"), str) else None
    min_version = None
    if node_req:
        # Pull the first version-looking token (e.g. ">=22.0.0" -> "22.0.0")
        m = re.search(r"(\d+(?:\.\d+){0,2})", node_req)
        if m:
            min_version = m.group(1)
    return Runtime(name="node", min_version=min_version, install_url="https://nodejs.org/")


def _python_runtime(pyproject_text: Optional[str], requirements_text: Optional[str]) -> Runtime:
    min_version = None
    if pyproject_text:
        # PEP 621: requires-python = ">=3.11"
        m = re.search(r"requires-python\s*=\s*['\"]([^'\"]+)['\"]", pyproject_text)
        if m:
            mv = re.search(r"(\d+\.\d+(?:\.\d+)?)", m.group(1))
            if mv:
                min_version = mv.group(1)
    return Runtime(name="python", min_version=min_version, install_url="https://python.org/")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def detect(root: Path) -> StackDetection:
    """Walk ``root`` and classify the project's stack.

    Looks at the scaffold dir if there's one (most SkyN3t projects nest
    their actual code under ``scaffold/``), otherwise the root itself.
    """
    if not root.is_dir():
        return StackDetection()

    # Most SkyN3t output puts the actual project under scaffold/. Look there
    # first, fall back to root if scaffold/ is empty or absent.
    scaffold = root / "scaffold"
    project_root = scaffold if (scaffold.is_dir() and any(scaffold.iterdir())) else root

    detection = StackDetection()
    web_dep_names: List[str] = []
    server_dep_names: List[str] = []
    py_dep_names: List[str] = []

    # ---- Node side ------------------------------------------------------
    pkg_path = project_root / "package.json"
    if pkg_path.is_file():
        pkg = _read_json(pkg_path) or {}
        deps = _node_deps(pkg)
        if deps:
            detection.runtimes.append(_node_runtime_from_pkg(pkg))
            web_dep_names = list(deps.keys())
            server_dep_names = list(deps.keys())
            # Pick the first matching web stack
            for dep_name, stack_name in _WEB_STACK_BY_DEP:
                if dep_name in deps:
                    detection.stack = stack_name
                    detection.family = "web"
                    break
            # Pick the first matching server stack
            for dep_name, stack_name in _SERVER_STACK_BY_NODE_DEP:
                if dep_name in deps:
                    if detection.family == "web":
                        # Already saw a web framework — this is fullstack.
                        detection.family = "fullstack"
                        # Keep the stack as the web one; the server stack is
                        # implicit from runtimes + the docker pass below.
                    else:
                        detection.stack = stack_name
                        detection.family = "server"
                    break
            # Services implied by Node deps
            for dep in deps:
                if dep in _SERVICE_BY_DEP and _SERVICE_BY_DEP[dep] not in detection.services:
                    detection.services.append(_SERVICE_BY_DEP[dep])

    # ---- Python side ----------------------------------------------------
    # Look in project_root first (most common), but also peek at the
    # artifact root when project_root is scaffold/. This catches the
    # fullstack monorepo layout:
    #   artifact/
    #     scaffold/        ← frontend (react/vite)
    #     requirements.txt ← backend (fastapi) — would be missed otherwise
    #     main.py
    py_search_dirs = [project_root]
    if project_root != root:
        py_search_dirs.append(root)

    pyproject_text: Optional[str] = None
    requirements_text: Optional[str] = None
    for search_dir in py_search_dirs:
        pp = search_dir / "pyproject.toml"
        rq = search_dir / "requirements.txt"
        if pp.is_file() and pyproject_text is None:
            pyproject_text = _read_text(pp)
        if rq.is_file() and requirements_text is None:
            requirements_text = _read_text(rq)

    if pyproject_text:
        py_dep_names.extend(_parse_pyproject_deps(pyproject_text))
    if requirements_text:
        py_dep_names.extend(_parse_requirements_txt(requirements_text))
    py_dep_set = {name for name in py_dep_names}

    if py_dep_names:
        detection.runtimes.append(_python_runtime(pyproject_text, requirements_text))
        for dep_name, stack_name in _SERVER_STACK_BY_PY_DEP:
            if dep_name in py_dep_set:
                if detection.family == "web":
                    # Web frontend + Python backend = fullstack
                    detection.family = "fullstack"
                elif detection.family == "unknown":
                    detection.family = "server"
                    detection.stack = stack_name
                # If family was already server (Node backend), keep the Node
                # stack as primary and let runtimes flag the Python presence.
                break
        # Services implied by Python deps
        for dep in py_dep_names:
            if dep in _SERVICE_BY_DEP and _SERVICE_BY_DEP[dep] not in detection.services:
                detection.services.append(_SERVICE_BY_DEP[dep])

    # ---- CLI / mobile / desktop ----------------------------------------
    # Only classify these when we didn't already find a web/server/fullstack
    # signal — a React frontend with a CLI helper is still a web app.
    if detection.family == "unknown":
        # Node CLI / mobile / desktop deps all live in package.json.
        if pkg_path.is_file():
            for dep_name, stack_name in _CLI_STACK_BY_NODE_DEP:
                if dep_name in deps:
                    detection.stack = stack_name
                    detection.family = "cli"
                    detection.confidence_notes.append(f"detected CLI dep {dep_name}")
                    break
            if detection.family == "unknown":
                for dep_name, stack_name in _MOBILE_STACK_BY_DEP:
                    if dep_name in deps:
                        detection.stack = stack_name
                        detection.family = "mobile"
                        detection.confidence_notes.append(f"detected mobile dep {dep_name}")
                        break
            if detection.family == "unknown":
                for dep_name, stack_name in _DESKTOP_STACK_BY_DEP:
                    if dep_name in deps:
                        detection.stack = stack_name
                        detection.family = "desktop"
                        detection.confidence_notes.append(f"detected desktop dep {dep_name}")
                        break
        # Python CLI deps.
        if detection.family == "unknown" and py_dep_names:
            for dep_name, stack_name in _CLI_STACK_BY_PY_DEP:
                if dep_name in py_dep_set:
                    detection.stack = stack_name
                    detection.family = "cli"
                    detection.confidence_notes.append(f"detected Python CLI dep {dep_name}")
                    break
        # Flutter / native mobile markers.
        if detection.family == "unknown":
            if (project_root / "pubspec.yaml").is_file():
                detection.stack = "flutter"
                detection.family = "mobile"
                detection.confidence_notes.append("detected Flutter pubspec.yaml")
            elif (
                (project_root / "ios" / "Runner.xcodeproj").is_dir()
                or (project_root / "android" / "app").is_dir()
            ):
                detection.stack = "native_mobile"
                detection.family = "mobile"
                detection.confidence_notes.append("detected iOS/Android project markers")
            elif (project_root / "src-tauri" / "tauri.conf.json").is_file():
                detection.stack = "tauri"
                detection.family = "desktop"
                detection.confidence_notes.append("detected Tauri config")

    # ---- Dockerfile / compose ------------------------------------------
    for candidate in ("Dockerfile", "dockerfile"):
        if (project_root / candidate).is_file() or (root / candidate).is_file():
            detection.has_dockerfile = True
            break
    for candidate in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        compose_path = project_root / candidate
        if not compose_path.is_file():
            compose_path = root / candidate
        if compose_path.is_file():
            detection.has_compose = True
            text = _read_text(compose_path) or ""
            # Compose services give us a more direct signal than dep
            # inference. Merge in services we didn't already infer.
            for svc in _parse_compose_services(text):
                lowered = svc.lower()
                # Only count services that look like infra, not "app" / "web".
                if lowered in {"postgres", "redis", "mongodb", "mongo", "elasticsearch",
                               "rabbitmq", "memcached", "minio", "clickhouse"}:
                    canonical = "mongodb" if lowered == "mongo" else lowered
                    if canonical not in detection.services:
                        detection.services.append(canonical)
            break

    # ---- Note any leftover ambiguity ------------------------------------
    if detection.family == "unknown":
        if web_dep_names or server_dep_names or py_dep_names:
            detection.confidence_notes.append(
                "manifest present but no recognized framework dep — likely a library or CLI tool"
            )
        elif not detection.runtimes:
            detection.confidence_notes.append("no manifest files found")
    elif detection.family in ("cli", "mobile", "desktop"):
        detection.confidence_notes.append(
            f"{detection.family} stack detected; built-in packaging templates are limited"
        )

    return detection


def detect_stack_from_scaffold(artifact_or_scaffold_dir: Path) -> str:
    """Return the canonical stack name for a scaffold/artifact directory.

    Thin wrapper over :func:`detect` that flattens the rich
    ``StackDetection`` to the single string the BuildPatternScoreboard
    keys on. ``detect`` is already scaffold-aware (it peeks under
    ``<root>/scaffold`` first), so callers can pass either the artifact
    root or the scaffold dir directly.

    This is THE one place build-outcome record sites should derive their
    ``stack`` arg so success and failure paths agree on the bucket name.
    Falls back to ``"unknown"`` when nothing is recognizable, and
    normalizes legacy aliases (e.g. ``vite_react`` → ``react_vite``) via
    :data:`_CANONICAL_STACK_NAMES`.
    """
    try:
        name = detect(Path(artifact_or_scaffold_dir)).stack or "unknown"
    except Exception:
        # Never let stack detection crash a record path — degrade to unknown.
        name = "unknown"
    return _CANONICAL_STACK_NAMES.get(name, name)
