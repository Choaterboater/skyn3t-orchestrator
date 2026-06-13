"""In-process MCP-compatible tool implementations.

Exposes the same tool surface as the standard ``mcp-filesystem`` and ``mcp-git``
servers but runs locally. The LLM gets a tool schema description and a way to
invoke each function. Later this can be swapped for stdio-MCP servers without
agent-code changes.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("skyn3t.adapters.mcp_tools")

REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_READ_BYTES = 200_000


def _safe_path(p: str) -> Path:
    """Resolve path inside REPO_ROOT, refuse traversal."""
    if Path(p).is_absolute():
        raise ValueError(f"absolute paths not allowed: {p}")
    target = (REPO_ROOT / p).resolve()
    try:
        target.relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes repo: {p}") from exc
    return target


# Tool 1: read_file ────────────────────────────────────────────────
def read_file(path: str) -> Dict[str, Any]:
    p = _safe_path(path)
    if not p.exists():
        return {"ok": False, "error": "not found"}
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "error": "binary file"}
    if len(text) > MAX_READ_BYTES:
        text = text[:MAX_READ_BYTES] + "\n…[truncated]…"
    return {"ok": True, "path": path, "content": text, "lines": text.count("\n") + 1}


# Tool 2: write_file (full replace) ────────────────────────────────
def write_file(path: str, content: str) -> Dict[str, Any]:
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}


# Tool 3: apply_replacement (find/replace, exact match required) ───
def apply_replacement(path: str, find: str, replace: str, count: int = 1) -> Dict[str, Any]:
    """Replace exactly ``count`` occurrences of ``find`` with ``replace``.
    If count=1 and find appears more or fewer times, fail safely.
    """
    p = _safe_path(path)
    if not p.exists():
        return {"ok": False, "error": "not found"}
    text = p.read_text(encoding="utf-8")
    occurrences = text.count(find)
    if count > 0 and occurrences != count:
        return {
            "ok": False,
            "error": f"found {occurrences} occurrences, expected {count}",
            "occurrences": occurrences,
        }
    new_text = text.replace(find, replace) if count <= 0 else text.replace(find, replace, count)
    if new_text == text:
        return {"ok": False, "error": "no change"}
    p.write_text(new_text, encoding="utf-8")
    return {"ok": True, "path": path, "replaced": occurrences if count <= 0 else count}


# Tool 4: list_dir ─────────────────────────────────────────────────
def list_dir(path: str = ".") -> Dict[str, Any]:
    p = _safe_path(path)
    if not p.is_dir():
        return {"ok": False, "error": "not a dir"}
    entries = sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir() if not e.name.startswith("."))
    return {"ok": True, "path": path, "entries": entries[:200]}


# Tool 5: grep (ripgrep-style content search) ──────────────────────
def grep(pattern: str, path: str = ".", max_results: int = 50) -> Dict[str, Any]:
    p = _safe_path(path)
    try:
        proc = subprocess.run(
            [
                "grep",
                "-rn",
                "--include=*.py",
                "--include=*.html",
                "--include=*.md",
                "--include=*.json",
                "--include=*.yaml",
                pattern,
                str(p),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        lines = proc.stdout.splitlines()[:max_results]
        return {"ok": True, "pattern": pattern, "matches": lines, "count": len(lines)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Tool 6: git_branch ───────────────────────────────────────────────
def git_branch(name: str) -> Dict[str, Any]:
    proc = subprocess.run(
        ["git", "checkout", "-b", name],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=15,
    )
    return {"ok": proc.returncode == 0, "stderr": proc.stderr.strip()[:300] or None}


# Tool 7: git_commit ───────────────────────────────────────────────
def git_commit(message: str) -> Dict[str, Any]:
    add = subprocess.run(
        ["git", "add", "-A"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=15,
    )
    if add.returncode != 0:
        return {"ok": False, "error": f"add: {add.stderr.strip()[:200]}"}
    commit = subprocess.run(
        ["git", "-c", "user.name=skyn3t", "-c", "user.email=skyn3t@local", "commit", "-m", message],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=15,
    )
    if commit.returncode != 0:
        return {"ok": False, "error": commit.stderr.strip()[:300] or "no changes to commit"}
    sha_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=10,
    )
    return {"ok": True, "sha": sha_proc.stdout.strip()[:7], "message": message[:80]}


# Tool 8: git_checkout (return to a branch) ────────────────────────
def git_checkout(name: str) -> Dict[str, Any]:
    proc = subprocess.run(
        ["git", "checkout", name],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=15,
    )
    return {"ok": proc.returncode == 0, "stderr": proc.stderr.strip()[:300] or None}


# Tool 9: pytest (run the test suite) ──────────────────────────────
def run_pytest(timeout: int = 180) -> Dict[str, Any]:
    proc = subprocess.run(
        ["python3", "-m", "pytest", "tests/", "-q", "--tb=line"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=timeout,
    )
    out = (proc.stdout + "\n" + proc.stderr)[-1500:]
    return {"ok": proc.returncode == 0, "rc": proc.returncode, "output": out}


# Schema descriptions the LLM gets ─────────────────────────────────
TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {"name": "read_file", "description": "Read a text file from the repo (max 200KB).",
     "args": ["path: str"]},
    {"name": "write_file", "description": "Replace a file's full contents.",
     "args": ["path: str", "content: str"]},
    {"name": "apply_replacement",
     "description": "Replace exactly N occurrences of `find` with `replace` in a file. "
                    "Fails if the count doesn't match — safer than diffs. count=0 = replace all.",
     "args": ["path: str", "find: str", "replace: str", "count: int (default 1)"]},
    {"name": "list_dir", "description": "List directory entries (capped at 200).",
     "args": ["path: str (default .)"]},
    {"name": "grep", "description": "Search file contents with grep -rn.",
     "args": ["pattern: str", "path: str (default .)", "max_results: int (default 50)"]},
    {"name": "git_branch", "description": "Create + checkout a new branch.",
     "args": ["name: str"]},
    {"name": "git_commit", "description": "Stage all and commit with message. Returns sha.",
     "args": ["message: str"]},
    {"name": "git_checkout", "description": "Checkout an existing branch.",
     "args": ["name: str"]},
    {"name": "run_pytest", "description": "Run the project's test suite, return pass/fail.",
     "args": []},
]

TOOLS = {
    "read_file": read_file,
    "write_file": write_file,
    "apply_replacement": apply_replacement,
    "list_dir": list_dir,
    "grep": grep,
    "git_branch": git_branch,
    "git_commit": git_commit,
    "git_checkout": git_checkout,
    "run_pytest": run_pytest,
}


def tool_manifest() -> str:
    """Render the tool manifest as a markdown block the LLM can read."""
    lines = ["# Available tools (MCP-style)", ""]
    for s in TOOL_SCHEMAS:
        lines.append(f"## {s['name']}")
        lines.append(s["description"])
        if s["args"]:
            lines.append("Args: " + ", ".join(s["args"]))
        lines.append("")
    lines.append("# Calling convention")
    lines.append("Reply with a fenced ```tool block per call:")
    lines.append("```tool")
    lines.append('{"name": "apply_replacement", "args": {"path": "skyn3t/web/app.py", "find": "...", "replace": "..."}}')
    lines.append("```")
    lines.append("Multiple tool calls allowed. After your tool calls, reply DONE on its own line.")
    return "\n".join(lines)
