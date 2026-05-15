"""Static cross-file consistency engine.

Parses the generated scaffold to build an import graph, verifies that
all relative imports resolve to existing files, checks that package.json
covers all external dependencies, and detects hallucinated services.

No LLM calls — pure Python/AST analysis. Runs in ~100ms for a 30-file
scaffold, making it cheap enough to run after every CodeAgent generation.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set


@dataclass
class ConsistencyIssue:
    severity: str  # "error" | "warning"
    category: str  # "broken_import" | "missing_dep" | "orphan_export" | "hallucination" | "missing_mount" | "design_quality" | "todo_stub"
    file: str
    message: str
    suggestion: str = ""


@dataclass
class ConsistencyReport:
    ok: bool
    issues: List[ConsistencyIssue] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "issues": [
                    {
                        "severity": i.severity,
                        "category": i.category,
                        "file": i.file,
                        "message": i.message,
                        "suggestion": i.suggestion,
                    }
                    for i in self.issues
                ],
            },
            indent=2,
        )


# JS/TS built-ins that never need a package.json entry
JS_BUILTIN_MODULES: Set[str] = {
    "react", "react-dom", "react-dom/client", "react/jsx-runtime",
}

# Service names that are allowed in the scaffold (seeded from brief)
# This is populated at runtime from the brief's detected services.
_HALLUCINATION_WHITELIST: Set[str] = set()

# Regexes for extracting JS/TS imports without a full parser.
# These are conservative — they catch the common cases and miss exotic
# dynamic imports, which is fine because dynamic imports are reported
# as warnings, not errors.
_IMPORT_RE = re.compile(
    r"""
    ^\s*import\s+(?:(?:\{[^}]*\}|[^'"{}]*?)\s+from\s+)?['"]([^'"]+)['"];
    """,
    re.VERBOSE | re.MULTILINE,
)
_REQUIRE_RE = re.compile(
    r"""
    (?:require|import)\s*\(\s*['"]([^'"]+)['"]\s*\)
    """,
    re.VERBOSE,
)

_ROUTER_DEFINITION_RE = re.compile(r"\bRouter\s*\(")
_ROUTER_EXPORT_RE = re.compile(r"\bexport\s+default\b|module\.exports\s*=")
_ROUTER_IMPORT_RE = re.compile(
    r"""
    ^\s*import\s+([A-Za-z_$][\w$]*)\s+from\s+['"]([^'"]+)['"];
    """,
    re.VERBOSE | re.MULTILINE,
)
_ROUTER_REQUIRE_RE = re.compile(
    r"""
    \b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)
    """,
    re.VERBOSE,
)
_APP_USE_VAR_RE = re.compile(
    r"""
    \bapp\.use\(\s*(?:['"][^'"]+['"]\s*,\s*)?([A-Za-z_$][\w$]*)\s*[),]
    """,
    re.VERBOSE,
)
_APP_USE_REQUIRE_RE = re.compile(
    r"""
    \bapp\.use\(\s*['"][^'"]+['"]\s*,\s*require\(\s*['"]([^'"]+)['"]\s*\)\s*\)
    """,
    re.VERBOSE,
)


def _router_var_name(route_file: Path) -> str:
    stem = route_file.stem
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", stem) if p]
    if not parts:
        return "router"
    head = parts[0].lower()
    tail = "".join(p.capitalize() for p in parts[1:])
    return f"{head}{tail}Router"


def _find_missing_router_mounts(scaffold_dir: Path) -> List[ConsistencyIssue]:
    """Detect Express routers that are defined/exported but never mounted."""
    issues: List[ConsistencyIssue] = []
    server_dir = scaffold_dir / "server"
    routes_dir = server_dir / "routes"
    if not routes_dir.exists():
        return issues

    route_files: Dict[str, Path] = {}
    for path in routes_dir.rglob("*"):
        if path.suffix not in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs") or not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _ROUTER_DEFINITION_RE.search(source) and _ROUTER_EXPORT_RE.search(source):
            rel = path.relative_to(scaffold_dir).as_posix()
            route_files[rel] = path

    if not route_files:
        return issues

    mounted_routes: Set[str] = set()
    for path in server_dir.rglob("*"):
        if path.suffix not in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs") or not path.is_file():
            continue
        if path.is_relative_to(routes_dir):
            continue

        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        imported_aliases: Dict[str, str] = {}
        for matcher in (_ROUTER_IMPORT_RE, _ROUTER_REQUIRE_RE):
            for m in matcher.finditer(source):
                alias, import_target = m.group(1), m.group(2)
                resolved = _resolve_relative(path, import_target)
                if resolved is None:
                    continue
                try:
                    resolved.relative_to(routes_dir)
                except ValueError:
                    continue
                imported_aliases[alias] = resolved.relative_to(scaffold_dir).as_posix()

        for m in _APP_USE_VAR_RE.finditer(source):
            alias = m.group(1)
            mounted_rel = imported_aliases.get(alias)
            if mounted_rel is not None:
                mounted_routes.add(mounted_rel)

        for m in _APP_USE_REQUIRE_RE.finditer(source):
            import_target = m.group(1)
            resolved = _resolve_relative(path, import_target)
            if resolved is None:
                continue
            try:
                resolved.relative_to(routes_dir)
            except ValueError:
                continue
            mounted_routes.add(resolved.relative_to(scaffold_dir).as_posix())

    for rel, path in route_files.items():
        if rel in mounted_routes:
            continue
        mount_prefix = "/" + path.with_suffix("").relative_to(routes_dir).as_posix()
        suggestion_prefix = "/api" + mount_prefix if not mount_prefix.startswith("/api/") else mount_prefix
        import_path = "./" + path.relative_to(server_dir).as_posix()
        router_var = _router_var_name(path)
        issues.append(
            ConsistencyIssue(
                severity="error",
                category="missing_mount",
                file=rel,
                message=(
                    "Express Router is exported but no app.use(...) mount was found in "
                    "server/*.js entry files."
                ),
                suggestion=(
                    f"Add `import {router_var} from '{import_path}'` and "
                    f"`app.use('{suggestion_prefix}', {router_var})` in server/index.js."
                ),
            )
        )

    return issues


def _extract_js_imports(source: str) -> Set[str]:
    """Extract import sources from JS/TS/JSX/TSX source text."""
    found: Set[str] = set()
    for m in _IMPORT_RE.finditer(source):
        found.add(m.group(1))
    for m in _REQUIRE_RE.finditer(source):
        found.add(m.group(1))
    return found


def _extract_py_imports(source: str) -> Set[str]:
    """Extract top-level import/module names from Python source."""
    found: Set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module.split(".")[0])
            elif node.level and node.level > 0:
                # relative import — handled elsewhere
                pass
    return found


def _resolve_relative(base: Path, target: str) -> Optional[Path]:
    """Resolve a relative import string against a base file path.

    Examples:
        base = src/App.jsx, target = ./components/Foo.jsx → src/components/Foo.jsx
        base = src/App.jsx, target = ../hooks/useX.js   → hooks/useX.js
        base = src/App.jsx, target = ./Foo              → src/Foo.jsx (or .js, .ts, .tsx)
    """
    if not target.startswith("."):
        return None
    # Normalize the relative path
    try:
        resolved = (base.parent / target).resolve()
    except (ValueError, OSError):
        return None
    # If the resolved path has no extension, try common JS extensions
    if resolved.suffix:
        return resolved
    for ext in (".jsx", ".js", ".tsx", ".ts", ".mjs", ".cjs"):
        candidate = resolved.with_suffix(ext)
        if candidate.exists():
            return candidate
    # Return the bare path so the caller can report it as missing
    return resolved.with_suffix("")


def _is_external_module(source: str) -> bool:
    """True when the import source is an npm/pip package, not a relative path."""
    return not source.startswith(".") and not source.startswith("/")


def _read_package_json_deps(scaffold_dir: Path) -> Set[str]:
    """Read dependency names from package.json files in the scaffold."""
    deps: Set[str] = set()
    for pkg_path in scaffold_dir.rglob("package.json"):
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            if isinstance(data.get(key), dict):
                deps.update(data[key].keys())
    return deps


def _scan_for_hallucinations(scaffold_dir: Path, allowed_services: Set[str]) -> List[ConsistencyIssue]:
    """Scan JS/JSX/MD files for mentions of services not in the allowed set.

    This catches Plex bleed-through: the brief asked for 7 services but
    the LLM mentioned Plex in a README or BRAND object.
    """
    issues: List[ConsistencyIssue] = []
    if not allowed_services:
        return issues

    # Known service words to scan for (case-insensitive)
    service_words = {
        "plex", "jellyfin", "emby", "sonarr", "radarr", "prowlarr",
        "qbittorrent", "transmission", "nzbget", "sabnzbd", "sonos",
        "docker", "home assistant", "home-assistant", "pihole", "pi-hole",
        "unifi", "overseerr", "tautulli",
    }
    for path in scaffold_dir.rglob("*"):
        if path.is_dir():
            continue
        if path.suffix not in (".js", ".jsx", ".ts", ".tsx", ".md", ".txt"):
            continue
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (OSError, UnicodeDecodeError):
            continue
        for word in service_words:
            if word not in allowed_services and word in text:
                # Avoid false positives: only flag if the word appears as a
                # standalone token (not inside another word like "complex").
                pattern = r"\b" + re.escape(word) + r"\b"
                if re.search(pattern, text):
                    rel = path.relative_to(scaffold_dir).as_posix()
                    issues.append(
                        ConsistencyIssue(
                            severity="warning",
                            category="hallucination",
                            file=rel,
                            message=f"Mentions '{word}' which is not in the brief's requested services.",
                            suggestion=f"Remove references to {word} or add it to the service registry.",
                        )
                    )
                    # Only report once per file per word
                    break
    return issues


def _scan_for_design_quality(scaffold_dir: Path) -> List[ConsistencyIssue]:
    """Lightweight visual polish checks for frontend scaffolds."""
    issues: List[ConsistencyIssue] = []
    css_files = [
        p for p in scaffold_dir.rglob("*")
        if p.is_file() and p.suffix in (".css", ".scss")
    ]
    ui_files = [
        p for p in scaffold_dir.rglob("*")
        if p.is_file() and p.suffix in (".js", ".jsx", ".ts", ".tsx", ".html")
    ]
    if not css_files and not ui_files:
        return issues

    css_blob_parts: List[str] = []
    for p in css_files:
        try:
            css_blob_parts.append(p.read_text(encoding="utf-8").lower())
        except (OSError, UnicodeDecodeError):
            continue
    css_blob = "\n".join(css_blob_parts)

    ui_blob_parts: List[str] = []
    for p in ui_files:
        try:
            ui_blob_parts.append(p.read_text(encoding="utf-8").lower())
        except (OSError, UnicodeDecodeError):
            continue
    ui_blob = "\n".join(ui_blob_parts)

    def warn(msg: str, suggestion: str) -> None:
        issues.append(
            ConsistencyIssue(
                severity="warning",
                category="design_quality",
                file="(frontend)",
                message=msg,
                suggestion=suggestion,
            )
        )

    has_tokens = ":root" in css_blob and "--" in css_blob
    has_focus = ":focus-visible" in css_blob or ":focus" in css_blob
    has_hover = ":hover" in css_blob
    has_responsive = "@media" in css_blob
    has_states = all(k in ui_blob for k in ("loading", "error", "empty"))

    if not has_tokens:
        warn(
            "No design-token block detected (:root CSS variables missing).",
            "Define color/spacing/typography tokens in :root and reference them across components.",
        )
    if not has_focus:
        warn(
            "No focus-visible/focus styling detected for interactive UI.",
            "Add :focus-visible styles for buttons/links/inputs to improve keyboard accessibility.",
        )
    if not has_hover:
        warn(
            "No hover-state styling detected for interactive elements.",
            "Add hover states on primary interactive controls to improve visual feedback.",
        )
    if not has_responsive:
        warn(
            "No responsive media query detected.",
            "Add at least one @media breakpoint for mobile/tablet layout behavior.",
        )
    if not has_states:
        warn(
            "UI state coverage appears incomplete (loading/error/empty states missing).",
            "Implement explicit loading, error, and empty states for primary views/components.",
        )
    return issues


def check_consistency(scaffold_dir: Path, brief: str = "") -> ConsistencyReport:
    """Run the full static consistency check on a scaffold directory.

    Args:
        scaffold_dir: Path to the generated scaffold (e.g. project/scaffold)
        brief: The original brief text, used for service hallucination detection

    Returns:
        ConsistencyReport with ok=True when no errors found.
    """
    issues: List[ConsistencyIssue] = []
    scaffold_dir = scaffold_dir.resolve()

    # ── 1. Build file index ──────────────────────────────────────────────
    file_index: Dict[str, Path] = {}
    for path in scaffold_dir.rglob("*"):
        if path.is_file():
            rel = path.relative_to(scaffold_dir).as_posix()
            file_index[rel] = path
            # Also index without extension for extensionless imports
            file_index[rel.rsplit(".", 1)[0]] = path

    # ── 2. Read package.json deps ────────────────────────────────────────
    npm_deps = _read_package_json_deps(scaffold_dir)

    # ── 3. Detect allowed services from brief ────────────────────────────
    from skyn3t.agents.stack_templates import _detect_services

    allowed_services = set(_detect_services(brief))

    # ── 4. Parse every source file ───────────────────────────────────────
    for rel, path in file_index.items():
        if path.suffix not in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        if path.suffix == ".py":
            imports = _extract_py_imports(source)
            # For Python, check that non-stdlib imports have a requirements.txt entry
            # (this is a lightweight check; full stdlib enumeration is overkill)
            for mod in imports:
                if mod in ("os", "sys", "json", "re", "pathlib", "typing", "dataclasses"):
                    continue
                req_path = scaffold_dir / "requirements.txt"
                if req_path.exists():
                    req_text = req_path.read_text(encoding="utf-8").lower()
                    if mod.lower() not in req_text:
                        issues.append(
                            ConsistencyIssue(
                                severity="warning",
                                category="missing_dep",
                                file=rel,
                                message=f"Python module '{mod}' imported but not listed in requirements.txt.",
                                suggestion=f"Add {mod} to requirements.txt.",
                            )
                        )
        else:
            imports = _extract_js_imports(source)
            for src in imports:
                if _is_external_module(src):
                    # Strip subpath (e.g. "react-dom/client" → "react-dom")
                    pkg = src.split("/")[0]
                    if pkg not in JS_BUILTIN_MODULES and pkg not in npm_deps:
                        issues.append(
                            ConsistencyIssue(
                                severity="warning",
                                category="missing_dep",
                                file=rel,
                                message=f"npm package '{pkg}' imported but not in package.json dependencies.",
                                suggestion=f"Add '{pkg}' to dependencies or devDependencies.",
                            )
                        )
                elif src.startswith("."):
                    resolved = _resolve_relative(path, src)
                    if resolved is None:
                        continue
                    # Check if the resolved path (with any common extension) exists
                    exists = resolved.exists()
                    if not exists and not resolved.suffix:
                        for ext in (".jsx", ".js", ".tsx", ".ts", ".mjs", ".cjs"):
                            if resolved.with_suffix(ext).exists():
                                exists = True
                                break
                    if not exists:
                        issues.append(
                            ConsistencyIssue(
                                severity="error",
                                category="broken_import",
                                file=rel,
                                message=f"Relative import '{src}' does not resolve to an existing file.",
                                suggestion="Create the missing file or fix the import path.",
                            )
                        )

    # ── 5. Hallucination scan ────────────────────────────────────────────
    issues.extend(_scan_for_hallucinations(scaffold_dir, allowed_services))

    # ── 6. Missing router mount check ─────────────────────────────────────
    issues.extend(_find_missing_router_mounts(scaffold_dir))

    # ── 7. Frontend design-quality scan ───────────────────────────────────
    issues.extend(_scan_for_design_quality(scaffold_dir))

    # ── 8. Orphan export check (lightweight) ─────────────────────────────
    # For each file that exports something, check if another file imports it.
    # This is O(n²) but n ≤ 40 so it's fine.
    export_map: Dict[str, List[str]] = {}
    for rel, path in file_index.items():
        if path.suffix not in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Look for export default / export function / export const
        if re.search(r"\bexport\s+(?:default\s+)?(?:function|const|class|let|var)\b", source):
            export_map[rel] = []

    for importer_rel, path in file_index.items():
        if path.suffix not in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        imports = _extract_js_imports(source)
        for src in imports:
            if not src.startswith("."):
                continue
            resolved = _resolve_relative(path, src)
            if resolved is None:
                continue
            resolved_rel = resolved.relative_to(scaffold_dir).as_posix()
            if resolved_rel in export_map:
                export_map[resolved_rel].append(importer_rel)
            # Also match without extension
            resolved_rel_no_ext = resolved_rel.rsplit(".", 1)[0]
            if resolved_rel_no_ext in export_map:
                export_map[resolved_rel_no_ext].append(importer_rel)

    for export_file, importers in export_map.items():
        if not importers and not any(
            export_file.endswith(f"/{name}") or export_file == name
            for name in ("index", "main", "App", "page", "layout")
        ):
            issues.append(
                ConsistencyIssue(
                    severity="warning",
                    category="orphan_export",
                    file=export_file,
                    message="File exports symbols but is never imported by another file.",
                    suggestion="Check if the file is dead code or if imports are missing.",
                )
            )

    # ── TODO-stub detector ────────────────────────────────────────────────
    # When CodeAgent's per-file LLM call returns empty, _placeholder_for
    # writes a stub with the marker `TODO[skyn3t]: code generation failed`.
    # node --check / vite build / boot all pass on these stubs because they
    # are syntactically valid; only this static check catches them.
    # Files that ship as stubs are silent failures — flag as errors so the
    # targeted_fix loop has a chance to regenerate them before the run
    # claims success.
    _STUB_MARKER = "TODO[skyn3t]: code generation failed"
    _seen_stubs: Set[Path] = set()
    for rel, path in file_index.items():
        # file_index has both with-extension and without entries that
        # point at the same Path; dedup on the resolved path so we report
        # the file once, not twice.
        if path in _seen_stubs:
            continue
        if path.suffix not in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py", ".css", ".html"):
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:2000]
        except OSError:
            continue
        if _STUB_MARKER not in head:
            continue
        _seen_stubs.add(path)
        # Use the path-with-extension form for the report
        rel_with_ext = path.relative_to(scaffold_dir).as_posix()
        issues.append(
            ConsistencyIssue(
                severity="error",
                category="todo_stub",
                file=rel_with_ext,
                message=(
                    "File ships as a 'code generation failed' stub — the LLM "
                    "returned empty for this path and a placeholder was written. "
                    "Verifiers (node --check / vite build / boot) all pass on "
                    "this stub because it's syntactically valid, but the file "
                    "has no real implementation."
                ),
                suggestion="Regenerate this file with a fresh prompt that includes the file's plan-purpose.",
            )
        )

    errors = [i for i in issues if i.severity == "error"]
    return ConsistencyReport(ok=len(errors) == 0, issues=issues)
