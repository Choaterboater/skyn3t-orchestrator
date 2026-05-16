"""Deterministic cross-stage contract checker.

Runs after CodeAgent and before ConsistencyReviewerAgent. Verifies that
the artifacts the swarm produced actually agree with each other:

- ``palette.json`` colors actually appear in the scaffold's CSS, and
  vice-versa (no slate-blue ``styles.css`` shipping under a warm-gunmetal
  ``palette.json``).
- ``tech_stack.json`` declared dependencies actually appear in
  ``package.json`` (no ``hono-node`` claim with an Express server).
- No placeholder strings (``Auto-planned``, ``<placeholder>``,
  ``TODO[skyn3t]``) leak into shipped JSON/Markdown.
- When the brief asks for an obvious feature (Cmd+K, glassmorphism, a
  health endpoint), the scaffold has at least one piece of evidence for
  it.

No LLM calls. Mirrors ``consistency_engine.check_consistency``'s shape
so the runner's fix-loop can pattern-match on the report identically.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


# ---------------------------------------------------------------------
# Public report shape
# ---------------------------------------------------------------------


@dataclass
class ContractFinding:
    severity: str            # "blocker" | "warning"
    category: str            # see CATEGORIES below
    file: str                # scaffold-relative or "(artifact root)"
    message: str
    suggestion: str = ""
    fix_hint: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ContractReport:
    ok: bool
    findings: List[ContractFinding] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "findings": [
                    {
                        "severity": f.severity,
                        "category": f.category,
                        "file": f.file,
                        "message": f.message,
                        "suggestion": f.suggestion,
                        "fix_hint": f.fix_hint,
                    }
                    for f in self.findings
                ],
            },
            indent=2,
        )


# ---------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------

# Directory parts to skip when walking the scaffold. Keep aligned with
# ``ReviewerAgent._SKIP_DIR_PARTS`` so we don't penalize vendored code.
_SKIP_DIRS: Set[str] = {
    "node_modules", "dist", "build", ".next", ".turbo", ".cache",
    ".parcel-cache", ".vite", ".svelte-kit", ".nuxt", ".git",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "venv", ".venv", "env", "target", "out",
}

# Hard placeholder literals — trigger a blocker.
PLACEHOLDER_HARD: Tuple[str, ...] = ("Auto-planned", "<placeholder>", "TODO[skyn3t]")
# Soft placeholder literals — warning only.
PLACEHOLDER_SOFT: Tuple[str, ...] = ("FIXME", "TBD")

# CLI tool-call narration that occasionally leaks into shipped files.
# Used by _check_cli_prose_leak. Distinct from the sanitizer's regex
# because we're scanning for a hit anywhere in the file, not deciding
# where content starts.
CLI_PROSE_LEAK_PATTERNS: Tuple[str, ...] = (
    r"●\s+(?:Search|Read|Write|Edit|List|Web\s+Search)",
    r"✗\s+(?:Search|Read|Write|Edit)",
    r"No\s+matches\s+found",
    r"Path\s+does\s+not\s+exist",
    r"Permission\s+denied\s+and",
    r"The\s+workspace\s+(?:looks|is)\s+empty",
    r"I['’](?:m|ll|ve)\s+(?:checking|writing|going|now)\s",
)

# CSS hex color regex. Word-boundary keeps us from matching long ids.
_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")

# tech_stack.json roles we map. ``infra``/``ci`` carry no npm contract.
_TECH_STACK_ROLES: Tuple[str, ...] = ("frontend", "backend", "db")

# Architecture-mention keyword tuple -> (required npm package fragments,
# required source-code import patterns). The second field is what fixes
# canary-115's false-pass: package.json declared `next` but no `app/`
# directory or `from "next"` import existed, so the architecture's
# Next.js claim was a lie. The drift check now verifies both that the
# package is declared AND that at least one import pattern appears in
# non-vendor source.
#
# Empty import_patterns means "package declaration is sufficient" — use
# this when the dep doesn't have a recognizable import shape (e.g. CSS-
# only deps).
ARCHITECTURE_TECH_MAP: List[Tuple[Tuple[str, ...], List[str], List[str]]] = [
    # framework mismatches
    ((r"\bnext\.?js\b", r"\bapp router\b"),
     ["next"],
     [r"from\s+['\"]next/", r"import\s+.*\s+from\s+['\"]next['\"]"]),
    ((r"\bhono\b",),
     ["hono", "@hono/node-server"],
     [r"from\s+['\"]hono['\"]", r"new\s+Hono\s*\("]),
    ((r"\bfastify\b",),
     ["fastify"],
     [r"require\s*\(\s*['\"]fastify['\"]", r"from\s+['\"]fastify['\"]"]),
    ((r"\bnestjs\b", r"\bnest\.js\b"),
     ["@nestjs/core"],
     [r"from\s+['\"]@nestjs/"]),
    ((r"\bsveltekit\b", r"\bsvelte ?kit\b"),
     ["@sveltejs/kit"],
     [r"from\s+['\"]@sveltejs/kit['\"]"]),
    # databases
    ((r"\bbetter-?sqlite3?\b",),
     ["better-sqlite3"],
     [r"require\s*\(\s*['\"]better-sqlite3['\"]", r"from\s+['\"]better-sqlite3['\"]"]),
    ((r"\bprisma\b",),
     ["@prisma/client", "prisma"],
     [r"from\s+['\"]@prisma/client['\"]", r"new\s+PrismaClient\s*\("]),
    ((r"\bdrizzle\b",),
     ["drizzle-orm"],
     [r"from\s+['\"]drizzle-orm"]),
    ((r"\bpostgres(?:ql)?\b", r"\bpg\b"),
     ["pg", "postgres", "@vercel/postgres"],
     [r"require\s*\(\s*['\"]pg['\"]", r"from\s+['\"]pg['\"]", r"from\s+['\"]postgres['\"]"]),
    ((r"\bmongodb\b", r"\bmongoose\b"),
     ["mongodb", "mongoose"],
     [r"from\s+['\"]mongoose['\"]", r"require\s*\(\s*['\"]mongodb['\"]"]),
    # scheduling — architecture promises cron but scaffold rarely ships it
    ((r"\bnode-cron\b", r"\bcron jobs?\b"),
     ["node-cron", "croner", "agenda"],
     [r"require\s*\(\s*['\"]node-cron['\"]", r"from\s+['\"]node-cron['\"]",
      r"from\s+['\"]croner['\"]", r"new\s+Agenda\s*\("]),
    # AES-256-GCM is a stdlib feature, but architecture promises it
    # specifically — check for the actual call shape, not just `crypto`
    # (every node project imports crypto somewhere).
    ((r"\baes-?256-?gcm\b", r"\benvelope encryption\b"),
     ["crypto", "node:crypto"],
     [r"createCipheriv\s*\(\s*['\"]aes-256-gcm",
      r"createDecipheriv\s*\(\s*['\"]aes-256-gcm"]),
]

# tech-stack name -> npm-package fragments. Match rule: at least one
# fragment must appear in the union of package.json dep names. Empty
# list means "no npm package expected — skip silently".
STACK_PACKAGE_MAP: Dict[str, List[str]] = {
    # frontend
    "react": ["react"],
    "react-vite": ["react", "vite"],
    "react-vite-tailwind": ["react", "vite", "tailwindcss"],
    "next": ["next", "react"],
    "nextjs": ["next", "react"],
    "next.js": ["next", "react"],
    "svelte-kit": ["@sveltejs/kit", "svelte"],
    "sveltekit": ["@sveltejs/kit", "svelte"],
    "vue-vite": ["vue", "vite"],
    "vue": ["vue"],
    "vanilla-vite": ["vite"],
    # backend
    "express": ["express"],
    "express-node": ["express"],
    "hono": ["hono"],
    "hono-node": ["hono", "@hono/node-server"],
    "fastify": ["fastify"],
    "koa": ["koa"],
    "nestjs": ["@nestjs/core"],
    "fastapi": ["fastapi"],
    "flask": ["flask"],
    # db
    "sqlite-better-sqlite3": ["better-sqlite3"],
    "better-sqlite3": ["better-sqlite3"],
    "sqlite": ["better-sqlite3", "sqlite3"],
    "postgres": ["pg", "postgres", "@vercel/postgres"],
    "postgresql": ["pg", "postgres"],
    "prisma-sqlite": ["@prisma/client", "prisma"],
    "drizzle-sqlite": ["drizzle-orm", "better-sqlite3"],
    "mongodb": ["mongodb", "mongoose"],
    # infra-only / no npm requirement
    "local-node": [],
    "docker-compose": [],
    "vercel": [],
    "fly": [],
    "ci": [],
    "none": [],
    "n/a": [],
    "": [],
}

# Brief-feature keyword tuples -> evidence regex + targeting metadata.
#
# Severity policy:
# - blocker=True for keywords the brief lists as core *visual* deliverables
#   that the swarm regularly drops (glassmorphism, dark mode). These are
#   deterministically detectable AND we have a clear fix target so the
#   targeted-fix loop can repair them.
# - Otherwise warning-only (keyword detection is heuristic — false
#   positives on decorative mentions should not block a run).
# - fix_target points the auto-regen at a specific file. When None or
#   no candidate exists in the scaffold, the finding stays a warning.
FEATURE_EVIDENCE_MAP: List[Tuple[Tuple[str, ...], Dict[str, Any]]] = [
    (("command palette", "cmd+k", "ctrl+k", "⌘k", "cmdk"),
     {"grep": r"(?i)\b(cmdk|kbar|command[- ]?palette|useHotkeys|Mod\+K)\b"}),
    (("glassmorphism", "glass effect", "frosted glass"),
     {"grep": r"backdrop-filter\s*:\s*blur\(",
      "blocker": True,
      "fix_target_globs": ("src/styles.css", "src/index.css", "src/app.css"),
      "fix_hint_text": (
          "Add real glassmorphism to the panels/cards used by this app. "
          "Every visible card, modal, and panel should include "
          "`backdrop-filter: blur(<value>)` (and `-webkit-backdrop-filter` "
          "for Safari), plus a translucent rgba background and a subtle "
          "1px border. Keep all existing layout, selectors, and class "
          "names — only add the glass treatment to surfaces."
      )}),
    (("dark mode", "dark theme"),
     {"grep": r"(?i)(data-theme=['\"]dark|prefers-color-scheme:\s*dark|class(?:Name)?=['\"][^'\"]*\bdark\b|color-scheme:\s*dark)",
      "blocker": True,
      "fix_target_globs": ("src/styles.css", "src/index.css", "src/app.css"),
      "fix_hint_text": (
          "Brief requires dark mode. Add `color-scheme: dark;` to :root, "
          "set the body background to a dark color from the palette, "
          "and ensure text contrast is readable. Preserve all existing "
          "rules — only ADD the dark-mode declarations."
      )}),
    (("light theme toggle", "theme toggle", "theme switcher"),
     {"grep": r"(?i)(toggleTheme|setTheme|theme-toggle|ThemeToggle)"}),
    (("activity feed", "activity log"),
     {"grep": r"(?i)(activity[- ]?feed|recent activity|<ActivityFeed|activity_log)"}),
    (("sidebar", "side nav"),
     {"grep": r"(?i)(<Sidebar|class(?:Name)?=['\"][^'\"]*\bsidebar\b|<aside\b)"}),
    (("health endpoint", "/health", "healthcheck"),
     {"grep": r"['\"]\/(?:api\/)?health(?:z)?['\"]"}),
    (("persistent config", "config persistence", "settings persist"),
     {"grep": r"(?i)(localStorage\.setItem|fs\.writeFile|writeFileSync|sqlite|better-sqlite3)"}),
    (("keyboard shortcut", "hotkey"),
     {"grep": r"(?i)(keydown|useHotkeys|Mousetrap|hotkey)"}),
    (("toast", "snackbar"),
     {"grep": r"(?i)(<Toaster|react-hot-toast|sonner|<Toast\b|<Snackbar)"}),
]


# ---------------------------------------------------------------------
# File-walk helpers
# ---------------------------------------------------------------------


def _should_skip(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = path.parts
    return any(part in _SKIP_DIRS for part in rel_parts)


def _iter_files(
    root: Path,
    suffixes: Optional[Iterable[str]] = None,
) -> Iterable[Tuple[str, Path]]:
    """Yield (rel_posix, abs_path) for files under root, pruning skip dirs."""
    if not root.exists():
        return
    suffix_set = {s.lower() for s in suffixes} if suffixes else None
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _should_skip(path, root):
            continue
        if suffix_set is not None and path.suffix.lower() not in suffix_set:
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        yield rel, path


def _load_json(path: Path) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _normalize_hex(hex_str: str) -> str:
    """Lowercase + expand #abc to #aabbcc; drop alpha on #rrggbbaa."""
    s = hex_str.lower()
    if s.startswith("#"):
        body = s[1:]
    else:
        body = s
    if len(body) == 3:
        body = "".join(c * 2 for c in body)
    if len(body) == 8:
        body = body[:6]
    return "#" + body


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


# ---------------------------------------------------------------------
# Palette check
# ---------------------------------------------------------------------


def _extract_hexes(text: str) -> Set[str]:
    return {_normalize_hex(m) for m in _HEX_RE.findall(text)}


def _palette_hexes_from_artifacts(artifact_dir: Path) -> Set[str]:
    """Collect normalized hex colors declared in palette.json / tokens.json."""
    hexes: Set[str] = set()
    for name in ("palette.json", "tokens.json"):
        path = artifact_dir / name
        if not path.exists():
            continue
        # Search raw text instead of walking JSON — covers nested designs.
        text = _read_text(path) or ""
        hexes.update(_extract_hexes(text))
    return hexes


def _css_hexes_in_scaffold(scaffold_dir: Path) -> Dict[str, Set[str]]:
    """Map relative CSS path -> normalized hexes used (post-comment-strip)."""
    out: Dict[str, Set[str]] = {}
    for rel, path in _iter_files(scaffold_dir, suffixes={".css", ".scss", ".sass"}):
        text = _read_text(path) or ""
        text = _strip_css_comments(text)
        hexes = _extract_hexes(text)
        if hexes:
            out[rel] = hexes
    return out


def _brief_requests_brand(brief: str) -> bool:
    return any(
        kw in brief.lower()
        for kw in ("brand", "palette", "color", "theme", "design system", "glassmorphism")
    )


def _check_palette(scaffold_dir: Path, artifact_dir: Path, brief: str) -> List[ContractFinding]:
    findings: List[ContractFinding] = []
    palette = _palette_hexes_from_artifacts(artifact_dir)
    if not palette:
        # Palette uses non-hex tokens (oklch/hsl/named) or no palette
        # was declared. Skip silently — false positives would block runs.
        return findings

    brand = _brief_requests_brand(brief)
    css_map = _css_hexes_in_scaffold(scaffold_dir)
    for rel, css_hexes in css_map.items():
        unauthorized = sorted(css_hexes - palette)
        if not unauthorized:
            continue
        severity = "blocker" if brand else "warning"
        findings.append(ContractFinding(
            severity=severity,
            category="palette_schism_css",
            file=rel,
            message=(
                f"{rel} uses {len(unauthorized)} hex color(s) not in "
                f"palette.json: {', '.join(unauthorized)}."
            ),
            suggestion=(
                "Rewrite the CSS using only the canonical palette: "
                + ", ".join(sorted(palette)) + "."
            ),
            fix_hint={
                "canonical_palette": sorted(palette),
                "offending_hexes": unauthorized,
            },
        ))

    # Reverse direction: palette declares colors that never appear in CSS.
    # Only emit if at least one CSS file was scanned (otherwise this is
    # a docs-only artifact set).
    if css_map:
        all_css_hexes: Set[str] = set().union(*css_map.values())
        unused = sorted(palette - all_css_hexes)
        if unused:
            findings.append(ContractFinding(
                severity="warning",
                category="palette_schism_palette",
                file="(artifact root)",
                message=(
                    "palette.json declares color(s) that never appear in "
                    f"any CSS file: {', '.join(unused)}."
                ),
                suggestion=(
                    "Either use these colors in styles.css or remove them "
                    "from palette.json so the brand kit matches the app."
                ),
                fix_hint={"unused_palette_hexes": unused},
            ))

    return findings


# ---------------------------------------------------------------------
# Language-coherence check (Python↔Node mismatch)
# ---------------------------------------------------------------------

# Tech-stack values that imply a Python scaffold.
_PYTHON_TECH_VALUES: Set[str] = {
    "fastapi", "flask", "django", "starlette",
}
# Tech-stack values that imply a Node scaffold.
_NODE_TECH_VALUES: Set[str] = {
    "express", "express-node", "hono", "hono-node", "fastify", "koa",
    "nestjs", "next", "nextjs", "next.js",
    "react", "react-vite", "react-vite-tailwind", "vue-vite", "vanilla-vite",
    "svelte-kit", "sveltekit",
}
# npm package names that are actually Python libraries — if any of these
# appear in package.json, that's a Python↔Node confusion. Will install
# either a squatter or break.
_PYTHON_LIB_NAMES_IN_NPM: Set[str] = {
    "fastapi", "flask", "django", "starlette", "uvicorn", "pydantic",
    "sqlalchemy", "alembic", "celery", "tornado",
}


def _check_language_coherence(
    scaffold_dir: Path,
    artifact_dir: Path,
) -> List[ContractFinding]:
    """Detect Python↔Node mixing — declared in one stack, scaffolded in another.

    Two patterns from canary-116/117:
    1. tech_stack.json says backend=fastapi but scaffold ships package.json
       (Node manifest, no pyproject.toml). CodeAgent silently downgraded
       to Express.
    2. package.json includes literal "fastapi": "..." as an npm dep. Will
       install some squatter package or break `npm install` outright.
    """
    findings: List[ContractFinding] = []

    # Case 1: tech_stack lies about language. Requires tech_stack.json.
    ts_path = artifact_dir / "tech_stack.json"
    promised_python = False
    declared_backend = ""
    declared_frontend = ""
    if ts_path.exists():
        ts = _load_json(ts_path)
        if isinstance(ts, dict):
            declared_backend = _resolve_stack_value(ts.get("backend")) or ""
            declared_frontend = _resolve_stack_value(ts.get("frontend")) or ""
            promised_python = (
                declared_backend in _PYTHON_TECH_VALUES
                or declared_frontend in _PYTHON_TECH_VALUES
            )

    # What did the scaffold actually ship? (computed even without
    # tech_stack.json — Case 2 below needs to scan package.json files
    # regardless of manifest presence.)
    has_package_json = any(
        path.name == "package.json"
        for _rel, path in _iter_files(scaffold_dir, suffixes={".json"})
    )
    has_pyproject = (scaffold_dir / "pyproject.toml").exists()
    has_requirements = (scaffold_dir / "requirements.txt").exists()
    is_python_scaffold = has_pyproject or has_requirements

    # Case 1: tech_stack says Python but scaffold is Node (no pyproject,
    # no requirements.txt, only package.json).
    if promised_python and has_package_json and not is_python_scaffold:
        findings.append(ContractFinding(
            severity="blocker",
            category="language_mismatch",
            file="tech_stack.json",
            message=(
                f"tech_stack.json declares Python ({declared_backend or declared_frontend}) "
                "but the scaffold ships a Node project (package.json present, no "
                "pyproject.toml or requirements.txt). CodeAgent likely silently "
                "downgraded to a Node stack — this is the canary-116/117 pattern."
            ),
            suggestion=(
                "Either revise tech_stack.json to match the actual Node scaffold, "
                "or scaffold a real Python project (pyproject.toml + Python source). "
                "Don't ship a manifest that lies about the language."
            ),
            fix_hint={
                "declared_backend": declared_backend,
                "declared_frontend": declared_frontend,
                "actual": "node",
            },
        ))

    # Case 2: Python lib names polluting package.json. Always a blocker —
    # `npm install` will either fail or pull a squatter.
    for _rel, path in _iter_files(scaffold_dir, suffixes={".json"}):
        if path.name != "package.json":
            continue
        # Skip vendored.
        try:
            rel_str = path.relative_to(scaffold_dir).as_posix()
        except ValueError:
            rel_str = path.name
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        polluted: List[str] = []
        for dep_key in ("dependencies", "devDependencies", "peerDependencies"):
            section = data.get(dep_key)
            if not isinstance(section, dict):
                continue
            for name in section.keys():
                if name.lower() in _PYTHON_LIB_NAMES_IN_NPM:
                    polluted.append(name)
        if polluted:
            findings.append(ContractFinding(
                severity="blocker",
                category="language_mismatch",
                file=rel_str,
                message=(
                    f"{rel_str} lists Python libraries as npm dependencies: "
                    f"{', '.join(polluted)}. These are not valid npm packages — "
                    "npm install will fail or pull squatter packages."
                ),
                suggestion=(
                    f"Remove {', '.join(polluted)} from package.json. If the "
                    "feature is genuinely needed, scaffold a Python service "
                    "alongside (with its own pyproject.toml) — don't mix."
                ),
                fix_hint={
                    "polluted_packages": polluted,
                    "package_json": rel_str,
                },
            ))

    return findings


# ---------------------------------------------------------------------
# tech_stack.json check
# ---------------------------------------------------------------------


def _read_package_deps(scaffold_dir: Path) -> Set[str]:
    """Union of dependency names across every non-vendored package.json."""
    deps: Set[str] = set()
    for _rel, path in _iter_files(scaffold_dir, suffixes={".json"}):
        if path.name != "package.json":
            continue
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            val = data.get(key)
            if isinstance(val, dict):
                deps.update(val.keys())
    return deps


def _resolve_stack_value(raw: Any) -> Optional[str]:
    """tech_stack values can be strings or {"name": "..."} objects."""
    if isinstance(raw, str):
        return raw.strip().lower()
    if isinstance(raw, dict):
        name = raw.get("name") or raw.get("type") or raw.get("framework")
        if isinstance(name, str):
            return name.strip().lower()
    return None


def _check_tech_stack(scaffold_dir: Path, artifact_dir: Path) -> List[ContractFinding]:
    findings: List[ContractFinding] = []
    ts_path = artifact_dir / "tech_stack.json"
    if not ts_path.exists():
        return findings
    ts = _load_json(ts_path)
    if not isinstance(ts, dict):
        return findings

    declared_deps = _read_package_deps(scaffold_dir)
    for role in _TECH_STACK_ROLES:
        value = _resolve_stack_value(ts.get(role))
        if not value:
            continue
        if value not in STACK_PACKAGE_MAP:
            findings.append(ContractFinding(
                severity="warning",
                category="tech_stack_mismatch",
                file="tech_stack.json",
                message=(
                    f"tech_stack.json {role!r} value {value!r} is unknown — "
                    "cannot verify package.json declares it."
                ),
                suggestion=(
                    "Either align the value with a known stack name "
                    "(e.g. 'express', 'react-vite', 'hono-node') or "
                    "extend STACK_PACKAGE_MAP."
                ),
                fix_hint={"role": role, "declared": value, "expected_packages": []},
            ))
            continue
        expected = STACK_PACKAGE_MAP[value]
        if not expected:
            continue
        if any(pkg in declared_deps for pkg in expected):
            continue
        target_pkg = _package_json_for_role(scaffold_dir, role)
        findings.append(ContractFinding(
            severity="blocker",
            category="tech_stack_mismatch",
            file=target_pkg,
            message=(
                f"tech_stack.json declares {role}={value!r} but no "
                f"package.json includes any of: {', '.join(expected)}."
            ),
            suggestion=(
                f"Add one of {expected} to {target_pkg} so the manifest "
                "matches the declared tech stack."
            ),
            fix_hint={
                "role": role,
                "declared": value,
                "expected_packages": expected,
            },
        ))

    return findings


def _package_json_for_role(scaffold_dir: Path, role: str) -> str:
    """Pick the most-likely package.json file the role maps to."""
    if role in ("backend", "db"):
        server_pkg = scaffold_dir / "server" / "package.json"
        if server_pkg.exists():
            return "server/package.json"
    root_pkg = scaffold_dir / "package.json"
    if root_pkg.exists():
        return "package.json"
    # Fall back to the first non-vendored package.json we can find.
    for rel, path in _iter_files(scaffold_dir, suffixes={".json"}):
        if path.name == "package.json":
            return rel
    return "package.json"


# ---------------------------------------------------------------------
# Architecture↔scaffold drift check
# ---------------------------------------------------------------------


def _check_architecture_drift(scaffold_dir: Path, artifact_dir: Path) -> List[ContractFinding]:
    """Detect when architecture.md names a tech that the scaffold lacks.

    Canary-113 example: architecture.md promised Next.js + Hono +
    better-sqlite3 + node-cron, but scaffold/package.json shipped
    React/Vite + Express + a JSON file. The reviewer LLM flagged the
    drift and deducted heavily. This makes the same gap deterministic:
    if architecture mentions X and X isn't in package.json deps, that's
    a blocker that the targeted-fix loop should add to package.json.

    Note: relies on architecture.md prose — false positives are mitigated
    by requiring the keyword to appear as a standalone token (not inside
    "we considered Next.js but chose…"). We accept a small false-positive
    rate because the cost of missing this drift is much higher than the
    cost of a redundant package add.
    """
    findings: List[ContractFinding] = []
    arch_path = artifact_dir / "architecture.md"
    if not arch_path.exists():
        return findings
    arch_text = _read_text(arch_path) or ""
    if not arch_text.strip():
        return findings

    # Strip "we considered X but chose Y" style negations to reduce false
    # positives. Cheap heuristic: drop sentences that contain "instead"
    # or "rather than" or "rejected" or "considered".
    sentences = re.split(r"(?<=[.!?])\s+", arch_text)
    kept = [
        s for s in sentences
        if not re.search(r"\b(instead|rather than|rejected|considered)\b", s, re.IGNORECASE)
    ]
    pruned = " ".join(kept).lower()

    declared_deps = _read_package_deps(scaffold_dir)
    source_blob = _scaffold_source_blob(scaffold_dir)
    seen: Set[str] = set()
    for keyword_patterns, expected_packages, import_patterns in ARCHITECTURE_TECH_MAP:
        if not any(re.search(kw, pruned, re.IGNORECASE) for kw in keyword_patterns):
            continue

        package_declared = any(pkg in declared_deps for pkg in expected_packages)
        import_present = (
            not import_patterns  # no import patterns means declaration suffices
            or any(re.search(p, source_blob) for p in import_patterns)
        )

        if package_declared and import_present:
            continue

        # Clean label for human-facing messages.
        label = keyword_patterns[0]
        label = re.sub(r"\\b", "", label)
        label = re.sub(r"[\\$^.*+?|()]", "", label)
        label = re.sub(r"\s+", " ", label).strip()
        if label in seen:
            continue
        seen.add(label)

        target_pkg = _package_json_for_role(
            scaffold_dir, "backend" if "server" in str(arch_path).lower() else "frontend"
        )

        # Tailor the message to which gate failed — package missing,
        # vs declared-but-unused (the canary-115 false-pass case).
        if not package_declared:
            message = (
                f"architecture.md describes {label!r} but no package.json "
                f"includes any of: {', '.join(expected_packages)}. The "
                "scaffold doesn't match its own architecture doc."
            )
            suggestion = (
                f"Either add one of {expected_packages} to {target_pkg}, "
                f"or revise architecture.md to drop the {label!r} claim."
            )
        else:
            message = (
                f"architecture.md describes {label!r} and package.json "
                f"declares it, but no source file imports/uses it. The "
                "dep is dead weight; the feature is unbuilt."
            )
            suggestion = (
                f"Either add real {label!r} usage (one of the import "
                "shapes the swarm expects) to the scaffold, or revise "
                "architecture.md to drop the claim."
            )

        findings.append(ContractFinding(
            severity="blocker",
            category="architecture_drift",
            file=target_pkg,
            message=message,
            suggestion=suggestion,
            fix_hint={
                "keyword": label,
                "expected_packages": expected_packages,
                "expected_import_patterns": import_patterns,
                "package_declared": package_declared,
                "import_present": import_present,
            },
        ))
    return findings


def _scaffold_source_blob(scaffold_dir: Path) -> str:
    """Concatenate non-vendor source for grep checks.

    Skips _SKIP_DIRS so vendor crypto/dotenv code can't make AES-256-GCM
    checks falsely pass (canary-115 pattern). Caps total size at 2 MB to
    keep regex fast.
    """
    chunks: List[str] = []
    total = 0
    code_suffixes = {
        ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
        ".py", ".rb", ".go", ".rs", ".java",
        ".html", ".vue", ".svelte",
    }
    for _rel, path in _iter_files(scaffold_dir, suffixes=code_suffixes):
        try:
            size = path.stat().st_size
            if size > 256 * 1024:
                continue
            if total + size > 2 * 1024 * 1024:
                break
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        chunks.append(text)
        total += size
    return "\n".join(chunks)


# ---------------------------------------------------------------------
# Placeholder leak check
# ---------------------------------------------------------------------


def _check_placeholders(scaffold_dir: Path) -> List[ContractFinding]:
    findings: List[ContractFinding] = []
    suffixes = {".json", ".md", ".html", ".css", ".scss"}
    for rel, path in _iter_files(scaffold_dir, suffixes=suffixes):
        # Skip generated/large lockfiles — TODO/FIXME mentions inside
        # them are vendor noise, not swarm output.
        if path.name in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}:
            continue
        # Skip docs/ — legitimate explanatory mentions of placeholders.
        if rel.startswith("docs/"):
            continue
        try:
            if path.stat().st_size > 256 * 1024:
                continue
        except OSError:
            continue
        text = _read_text(path)
        if text is None:
            continue
        for literal in PLACEHOLDER_HARD:
            if literal in text:
                findings.append(ContractFinding(
                    severity="blocker",
                    category="placeholder_leak",
                    file=rel,
                    message=f"{rel} contains placeholder literal {literal!r}.",
                    suggestion=(
                        f"Remove {literal!r} and replace with concrete "
                        "content consistent with the brief."
                    ),
                    fix_hint={"literal": literal},
                ))
        for literal in PLACEHOLDER_SOFT:
            # Word-boundary match so "fixmedia" doesn't trip FIXME.
            if re.search(rf"\b{re.escape(literal)}\b", text):
                findings.append(ContractFinding(
                    severity="warning",
                    category="placeholder_leak",
                    file=rel,
                    message=f"{rel} contains soft placeholder {literal!r}.",
                    suggestion=(
                        f"Resolve the {literal} marker or convert it to a "
                        "tracked issue."
                    ),
                    fix_hint={"literal": literal},
                ))
    return findings


# ---------------------------------------------------------------------
# CLI tool-call prose leak check
# ---------------------------------------------------------------------


def _check_cli_prose_leak(scaffold_dir: Path) -> List[ContractFinding]:
    """Find files where LLM tool-call narration leaked into the body.

    canary-113's `scaffold/server/README.md` shipped with the first 18
    lines being literal "● Search (glob) │ No matches found" telemetry
    that the CodeAgent emitted while planning. The reviewer LLM flagged
    it as a ship-blocker but the swarm had no deterministic check.
    """
    findings: List[ContractFinding] = []
    leak_re = re.compile("|".join(CLI_PROSE_LEAK_PATTERNS), re.IGNORECASE)
    text_suffixes = {".md", ".markdown", ".html", ".txt", ".json", ".yml", ".yaml"}
    for rel, path in _iter_files(scaffold_dir, suffixes=text_suffixes):
        # Skip lockfiles/manifests where these strings could appear in
        # legitimate dependency descriptions.
        if path.name in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}:
            continue
        try:
            if path.stat().st_size > 256 * 1024:
                continue
        except OSError:
            continue
        text = _read_text(path)
        if text is None:
            continue
        # Only flag when the leak is in the FIRST 1500 chars — that's where
        # CodeAgent's planning chatter lands. Random middle-of-file matches
        # are likely legitimate docs about LLM tooling.
        head = text[:1500]
        m = leak_re.search(head)
        if not m:
            continue
        findings.append(ContractFinding(
            severity="blocker",
            category="cli_prose_leak",
            file=rel,
            message=(
                f"{rel} contains LLM tool-call narration in its first 1500 "
                f"chars (matched: {m.group(0)!r}). This is CodeAgent's "
                "planning chatter, not file content."
            ),
            suggestion=(
                "Strip the leading narration and replace with real file "
                "content. The sanitizer in code_agent._strip_cli_prelude "
                "should have caught this — if it didn't, the narration "
                "uses an unusual lead-in."
            ),
            fix_hint={"matched": m.group(0)},
        ))
    return findings


# ---------------------------------------------------------------------
# Brief-feature evidence check
# ---------------------------------------------------------------------


def _resolve_fix_target(scaffold_dir: Path, globs: Iterable[str]) -> Optional[str]:
    """Return the first scaffold-relative path matching any of the globs."""
    for rel in globs:
        if (scaffold_dir / rel).is_file():
            return rel
    return None


def _check_feature_evidence(brief: str, scaffold_dir: Path) -> List[ContractFinding]:
    findings: List[ContractFinding] = []
    if not brief:
        return findings
    brief_lower = brief.lower()

    # Read every scaffold source file once — feature grep regex runs
    # against the concatenated body. Skip vendored code.
    blob_parts: List[str] = []
    code_suffixes = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css",
                     ".scss", ".html", ".vue", ".svelte"}
    for _rel, path in _iter_files(scaffold_dir, suffixes=code_suffixes):
        text = _read_text(path)
        if text:
            blob_parts.append(text)
    blob = "\n".join(blob_parts)

    for keywords, requirement in FEATURE_EVIDENCE_MAP:
        if not any(kw in brief_lower for kw in keywords):
            continue
        pattern = requirement.get("grep")
        if not pattern:
            continue
        if re.search(pattern, blob):
            continue

        keyword = keywords[0]
        is_blocker = bool(requirement.get("blocker"))
        fix_target = None
        if is_blocker:
            globs = requirement.get("fix_target_globs") or ()
            fix_target = _resolve_fix_target(scaffold_dir, globs)

        # Promote to blocker only when we have a concrete file to fix.
        # Otherwise the targeted-fix loop has nothing to act on and we'd
        # just block runs that nobody can repair.
        severity = "blocker" if (is_blocker and fix_target) else "warning"
        file_ref = fix_target or "(scaffold root)"
        fix_hint: Dict[str, Any] = {"keyword": keyword, "grep": pattern}
        if severity == "blocker":
            fix_hint["fix_target"] = fix_target
            fix_hint["fix_instruction"] = requirement.get("fix_hint_text", "")

        findings.append(ContractFinding(
            severity=severity,
            category="missing_feature_evidence",
            file=file_ref,
            message=(
                f"Brief mentions '{keyword}' but the scaffold has no "
                "matching evidence (no file matches the expected pattern)."
            ),
            suggestion=(
                f"Add an implementation of '{keyword}' that matches the "
                f"detection regex: {pattern}."
            ),
            fix_hint=fix_hint,
        ))
    return findings


# ---------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------


def check_contract(
    scaffold_dir: Path,
    brief: str,
    artifact_dir: Path,
) -> ContractReport:
    """Run all four contract checks and return a combined report.

    Findings are deduplicated on (category, file, message).
    """
    scaffold_dir = Path(scaffold_dir)
    artifact_dir = Path(artifact_dir)

    findings: List[ContractFinding] = []
    findings.extend(_check_palette(scaffold_dir, artifact_dir, brief or ""))
    findings.extend(_check_language_coherence(scaffold_dir, artifact_dir))
    findings.extend(_check_tech_stack(scaffold_dir, artifact_dir))
    findings.extend(_check_architecture_drift(scaffold_dir, artifact_dir))
    findings.extend(_check_placeholders(scaffold_dir))
    findings.extend(_check_cli_prose_leak(scaffold_dir))
    findings.extend(_check_feature_evidence(brief or "", scaffold_dir))

    # Dedup
    seen: Set[Tuple[str, str, str]] = set()
    deduped: List[ContractFinding] = []
    for f in findings:
        key = (f.category, f.file, f.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)

    has_blocker = any(f.severity == "blocker" for f in deduped)
    return ContractReport(ok=not has_blocker, findings=deduped)
