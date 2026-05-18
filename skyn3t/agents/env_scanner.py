"""Scan a project directory for environment-variable references.

Used by PackagingAgent (and anything else that wants to know "what config
does this app need?") to generate accurate Settings UIs, .env.example
files, and READMEs without asking the LLM to guess.

No LLM calls. Pure regex + light AST traversal. Five idioms supported:

1. ``process.env.X`` / ``process.env["X"]``  — Node.js
2. ``import.meta.env.X`` / ``import.meta.env["X"]`` — Vite / ESM
3. ``os.getenv("X")`` / ``os.getenv("X", default)`` — Python stdlib
4. ``os.environ["X"]`` / ``os.environ.get("X")`` — Python stdlib
5. ``class Settings(BaseSettings): X: str = ...`` — pydantic-settings

Each detected var gets a structured EnvVarRef back so downstream
generators can render labels, types, default values, and help text
without re-parsing source.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class EnvVarRef:
    """One environment-variable reference, aggregated across all sites."""

    name: str
    used_in: List[str] = field(default_factory=list)  # relative file paths
    # The first non-empty default we found. Most env vars have no default
    # (caller must supply); ones with a default usually represent optional
    # tuning knobs rather than required secrets.
    default: Optional[str] = None
    # Type hint inferred from the call site. "string" by default; "int" /
    # "bool" / "url" / "secret" / "email" when we can tell.
    type_hint: str = "string"
    # Heuristic flag — names matching common secret patterns (KEY, TOKEN,
    # SECRET, PASSWORD) get masked in generated UIs by default.
    is_secret: bool = False
    # Source of detection — useful for debugging and for downstream
    # generators that want to weight idioms differently.
    idiom: str = ""  # "node" | "vite" | "python_getenv" | "python_environ" | "pydantic"


@dataclass
class ScanResult:
    """Aggregated result of an entire project scan."""

    vars: Dict[str, EnvVarRef] = field(default_factory=dict)
    scanned_files: int = 0
    skipped_files: int = 0

    def required(self) -> List[EnvVarRef]:
        """Vars with no default — the user MUST supply these."""
        return [v for v in self.vars.values() if v.default is None]

    def optional(self) -> List[EnvVarRef]:
        """Vars with a default — operator can override but isn't required to."""
        return [v for v in self.vars.values() if v.default is not None]


# ---------------------------------------------------------------------------
# Regex patterns (Node / Vite / Python textual idioms)
# ---------------------------------------------------------------------------

# `process.env.FOO` or `process.env["FOO"]` or `process.env['FOO']`
_RE_NODE = re.compile(
    r"process\.env\."  # bare attribute access
    r"([A-Z_][A-Z0-9_]*)\b"
    r"|process\.env\[\s*['\"]"  # bracket access
    r"([A-Z_][A-Z0-9_]*)"
    r"['\"]\s*\]"
)

# `import.meta.env.FOO` / `import.meta.env["FOO"]` — Vite + most modern
# bundlers. Note: Vite requires the `VITE_` prefix for client-side vars,
# but we accept all names and let downstream filter.
_RE_VITE = re.compile(
    r"import\.meta\.env\."
    r"([A-Z_][A-Z0-9_]*)\b"
    r"|import\.meta\.env\[\s*['\"]"
    r"([A-Z_][A-Z0-9_]*)"
    r"['\"]\s*\]"
)

# `os.getenv("X")` / `os.getenv("X", default)` / `os.environ["X"]` /
# `os.environ.get("X")` — handled by the Python AST walker for accuracy,
# but the regex versions catch f-strings and other places AST misses.
_RE_PY_GETENV = re.compile(
    r"os\.getenv\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]"
)
_RE_PY_ENVIRON = re.compile(
    r"os\.environ\[\s*['\"]([A-Z_][A-Z0-9_]*)['\"]\s*\]"
    r"|os\.environ\.get\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]"
)


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

# Token sets for name-based type inference. Order matters: URL is checked
# BEFORE secret so MY_API_URL classifies as url (a connection target) not
# secret (a credential). Int is checked before bool so PORT_ENABLED falls
# into the more useful bucket. "API" alone is too vague to mean secret;
# we require it to be paired with KEY/TOKEN/SECRET (which are themselves
# in the list, so MY_API_KEY still classifies as secret via KEY).
_URL_TOKENS = ("URL", "URI", "ENDPOINT", "HOST")
_INT_TOKENS = ("PORT", "TIMEOUT", "LIMIT", "COUNT", "MAX", "MIN", "INTERVAL", "RETRIES")
_BOOL_TOKENS = ("ENABLE", "ENABLED", "DEBUG", "VERBOSE", "DRY_RUN", "DISABLE", "DISABLED")
_EMAIL_TOKENS = ("EMAIL", "FROM_ADDR", "SMTP_USER")
_SECRET_TOKENS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS")


def _name_parts(name: str) -> Set[str]:
    """Split FOO_BAR_BAZ into {"FOO", "BAR", "BAZ"} for word-boundary matching."""
    return {p for p in name.upper().split("_") if p}


def _infer_type(name: str) -> str:
    """Guess a type from the variable name. Conservative — defaults to string.

    Order: url > int > bool > email > secret > string. URL wins over
    secret so DATABASE_URL / API_ENDPOINT classify as connection targets,
    not credentials. The secret check uses word-boundary matching (the
    name must contain a `_KEY` / `_TOKEN` / `_SECRET` / `_PASSWORD` part)
    so things like `KEYBOARD_LAYOUT` don't get masked.
    """
    parts = _name_parts(name)
    if parts & set(_URL_TOKENS):
        return "url"
    if parts & set(_INT_TOKENS):
        return "int"
    if parts & set(_BOOL_TOKENS):
        return "bool"
    if parts & set(_EMAIL_TOKENS):
        return "email"
    if parts & set(_SECRET_TOKENS):
        return "secret"
    return "string"


def _is_secret(name: str) -> bool:
    """A var is a secret iff one of its underscore-separated parts is a
    credential token AND it isn't more naturally a URL/int/bool."""
    inferred = _infer_type(name)
    return inferred == "secret"


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

_JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_PY_EXTS = {".py"}
_SKIP_DIRS = {
    "node_modules", "dist", "build", ".next", ".cache", ".git",
    "__pycache__", ".venv", "venv", "env", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "coverage", ".coverage",
}


def _iter_source_files(root: Path) -> List[Path]:
    """Walk root, yield JS/TS/Python files, skip vendored + cache dirs."""
    out: List[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in _JS_EXTS or path.suffix.lower() in _PY_EXTS:
            out.append(path)
    return out


# ---------------------------------------------------------------------------
# Per-idiom extraction
# ---------------------------------------------------------------------------

def _scan_js_text(text: str) -> List[tuple[str, str]]:
    """Return [(var_name, idiom), ...] for one JS/TS file."""
    found: List[tuple[str, str]] = []
    for match in _RE_NODE.finditer(text):
        name = match.group(1) or match.group(2)
        if name:
            found.append((name, "node"))
    for match in _RE_VITE.finditer(text):
        name = match.group(1) or match.group(2)
        if name:
            found.append((name, "vite"))
    return found


def _scan_py_text(text: str, path: Path) -> List[tuple[str, str, Optional[str]]]:
    """Return [(var_name, idiom, default), ...] for one Python file.

    Uses ast walking for getenv/environ so we can pull the default arg
    accurately; falls back to regex for stuff the AST can't see (e.g.
    f-strings, dynamic lookups). Pydantic Settings classes are detected
    via ast.ClassDef inspection.
    """
    found: List[tuple[str, str, Optional[str]]] = []

    # Try AST first — gives us defaults for free.
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            # os.getenv("X") / os.getenv("X", default)
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                    and func.attr == "getenv"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    name = node.args[0].value
                    default = None
                    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                        default = _stringify_constant(node.args[1].value)
                    found.append((name, "python_getenv", default))
                    continue
                # os.environ.get("X", default)
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "os"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    name = node.args[0].value
                    default = None
                    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                        default = _stringify_constant(node.args[1].value)
                    found.append((name, "python_environ", default))
                    continue

            # os.environ["X"]
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "environ"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "os"
            ):
                key = _const_str(node.slice)
                if key:
                    found.append((key, "python_environ", None))

            # Pydantic Settings: class Foo(BaseSettings): FIELD: type = default
            if isinstance(node, ast.ClassDef):
                base_names = {_attr_name(b) for b in node.bases}
                if "BaseSettings" in base_names:
                    for stmt in node.body:
                        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                            name = stmt.target.id
                            # Pydantic field names are usually lowercase; the
                            # env var is the uppercase form.
                            env_name = name.upper()
                            default = None
                            if stmt.value is not None and isinstance(stmt.value, ast.Constant):
                                default = _stringify_constant(stmt.value.value)
                            found.append((env_name, "pydantic", default))

    # Regex sweep for things AST missed (e.g. inside f-strings, eval'd code).
    seen = {n for n, _, _ in found}
    for match in _RE_PY_GETENV.finditer(text):
        name = match.group(1)
        if name not in seen:
            found.append((name, "python_getenv", None))
            seen.add(name)
    for match in _RE_PY_ENVIRON.finditer(text):
        name = match.group(1) or match.group(2)
        if name and name not in seen:
            found.append((name, "python_environ", None))
            seen.add(name)

    return found


def _const_str(node: ast.AST) -> Optional[str]:
    """Pull a string literal out of an ast Subscript slice (handles 3.8+ shape)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Index):  # py<3.9 shape, harmless guard
        return _const_str(node.value)  # type: ignore[attr-defined]
    return None


def _attr_name(node: ast.AST) -> str:
    """Render an ast.Attribute/Name as a dotted string for base-class checks."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _stringify_constant(value: object) -> str:
    """Render an AST constant back to text suitable for .env.example."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scan(root: Path) -> ScanResult:
    """Walk ``root`` and return every env-var reference we can find.

    Safe to call on a directory that doesn't exist (returns empty result).
    Safe to call on a directory with binary / non-UTF-8 files (skipped).
    """
    result = ScanResult()
    if not root.is_dir():
        return result

    files = _iter_source_files(root)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            result.skipped_files += 1
            continue
        result.scanned_files += 1
        rel = str(path.relative_to(root))

        if path.suffix.lower() in _JS_EXTS:
            for name, idiom in _scan_js_text(text):
                _record(result, name, idiom, default=None, used_in=rel)
        elif path.suffix.lower() in _PY_EXTS:
            for name, idiom, default in _scan_py_text(text, path):
                _record(result, name, idiom, default=default, used_in=rel)

    return result


def _record(result: ScanResult, name: str, idiom: str, *, default: Optional[str], used_in: str) -> None:
    """Aggregate one detection into the result, dedup by name."""
    ref = result.vars.get(name)
    if ref is None:
        ref = EnvVarRef(
            name=name,
            type_hint=_infer_type(name),
            is_secret=_is_secret(name),
            idiom=idiom,
        )
        result.vars[name] = ref
    if used_in not in ref.used_in:
        ref.used_in.append(used_in)
    # Keep the first non-empty default we see — later sites that omit a
    # default don't erase an earlier one.
    if ref.default is None and default is not None and default != "":
        ref.default = default
