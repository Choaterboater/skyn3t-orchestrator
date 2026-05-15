"""Targeted fix engine — maps build/consistency errors to per-file regeneration.

When the verifier or consistency engine finds an error, the old behavior was to
send the ENTIRE scaffold + build log to the LLM and get back a full rewrite of
multiple files. That's wasteful and often introduces new bugs in untouched files.

This module provides `apply_targeted_fix()` which:
1. Reads the error and maps it to the specific file(s) responsible
2. For each affected file, constructs a focused prompt with:
   - The error message
   - The current (broken) file content
   - The brief context
3. Calls the LLM to regenerate ONLY that file
4. If an import target is missing, generates a placeholder stub

This is cheaper (~1/10th the tokens) and safer (untouched files stay intact).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List

logger = logging.getLogger("skyn3t.agents.targeted_fix")


@dataclass
class FileIssue:
    path: str
    error_message: str
    suggested_action: str  # "regenerate" | "patch" | "create_placeholder"


@dataclass
class FixResult:
    ok: bool
    files_changed: List[str]
    files_created: List[str]
    errors: List[str]


def _parse_build_errors(stderr: str, stdout: str) -> List[FileIssue]:
    """Extract (file, line, message) tuples from Vite / webpack / tsc output.

    Recognizes common error patterns:
      - Vite:  "src/App.jsx:12:3: ERROR: Unexpected token"
      - tsc:   "src/App.tsx(12,3): error TS1005: '}' expected."
      - ESLint: "/path/to/file.js: line 12, col 3, Error - ..."
      - node:  "SyntaxError: /path/file.js: Unexpected token (12:3)"
    """
    import re

    text = f"{stderr}\n{stdout}"
    issues: List[FileIssue] = []

    # Pattern 1: Vite / esbuild / rollup
    # file:line:col: ERROR: message
    for m in re.finditer(
        r"^\s*(?:\x1b\[\d+m)?([\w./-]+\.(?:js|jsx|ts|tsx|mjs|cjs)):(\d+):(\d+):\s*error:\s*(.+)$",
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        issues.append(
            FileIssue(
                path=m.group(1),
                error_message=f"Line {m.group(2)}, col {m.group(3)}: {m.group(4).strip()}",
                suggested_action="regenerate",
            )
        )

    # Pattern 2: TypeScript / tsc
    # file(line,col): error TSxxxx: message
    for m in re.finditer(
        r"^\s*(?:\x1b\[\d+m)?([\w./-]+\.(?:ts|tsx))\((\d+),(\d+)\):\s*error\s+\w+:\s*(.+)$",
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        issues.append(
            FileIssue(
                path=m.group(1),
                error_message=f"Line {m.group(2)}, col {m.group(3)}: {m.group(4).strip()}",
                suggested_action="regenerate",
            )
        )

    # Pattern 3: node SyntaxError
    # SyntaxError: /path/file.js: Unexpected token (12:3)
    for m in re.finditer(
        r"SyntaxError:\s+([\w./-]+\.(?:js|jsx|ts|tsx|mjs|cjs)):\s*(.+?)\s*\((\d+):(\d+)\)",
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        issues.append(
            FileIssue(
                path=m.group(1),
                error_message=f"Line {m.group(3)}, col {m.group(4)}: {m.group(2).strip()}",
                suggested_action="regenerate",
            )
        )

    # Pattern 4: Cannot find module 'X'
    # This maps to a missing dependency OR a missing local file.
    for m in re.finditer(
        r"Cannot\s+find\s+module\s+['\"]([^'\"]+)['\"]",
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        mod = m.group(1)
        if mod.startswith("."):
            # Missing local file — create placeholder
            issues.append(
                FileIssue(
                    path=mod,
                    error_message=f"Missing module: {mod}",
                    suggested_action="create_placeholder",
                )
            )
        else:
            # Missing npm package — regenerate package.json
            issues.append(
                FileIssue(
                    path="package.json",
                    error_message=f"Missing dependency: {mod}",
                    suggested_action="regenerate",
                )
            )

    # Pattern 5: Rollup / Vite "X is not exported by Y"
    # file(line:col): "X" is not exported by "file"
    for m in re.finditer(
        r'^\s*(?:\x1b\[\d+m)?[\w./-]+\s*\((\d+):(\d+)\):\s*"[^"]+"\s+is\s+not\s+exported\s+by\s+"([^"]+)"',
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        issues.append(
            FileIssue(
                path=m.group(3),
                error_message=f"Missing export (line {m.group(1)}, col {m.group(2)})",
                suggested_action="regenerate",
            )
        )

    # Pattern 6: Rollup / Vite "X" is not exported by "Y" (no line prefix)
    # e.g. [commonjs--resolver] "default" is not exported by "src/hooks/useConfig.js"
    for m in re.finditer(
        r'"([^"]+)"\s+is\s+not\s+exported\s+by\s+"([^"]+)"',
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        # Skip if this was already captured by Pattern 5
        path = m.group(2)
        if any(i.path == path and "Missing export" in i.error_message for i in issues):
            continue
        issues.append(
            FileIssue(
                path=path,
                error_message=f"Missing export: {m.group(1)}",
                suggested_action="regenerate",
            )
        )

    # Deduplicate by path, keeping the first error message.
    seen: set = set()
    deduped: List[FileIssue] = []
    for i in issues:
        key = (i.path, i.suggested_action)
        if key not in seen:
            seen.add(key)
            deduped.append(i)
    return deduped


def _strip_preamble(content: str, rel_path: str) -> str:
    """Remove markdown fences and LLM preamble from regenerated file content.

    Models often prefix file content with chain-of-thought like
    "Here's the fixed file:" or wrap it in ``` fences. This strips
    both, then finds the first line that looks like actual code.
    """
    content = content.strip()
    # Strip markdown fences
    if content.startswith("```"):
        content = "\n".join(content.splitlines()[1:])
    if content.endswith("```"):
        content = "\n".join(content.splitlines()[:-1])
    content = content.strip()

    ext = Path(rel_path).suffix.lower()
    lines = content.splitlines()

    # File-type aware start-of-content detection
    if ext == ".json":
        # JSON starts with { or [
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and (stripped.startswith("{") or stripped.startswith("[")):
                return "\n".join(lines[i:])
    elif ext in (".html", ".htm"):
        # HTML starts with <
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and stripped.startswith("<"):
                return "\n".join(lines[i:])
    elif ext in (".css", ".scss"):
        # CSS starts with /*, @, or a selector/rule
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and (stripped.startswith("/*") or stripped.startswith("@") or stripped[0].isalnum()):
                return "\n".join(lines[i:])
    elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        # JS/TS starts with import, export, const, let, var, function, class,
        # comment, or use strict
        js_starts = ("import ", "export ", "const ", "let ", "var ", "function ", "class ", "//", "/*", "use strict", '"use strict"', "'use strict'")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and any(stripped.startswith(s) for s in js_starts):
                return "\n".join(lines[i:])
    else:
        # Generic: skip blank lines and obvious preamble markers
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not stripped.lower().startswith(("here", "okay", "sure", "below", "the ", "this ", "i've ", "let me")):
                return "\n".join(lines[i:])

    # Fallback: if no start marker found, return as-is (might still be valid)
    return content


def _validate_syntax(content: str, ext: str, file_path: str) -> str:
    """Validate syntax of generated code. Returns error message if invalid, empty string if valid."""
    if not content or not content.strip():
        return "Empty content"

    # TypeScript/JavaScript validation
    if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        # Check for common incomplete patterns
        if content.strip().endswith("interface ") or content.strip().endswith("interface{"):
            return "Incomplete interface declaration"
        if content.strip().endswith("class ") or content.strip().endswith("class{"):
            return "Incomplete class declaration"
        if content.strip().endswith("function ") or content.strip().endswith("function("):
            return "Incomplete function declaration"
        if content.strip().endswith("export "):
            return "Incomplete export statement"
        # Check for unmatched braces
        open_braces = content.count("{")
        close_braces = content.count("}")
        if open_braces != close_braces:
            return f"Unmatched braces: {open_braces} {{ vs {close_braces} }}"
        # Check for unmatched parens
        open_parens = content.count("(")
        close_parens = content.count(")")
        if open_parens != close_parens:
            return f"Unmatched parentheses: {open_parens} ( vs {close_parens} )"
        # Check for unmatched brackets
        open_brackets = content.count("[")
        close_brackets = content.count("]")
        if open_brackets != close_brackets:
            return f"Unmatched brackets: {open_brackets} [ vs {close_brackets} ]"

    # Python validation
    if ext in (".py",):
        lines = content.split("\n")
        # Check for incomplete class/def/if/for/while
        for line in lines:
            stripped = line.rstrip()
            if stripped.endswith(":") and not any(c in stripped for c in "()[]{}"):
                # Could be incomplete
                if any(kw in stripped for kw in ["class ", "def ", "if ", "for ", "while "]):
                    # This is ok — line ending with : is expected
                    pass

    # JSON validation
    if ext == ".json":
        try:
            import json
            json.loads(content)
        except Exception as e:
            return f"Invalid JSON: {str(e)}"

    return ""  # Valid


def _placeholder_for(rel_path: str) -> str:
    """Return a syntactically valid stub for a missing file."""
    ext = Path(rel_path).suffix.lower()
    if ext in (".jsx", ".tsx"):
        return (
            "// Auto-generated placeholder — replace with real implementation.\n"
            "export default function Placeholder() {\n"
            "  return <div>Placeholder</div>;\n"
            "}\n"
        )
    if ext in (".js", ".ts", ".mjs", ".cjs"):
        # Use hook pattern for typical hook imports (useX.ts/useX.js)
        if rel_path.lower().startswith("use") or "/use" in rel_path.lower():
            return (
                "// Auto-generated placeholder hook — replace with real implementation.\n"
                "export default function usePlaceholder() {\n"
                "  return {};\n"
                "}\n"
            )
        # Use router pattern for typical router imports (router.ts, routes.js, etc.)
        if "router" in rel_path.lower() or "route" in rel_path.lower():
            return (
                "// Auto-generated placeholder router — replace with real implementation.\n"
                "import { Router } from 'express';\n"
                "const router = Router();\n"
                "export default router;\n"
            )
        # Use query function for typical query imports (queries.ts, api.ts, etc.)
        if "quer" in rel_path.lower() or "api" in rel_path.lower():
            return (
                "// Auto-generated placeholder — replace with real implementation.\n"
                "export async function query(params) {\n"
                "  return {};\n"
                "}\n"
                "export default query;\n"
            )
        return (
            "// Auto-generated placeholder — replace with real implementation.\n"
            "export default function placeholder() {\n"
            "  return {};\n"
            "}\n"
        )
    if ext == ".json":
        return "{}\n"
    if ext in (".css", ".scss"):
        return "/* Auto-generated placeholder */\n"
    return "// Auto-generated placeholder\n"


async def apply_targeted_fix(
    scaffold_dir: Path,
    issues: List[FileIssue],
    *,
    llm_client=None,
    brief: str = "",
    stack: str = "",
) -> FixResult:
    """Apply targeted fixes for a list of FileIssues.

    Args:
        scaffold_dir: Root of the scaffold directory.
        issues: List of issues from consistency engine or build verifier.
        llm_client: An LLMClient instance for regeneration. If None,
            only placeholder creation and simple patches are attempted.
        brief: The original project brief for context.
        stack: The detected stack key (react_vite, next, etc.).

    Returns:
        FixResult with changed/created file lists.
    """
    changed: List[str] = []
    created: List[str] = []
    errors: List[str] = []

    # Resolve once so containment checks below have a stable parent.
    scaffold_root = scaffold_dir.resolve()

    for issue in issues:
        # Defense in depth — the LLM (and upstream regex-based extractors)
        # can return ``../../../repo/app/index.html`` or absolute paths.
        # ``Path('/x') / '/y'`` discards ``/x`` and writes to ``/y``,
        # which has been leaking generated files into the SkyN3t repo
        # root. Reject anything that escapes ``scaffold_dir``.
        raw_path = (issue.path or "").strip()
        if not raw_path or raw_path.startswith("/") or ".." in raw_path.split("/"):
            errors.append(f"Refusing path that escapes scaffold: {raw_path!r}")
            continue
        target_path = (scaffold_dir / raw_path).resolve()
        try:
            target_path.relative_to(scaffold_root)
        except ValueError:
            errors.append(f"Refusing path outside scaffold: {raw_path!r}")
            continue

        if issue.suggested_action == "create_placeholder":
            if not target_path.exists():
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(_placeholder_for(issue.path), encoding="utf-8")
                created.append(issue.path)
                logger.info("Created placeholder: %s", issue.path)
            continue

        if issue.suggested_action == "regenerate":
            if not target_path.exists():
                # Create placeholder instead of failing
                logger.info("Missing file %s, creating placeholder", issue.path)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(_placeholder_for(issue.path), encoding="utf-8")
                created.append(issue.path)
                continue

            if llm_client is None:
                errors.append(f"No LLM client available to regenerate: {issue.path}")
                continue

            old_content = target_path.read_text(encoding="utf-8")
            prompt = (
                f"Fix the following error in this {stack or 'project'} file:\n\n"
                f"ERROR: {issue.error_message}\n\n"
                f"CURRENT FILE ({issue.path}):\n"
                f"```\n{old_content}\n```\n\n"
                f"Rewrite the ENTIRE file so the error is fixed. "
                f"Do not change the file's overall purpose or exports. "
                f"Only fix the specific error. "
                f"Return ONLY the fixed file content, no markdown fences, no explanations."
            )
            try:
                new_content = await llm_client.complete(prompt, max_tokens=4000, temperature=0.2)
            except Exception as exc:
                errors.append(f"LLM call failed for {issue.path}: {exc}")
                continue

            # Strip fences and preamble text the model may have added
            new_content = _strip_preamble(new_content, issue.path)

            if not new_content or not new_content.strip():
                errors.append(f"LLM returned empty content for {issue.path}")
                continue

            # Validate syntax before writing
            ext = Path(issue.path).suffix.lower()
            validation_error = _validate_syntax(new_content, ext, issue.path)
            if validation_error:
                logger.warning(
                    "LLM generated invalid syntax for %s: %s. Using placeholder instead.",
                    issue.path,
                    validation_error,
                )
                # Fall back to placeholder instead of writing invalid code
                target_path.write_text(_placeholder_for(issue.path), encoding="utf-8")
                created.append(issue.path)
                continue

            target_path.write_text(new_content + "\n", encoding="utf-8")
            changed.append(issue.path)
            logger.info("Regenerated: %s", issue.path)
            continue

        # Unknown action
        errors.append(f"Unknown fix action '{issue.suggested_action}' for {issue.path}")

    return FixResult(
        ok=len(errors) == 0 and (len(changed) > 0 or len(created) > 0),
        files_changed=changed,
        files_created=created,
        errors=errors,
    )
