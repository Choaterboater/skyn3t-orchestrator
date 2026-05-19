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
    category: str  # "broken_import" | "missing_dep" | "orphan_export" | "hallucination" | "missing_mount" | "design_quality" | "todo_stub" | "cross_artifact_palette_drift" | "cross_artifact_font_drift" | "brand_kit_ignored_by_scaffold"
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
_ORGANIC_STUB_PATTERNS = (
    re.compile(r"(?im)^\s*(//|#|/\*)\s*(TODO|FIXME)\b"),
    re.compile(r"""(?i)throw\s+new\s+Error\s*\(\s*['"][^'"]*not\s+implemented"""),
    re.compile(r"(?i)raise\s+NotImplementedError\b"),
    # "replace with real implementation" anywhere in a line comment.
    # Previously this required the phrase to follow the comment delimiter
    # directly; e79bc0 shipped `// Auto-generated placeholder — replace
    # with real implementation.` which had a prefix and slipped past.
    re.compile(r"(?im)^\s*(//|#).*?\breplace\s+with\s+real\s+implementation\b"),
    # "auto-generated placeholder" anywhere in a line comment — common
    # shape for scaffolding cruft that escaped per-file LLM writes.
    re.compile(r"(?im)^\s*(//|#).*?\bauto-?generated\s+placeholder\b"),
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


# Captures `import { Foo, Bar as B } from './baz'` and
# `import Default, { Named } from './baz'` and the plain default
# `import Default from './baz'`. We need the FULL specifier shape
# (not just the path) to validate that the target file actually
# exports under the name we're trying to import.
_DETAILED_IMPORT_RE = re.compile(
    r"""
    ^\s*import\s+
    (?P<specifiers>
        (?:[A-Za-z_$][\w$]*\s*,\s*)?       # optional `Default,`
        \{[^}]*\}                            # the `{ Named, ... }` block
        |
        \{[^}]*\}                            # bare `{ Named, ... }`
        |
        [A-Za-z_$][\w$]*                    # bare `Default`
    )
    \s+from\s+['"](?P<path>[^'"]+)['"]\s*;?
    """,
    re.VERBOSE | re.MULTILINE,
)


def _extract_js_imports_detailed(source: str) -> List[tuple]:
    """Extract `(target_path, default_name_or_None, named_imports)`
    tuples from a JS/TS source.

    Returns:
        List of (path, default_local_name, frozenset(named_imports))
        for each import statement that has at least one specifier.
        Side-effect-only imports (`import './foo.css';`) are NOT
        returned — they're handled by `_extract_js_imports`.
    """
    out: List[tuple] = []
    for m in _DETAILED_IMPORT_RE.finditer(source):
        path = m.group("path")
        spec = m.group("specifiers").strip()

        default_name: Optional[str] = None
        named: Set[str] = set()

        # Split off the named-import block (the `{...}` part).
        brace_open = spec.find("{")
        if brace_open >= 0:
            # `Default, { A, B as bb }` shape
            if brace_open > 0:
                lead = spec[:brace_open].rstrip().rstrip(",").strip()
                if lead:
                    default_name = lead
            brace_close = spec.find("}", brace_open)
            if brace_close > brace_open:
                inner = spec[brace_open + 1 : brace_close]
                for piece in inner.split(","):
                    name = piece.strip()
                    if not name:
                        continue
                    # `Foo as Bar` — the *source* name is what matters
                    # for export validation; the local rename is
                    # irrelevant here.
                    if " as " in name:
                        name = name.split(" as ", 1)[0].strip()
                    if name:
                        named.add(name)
        else:
            # Pure default import: `import Default from './x'`.
            default_name = spec

        out.append((path, default_name, frozenset(named)))
    return out


# Captures the kinds of exports we care about for validation:
#   export default <expr>
#   export default function Foo(...) {}
#   export default class Foo {}
#   export const Foo = ...
#   export let Foo = ...
#   export var Foo = ...
#   export function Foo(...) {}
#   export class Foo {}
#   export { Foo, Bar as B }      ← re-exports also count as named
_EXPORT_DEFAULT_RE = re.compile(r"\bexport\s+default\b")
_EXPORT_NAMED_DECL_RE = re.compile(
    r"\bexport\s+(?:async\s+)?"
    r"(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)"
)
_EXPORT_NAMED_BLOCK_RE = re.compile(r"\bexport\s*\{([^}]*)\}")


def _extract_js_exports(source: str) -> tuple:
    """Return ``(has_default, frozenset(named_exports))`` for a JS/TS
    source. Conservative — relies on regex, so weird patterns like
    `module.exports = { foo, bar }` get reported as having no
    detectable exports (treated the same as a plain script).
    """
    has_default = bool(_EXPORT_DEFAULT_RE.search(source))
    named: Set[str] = set()
    for m in _EXPORT_NAMED_DECL_RE.finditer(source):
        named.add(m.group(1))
    for m in _EXPORT_NAMED_BLOCK_RE.finditer(source):
        block = m.group(1)
        for piece in block.split(","):
            name = piece.strip()
            if not name:
                continue
            # `foo as Foo` — the EXPORTED name (after `as`) is what
            # importers see.
            if " as " in name:
                name = name.split(" as ", 1)[1].strip()
            if name and name != "default":
                named.add(name)
    return has_default, frozenset(named)


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


def _scan_for_import_style_mismatch(
    scaffold_dir: Path, file_index: Dict[str, Path]
) -> List[ConsistencyIssue]:
    """Detect import-style mismatches between importer and target.

    Specifically: `import { Foo } from './bar'` when bar.js doesn't
    have a named export `Foo` — only `export default Foo`. The
    target file resolves, the import compiles, but at runtime `Foo`
    is `undefined` and any use crashes.

    This is a fairly narrow check by design — we only flag the case
    where the target file has detectable named exports + a default
    export, and the importer asks for a name that's missing from
    named but matches the default's identifier. Outside that
    pattern (re-exports through index files, type-only imports,
    weird module patterns), we stay silent rather than false-flag.
    """
    issues: List[ConsistencyIssue] = []

    # Cache resolved exports per file so we don't re-parse the same
    # target on every importer's reference.
    exports_cache: Dict[Path, tuple] = {}

    def _get_exports(path: Path) -> tuple:
        if path in exports_cache:
            return exports_cache[path]
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            exports_cache[path] = (False, frozenset())
            return exports_cache[path]
        exports_cache[path] = _extract_js_exports(text)
        return exports_cache[path]

    for rel, path in file_index.items():
        if path.suffix not in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for import_path, default_name, named in _extract_js_imports_detailed(source):
            if not named:
                continue  # only the `import { X }` case is interesting
            if not import_path.startswith("."):
                continue  # external modules — can't validate without traversal
            resolved = _resolve_relative(path, import_path)
            if resolved is None:
                continue
            # Try the same extension fallback as the broken_import
            # check so target.jsx resolves from `./target`.
            target = resolved if resolved.exists() else None
            if target is None:
                for ext in (".jsx", ".js", ".tsx", ".ts", ".mjs", ".cjs"):
                    candidate = resolved.with_suffix(ext)
                    if candidate.exists():
                        target = candidate
                        break
            if target is None:
                continue  # broken_import check already flagged this
            has_default, named_exports = _get_exports(target)

            missing = [name for name in named if name not in named_exports]
            if not missing:
                continue
            for missing_name in missing:
                # Don't flag if the target has no detectable named OR
                # default — likely a non-standard module pattern
                # (CommonJS module.exports = {...}) we can't validate.
                if not has_default and not named_exports:
                    continue
                # The high-signal case: target HAS a default export
                # that probably matches the importer's intent, but
                # the importer used named-import syntax.
                if has_default:
                    suggestion = (
                        f"Change `import {{ {missing_name} }} from "
                        f"'{import_path}'` to `import {missing_name} "
                        f"from '{import_path}'`, or add "
                        f"`export {{ {missing_name} }}` to "
                        f"{target.relative_to(scaffold_dir).as_posix()}."
                    )
                else:
                    suggestion = (
                        f"Either add `export {{ {missing_name} }}` "
                        f"or `export const {missing_name} = ...` to "
                        f"{target.relative_to(scaffold_dir).as_posix()}, "
                        f"or remove the import."
                    )
                issues.append(
                    ConsistencyIssue(
                        severity="error",
                        category="broken_import",
                        file=rel,
                        message=(
                            f"Named import {{ {missing_name} }} from "
                            f"'{import_path}' — the target file does not "
                            f"export `{missing_name}` "
                            + (
                                "(it has a default export instead)."
                                if has_default
                                else "(no matching named export found)."
                            )
                        ),
                        suggestion=suggestion,
                    )
                )
    return issues


def _scan_tech_stack_claim_drift(scaffold_dir: Path) -> List[ConsistencyIssue]:
    """Detect when tech_stack.json's declared values don't have matching
    artifacts in the scaffold.

    Specifically checks the ``ci`` and ``db`` slots — both common
    hallucinations. e79bc0 declared ``ci: github-actions`` but shipped
    zero workflow files. tactrax declared ``db: better-sqlite3`` with
    no SQLite usage in the code.

    The check is conservative: we only flag when the architect
    committed to a SPECIFIC implementation that has a deterministic
    file footprint. Unknown / 'none' values are skipped.
    """
    issues: List[ConsistencyIssue] = []
    # tech_stack.json lives at the project root (one level up from
    # scaffold/).
    project_dir = scaffold_dir.parent
    tech_stack_path = project_dir / "tech_stack.json"
    if not tech_stack_path.is_file():
        return issues
    try:
        data = json.loads(tech_stack_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return issues
    if not isinstance(data, dict):
        return issues

    ci = str(data.get("ci") or "").strip().lower()
    db = str(data.get("db") or "").strip().lower()

    # CI check: github-actions claim needs .github/workflows/*.yml
    # somewhere. Look in both project_dir and scaffold_dir — different
    # projects use different layouts.
    if ci == "github-actions":
        workflows_dirs = (
            project_dir / ".github" / "workflows",
            scaffold_dir / ".github" / "workflows",
        )
        has_workflow = any(
            d.is_dir()
            and any(f.suffix in (".yml", ".yaml") for f in d.iterdir() if f.is_file())
            for d in workflows_dirs
        )
        if not has_workflow:
            issues.append(ConsistencyIssue(
                severity="warning",
                category="hallucination",
                file="tech_stack.json",
                message=(
                    "tech_stack.json declares `ci: github-actions` but no "
                    ".github/workflows/*.yml files exist in either the "
                    "scaffold or project root."
                ),
                suggestion=(
                    "Either ship a workflow file (e.g. "
                    ".github/workflows/ci.yml with build + test jobs) "
                    "or change `ci` to `none` so the claim matches "
                    "what was actually built."
                ),
            ))

    # DB check: SQLite / better-sqlite3 claim needs at least one
    # source file that references it.
    if db in {"better-sqlite3", "sqlite", "sqlite3"}:
        sqlite_referenced = False
        # Check package.json deps (server side: import 'better-sqlite3').
        for pkg in scaffold_dir.rglob("package.json"):
            try:
                pkg_data = json.loads(pkg.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            deps = {}
            for key in ("dependencies", "devDependencies"):
                if isinstance(pkg_data.get(key), dict):
                    deps.update(pkg_data[key])
            if any("sqlite" in name.lower() for name in deps):
                sqlite_referenced = True
                break
        # Check code files for `import 'better-sqlite3'` / similar.
        if not sqlite_referenced:
            for path in scaffold_dir.rglob("*"):
                if path.suffix not in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
                    continue
                try:
                    source = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if "sqlite" in source.lower() or ".db" in source:
                    sqlite_referenced = True
                    break
        if not sqlite_referenced:
            issues.append(ConsistencyIssue(
                severity="warning",
                category="hallucination",
                file="tech_stack.json",
                message=(
                    f"tech_stack.json declares `db: {db}` but no "
                    f"SQLite reference (package dep, import, or .db "
                    f"file) was found in the scaffold."
                ),
                suggestion=(
                    f"Either ship the {db} integration "
                    f"(add to package.json, import in the server, "
                    f"create a schema) or change `db` to `none`."
                ),
            ))

    return issues


# Path-like first line in a code file is almost always the
# CodeAgent per-file marker leaking into the body. Real shape:
# `src/components/ProgressRing.jsx` (bare, with slashes, ends in .jsx).
# We're conservative: must contain `/`, end in a known code extension,
# have no whitespace, and not be inside a comment.
_LEAKED_PATH_FIRST_LINE_RE = re.compile(
    r"^(?!\s)(?!//|#|/\*)[A-Za-z0-9_\-./]+\.(?:jsx?|tsx?|mjs|cjs|css|html|py)\s*$"
)


def _scan_for_leaked_path_markers(
    scaffold_dir: Path, file_index: Dict[str, Path]
) -> List[ConsistencyIssue]:
    """Detect files whose first line is a bare path string — almost
    always the per-file marker leaking through CodeAgent's output
    extraction. Vite / esbuild / node all fail on these as syntax
    errors; flag them so the targeted-fix loop regenerates."""
    issues: List[ConsistencyIssue] = []
    seen: Set[Path] = set()
    for rel, path in file_index.items():
        if path in seen:
            continue
        if path.suffix not in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            continue
        seen.add(path)
        try:
            head = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        if not head:
            continue
        first = head[0].rstrip()
        if not first:
            # Allow a leading blank line; check the next non-blank.
            for line in head[1:5]:
                line_stripped = line.rstrip()
                if line_stripped:
                    first = line_stripped
                    break
        if not _LEAKED_PATH_FIRST_LINE_RE.match(first):
            continue
        rel_with_ext = path.relative_to(scaffold_dir).as_posix()
        issues.append(ConsistencyIssue(
            severity="error",
            category="broken_import",
            file=rel_with_ext,
            message=(
                f"File starts with a bare path string `{first}` — "
                "almost certainly the per-file marker leaked into the "
                "output. Vite/esbuild/node will fail on this as a "
                "syntax error."
            ),
            suggestion=(
                "Delete the first line and regenerate the file (or "
                "have the targeted-fix loop strip the marker)."
            ),
        ))
    return issues


# Tailwind utility-class shapes that strongly imply Tailwind is in use.
# We look for at least one MULTI-PART class (e.g. bg-slate-900, h-14,
# px-3, rounded-xl) to avoid false-firing on plain CSS classes that
# happen to share a word like "flex" or "grid".
_TAILWIND_CLASS_RE = re.compile(
    r'\bclassName\s*=\s*["`\'][^"`\']*\b'
    r'(?:bg|text|border|ring|shadow|from|to|via|hover|focus|p|m|px|py|pt|pb|pl|pr|'
    r'mx|my|mt|mb|ml|mr|w|h|gap|space|rounded|opacity|z|inset|top|left|right|bottom|'
    r'flex|grid|order|col|row|justify|items|self|content|place)-'
)


def _scan_for_tailwind_without_config(scaffold_dir: Path) -> List[ConsistencyIssue]:
    """Flag scaffolds that use Tailwind utility classes in JSX but have
    no tailwindcss dep / no tailwind.config.* / no @tailwind directive.

    Real bug from 3c6a98: 4 components used Tailwind classes; no
    tailwindcss in package.json; no config files. Classes shipped as
    dead strings and the UI rendered unstyled. The build verifier
    passes because Vite happily compiles JSX with no idea what
    "bg-slate-900" means.
    """
    issues: List[ConsistencyIssue] = []

    # Collect package.json dependency unions across the scaffold.
    deps: Set[str] = set()
    for pkg_path in scaffold_dir.rglob("package.json"):
        try:
            data = json.loads(pkg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for key in ("dependencies", "devDependencies"):
            section = data.get(key)
            if isinstance(section, dict):
                deps.update(section.keys())

    has_tailwind_dep = "tailwindcss" in deps
    if has_tailwind_dep:
        return issues  # nothing to flag — Tailwind is properly installed

    # Check for tailwind.config.{js,cjs,mjs,ts} anywhere in scaffold.
    has_tailwind_config = any(
        (scaffold_dir / f"tailwind.config{ext}").is_file()
        for ext in (".js", ".cjs", ".mjs", ".ts")
    ) or any(scaffold_dir.rglob("tailwind.config.*"))

    # Check for @tailwind directives in CSS files (alternative way to
    # know Tailwind is in use, e.g. via PostCSS without an explicit
    # dependency in this package.json).
    has_tailwind_directive = False
    for css in scaffold_dir.rglob("*.css"):
        try:
            text = css.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "@tailwind" in text:
            has_tailwind_directive = True
            break

    if has_tailwind_config or has_tailwind_directive:
        return issues  # something else proves Tailwind is wired

    # Scan JSX / TSX files for Tailwind utility classes.
    tailwind_users: List[str] = []
    for path in scaffold_dir.rglob("*"):
        if path.suffix not in (".jsx", ".tsx", ".html"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _TAILWIND_CLASS_RE.search(source):
            tailwind_users.append(path.relative_to(scaffold_dir).as_posix())

    if not tailwind_users:
        return issues  # no Tailwind classes used — no problem

    # Cap at 5 files in the message for readability.
    preview = ", ".join(tailwind_users[:5])
    more = f" (+{len(tailwind_users) - 5} more)" if len(tailwind_users) > 5 else ""
    issues.append(ConsistencyIssue(
        severity="error",
        category="missing_dep",
        file="package.json",
        message=(
            f"{len(tailwind_users)} file(s) use Tailwind classes "
            f"({preview}{more}) but tailwindcss is not in any "
            "package.json and no tailwind.config.* / @tailwind "
            "directive was found. Classes will ship as dead strings."
        ),
        suggestion=(
            "Either add `tailwindcss` to package.json (plus "
            "`tailwind.config.js` and `@tailwind base; @tailwind "
            "components; @tailwind utilities;` in your main CSS), "
            "or replace the Tailwind classes with plain CSS."
        ),
    ))
    return issues


# Backend framework names that imply "the scaffold has a server the
# frontend is supposed to talk to." Static-only / none / unknown are
# excluded.
_REAL_BACKEND_FRAMEWORKS = {
    "express", "fastify", "koa", "hono", "hono-node",
    "fastapi", "flask", "django", "starlette", "aiohttp", "bottle",
    "next",  # API routes
}

# Regex hints that a frontend file is actually making API calls.
# Conservative — we just need ONE call site to know the frontend
# isn't a pure localStorage app.
_FRONTEND_API_CALL_RE = re.compile(
    r"\b(?:"
    r"fetch\s*\(\s*['\"`]\s*(?:/api|http)|"   # fetch('/api/...') / fetch('http...
    r"axios\.|axios\s*\(|"                      # axios calls
    r"XMLHttpRequest|"                           # raw XHR
    r"useSWR\s*\(|useQuery\s*\(|"               # SWR / TanStack Query
    r"api\.get\(|api\.post\(|api\.put\(|api\.delete\("  # api.get() etc
    r")"
)


def _scan_for_frontend_ignores_backend(scaffold_dir: Path) -> List[ConsistencyIssue]:
    """Detect when tech_stack.json promises a backend but the frontend
    never calls it.

    Strong signal of "two disconnected halves": architecture.md
    describes /api/habits, decisions.json pins backend_port=3000,
    server/index.js exists — but App.jsx + every component uses pure
    localStorage and zero fetch calls. The user gets a frontend that
    can't possibly talk to the backend that was just built.
    """
    issues: List[ConsistencyIssue] = []
    project_dir = scaffold_dir.parent

    # Determine "is there supposed to be a backend?"
    tech_stack_path = project_dir / "tech_stack.json"
    if not tech_stack_path.is_file():
        return issues
    try:
        data = json.loads(tech_stack_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return issues
    if not isinstance(data, dict):
        return issues
    backend = str(data.get("backend") or "").strip().lower()
    if backend not in _REAL_BACKEND_FRAMEWORKS:
        return issues  # static / none / unknown — frontend is allowed to be local-only

    # Confirm a server actually shipped — if there's no server code
    # at all, the "frontend ignores backend" diagnosis is wrong (the
    # right diagnosis is "backend is fictional", which the
    # tech_stack-vs-reality check already catches).
    server_dir = scaffold_dir / "server"
    if not server_dir.is_dir():
        return issues  # no server to be ignored
    server_code_present = any(
        f.suffix in (".js", ".jsx", ".ts", ".tsx", ".py", ".mjs", ".cjs")
        for f in server_dir.rglob("*")
        if f.is_file()
    )
    if not server_code_present:
        return issues

    # Scan frontend JSX/TSX for any API call patterns.
    src_dir = scaffold_dir / "src"
    if not src_dir.is_dir():
        # No src/ → not a standard frontend layout, skip
        return issues

    frontend_files_scanned = 0
    frontend_files_with_api_calls = 0
    for path in src_dir.rglob("*"):
        if path.suffix not in (".jsx", ".tsx", ".js", ".ts", ".mjs"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        frontend_files_scanned += 1
        if _FRONTEND_API_CALL_RE.search(source):
            frontend_files_with_api_calls += 1

    if frontend_files_scanned == 0:
        return issues  # nothing to scan
    if frontend_files_with_api_calls > 0:
        return issues  # at least one frontend file IS calling the API

    # Strong signal: backend exists, frontend has source files, NONE
    # of them call the API. The two halves are disconnected.
    issues.append(ConsistencyIssue(
        severity="error",
        category="contradiction",
        file="src/",
        message=(
            f"tech_stack.json declares `backend: {backend}` and "
            f"server/ has real code, but {frontend_files_scanned} "
            f"frontend file(s) under src/ make ZERO API calls "
            "(no fetch, axios, useSWR, useQuery, etc.). The frontend "
            "appears to be pure-localStorage and never talks to the "
            "backend that was built."
        ),
        suggestion=(
            "Either wire the frontend to call the backend "
            "(`fetch('/api/<resource>')` in App.jsx / hooks), or "
            "change `backend` to `none` and drop the server/ "
            "directory if the app really is local-only."
        ),
    ))
    return issues


def _scan_for_hallucinations(scaffold_dir: Path, allowed_services: Set[str]) -> List[ConsistencyIssue]:
    """Scan JS/JSX/MD files for mentions of services not in the allowed set.

    This catches Plex bleed-through: the brief asked for 7 services but
    the LLM mentioned Plex in a README or BRAND object.
    """
    issues: List[ConsistencyIssue] = []
    if not allowed_services:
        return issues

    # Known service words to scan for (case-insensitive). Some have
    # display/slug variants that must all map to the same canonical id
    # so we don't flag "Home Assistant" as a hallucination when the
    # brief allowed "home_assistant".
    service_aliases: Dict[str, set[str]] = {
        "plex": {"plex"},
        "jellyfin": {"jellyfin"},
        "emby": {"emby"},
        "sonarr": {"sonarr"},
        "radarr": {"radarr"},
        "prowlarr": {"prowlarr"},
        "qbittorrent": {"qbittorrent"},
        "transmission": {"transmission"},
        "nzbget": {"nzbget"},
        "sabnzbd": {"sabnzbd"},
        "sonos": {"sonos"},
        "docker": {"docker"},
        "home_assistant": {"home_assistant", "home assistant", "home-assistant"},
        "pihole": {"pihole", "pi-hole", "pi hole"},
        "unifi": {"unifi"},
        "overseerr": {"overseerr"},
        "tautulli": {"tautulli"},
    }
    # Lowercase the caller-allowed set and expand it through the alias
    # map so callers can pass either slug or display form.
    allowed_normalized: set[str] = set()
    for svc in allowed_services:
        lowered = svc.lower()
        for canonical, aliases in service_aliases.items():
            if lowered == canonical or lowered in aliases:
                allowed_normalized.add(canonical)
                break
        else:
            allowed_normalized.add(lowered)

    for path in scaffold_dir.rglob("*"):
        if path.is_dir():
            continue
        if path.suffix not in (".js", ".jsx", ".ts", ".tsx", ".md", ".txt"):
            continue
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (OSError, UnicodeDecodeError):
            continue
        # Scan each canonical service. A canonical is "mentioned" if
        # ANY of its aliases appears as a standalone token. It's a
        # hallucination only when the canonical isn't in the allowed
        # set.
        for canonical, aliases in service_aliases.items():
            if canonical in allowed_normalized:
                continue
            matched_word: Optional[str] = None
            for word in aliases:
                if word not in text:
                    continue
                # Avoid false positives: only flag if the word appears
                # as a standalone token (not inside another word like
                # "complex").
                pattern = r"\b" + re.escape(word) + r"\b"
                if re.search(pattern, text):
                    matched_word = word
                    break
            if matched_word is None:
                continue
            rel = path.relative_to(scaffold_dir).as_posix()
            issues.append(
                ConsistencyIssue(
                    severity="warning",
                    category="hallucination",
                    file=rel,
                    message=(
                        f"Mentions '{matched_word}' which is not in the "
                        f"brief's requested services."
                    ),
                    suggestion=(
                        f"Remove references to {matched_word} or add it "
                        f"to the service registry."
                    ),
                )
            )
            # Only report each canonical once per file
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


_HEX_RE = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
_RE_MATCH_TYPE = re.Match  # type alias for the inner replace helper
_FONT_FAMILY_RE = re.compile(
    r"""(?:font[\-\s_]?family|font[\-\s_]?display|font[\-\s_]?mono)\s*[:=]\s*['"]?([A-Z][A-Za-z0-9 _\-]+)""",
    re.IGNORECASE,
)
_NUMERIC_PALETTE_KEYS = ("bg", "background", "surface", "accent", "primary", "text", "muted", "border")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _normalize_hex(hex_code: str) -> str:
    """Return ``#rrggbb`` lowercase. Expands 3-char shorthand."""
    code = hex_code.strip().lstrip("#").lower()
    if len(code) == 3:
        code = "".join(c * 2 for c in code)
    return f"#{code}"


def _extract_hexes(text: str) -> Set[str]:
    return {_normalize_hex(m.group(1)) for m in _HEX_RE.finditer(text)}


def _extract_fonts(text: str) -> Set[str]:
    """Best-effort font-family extraction. Catches CSS ``font-family:`` and
    Tailwind / JSON ``font-display`` / ``font-mono`` style declarations.
    Filters out generic family fallbacks (sans-serif, monospace, etc.)."""
    fonts: Set[str] = set()
    for match in _FONT_FAMILY_RE.finditer(text):
        raw = match.group(1).strip().strip("'\"")
        if not raw:
            continue
        first = raw.split(",")[0].strip().strip("'\"")
        if not first:
            continue
        if first.lower() in {"sans-serif", "serif", "monospace", "system-ui", "ui-monospace", "inherit"}:
            continue
        fonts.add(first)
    return fonts


def _extract_palette_json_hexes(project_dir: Path) -> Set[str]:
    """Pull color hex codes out of palette.json, whether it's flat
    {"bg": "#...", "accent": "#..."} or a list-of-objects shape."""
    p = project_dir / "palette.json"
    if not p.exists():
        return set()
    try:
        data = json.loads(_read(p))
    except Exception:  # noqa: BLE001
        return set()
    out: Set[str] = set()
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, str):
                out.update(_extract_hexes(v))
            elif isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, str):
                        out.update(_extract_hexes(vv))
    elif isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                for vv in entry.values():
                    if isinstance(vv, str):
                        out.update(_extract_hexes(vv))
            elif isinstance(entry, str):
                out.update(_extract_hexes(entry))
    return out


def _scan_cross_artifact_drift(
    scaffold_dir: Path, brief: str = ""
) -> List[ConsistencyIssue]:
    """Detect when a project's design artifacts disagree with each other.

    Real example from a project that scored 63: ``palette.json`` listed 5
    colors, ``brand.md`` listed 10, ``tokens.css`` used different cyan
    shades. The reviewer flagged it as ~15 points of deductions. This
    check surfaces the same disagreements as warnings so the targeted-fix
    loop can resolve them BEFORE the reviewer scores the project.

    Looks at the project root (one level up from ``scaffold/``) for the
    "source of truth" artifacts: ``palette.json``, ``brand.md``,
    ``tokens.css``, ``tokens.json``, ``components.md``.
    """
    issues: List[ConsistencyIssue] = []
    project_dir = scaffold_dir.parent
    if not project_dir.exists():
        return issues

    palette_hexes = _extract_palette_json_hexes(project_dir)
    brand_text = _read(project_dir / "brand.md")
    brand_hexes = _extract_hexes(brand_text)
    tokens_css_text = _read(project_dir / "tokens.css")
    tokens_css_hexes = _extract_hexes(tokens_css_text)
    components_text = _read(project_dir / "components.md")
    components_hexes = _extract_hexes(components_text)

    sources: Dict[str, Set[str]] = {}
    if palette_hexes:
        sources["palette.json"] = palette_hexes
    if brand_hexes:
        sources["brand.md"] = brand_hexes
    if tokens_css_hexes:
        sources["tokens.css"] = tokens_css_hexes
    if components_hexes:
        sources["components.md"] = components_hexes

    # Drift = a source declares a hex that no other source uses. We
    # compare the SMALLEST source as the canonical, since palette.json
    # is usually that — it's the curated short-list. If any larger
    # source uses a color that isn't in palette.json, flag it.
    if len(sources) >= 2 and "palette.json" in sources:
        canonical = sources["palette.json"]
        for name, hexes in sources.items():
            if name == "palette.json":
                continue
            extras = hexes - canonical
            if len(extras) >= 2:
                preview = ", ".join(sorted(extras)[:5])
                issues.append(ConsistencyIssue(
                    severity="warning",
                    category="cross_artifact_palette_drift",
                    file=name,
                    message=(
                        f"{name} uses colors not in palette.json: {preview}"
                        + (f" (+{len(extras) - 5} more)" if len(extras) > 5 else "")
                    ),
                    suggestion=(
                        "Align colors with palette.json. The brand kit is the "
                        "single source of truth — every other artifact should "
                        "reference these hex codes, not invent new ones."
                    ),
                ))

    # Font drift: brand.md, tokens.css, and components.md should agree on
    # the font families being used.
    brand_fonts = _extract_fonts(brand_text)
    tokens_fonts = _extract_fonts(tokens_css_text)
    components_fonts = _extract_fonts(components_text)
    font_sources: Dict[str, Set[str]] = {}
    if brand_fonts:
        font_sources["brand.md"] = brand_fonts
    if tokens_fonts:
        font_sources["tokens.css"] = tokens_fonts
    if components_fonts:
        font_sources["components.md"] = components_fonts
    if len(font_sources) >= 2:
        # Treat brand.md as canonical when present (it's the designer's output);
        # else fall back to tokens.css.
        canonical_name = "brand.md" if "brand.md" in font_sources else "tokens.css"
        canonical_fonts = font_sources.get(canonical_name, set())
        # Case-insensitive set for comparison
        canonical_lower = {f.lower() for f in canonical_fonts}
        for name, fonts in font_sources.items():
            if name == canonical_name:
                continue
            extras = {f for f in fonts if f.lower() not in canonical_lower}
            if extras:
                preview = ", ".join(sorted(extras)[:3])
                issues.append(ConsistencyIssue(
                    severity="warning",
                    category="cross_artifact_font_drift",
                    file=name,
                    message=(
                        f"{name} uses font families not in {canonical_name}: {preview}"
                    ),
                    suggestion=(
                        f"Align fonts with {canonical_name}. brand.md is the "
                        "canonical source for typography; tokens.css and any "
                        "components.md should reference the SAME families."
                    ),
                ))

    # Scaffold-vs-brand-kit drift: at least one scaffold file should
    # actually use a color from palette.json. Otherwise the brand kit is
    # decorative — the scaffold ignored it.
    if palette_hexes:
        scaffold_used: Set[str] = set()
        for path in scaffold_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in (".css", ".scss", ".jsx", ".tsx", ".js", ".ts", ".html"):
                continue
            try:
                scaffold_used.update(_extract_hexes(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue
        if scaffold_used:
            overlap = palette_hexes & scaffold_used
            if not overlap:
                preview = ", ".join(sorted(palette_hexes)[:3])
                issues.append(ConsistencyIssue(
                    severity="error",
                    category="brand_kit_ignored_by_scaffold",
                    file="(scaffold)",
                    message=(
                        f"Scaffold uses no colors from palette.json ({preview}…). "
                        "The brand kit was generated but the code ignored it."
                    ),
                    suggestion=(
                        "Update scaffold CSS/JSX to use palette.json hex codes. "
                        "Otherwise the brand work was wasted."
                    ),
                ))

    # Collapsed-palette check: brand.md frequently ships with
    # bg / surface / surface-2 / border all the same hex (e79bc0:
    # all `#f5f5f0`; tactrax: `Border: #FFFFFF` on `Background:
    # #FFFFFF`). The LLM's own commentary often acknowledges
    # "warmth from the contrast between them" — but there's no
    # contrast. Borders render invisibly, gentle-elevation
    # surfaces look like flat blocks.
    if brand_text:
        issues.extend(_check_collapsed_brand_palette(brand_text))

    # Entry-file specific brand drift: the scaffold-wide check above
    # passes if ANY file uses the palette. e79bc0 had HabitCard.jsx
    # (orphaned) using the brand colors while App.jsx (active) used
    # bg-slate-900 + emerald-400 + gradients + emoji — the user only
    # ever sees App.jsx. The reviewer correctly flagged this even
    # though "scaffold-wide" had palette coverage.
    if palette_hexes:
        issues.extend(
            _scan_entry_file_brand_drift(scaffold_dir, palette_hexes)
        )

    return issues


# Capture rows in brand.md's palette tables. Real examples:
#   | `bg` | `#F5F5F0` | Canvas — warm off-white...
#   | `surface` | `#f5f5f0` | Card surfaces...
#   - **Background**`#FFFFFF`
#   - **Border**`#FFFFFF`
# Patterns: token name wrapped in backticks or **bold**, followed
# by a hex literal in backticks or bare.
_BRAND_TOKEN_LINE_RES = (
    re.compile(
        r"^\s*\|\s*[`*]+\s*(?P<token>[a-z][a-z0-9_-]*)\s*[`*]+\s*\|\s*"
        r"[`*]*\s*(?P<hex>#[0-9a-fA-F]{3,8})\s*[`*]*",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"^\s*[-*]\s*\*\*(?P<token>[A-Za-z][A-Za-z0-9_-]*)\*\*\s*"
        r"[`]?\s*(?P<hex>#[0-9a-fA-F]{3,8})",
        re.MULTILINE,
    ),
)

# Tokens that MUST be distinct from `bg` for the layout to render
# correctly. Border-same-as-bg is the headline bug; surface-same-
# as-bg means the card-vs-canvas distinction disappears.
_SURFACE_TOKENS = {"surface", "surface-1", "surface-2", "card"}
_BORDER_TOKENS = {"border", "border-strong", "divider", "hairline"}


def _check_collapsed_brand_palette(brand_text: str) -> List[ConsistencyIssue]:
    """Parse brand.md's palette table and flag tokens collapsed to bg."""
    issues: List[ConsistencyIssue] = []
    tokens: Dict[str, str] = {}
    for pattern in _BRAND_TOKEN_LINE_RES:
        for m in pattern.finditer(brand_text):
            name = m.group("token").lower()
            hex_code = m.group("hex").lower()
            tokens.setdefault(name, hex_code)

    bg = tokens.get("bg") or tokens.get("background")
    if not bg:
        return issues

    collapsed_borders = [t for t in _BORDER_TOKENS if t in tokens and tokens[t] == bg]
    collapsed_surfaces = [t for t in _SURFACE_TOKENS if t in tokens and tokens[t] == bg]

    if collapsed_borders:
        names = ", ".join(sorted(collapsed_borders))
        issues.append(ConsistencyIssue(
            severity="warning",
            category="design_quality",
            file="brand.md",
            message=(
                f"brand.md declares {names} = {bg}, the same hex as "
                f"`bg`. Borders rendered with this token will be "
                f"invisible — `border-[{bg}]` on a `bg-[{bg}]` "
                f"surface has no contrast."
            ),
            suggestion=(
                "Pick a border color that's 5-10% darker/lighter than "
                "`bg` (or a desaturated accent) so the hairline rules "
                "the brand promises are actually visible."
            ),
        ))
    if collapsed_surfaces:
        names = ", ".join(sorted(collapsed_surfaces))
        issues.append(ConsistencyIssue(
            severity="warning",
            category="design_quality",
            file="brand.md",
            message=(
                f"brand.md declares {names} = {bg}, the same hex as "
                f"`bg`. Cards using these tokens will be "
                f"indistinguishable from the canvas — the 'gentle "
                f"elevation' the doc promises won't render."
            ),
            suggestion=(
                "Shift surface tokens 3-5% off `bg` so cards have "
                "real elevation. For light themes, slightly darker; "
                "for dark themes, slightly lighter."
            ),
        ))
    return issues


# Tailwind utility names that strongly imply a dark / saturated theme
# (e.g. `bg-slate-900`, `bg-zinc-950`, `text-emerald-400`). When App.jsx
# uses these AND zero palette hexes, the LLM almost certainly ignored
# brand.md in favor of its training-set defaults.
_DARK_TAILWIND_RE = re.compile(
    r"\b(?:bg|text|border|ring|shadow|from|to|via)-"
    r"(?:slate|zinc|neutral|stone|gray|emerald|teal|rose|cyan|"
    r"indigo|violet|fuchsia|sky|amber|lime|red)-"
    r"(?:[5-9]\d{2})\b"
)
# Gradients + glassmorphism are the other off-brief patterns
# review.md repeatedly flagged.
_TAILWIND_GRADIENT_RE = re.compile(r"\bbg-gradient-to-")
_GLASSMORPHISM_RE = re.compile(r"\bbackdrop-blur(?:-\w+)?\b")
# Emoji are a common brand-violation: "no confetti, no exclamation
# points" type brand voices wouldn't ship 🔥. Match common emoji
# unicode ranges.
_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001F9FF\U0001FA70-\U0001FAFF☀-⛿✀-➿]"
)

# Entry files we care about — the user-visible top-level scaffold.
_ENTRY_FILE_REL_PATTERNS = (
    "src/App.jsx", "src/App.tsx",
    "App.jsx", "App.tsx",
    "src/app.jsx", "src/app.tsx",
    "src/pages/index.tsx", "src/pages/index.jsx",
    "app/page.tsx", "app/page.jsx",
)


def _scan_entry_file_brand_drift(
    scaffold_dir: Path, palette_hexes: Set[str]
) -> List[ConsistencyIssue]:
    """Flag entry files that use zero palette hex codes AND heavy
    dark-Tailwind / gradient / glassmorphism / emoji styling — strong
    signal the LLM emitted training-set defaults instead of honoring
    brand.md."""
    issues: List[ConsistencyIssue] = []
    for rel in _ENTRY_FILE_REL_PATTERNS:
        path = scaffold_dir / rel
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        file_hexes = _extract_hexes(source)
        if file_hexes & palette_hexes:
            continue  # entry file uses at least one palette color

        # Count drift signals — we need MULTIPLE to be confident
        # this is brand drift and not a legitimate dark theme.
        dark_hits = len(_DARK_TAILWIND_RE.findall(source))
        gradient_hits = len(_TAILWIND_GRADIENT_RE.findall(source))
        glass_hits = len(_GLASSMORPHISM_RE.findall(source))
        emoji_hits = len(_EMOJI_RE.findall(source))

        signal_count = (
            (1 if dark_hits >= 3 else 0)
            + (1 if gradient_hits >= 1 else 0)
            + (1 if glass_hits >= 1 else 0)
            + (1 if emoji_hits >= 1 else 0)
        )
        if signal_count < 2:
            continue  # not enough signal — stay quiet

        signals: List[str] = []
        if dark_hits >= 3:
            signals.append(f"{dark_hits} dark-Tailwind class(es)")
        if gradient_hits >= 1:
            signals.append(f"{gradient_hits} gradient(s)")
        if glass_hits >= 1:
            signals.append(f"{glass_hits} glassmorphism class(es)")
        if emoji_hits >= 1:
            signals.append(f"{emoji_hits} emoji")
        preview = ", ".join(sorted(palette_hexes)[:3])

        issues.append(ConsistencyIssue(
            severity="error",
            category="brand_kit_ignored_by_scaffold",
            file=rel,
            message=(
                f"{rel} uses none of the palette.json hex codes "
                f"({preview}…) AND heavy off-brief styling: "
                + "; ".join(signals)
                + ". Strong signal the LLM ignored brand.md."
            ),
            suggestion=(
                "Rewrite the file using palette.json hex codes (or "
                "Tailwind arbitrary values like `bg-[#F5F5F0]`). "
                "Drop the dark-mode default Tailwind classes, "
                "gradients, glassmorphism, and emoji unless brand.md "
                "explicitly endorses them."
            ),
        ))
    return issues


def _nearest_palette_color(target_hex: str, palette: Set[str]) -> str:
    """Pick the palette color closest to ``target_hex`` by RGB distance.

    Used by the auto-fix path: when tokens.css declares ``#1A2B3C`` but
    palette.json only has ``#1B2A3D``, we want to map the off-by-a-few
    color to the canonical palette entry instead of dropping it.
    """
    if not palette:
        return target_hex
    try:
        t_r = int(target_hex[1:3], 16)
        t_g = int(target_hex[3:5], 16)
        t_b = int(target_hex[5:7], 16)
    except (ValueError, IndexError):
        return target_hex
    best_color = target_hex
    best_dist = 10**9
    for p in palette:
        try:
            p_r = int(p[1:3], 16)
            p_g = int(p[3:5], 16)
            p_b = int(p[5:7], 16)
        except (ValueError, IndexError):
            continue
        # Squared euclidean — no need for sqrt for comparison.
        dist = (t_r - p_r) ** 2 + (t_g - p_g) ** 2 + (t_b - p_b) ** 2
        if dist < best_dist:
            best_dist = dist
            best_color = p
    return best_color


def auto_fix_cross_artifact_drift(scaffold_dir: Path) -> Dict[str, int]:
    """Rewrite tokens.css / components.md / scaffold files so all hex
    color literals come from palette.json.

    Strategy: take every hex literal in the target files. If it's not in
    palette.json, replace it with the palette's nearest-neighbor color
    (by RGB distance). This collapses 3 contradicting palettes into 1
    without an LLM call.

    Returns a dict ``{file_path: num_replacements}`` for the audit log.
    Safe to call when palette.json is missing — returns empty dict.
    """
    project_dir = scaffold_dir.parent
    if not project_dir.exists():
        return {}

    palette_hexes = _extract_palette_json_hexes(project_dir)
    if len(palette_hexes) < 2:
        # No canonical palette to sync against. Skip silently.
        return {}

    # Files whose hex literals should match palette.json.
    fix_targets: List[Path] = [
        project_dir / "tokens.css",
        project_dir / "components.md",
        project_dir / "brand.md",
    ]
    # Plus scaffold CSS + JSX (the actual code).
    if scaffold_dir.exists():
        for ext in (".css", ".scss", ".jsx", ".tsx", ".js", ".ts"):
            fix_targets.extend(scaffold_dir.rglob(f"*{ext}"))

    edits: Dict[str, int] = {}
    for path in fix_targets:
        if not path.is_file():
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Find every hex literal. For each that's NOT in palette, replace
        # with the nearest palette color. Preserve casing in surrounding
        # text — only the literal itself changes.
        replacements = 0

        def _replace(match: "_RE_MATCH_TYPE") -> str:  # type: ignore[name-defined]
            nonlocal replacements
            raw: str = match.group(0)
            try:
                normalized = _normalize_hex(raw)
            except Exception:  # noqa: BLE001
                return raw
            if normalized in palette_hexes:
                return raw  # already canonical
            nearest = _nearest_palette_color(normalized, palette_hexes)
            if nearest == normalized:
                return raw  # no better match (palette empty edge case)
            replacements += 1
            return nearest

        new_text = _HEX_RE.sub(_replace, original)
        if replacements > 0 and new_text != original:
            try:
                path.write_text(new_text, encoding="utf-8")
                edits[str(path.relative_to(project_dir))] = replacements
            except OSError:
                continue

    return edits


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

    # ── 4b. Default-vs-named import-style mismatch ───────────────────────
    # The "file exists" check above catches missing files, but not the
    # subtler case where the target file exists but doesn't export
    # under the name we're trying to import. Real bug from e79bc0
    # review: HabitList.jsx does `import { HabitCard } from './HabitCard'`
    # (named) but HabitCard.jsx is `export default HabitCard` — would
    # crash with "HabitCard is undefined" at runtime if anyone wired
    # it up.
    issues.extend(_scan_for_import_style_mismatch(scaffold_dir, file_index))

    # ── 4c. Leaked path-marker as first line ──────────────────────────────
    # When CodeAgent's per-file output extraction fails to strip the
    # marker line (e.g. `// === src/components/ProgressRing.jsx ===`),
    # the path string survives as the first line of the file body.
    # Real bug from 3c6a98: ProgressRing.jsx shipped with literal
    # "src/components/ProgressRing.jsx" as line 1. Vite build fails.
    issues.extend(_scan_for_leaked_path_markers(scaffold_dir, file_index))

    # ── 5. Hallucination scan ────────────────────────────────────────────
    issues.extend(_scan_for_hallucinations(scaffold_dir, allowed_services))

    # ── 6. Missing router mount check ─────────────────────────────────────
    issues.extend(_find_missing_router_mounts(scaffold_dir))

    # ── 7. Frontend design-quality scan ───────────────────────────────────
    issues.extend(_scan_for_design_quality(scaffold_dir))

    # ── 7b. Cross-artifact drift (palette / fonts / brand-kit usage) ────
    issues.extend(_scan_cross_artifact_drift(scaffold_dir, brief))

    # ── 7c. tech_stack.json claims vs scaffold reality ────────────────────
    # e79bc0's tech_stack.json declared `ci: github-actions` but the
    # scaffold shipped no .github/workflows/*.yml. The reviewer flagged
    # this as a hallucinated claim; this check catches it deterministically.
    issues.extend(_scan_tech_stack_claim_drift(scaffold_dir))

    # ── 7d. Tailwind classes without Tailwind installed ───────────────────
    # Real bug from 3c6a98: AddHabitForm / HabitCard / HabitList etc.
    # all used Tailwind classes (`bg-slate-900`, `text-emerald-300`,
    # `flex`, `gap-4`) but package.json had no tailwindcss dep, no
    # tailwind.config.js, no PostCSS config. The classes ship as dead
    # strings — the markup renders but the styling is invisible.
    issues.extend(_scan_for_tailwind_without_config(scaffold_dir))

    # ── 7e. Frontend ignores promised backend ─────────────────────────────
    # Real bug from e79bc0 + others: tech_stack.json declares a backend
    # framework (express, fastapi, ...) and architecture.md spec'd
    # /api/habits endpoints, but App.jsx never calls fetch('/api/...').
    # The scaffold is two disconnected halves: a frontend that uses
    # localStorage and a backend that nobody calls.
    issues.extend(_scan_for_frontend_ignores_backend(scaffold_dir))

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
    # When the post-code targeted-fix backfill writes a stub for an
    # unresolved import, it tags the file with `@skyn3t-backfill-stub`.
    # node --check / vite build / boot all pass on these stubs because they
    # are syntactically valid; only this static check catches them.
    # Files that ship as stubs are silent failures — flag as errors so the
    # targeted_fix loop has a chance to regenerate them before the run
    # claims success.
    _STUB_MARKERS = (
        # CodeAgent's empty-LLM-output placeholder (legacy marker).
        ("TODO[skyn3t]: code generation failed", "code-generation-failed"),
        # Targeted-fix backfill for unresolved imports — e79bc0 shipped
        # a `// @skyn3t-backfill-stub: for missing import.` HabitDashboard
        # that `return null`s. The reviewer caught it; this catches it
        # deterministically.
        ("@skyn3t-backfill-stub", "backfill-stub"),
    )
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
        matched_kind: Optional[str] = None
        for marker, kind in _STUB_MARKERS:
            if marker in head:
                matched_kind = kind
                break
        if matched_kind is None:
            continue
        _seen_stubs.add(path)
        # Use the path-with-extension form for the report
        rel_with_ext = path.relative_to(scaffold_dir).as_posix()
        if matched_kind == "backfill-stub":
            message = (
                "File ships as a backfill stub — generated by the "
                "post-code fix loop to satisfy an unresolved import, "
                "but the body is a placeholder (typically `return "
                "null` or `pass`). Verifiers pass because the file is "
                "syntactically valid, but the named export does nothing."
            )
            suggestion = (
                "Either implement the component / function this stub "
                "is impersonating, or remove the import that referenced "
                "it. Don't ship the @skyn3t-backfill-stub marker."
            )
        else:
            message = (
                "File ships as a 'code generation failed' stub — the LLM "
                "returned empty for this path and a placeholder was written. "
                "Verifiers (node --check / vite build / boot) all pass on "
                "this stub because it's syntactically valid, but the file "
                "has no real implementation."
            )
            suggestion = "Regenerate this file with a fresh prompt that includes the file's plan-purpose."
        issues.append(
            ConsistencyIssue(
                severity="error",
                category="todo_stub",
                file=rel_with_ext,
                message=message,
                suggestion=suggestion,
            )
        )

    _seen_organic_stubs: Set[Path] = set()
    organic_stub_suffixes = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py"}
    for rel, path in file_index.items():
        if path in _seen_organic_stubs:
            continue
        if path.suffix not in organic_stub_suffixes:
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:4000]
        except OSError:
            continue
        # Skip if any known SkyN3t stub marker is present — those are
        # already flagged with a sharper message by the loop above.
        if any(marker in head for marker, _ in _STUB_MARKERS):
            continue
        if not any(pattern.search(head) for pattern in _ORGANIC_STUB_PATTERNS):
            continue
        _seen_organic_stubs.add(path)
        rel_with_ext = path.relative_to(scaffold_dir).as_posix()
        issues.append(
            ConsistencyIssue(
                severity="error",
                category="todo_stub",
                file=rel_with_ext,
                message=(
                    "File appears to contain an unresolved placeholder implementation "
                    "(for example TODO/FIXME or not-implemented stub text)."
                ),
                suggestion="Replace the placeholder with a real implementation; do not ship TODO stubs.",
            )
        )

    errors = [i for i in issues if i.severity == "error"]
    return ConsistencyReport(ok=len(errors) == 0, issues=issues)
