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

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

logger = logging.getLogger("skyn3t.agents.targeted_fix")

# Per-file regen timeout. Used to be a flat 90s, which fired
# consistently on the largest scaffold files (App.jsx, styles.css,
# config-store.js) — the targeted-fix loop would then "preserve
# existing file instead" and the same 3 issues would resurface in the
# next critique round (canary-19/20 pattern). We now scale the budget
# with the existing file's size, since regen time grows roughly linearly
# with output length on streaming CLI backends. The shared LLM client
# already enforces its own streaming-idle + hard-cap window, so a
# generous outer ceiling here just lets the inner one do its job.
_REGENERATE_TIMEOUT_BASE_SECONDS = 90.0
_REGENERATE_TIMEOUT_PER_KB_SECONDS = 12.0
_REGENERATE_TIMEOUT_MAX_SECONDS = 300.0


def _regenerate_timeout_for(existing_content: str) -> float:
    """Per-file regen budget, scaled by current file size."""
    size_kb = max(0, len(existing_content)) / 1024.0
    budget = _REGENERATE_TIMEOUT_BASE_SECONDS + size_kb * _REGENERATE_TIMEOUT_PER_KB_SECONDS
    return min(_REGENERATE_TIMEOUT_MAX_SECONDS, budget)


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


def _preserve_existing_on_regenerate_failure(issue: FileIssue, reason: str) -> None:
    logger.warning(
        "Targeted fix could not safely rewrite %s (%s). Preserving existing file.",
        issue.path,
        reason,
    )


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

    # Pattern 7: Node ESM runtime "does not provide an export named"
    export_match = re.search(
        r"does not provide an export named ['\"]([^'\"]+)['\"]",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if export_match is not None:
        lines = text.splitlines()
        importer_path = None
        import_spec = None
        for idx, line in enumerate(lines):
            path_match = re.search(
                r"^(?:file://)?(.+\.(?:js|jsx|ts|tsx|mjs|cjs)):\d+",
                line.strip(),
            )
            if path_match is None:
                continue
            importer_path = Path(path_match.group(1))
            if idx + 1 < len(lines):
                import_match = re.search(r"""from\s+['"]([^'"]+)['"]""", lines[idx + 1])
                if import_match is not None:
                    import_spec = import_match.group(1)
            break

        target_path = None
        if importer_path is not None and import_spec and import_spec.startswith("."):
            resolved = (importer_path.resolve().parent / import_spec).resolve()
            resolved_posix = resolved.as_posix()
            marker = "/scaffold/"
            if marker in resolved_posix:
                target_path = resolved_posix.split(marker, 1)[1]
            else:
                target_path = resolved.name
        elif import_spec:
            target_path = import_spec

        if target_path:
            issues.append(
                FileIssue(
                    path=target_path,
                    error_message=f"Missing export: {export_match.group(1)}",
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


# Lines that are *never* real file content — CLI tool-call telemetry
# emitted by copilot/claude/kimi backends despite the prompt forbidding it.
# Kept compatible with code_agent._CLI_TRACE_PATTERNS so the two
# sanitizers can't diverge silently. See also styles.css canary-19
# incident where prose like "I'm checking the surrounding components..."
# leaked into a CSS file because the old CSS branch only required
# alnum-leading lines.
_CLI_TRACE_LINE_RE = re.compile(
    r"^\s*(?:"
    r"[●✗✓└│]"                                # tree bullets / status markers
    r"|(?:Read|Search|Write|Edit|List|Web\s+Search|Locate)\s"  # tool names
    r"|(?:I['’]m|I['’]ll|I\s+will|I['’]ve|I\s+have|I\s+can|"
    r"Let\s+me|Here(?:['’]s|\s+is)|Sure|Okay|Understood|"
    r"The\s+(?:existing|workspace|file)|This\s+(?:file|is|looks?)|"
    r"That\s+(?:looks?|is)|It\s+(?:already|is|looks?)|"
    r"No\s+(?:changes|matches?)|Looks?\s+good|Below|"
    r"Path\s+does\s+not\s+exist|Permission\s+denied|"
    r"No\s+matches\s+found|The\s+workspace\s+looks)\b"
    r")",
    re.IGNORECASE,
)


def _is_cli_trace_line(line: str) -> bool:
    """True for CLI narration / tool-call telemetry lines."""
    return bool(_CLI_TRACE_LINE_RE.match(line))


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

    def _scan(start_test) -> str:
        """Walk lines, skipping CLI trace + blank lines, until start_test matches."""
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if _is_cli_trace_line(stripped):
                continue
            if start_test(stripped):
                return "\n".join(lines[i:])
        # No clear start marker — return original content untouched.
        return content

    # File-type aware start-of-content detection
    if ext == ".json":
        return _scan(lambda s: s.startswith(("{", "[")))
    if ext in (".html", ".htm"):
        return _scan(lambda s: s.startswith("<"))
    if ext in (".css", ".scss", ".sass", ".less"):
        # Real CSS lines begin with /*, @, :root, a selector char, or a
        # bare identifier followed by ,/{/:. Plain prose (even alnum)
        # must NOT pass here — canary-19 styles.css was wrecked because
        # the old rule accepted "I'm checking ..." as a CSS selector.
        css_start_re = re.compile(
            r"^(?:/\*|@[a-zA-Z]|:root\b|[.#*&]|"
            r"[a-zA-Z][\w-]*\s*(?:[,{]|::?[\w-]+|\s+\{))"
        )
        return _scan(lambda s: bool(css_start_re.match(s)))
    if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        js_starts = (
            "import ", "import{", "import(", "export ",
            "const ", "let ", "var ", "function ", "function(",
            "class ", "async ", "//", "/*", "#!",
            "use strict", '"use strict"', "'use strict'",
            "module.exports", "require(",
        )
        return _scan(lambda s: any(s.startswith(p) for p in js_starts))
    if ext == ".py":
        py_starts_re = re.compile(
            r"^(?:import\s|from\s|def\s|async\s+def\s|class\s|@\w|#!|#\s|\"\"\"|''')"
        )
        return _scan(lambda s: bool(py_starts_re.match(s)))
    if ext in (".yml", ".yaml"):
        yaml_start_re = re.compile(r"^(?:---|[a-zA-Z_][\w\-]*\s*:|#\s|-\s)")
        return _scan(lambda s: bool(yaml_start_re.match(s)))
    if ext in (".env",) or Path(rel_path).name.startswith(".env"):
        env_start_re = re.compile(r"^(?:#\s|[A-Z][A-Z0-9_]*=)")
        return _scan(lambda s: bool(env_start_re.match(s)))
    if ext in (".md", ".markdown"):
        # Real markdown starts with a heading, frontmatter, list, blockquote,
        # fenced code block, or HTML tag. Plain prose-with-capital is NOT
        # enough — canary-113's server/README.md shipped LLM tool-call
        # narration ("I'm checking the project structure...") because the
        # old regex accepted the leading capital.
        md_start_re = re.compile(r"^(?:#{1,6}\s|---\s*$|\*\s|-\s|\d+\.\s|>\s|```|<!?[a-zA-Z])")
        return _scan(lambda s: bool(md_start_re.match(s)))

    # Generic: skip blank lines, CLI trace lines, and the most common
    # narration lead-ins. Anything else is treated as content.
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if _is_cli_trace_line(stripped):
            continue
        lower = stripped.lower()
        if lower.startswith(
            ("here", "okay", "sure", "below", "the ", "this ", "i've ",
             "i'm ", "i ", "let me", "understood")
        ):
            continue
        return "\n".join(lines[i:])
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
            # Refuse extension-less paths: writing `ActivityFeed` (no .jsx)
            # ships a stub alongside the real `ActivityFeed.jsx` and
            # confuses both Vite and the reviewer. canary-121 shipped two
            # such ghost files which the reviewer LLM penalized as
            # "TODO/placeholder leak."  Infer .jsx for components/* paths,
            # .js otherwise.
            inferred_path = issue.path
            if not Path(issue.path).suffix:
                lower = issue.path.lower()
                if "/components/" in lower.replace("\\", "/") or "/pages/" in lower:
                    inferred_path = issue.path + ".jsx"
                else:
                    inferred_path = issue.path + ".js"
                target_path = (scaffold_dir / inferred_path).resolve()
                try:
                    target_path.relative_to(scaffold_root)
                except ValueError:
                    errors.append(f"Refusing path outside scaffold: {inferred_path!r}")
                    continue
            if not target_path.exists():
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(_placeholder_for(inferred_path), encoding="utf-8")
                created.append(inferred_path)
                logger.info("Created placeholder: %s", inferred_path)
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
                f"Output the COMPLETE file; never truncate or stop mid-function. "
                f"Do not add TODO, FIXME, placeholder, or 'not implemented' markers. "
                f"Do not remove or rename existing exports. "
                f"The result must be build-valid for this file type. "
                f"Only fix the specific error. "
                f"Return ONLY the fixed file content, no markdown fences, no explanations."
            )
            regen_timeout = _regenerate_timeout_for(old_content)
            try:
                new_content = await asyncio.wait_for(
                    llm_client.complete(prompt, max_tokens=4000, temperature=0.2),
                    timeout=regen_timeout,
                )
            except asyncio.TimeoutError:
                _preserve_existing_on_regenerate_failure(
                    issue,
                    f"timeout after {regen_timeout:.0f}s",
                )
                errors.append(
                    f"Timed out regenerating {issue.path} after "
                    f"{regen_timeout:.0f}s; preserved existing file instead."
                )
                continue
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
                _preserve_existing_on_regenerate_failure(
                    issue,
                    f"invalid syntax: {validation_error}",
                )
                errors.append(
                    f"Invalid regenerated content for {issue.path}: "
                    f"{validation_error}; preserved existing file instead."
                )
                continue

            from skyn3t.agents.code_agent import _syntax_ok

            if not _syntax_ok(new_content, issue.path):
                _preserve_existing_on_regenerate_failure(
                    issue,
                    "build-invalid output",
                )
                errors.append(
                    f"Build-invalid regenerated content for {issue.path}; "
                    "preserved existing file instead."
                )
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
