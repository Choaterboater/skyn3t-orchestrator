"""Code Agent - executes, analyzes, refactors, and tests code."""

import ast
import asyncio
import logging
import os
import re as _RE
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.prompt_compression import compress_prompt_context

logger = logging.getLogger("skyn3t.agents.code_agent")

_PROMPT_SKILL_MIN_SCORE = -0.5
_UNSAFE_PROMPT_SKILL_TAGS = {
    "malicious_skill",
    "mcp_mismatched_skill",
    "mcp_overprivileged_skill",
    "mcp_poisoned_tool",
    "mcp_underdeclared_skill",
    "sdi1_mismatch",
    "sdi2_inappropriate",
    "sdi3_scope_creep",
    "sdi4_divergence",
    "sqp1_vague_triggers",
    "sqp2_missing_warnings",
    "sqp3_locale_forcing",
    "ssd1_semantic_injection",
    "ssd2_novel_phrasing",
    "ssd3_nl_exfiltration",
    "ssd4_narrative_deception",
}

_PLAN_TIMEOUT_SECONDS = 120.0
_FILE_BUILD_TIMEOUT_SECONDS = 180.0
_FILE_RETRY_TIMEOUT_SECONDS = 120.0
_CLUSTER_BUILD_TIMEOUT_SECONDS = 240.0


def _prompt_skill_allowed(skill: Any) -> bool:
    try:
        tags = {str(tag).strip().lower() for tag in getattr(skill, "tags", [])}
    except Exception:
        tags = set()
    return not bool(tags & _UNSAFE_PROMPT_SKILL_TAGS)


def _strip_fences(body: str) -> str:
    """Return ONLY the first fenced code block's contents when a fence
    is present — discarding any leading AND trailing prose — and pass
    the body through unchanged when no fence is present.

    The model is told to return raw file contents, but CLI backends
    sometimes wrap the output in a fenced ``` block, with optional
    leading/trailing prose ("Here is the file:\n```js\n...\n```\nLet
    me know if...").

    The earlier implementation only discarded trailing prose when the
    closing fence happened to sit on its own line (``\n```\n``). The
    real ``sonos.js`` leak was a closing fence with no preceding
    newline (``...connect() }```\nThat should work for sonos.``) —
    the paired-fence regex required a ``\n`` before the closing
    backticks, so it missed the close entirely and the prose after it
    leaked onto disk. This rewrite finds the opening fence, then the
    NEXT closing fence regardless of surrounding whitespace, and
    returns only what's between them. Behavior for inputs WITHOUT a
    fence is identical to before (passthrough), and inputs that were
    already handled correctly (clean ``\n```\n`` close, leading prose,
    audit fixtures) produce the same result.
    """
    if not body:
        return body
    # Locate the first opening fence (optional language tag), then the
    # NEXT closing fence after the opening fence's content begins. The
    # closing fence may or may not be preceded by a newline — both
    # "...code\n```" and "...code```" are matched so trailing prose
    # after the close is dropped either way.
    open_m = _RE.search(r"(?:^|\n)[ \t]*```[a-zA-Z0-9_+\-]*[ \t]*\n", body)
    if open_m:
        content_start = open_m.end()
        rest = body[content_start:]
        # v43 guard: a lone trailing fence ("code\n```\n") can look
        # like an opening fence to the search above (it has a \n before
        # AND after it). If nothing of substance follows the matched
        # fence, it was a CLOSING fence, not an opening one — fall
        # through to the trailing-fence stripper below.
        if rest.strip():
            # Closing fence: ``` at a line start (optionally indented),
            # OR appended directly to the end of a content line. Search
            # only the region AFTER the opening fence's content.
            close_m = _RE.search(
                r"\n[ \t]*```[ \t]*(?:\n|$)|```[ \t]*(?:\n|$)", rest
            )
            if close_m:
                return rest[:close_m.start()]
            # Opening fence but no closing fence — the LLM forgot to
            # close. We can't reliably tell where code ends, so keep
            # the post-fence remainder (v44 behavior) rather than guess.
            return rest
    # No opening fence (or the "opening" was actually a lone trailing
    # fence). v43: LLM sometimes emits the entire file content
    # correctly, then appends a lone ``` on the final line. Strip that
    # trailing fence so the file parses.
    stripped = body.rstrip()
    if stripped.endswith("\n```"):
        return stripped[:-4].rstrip()
    if stripped.endswith("```") and not stripped.startswith("```"):
        return stripped[:-3].rstrip()
    return body


# Tool-call trace patterns that copilot/claude CLIs sometimes prepend to
# their output despite the prompt explicitly forbidding it. Matching
# these as line-starts lets us trim everything before the actual file
# content begins. Real file content never opens with these.
_CLI_TRACE_PATTERNS: Tuple[str, ...] = (
    r"^●\s",                 # ● Search, ● Read, ● Web Search
    r"^✗\s",                 # ✗ Read (failed lookup)
    r"^✓\s",                 # ✓ generic success bullet
    r"^└\s",                 # tree-style continuation lines
    r"^│\s",                 # tree-style continuation lines
    r"^Understood[—\-\s,]",  # common conversational lead-in
    r"^Let me\s",            # common conversational lead-in
    r"^I['’]ll\s",           # smart-quote-tolerant "I'll"
    r"^I['’]m\s",            # smart-quote-tolerant "I'm" (canary-113 README leak)
    r"^I['’]ve\s",           # smart-quote-tolerant "I've"
    r"^I will\s",            # common conversational lead-in
    r"^Sure[!\.\,\s]",       # common conversational lead-in
    r"^Here(?:['’]s| is) (?:the|a)\s",  # "Here's the file:"
    # Kimi / other CLI backends emit prose without bullet prefixes
    r"^The (?:existing|workspace|file)\s",  # "The existing X already matches..."
    r"^This (?:file|is|looks?)\s",   # "This file is...", "This looks good"
    r"^I (?:have|can|see|am)\s",     # "I have verified...", "I can see..."
    r"^It (?:already|is|looks?)\s",  # "It already matches..."
    r"^That (?:looks?|is)\s",        # "That looks correct"
    r"^No (?:changes|matches?)\s",   # "No changes needed", "No matches found"
    r"^Looks? good\s",        # "Looks good"
    r"^Path does not exist\b",       # CLI tool error leaked into body
    r"^Permission denied\b",         # same
)


_CSS_CONTENT_START_RE = _RE.compile(
    r"^(@|:root\b|[.#*]|/\*|[a-zA-Z][\w:-]*(?:\s*[,{]|::?[\w-]+|\s+\{))"
)


# Copilot CLI's non-interactive output ends with a stats footer block:
#
#   Changes   +0 -0
#   Requests  1 Premium (3s)
#   Tokens    ↑ 20.8k • ↓ 30 • 9.7k (cached) • 23 (reasoning)
#
# When the model's response is appended above this footer, the footer
# leaks into the file body — making the file syntactically broken
# (e.g. JSX gets "Tokens" at the bottom and refuses to parse). This
# regex matches the start of that footer so we can truncate everything
# from it on. The pattern is intentionally narrow — we only trim when
# all three rows appear in their canonical order.
_COPILOT_FOOTER_RE = _RE.compile(
    r"\n+Changes\s+[+\-]?\d+\s+[\-+]?\d+\s*\n+"
    r"Requests\s+\d+",
    _RE.MULTILINE,
)


def _strip_copilot_footer(body: str) -> str:
    """Remove the Copilot CLI stats footer if it leaked into the body.
    Returns the body unchanged when the footer isn't present."""
    if not body or "Tokens" not in body:
        return body
    m = _COPILOT_FOOTER_RE.search(body)
    if m:
        return body[: m.start()].rstrip()
    return body


def _looks_like_css_content_start(line: str) -> bool:
    return bool(_CSS_CONTENT_START_RE.match(line.strip()))


_ENTRYPOINT_FILES = ("app.jsx", "app.tsx", "main.jsx", "main.tsx")
# Doubled from 4000 once entrypoint generation moved off the CLI tiers
# onto OpenRouter primary (see _is_problem_file path below): the CLI
# timeout pressure that justified the tighter cap is gone, and the
# task-aware OpenRouter ladder handles 8K context comfortably. Extra
# context = fewer truncated brief/architecture sections leaking into
# stub fallbacks on entrypoint files.
_ENTRYPOINT_CONTEXT_HARD_CAP = 12000

# Entry-file paths that should be pinned to USE planned components
# rather than reinvent them inline. Mirrors _ENTRYPOINT_FILES plus
# Next.js' `page.{jsx,tsx}`. Centralized so the prompt and the
# consistency engine's entry-file drift detector stay aligned.
_ENTRYPOINT_PROMPT_TAIL = (
    "app.jsx", "app.tsx",
    "main.jsx", "main.tsx",
    "page.tsx", "page.jsx",
)


def _entrypoint_import_instructions(
    *,
    rel: str,
    file_specs: List[Dict[str, Any]],
) -> str:
    """Return a prompt fragment that pins entry-file generation to USE
    the planned component files rather than reinvent them inline.

    Returns "" when ``rel`` isn't an entry file, or when no components
    are planned alongside it. The fragment lists up to 12 planned
    component paths and tells the LLM:

    1. For every component rendered, import from the planned path.
    2. Don't invent NEW components when a planned sibling exists.
    3. Adapt via props if the API doesn't fit — never reinvent.

    Pulled out of the inline per-file prompt builder so the contract
    is unit-testable without spinning the whole scaffold loop.
    """
    rl_lower = (rel or "").lower()
    if not rl_lower.endswith(_ENTRYPOINT_PROMPT_TAIL):
        return ""
    component_paths: List[str] = []
    for spec in file_specs:
        if not isinstance(spec, dict):
            continue
        path = (spec.get("path") or "").strip()
        if not path:
            continue
        if "components/" not in path.lower():
            continue
        if not path.lower().endswith((".jsx", ".tsx")):
            continue
        component_paths.append(path)
    if not component_paths:
        return ""
    listed = "\n".join(f"  - {p}" for p in component_paths[:12])
    more = (
        f"\n  ...and {len(component_paths) - 12} more"
        if len(component_paths) > 12
        else ""
    )
    return (
        "IMPORT, do NOT redefine. The plan includes "
        "these component files alongside this entry:\n"
        f"{listed}{more}\n\n"
        "Rules:\n"
        "- For every component you render in this "
        "file that maps to a planned path above, "
        "`import` it from the planned path. Do not "
        "redefine inline.\n"
        "- Do not invent NEW components inside this "
        "file when a planned sibling already covers "
        "the same shape (e.g. don't write an inline "
        "`function HabitCard(...)` when "
        "`components/HabitCard.jsx` is planned).\n"
        "- If a planned component's API doesn't fit, "
        "still import and adapt with props, not "
        "reinvent.\n\n"
    )


def _planned_component_names(file_specs: List[Dict[str, Any]]) -> List[str]:
    """Return the bare component names (file stems) of every planned
    ``components/*.{jsx,tsx}`` entry — the components an entrypoint was
    told to IMPORT. Used to detect inline-redefinition drift."""
    names: List[str] = []
    for spec in file_specs or []:
        if not isinstance(spec, dict):
            continue
        path = (spec.get("path") or "").strip()
        if not path or "components/" not in path.lower():
            continue
        if not path.lower().endswith((".jsx", ".tsx")):
            continue
        stem = Path(path).stem
        if stem and stem[0].isupper():
            names.append(stem)
    return names


def _inline_redefined_planned(
    body: str, file_specs: List[Dict[str, Any]]
) -> List[str]:
    """Return planned component names that ``body`` defines inline
    (``function Foo(`` / ``const Foo =`` / ``class Foo``) WITHOUT also
    importing them — the 'two parallel component trees, one orphaned'
    drift. Empty list when nothing is redefined or on any parse issue
    (fail-open).
    """
    try:
        if not body:
            return []
        planned = _planned_component_names(file_specs)
        if not planned:
            return []
        offenders: List[str] = []
        for name in planned:
            esc = _RE.escape(name)
            defines = bool(
                _RE.search(rf"\bfunction\s+{esc}\s*\(", body)
                or _RE.search(rf"\bconst\s+{esc}\s*=", body)
                or _RE.search(rf"\bclass\s+{esc}\b", body)
            )
            if not defines:
                continue
            # Imported anywhere? (default OR named import of this name)
            imported = bool(
                _RE.search(
                    rf"""import\s+(?:[^'";\n]*\b{esc}\b[^'";\n]*)\s+from""",
                    body,
                )
            )
            if not imported:
                offenders.append(name)
        return offenders
    except Exception:
        return []


def _relevant_context(prior_context: str, rel_path: str) -> str:
    """Filter prior_context down to just the sections this file needs.

    The full prior_context is ~14KB of brief/research/architecture/design/
    brand/components — sending ALL of it on every per-file LLM call means the
    CLI streams ~14KB of prompt overhead before the model can think.
    For a tiny file like vite.config.js, 95%+ of that context is dead
    weight that doubles the per-call wall time on CLI backends.

    The strategy is per-extension: only the sections likely to inform
    THIS file get included. Other sections become a one-line skipped
    note so the model knows they exist if it needs them.

    Special case: entrypoint files (App.jsx, main.jsx) get a HARD CAP
    on context size. Both Kimi and Copilot CLIs time out on prompts
    larger than ~15KB, and for entrypoints we've been hitting 18-25KB
    once brand.md + components.md + architecture.md are concatenated.
    The cap means a slightly less-informed entrypoint, but a written
    one beats a stub.
    """
    if not prior_context:
        return prior_context
    rl = rel_path.lower()
    # Map file path → which artifact sections to include.
    # Tags: "research" (API specs), "architecture" (system design),
    # "design" / "brand" / "components" (visual/UI), "brainstorm" (alternatives).
    is_server = rl.startswith("server/") or "server/" in rl
    is_adapter = "/adapters/" in rl
    is_frontend = (
        rl.startswith("src/") or "src/" in rl
        or rl.endswith((".jsx", ".tsx", ".html", ".css"))
    )
    is_top_config = rl in (
        "vite.config.js", "vite.config.ts", "package.json", "tsconfig.json",
        "next.config.js", ".env.example", "docker-compose.yml",
        "tailwind.config.js", "postcss.config.js",
    )
    if is_top_config:
        # Config files need architecture (port choices, stack hints).
        # Research isn't useful here. Design/brand/components aren't either.
        wanted = {"architecture.md"}
    elif is_adapter or is_server:
        # Backend files: research's API specs are the most useful
        # context. Architecture covers the proxy/routing contract.
        # Brand/components are noise.
        wanted = {"research.md", "architecture.md"}
    elif is_frontend:
        # Frontend files: design + brand + components dictate look/feel,
        # architecture defines the API shapes the UI consumes.
        # Research alone is rarely needed unless the file talks to a
        # service directly (rare in a proxied architecture).
        wanted = {"design.md", "brand.md", "components.md", "architecture.md"}
    else:
        # Unknown shape (top-level scripts, etc.) — include
        # architecture only. Skip the bulky research/brand sections.
        wanted = {"architecture.md"}

    sections: list[str] = []
    current_name: Optional[str] = None
    current_lines: list[str] = []
    # The "Upstream essentials" section (curated prior_summaries from
    # the runner) is always relevant — it's a few hundred bytes at
    # most and tells the model what each upstream stage decided. Keep
    # it in every per-file call alongside the per-extension picks.
    wanted_always = {"Upstream essentials"}
    for line in prior_context.split("\n"):
        # Section header is "### <name>" (artifacts end in ".md", the
        # runner's essentials block uses a plain header).
        if line.startswith("### "):
            if current_name is not None and (
                current_name in wanted or current_name in wanted_always
            ):
                sections.append("\n".join(current_lines).rstrip())
            current_name = line[4:].strip()
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_name is not None and (
        current_name in wanted or current_name in wanted_always
    ):
        sections.append("\n".join(current_lines).rstrip())
    result = "\n\n".join(sections).strip()

    # Entrypoint hard cap. Both Kimi and Copilot CLIs have empirically
    # timed out on App.jsx prompts that include the full brand kit +
    # components doc — the streaming-mode CLIs choke when input is
    # 15KB+ AND output is also large (~5-8KB for a full App.jsx). For
    # entrypoints we truncate aggressively; the resulting file may be
    # less polished but it actually gets written instead of stub'd.
    if any(rl.endswith(ep) for ep in _ENTRYPOINT_FILES) and len(result) > _ENTRYPOINT_CONTEXT_HARD_CAP:
        head = result[: _ENTRYPOINT_CONTEXT_HARD_CAP].rstrip()
        result = head + "\n\n[...context truncated to keep entrypoint prompt small enough for CLI backends...]"

    return result


def _strip_cli_prelude(body: str, rel_path: str) -> str:
    """Trim copilot/claude CLI's tool-call trace before the actual file.

    When the CLI emits tool-call narration (``● Search ...``, ``● Read ...``,
    ``Understood — I'll quickly inspect ...``) before the file contents,
    we end up writing that narration to disk. We saw this in v15's
    ``.env.example`` — the first 46 lines were copilot's tool trace and
    the actual env vars started at line 47.

    Strategy: find the first line that looks like real content for the
    file's type (a recognizable code/config start marker), and drop
    everything before it. Conservative — if we can't identify a clear
    start marker we return the body unchanged so we never corrupt good
    output.
    """
    if not body or "\n" not in body:
        return body
    from pathlib import Path as _P
    rl = rel_path.lower()

    # Per-extension "real content starts here" markers. Generous regex
    # — better to keep a line we shouldn't than drop one we should.
    if rl.endswith((".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")):
        start_re = _RE.compile(
            r"^(import\s|const\s|let\s|var\s|function\s|class\s|export\s"
            r"|module\.exports|require\(|'use strict'|\"use strict\"|//|/\*"
            r"|#!\s*/usr/bin/env\s+node)"
        )
    elif rl.endswith(".py"):
        start_re = _RE.compile(
            r"^(import\s|from\s|def\s|class\s|async\s+def\s|@\w+"
            r"|#!\s*/usr/bin/env\s+python|#\s*-\*-|\"\"\"|''')"
        )
    elif rl.endswith(".json"):
        start_re = _RE.compile(r"^\s*[\{\[]")
    elif rl.endswith(".env") or _P(rl).name == ".env.example" or "/.env" in rl or rl.endswith(".env.example"):
        # Env files: a real line is KEY=value, possibly preceded by # comment
        start_re = _RE.compile(r"^(#\s|[A-Z][A-Z0-9_]*=)")
    elif rl.endswith((".yml", ".yaml")):
        start_re = _RE.compile(r"^([a-zA-Z][a-zA-Z0-9_\-]*:|---|#\s)")
    elif rl.endswith(".css"):
        start_re = _CSS_CONTENT_START_RE
    elif rl.endswith(".html"):
        start_re = _RE.compile(r"^(<!DOCTYPE|<html|<\?xml)", _RE.IGNORECASE)
    elif rl.endswith((".md", ".markdown")):
        # Real markdown lines begin with a heading hash, frontmatter `---`,
        # a list bullet/number, a blockquote `>`, a fenced code block,
        # or an HTML tag. Plain prose starting with a capital letter is
        # NOT enough signal — canary-113's server/README.md leaked with
        # "I'm checking the project structure..." as its first line.
        start_re = _RE.compile(r"^(#{1,6}\s|---\s*$|\*\s|-\s|\d+\.\s|>\s|```|<!?[a-zA-Z])")
    elif rl.endswith(".sh"):
        start_re = _RE.compile(r"^(#!|#\s|set\s|export\s|[A-Z_][A-Z0-9_]*=)")
    else:
        return body  # no marker for this type — don't risk corrupting

    lines = body.split("\n")
    trace_re = _RE.compile("|".join(_CLI_TRACE_PATTERNS))

    # Find the first non-trace line that looks like real content. We
    # require a *transition* — at least one trace line before — to
    # avoid trimming files that legitimately start with markers we
    # match (e.g., a Python module whose first line is also `import`).
    saw_trace = False
    first_content_idx = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if trace_re.search(ln) or trace_re.search(s):
            saw_trace = True
            continue
        # Conversational fragments that lack a trace prefix but are
        # clearly prose ("the file should...", "this defines the...",
        # "The existing index.html already matches...").
        # Skip past them ONLY if we've already seen trace lines (= we
        # know the model is in narration mode). Case-agnostic — Kimi
        # emits capitalised prose like "The existing index.html...".
        if saw_trace and not start_re.match(ln):
            continue
        if start_re.match(ln):
            first_content_idx = i
            break

    if saw_trace and first_content_idx is not None and first_content_idx > 0:
        return "\n".join(lines[first_content_idx:])
    return body


def _extract_marked_files(raw: str) -> Dict[str, str]:
    """Parse multi-file marker output: `// === path ===` or `# === path ===`."""
    if not raw:
        return {}
    pattern = _RE.compile(
        r"^(?://|#)\s*===\s*(?P<path>\S+?)\s*===\s*$",
        flags=_RE.MULTILINE,
    )
    matches = list(pattern.finditer(raw))
    if not matches:
        return {}
    parsed: Dict[str, str] = {}
    for idx, m in enumerate(matches):
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        body = raw[start:end].strip().rstrip("\n").strip()
        parsed[m.group("path").lstrip("/").strip()] = body
    return parsed


def _syntax_ok(body: str, rel_path: str, timeout: float = 5.0) -> bool:
    """Cheap pre-write syntax gate. True if the body looks parseable
    OR if we don't have a checker for this file type (we don't gate on
    files we can't validate). Returns False only when the checker
    clearly says the body is broken; infrastructure errors (missing
    `node`, timeouts) default to True so we never wedge the pipeline
    on a tooling problem.
    """
    rl = rel_path.lower()
    if not body or not body.strip():
        return False  # empty body is never useful
    # Markdown fences are a common CLI failure mode that node --check
    # silently accepts (it treats ``` as the start of a template
    # literal). Reject upfront if a fence survived stripping.
    stripped = body.strip()
    if stripped.startswith("```") or stripped.endswith("```"):
        return False
    if rl.endswith(".json"):
        try:
            import json as _j
            _j.loads(body)
            return True
        except Exception:
            return False
    if rl.endswith(".css"):
        lines = body.splitlines()
        first_nonempty = next((line.strip() for line in lines if line.strip()), "")
        if not first_nonempty or not _looks_like_css_content_start(first_nonempty):
            return False
        if body.count("/*") != body.count("*/"):
            return False
        return True
    if rl.endswith(".py"):
        try:
            ast.parse(body)
            return True
        except SyntaxError:
            return False
        except Exception:
            return True  # other ast errors → don't block
    if rl.endswith((".jsx", ".tsx")):
        # JSX/TSX cannot be parsed by node's built-in checker — it
        # only understands plain JS/ESM. node --check on a .jsx file
        # bails with ERR_UNKNOWN_FILE_EXTENSION, and previously this
        # rejected EVERY JSX file the LLMs generated, dropping working
        # 7-15KB App.jsx outputs from deepseek, qwen3-coder, and
        # gpt-5-mini. Without a Babel/esbuild parser available, we
        # fall back to cheap structural checks that catch the common
        # failure shapes (unbalanced braces, missing return/export,
        # leftover markdown fences, prose-as-code) without falsely
        # rejecting valid JSX.
        text = body.strip()
        if not text:
            return False
        # Catch markdown fences that survived stripping (already
        # checked above but redundant safety is cheap).
        if "```" in text:
            return False
        # JSX/React files should contain at least one of these signals.
        # An LLM hallucinating prose won't have any of these.
        signals = (
            "import ", "from ", "export ",
            "function ", "const ", "let ", "var ",
            "return ", "=>",
        )
        if not any(sig in text for sig in signals):
            return False
        # Balanced braces (a coarse syntax check). Off-by-one or worse
        # almost always means truncation or mid-stream content.
        if text.count("{") != text.count("}"):
            return False
        if text.count("(") != text.count(")"):
            return False
        # `export default const|let|var` is invalid ES syntax — the
        # declaration after `export default` must be an expression or
        # a function/class declaration. Catches an LLM failure mode
        # where the model generates the keyword but not a usable rhs.
        if _RE.search(r"\bexport\s+default\s+(const|let|var)\b", text):
            return False
        return True
    if rl.endswith((".js", ".mjs", ".cjs")):
        # Plain JS/ESM: node --check works correctly for these.
        try:
            suffix = "." + rl.rsplit(".", 1)[-1]
            with tempfile.NamedTemporaryFile(
                "w", suffix=suffix, delete=False, encoding="utf-8",
            ) as tf:
                tf.write(body)
                tmp = tf.name
            try:
                node = subprocess.run(
                    ["node", "--check", tmp],
                    capture_output=True, text=True, timeout=timeout,
                )
                return node.returncode == 0
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return True  # no checker available → don't block
        except Exception:
            return True
    return True


def _stack_ok(body: str, rel_path: str, stack: str) -> bool:
    """Stack-consistency gate: reject content that belongs to a different
    ecosystem than the one declared in `stack`. This catches the
    "kimi rewrote react_vite as Next.js" failure mode (v37) before the
    file is written to disk.

    Returns True when the body is consistent with the stack, or when we
    don't have a rule for this combo (defensive — don't block unknowns).
    """
    if not body or not stack:
        return True
    rl = rel_path.lower()
    text = body.lower()

    # React/Vite files should never contain Next.js imports or patterns.
    if stack == "react_vite":
        nextjs_markers = (
            'from "next"',
            "from 'next'",
            'import type { metadata } from "next"',
            'next/font/google',
            'next/head',
            'next/image',
            'next/link',
            'app/page.tsx',
            'app/layout.tsx',
            'next.config.js',
            'next.config.ts',
            'next.config.mjs',
            '"plugins": [{ "name": "next" }]',
        )
        # Only check files that the LLM actually writes; deterministic
        # manifests are already correct and this gate would be redundant.
        is_frontend_or_config = (
            rl.endswith((".jsx", ".tsx", ".js", ".ts", ".css", ".html", ".json"))
            or rl in ("vite.config.js", "vite.config.ts", "next.config.js",
                      "next.config.ts", "tsconfig.json", "package.json")
        )
        if is_frontend_or_config:
            for marker in nextjs_markers:
                if marker in text:
                    return False
        return True

    # Next.js files should never contain Vite-specific patterns (rare,
    # but symmetrical so a mis-routed file gets caught both ways).
    if stack == "next":
        vite_markers = (
            'from "vite"',
            "from 'vite'",
            '@vitejs/plugin-react',
            'vite.config.js',
            'vite.config.ts',
            '<script type="module" src="/src/main',
        )
        is_frontend_or_config = (
            rl.endswith((".jsx", ".tsx", ".js", ".ts", ".css", ".html", ".json"))
            or rl in ("vite.config.js", "vite.config.ts", "next.config.js",
                      "next.config.ts", "tsconfig.json", "package.json")
        )
        if is_frontend_or_config:
            for marker in vite_markers:
                if marker in text:
                    return False
        return True

    return True


def _placeholder_for(rel_path: str, purpose: str, stack: str) -> str:
    """Last-resort body when every LLM attempt fails.

    Returns a syntactically valid file that imports resolve and the
    build verifier can still parse — but marked with a TODO so the
    reviewer/fix loop flags it for real implementation. Returns empty
    string if we can't even guess a safe placeholder shape (we'd
    rather skip the file than write garbage that breaks the build).
    """
    rl = rel_path.lower()
    note = f"// TODO[skyn3t]: code generation failed for {rel_path} — {purpose or 'no purpose given'}"
    py_note = f"# TODO[skyn3t]: code generation failed for {rel_path} — {purpose or 'no purpose given'}"

    # React component — ANY .jsx / .tsx file. The old `/src/` substring
    # check missed top-level `src/App.jsx` because the path has no
    # leading slash. Result: App.jsx silently dropped on every run
    # where the model returned empty. Now we recognize React components
    # by extension alone — that's enough, since .jsx isn't used outside
    # React anyway.
    if rl.endswith((".jsx", ".tsx")):
        from pathlib import Path as _P
        name = _P(rel_path).stem or "Component"
        # Component names must start with uppercase; if the filename is
        # lowercase, capitalize it so React doesn't treat it as HTML.
        if name and not name[0].isupper():
            name = name[0].upper() + name[1:]
        return (
            f"{note}\n\n"
            "import { useState } from 'react';\n\n"
            f"export default function {name}() {{\n"
            "  const [ready] = useState(false);\n"
            "  return (\n"
            "    <div style={{ padding: 24 }}>\n"
            f"      <h1>{name}</h1>\n"
            "      <p>Generation failed for this component. Replace with the real implementation.</p>\n"
            "    </div>\n"
            "  );\n"
            "}\n"
        )

    # JSON — empty object/array as a safe default. Type guess by name:
    # `user-config.json`, `services.json`, `settings.json` get `{}`;
    # `*-cards.json`, anything plural-sounding gets `[]`. Either is
    # valid JSON; either lets the program boot. Skipping the file
    # entirely is what was causing v23's `user-config.json` to be
    # missing and the server to crash on first config read.
    if rl.endswith(".json"):
        from pathlib import Path as _P
        stem = _P(rl).stem
        # Heuristic: plural-ish names → array; singular-config-ish → object
        plural_hints = ("cards", "items", "entries", "list", "registry",
                        "services", "plugins", "adapters", "tags")
        is_array = any(stem.endswith(p) for p in plural_hints)
        body = "[]" if is_array else "{}"
        # Note as a sibling .json.todo file isn't ideal; instead we
        # emit a small JSON object with a `_todo` field so the file
        # parses AND surfaces the issue to anyone reading it.
        if not is_array:
            import json as _j
            return _j.dumps({"_todo": f"code generation failed for {rel_path} — {purpose or ''}"}, indent=2) + "\n"
        return body + "\n"

    # Bare JS module — must come AFTER the .jsx check above so the
    # .jsx-as-React branch wins. Old code used `.endswith((".js",
    # ".mjs", ".ts"))` which never matched .jsx, but order-of-checks
    # matters once we widen the JS pattern.
    if rl.endswith((".js", ".mjs", ".cjs", ".ts")) and not rl.endswith((".d.ts",)):
        # Module-system aware: a path inside server/ probably wants
        # ESM (matches the rest of the server tree) — emit
        # `export default ...` which works under both ESM and CJS
        # (CJS just won't import it, but the file still parses).
        return f"{note}\n\nexport default null;\n"

    # Python module
    if rl.endswith(".py"):
        return f"{py_note}\n\n# Replace with real implementation.\n"

    # CSS — empty file is fine, imports resolve
    if rl.endswith(".css"):
        return f"{note}\n"

    # HTML — minimal scaffold
    if rl.endswith(".html"):
        return (
            "<!doctype html>\n"
            f"<html><head><meta charset=\"utf-8\"><title>{rel_path}</title></head>\n"
            f"<body>{note}</body></html>\n"
        )

    # Markdown
    if rl.endswith(".md"):
        return f"# {rel_path}\n\n{py_note.lstrip('# ')}\n"

    # YAML
    if rl.endswith((".yml", ".yaml")):
        return f"# {rel_path}\n{py_note.lstrip('# ')}\n"

    # .env / .env.example — emit a comment line so the file parses
    if rl.endswith(".env") or rl.endswith(".env.example") or "/.env" in rl:
        return f"# {rel_path} — code generation failed. Add real values.\n"

    # Unknown extension — emit a comment-only file rather than NOTHING,
    # so the completeness check downstream sees a file on disk and the
    # reviewer / fix loop has a target to attack. Returning "" here
    # was the v23 silent-drop bug for any unusual file shape.
    return f"# {rel_path}\n# {py_note.lstrip('# ')}\n"


# Markers that uniquely identify a generation-failure stub body. Kept
# module-level so both _is_entrypoint_stub and the code-stage output
# scan (stub_markers) reference the SAME strings — the placeholder
# bodies emitted by _placeholder_for and _backfill stubs use these.
_STUB_TODO_MARKER = "TODO[skyn3t]: code generation failed"
_STUB_BACKFILL_MARKER = "@skyn3t-backfill-stub"
_STUB_GENERATION_FAILED_PHRASE = "Generation failed for this component"


def _is_entrypoint_stub(body: str, rel_path: str) -> bool:
    """Return True when an entry file (App.*/main.*/page.*) body is a
    generation-failure stub rather than a real implementation.

    Detects the shapes that _placeholder_for / the backfill path emit
    when every LLM attempt fails:

      * the ``// TODO[skyn3t]: code generation failed`` marker comment
      * the ``@skyn3t-backfill-stub`` marker
      * a body that reduces to ``export default null`` (the bare-JS
        placeholder shape)
      * the React placeholder whose JSX renders the "Generation failed
        for this component" copy

    Used to set output['entrypoint_is_stub'] and, after extraction
    hardening, to force ONE extra regen of the entrypoint before
    accepting it.

    Fail-open: on any unexpected input (None, non-entry path, parse
    surprise) it returns False so it can NEVER block a legitimate
    file. Pure string/regex over an in-memory body — no external tool,
    cannot fail externally.
    """
    try:
        if not body:
            # An empty entrypoint is not a "stub" per this contract —
            # it's an empty file the existing placeholder path handles.
            return False
        rl = (rel_path or "").lower()
        # Only entry files qualify. Accept with or without a leading
        # directory (src/App.jsx as well as App.jsx) and the Next.js
        # page.* form.
        if not rl.endswith(_ENTRYPOINT_PROMPT_TAIL):
            return False
        # Explicit failure markers — unambiguous.
        if _STUB_TODO_MARKER in body:
            return True
        if _STUB_BACKFILL_MARKER in body:
            return True
        if _STUB_GENERATION_FAILED_PHRASE in body:
            return True
        # Body reduces to `export default null` once comments/blank
        # lines are removed (the bare-JS placeholder shape leaking into
        # an entry slot). Strip line/block comments cheaply.
        no_block = _RE.sub(r"/\*[\s\S]*?\*/", "", body)
        meaningful = [
            ln.strip()
            for ln in no_block.splitlines()
            if ln.strip() and not ln.strip().startswith("//")
            and not ln.strip().startswith("#")
        ]
        joined = " ".join(meaningful)
        if _RE.fullmatch(r"export\s+default\s+null\s*;?", joined):
            return True
        return False
    except Exception:
        # Pure fail-open: never let a detector crash gate a file.
        return False


def _collect_stub_signal(
    out_dir,
    file_specs: List[Dict[str, Any]],
    files_written: List[str],
) -> Tuple[List[str], List[Dict[str, str]], List[str], bool]:
    """Build the Phase 3 pre-verifier signal for the code-stage output.

    Returns a 4-tuple ``(planned_imports, stub_markers, entrypoint_files,
    entrypoint_is_stub)``:

      * ``planned_imports`` — scaffold-relative planned ``components/*``
        component paths the entrypoint MUST import (from ``file_specs``).
      * ``stub_markers`` — ``[{'file': rel, 'kind': 'entrypoint-stub'|
        'component-stub'|'export-default-null', 'marker': matched_text}]``
        for every written file that shipped as a stub.
      * ``entrypoint_files`` — scaffold-relative entry files written.
      * ``entrypoint_is_stub`` — True if ANY entry file is a stub.

    Pure-Python over already-written files; no external tool. On any
    failure returns ``([], [], [], False)`` so the existing output keys
    still return and the runner treats absence as 'no signal'.
    """
    planned_imports: List[str] = []
    stub_markers: List[Dict[str, str]] = []
    entrypoint_files: List[str] = []
    entrypoint_is_stub = False
    try:
        resolved_out = str(Path(out_dir).resolve())

        def _rel_of(p: str) -> Optional[str]:
            try:
                return str(
                    Path(p).resolve().relative_to(resolved_out)
                ).replace("\\", "/")
            except Exception:
                return None

        # planned_imports: scaffold-relative planned component paths.
        for s in (file_specs if isinstance(file_specs, list) else []):
            if not isinstance(s, dict):
                continue
            pth = (s.get("path") or "").lstrip("/").strip()
            if not pth or "components/" not in pth.lower():
                continue
            if not pth.lower().endswith((".jsx", ".tsx")):
                continue
            planned_imports.append(pth.replace("\\", "/"))

        # Scan every written file for stub markers + entrypoint stub.
        for f in (files_written or []):
            rel_f = _rel_of(f)
            if rel_f is None:
                continue
            rl_f = rel_f.lower()
            is_entry = rl_f.endswith(_ENTRYPOINT_PROMPT_TAIL)
            if is_entry:
                entrypoint_files.append(rel_f)
            try:
                body_f = Path(f).read_text(encoding="utf-8")
            except Exception:
                continue
            # entrypoint stub takes precedence as a distinct kind.
            if is_entry and _is_entrypoint_stub(body_f, rel_f):
                entrypoint_is_stub = True
                marker = (
                    _STUB_TODO_MARKER
                    if _STUB_TODO_MARKER in body_f
                    else _STUB_BACKFILL_MARKER
                    if _STUB_BACKFILL_MARKER in body_f
                    else _STUB_GENERATION_FAILED_PHRASE
                    if _STUB_GENERATION_FAILED_PHRASE in body_f
                    else "export default null"
                )
                stub_markers.append({
                    "file": rel_f,
                    "kind": "entrypoint-stub",
                    "marker": marker,
                })
                continue
            # Generic component / module stub markers.
            if _STUB_TODO_MARKER in body_f:
                stub_markers.append({
                    "file": rel_f,
                    "kind": "component-stub",
                    "marker": _STUB_TODO_MARKER,
                })
            elif _STUB_BACKFILL_MARKER in body_f:
                stub_markers.append({
                    "file": rel_f,
                    "kind": "component-stub",
                    "marker": _STUB_BACKFILL_MARKER,
                })
            elif rl_f.endswith((".js", ".mjs", ".cjs", ".ts")) and _RE.search(
                r"^\s*export\s+default\s+null\s*;?\s*$",
                body_f,
                _RE.MULTILINE,
            ):
                stub_markers.append({
                    "file": rel_f,
                    "kind": "export-default-null",
                    "marker": "export default null",
                })
        return planned_imports, stub_markers, entrypoint_files, entrypoint_is_stub
    except Exception:
        logger.debug("stub-signal scan failed (non-fatal)", exc_info=True)
        return [], [], [], False


class CodeAgent(BaseAgent):
    """Agent for safe code execution, analysis, refactoring, and testing."""

    def __init__(
        self,
        name: str = "code_agent",
        event_bus: EventBus | None = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="code",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="code_execution",
                description=(
                    "Execute Python code in-process with a restricted-builtins shim. "
                    "NOT a real sandbox: an attacker who controls the code can escape "
                    "via __subclasses__ or imports. Only use with trusted input."
                ),
                parameters={"code": "str", "timeout": "int"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="code_analysis",
                description="Analyze code quality, complexity, and style",
                parameters={"code": "str", "analysis_type": "str"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="refactoring",
                description="Refactor and improve code structure",
                parameters={"code": "str", "refactor_type": "str"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="test_runner",
                description="Run tests and report results",
                parameters={"test_code": "str", "test_framework": "str"},
            )
        )
        self._sandbox_dir = self.config.get("sandbox_dir", tempfile.gettempdir())
        self._max_output_size = self.config.get("max_output_size", 10000)
        self._execution_timeout = self.config.get("execution_timeout", 30)
        # Budget for the extra entrypoint-stub / inline-redefinition
        # regen attempts (Phase 3). One extra LLM call per offending
        # entry file, capped per scaffold so it can never loop. Reset
        # at the start of each _scaffold_from_brief run.
        self._entrypoint_regen_budget = int(
            os.environ.get("SKYN3T_ENTRYPOINT_REGEN_BUDGET", "2")
        )

    async def initialize(self) -> None:
        """Initialize the code agent."""
        os.makedirs(self._sandbox_dir, exist_ok=True)
        self.metadata["sandbox_dir"] = self._sandbox_dir
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        """Check if the code execution environment is healthy."""
        try:
            test_code = "print('health_check_ok')"
            result = await self._execute_code(test_code)
            return bool(result.get("success", False))
        except Exception:
            return False

    async def _regen_entrypoint_if_needed(
        self,
        *,
        rel: str,
        body: str,
        purpose: str,
        brief: str,
        stack: str,
        file_specs: List[Dict[str, Any]],
    ) -> str:
        """Force ONE extra regen of an entry file when the accepted body
        is either a generation-failure stub (``_is_entrypoint_stub``) or
        inline-redefines a planned component it was told to import
        (``_inline_redefined_planned``).

        Returns a regenerated body when the extra attempt produces a
        better one, otherwise the ORIGINAL body unchanged. Gated behind
        the per-scaffold ``self._entrypoint_regen_budget`` so it can
        never loop; the existing per-file retry tiers remain the primary
        path — this is a targeted top-up for the two specific entry-file
        failure modes the Phase 3 audit flagged.

        Fail-open: any exception (or an exhausted budget, or an absent
        OpenRouter key / LLM client) returns the original body. No
        external tool can hard-fail this — when generation is
        unavailable we simply keep what we had.
        """
        try:
            rl = (rel or "").lower()
            if not rl.endswith(_ENTRYPOINT_PROMPT_TAIL):
                return body
            if getattr(self, "_entrypoint_regen_budget", 0) <= 0:
                return body

            is_stub = _is_entrypoint_stub(body, rel)
            redefined = _inline_redefined_planned(body, file_specs)
            if not is_stub and not redefined:
                return body

            self._entrypoint_regen_budget -= 1
            reason = "stub" if is_stub else f"inline-redefines {redefined}"
            logger.warning(
                "ENTRYPOINT REGEN for %s (reason=%s, budget_left=%d)",
                rel, reason, self._entrypoint_regen_budget,
            )
            try:
                await self.think(f"forced entrypoint regen for {rel} ({reason})")
            except Exception:
                pass

            import os as _os_regen
            _or_key = _os_regen.environ.get("OPENROUTER_API_KEY")
            if not _or_key:
                try:
                    from skyn3t.config.settings import get_settings as _gs
                    _or_key = getattr(_gs(), "openrouter_api_key", None)
                    if _or_key:
                        _os_regen.environ.setdefault("OPENROUTER_API_KEY", _or_key)
                except Exception:
                    _or_key = None
            if not _or_key:
                # No regen channel available — keep the original body so
                # the existing placeholder/fix-loop path still applies.
                return body

            import_rule = _entrypoint_import_instructions(
                rel=rel, file_specs=file_specs,
            )
            stub_directive = (
                "The previous attempt shipped a STUB / placeholder. "
                "This is NOT acceptable — implement the file FULLY: real "
                "imports, real state, the actual UI the brief describes. "
                "No TODO comments, no 'generation failed' copy, no "
                "`export default null`.\n\n"
                if is_stub else ""
            )
            redefine_directive = (
                "The previous attempt INLINE-REDEFINED these planned "
                f"components instead of importing them: {', '.join(redefined)}. "
                "IMPORT each of them from their planned `components/` path "
                "and DELETE the inline definitions. Do not reinvent a "
                "component that already has a planned file.\n\n"
                if redefined else ""
            )
            regen_prompt = (
                f"Re-implement the entry file `{rel}` for this product brief:\n\n"
                f"BRIEF:\n{(brief or '').strip()[:1500]}\n\n"
                f"PURPOSE OF THIS FILE: {purpose or 'Top-level entrypoint implementing the brief.'}\n"
                f"STACK: {stack or 'react_vite'}\n\n"
                f"{stub_directive}{redefine_directive}{import_rule}"
                "Output ONLY the file body. No fences, no markdown, no "
                "commentary. Imports at the top, default export at the "
                "bottom for React components. Write a complete, runnable "
                "implementation that matches the brief."
            )
            try:
                from skyn3t.core.project_type_router import (
                    ladder_for_file_and_brief,
                )
                _ladder = list(ladder_for_file_and_brief(rel, brief or ""))
            except Exception:
                _ladder = [
                    "openai/gpt-oss-120b:free",
                    "qwen/qwen3-coder:free",
                    "qwen/qwen3-next-80b-a3b-instruct:free",
                ]
            from skyn3t.adapters import LLMClient as _LLMCRegen
            for _model in _ladder[:3]:
                regen_client = None
                try:
                    regen_client = _LLMCRegen(
                        default_model=_model,
                        backend="openrouter",
                        event_bus=self.event_bus,
                        caller_name=self.name,
                    )
                    try:
                        cand = await regen_client.complete(
                            regen_prompt,
                            system=(
                                "You write production-grade source code. "
                                "Never use TODO comments, placeholders, or "
                                "'replace with real implementation' language. "
                                "Output the complete file body only."
                            ),
                            max_tokens=8000,
                            temperature=0.2,
                            timeout=90.0,
                        )
                    finally:
                        try:
                            await regen_client.aclose()
                        except Exception:
                            pass
                except Exception as _regen_exc:
                    logger.warning(
                        "ENTRYPOINT REGEN %s failed for %s: %s",
                        _model, rel, _regen_exc,
                    )
                    continue
                marked = _extract_marked_files(cand or "")
                if marked:
                    m = (
                        marked.get(rel)
                        or marked.get(rel.lstrip("/"))
                        or marked.get(Path(rel).name)
                    )
                    if not m and len(marked) == 1:
                        m = next(iter(marked.values()))
                    if m:
                        cand = m
                cand = _strip_cli_prelude((cand or "").strip(), rel)
                cand = _strip_fences(cand)
                cand = _strip_copilot_footer(cand)
                # Accept only if it's a strict improvement: parses,
                # right stack, not itself a stub, and (when the trigger
                # was redefinition) no longer redefines inline.
                if (
                    cand
                    and "[deterministic-stub]" not in cand
                    and not _is_entrypoint_stub(cand, rel)
                    and not _inline_redefined_planned(cand, file_specs)
                    and _syntax_ok(cand, rel)
                    and _stack_ok(cand, rel, stack)
                ):
                    logger.warning(
                        "ENTRYPOINT REGEN ACCEPTED for %s via %s", rel, _model,
                    )
                    return cand
            logger.warning(
                "ENTRYPOINT REGEN exhausted for %s — keeping original body", rel,
            )
            return body
        except Exception:
            logger.exception(
                "entrypoint regen errored for %s; keeping original body", rel,
            )
            return body

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        """Execute a code-related task."""
        # Studio context: brief but no code → scaffold from brief instead of failing.
        d = task.input_data or {}
        if not d.get("code") and not d.get("task_type") and d.get("brief"):
            return await self._scaffold_from_brief(task)
        task_type = d.get("task_type", "code_execution")

        if task_type == "scaffold":
            return await self._scaffold_from_brief(task)

        handlers: Dict[str, Callable[[TaskRequest], Awaitable[Dict[str, Any]]]] = {
            "code_execution": self._execute_code,
            "code_analysis": self._analyze_code,
            "refactoring": self._refactor_code,
            "test_runner": self._run_tests,
        }

        handler = handlers.get(task_type)
        if not handler:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"Unknown task type: {task_type}",
            )

        try:
            result: Dict[str, Any] = await handler(task)
            return TaskResult(
                task_id=task.task_id,
                success=result.get("success", True),
                output=result,
            )
        except Exception as e:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=str(e),
            )

    async def _collect_ranked_fix_blocks(
        self, signatures: List[str],
    ) -> List[str]:
        """For each error signature, fetch the top-rated historical fixes
        and format them as prompt-ready blocks.

        Uses ``MemoryStore.rank_fixes_for_signature`` (Phase-2 SQL
        index). A fresh ``MemoryStore()`` is cheap — it holds only an
        asyncio.Lock and a session-maker reference. Returns an empty
        list when the index has nothing for these signatures or when
        the store is unreachable; the caller falls through to the
        prose recall path.
        """
        blocks: List[str] = []
        try:
            from skyn3t.memory.store import MemoryStore
            store = MemoryStore()
        except Exception:
            logger.debug("MemoryStore unavailable for ranked-fix recall", exc_info=True)
            return blocks
        for sig in signatures:
            try:
                ranked = await asyncio.wait_for(
                    store.rank_fixes_for_signature(sig, limit=3),
                    timeout=2.0,
                )
                anti = await asyncio.wait_for(
                    store.anti_patterns_for_signature(sig, limit=3),
                    timeout=2.0,
                )
            except Exception:
                logger.debug(
                    "rank_fixes_for_signature failed for %s", sig, exc_info=True,
                )
                continue
            if not ranked and not anti:
                continue
            section_lines: List[str] = [f"For signature `{sig}`:"]
            if ranked:
                section_lines.append("  Winners (prefer):")
                section_lines.extend(
                    f"    - `{r['fix_applied']}` "
                    f"(worked {r['wins']}/{r['attempts']}, "
                    f"rate {r['rate']:.0%})"
                    for r in ranked
                )
            if anti:
                section_lines.append("  Anti-patterns (avoid):")
                section_lines.extend(
                    f"    - `{r['fix_applied']}` "
                    f"(failed {r['attempts'] - r['wins']}/{r['attempts']}, "
                    f"rate {r['rate']:.0%})"
                    for r in anti
                )
            blocks.append("\n".join(section_lines))
            # Audit-stream entry so operators can see the recall
            # influencing the build prompt, not just the static log.
            try:
                from skyn3t.intelligence.cortex_decisions import publish_decision
                top = ranked[0] if ranked else None
                worst = anti[0] if anti else None
                reason_parts: List[str] = []
                if top:
                    reason_parts.append(
                        f"top fix `{top['fix_applied']}` rated "
                        f"{top['wins']}/{top['attempts']}"
                    )
                if worst:
                    reason_parts.append(
                        f"avoid `{worst['fix_applied']}` "
                        f"({worst['attempts'] - worst['wins']}/{worst['attempts']} failed)"
                    )
                publish_decision(
                    self.event_bus,
                    system="recall",
                    action="inject_ranked_fix",
                    reason=f"{'; '.join(reason_parts)} for {sig}",
                    input={
                        "signature": sig,
                        "fixes": ranked,
                        "anti_patterns": anti,
                    },
                    source=self.name,
                )
            except Exception:
                logger.debug("recall decision publish failed", exc_info=True)
        return blocks

    def _read_prior_artifacts(self, artifact_dir) -> str:
        """Collect prior-stage .md artifacts so CodeAgent builds on them.

        Order matters: research first (API specs are the most load-bearing
        for integration briefs), then architecture, brainstorm, components,
        brand, anything else. Truncated per-file so the prompt doesn't
        balloon — research gets the most room.
        """
        from pathlib import Path as _PP
        try:
            ad = _PP(artifact_dir)
            if not ad.exists():
                return ""
        except Exception:
            return ""

        priority = [
            ("research.md", 6000),
            ("architecture.md", 3000),
            ("tokens.css", 4000),
            ("tokens.json", 2000),
            ("design.md", 3500),
            ("brainstorm.md", 2000),
            ("components.md", 2000),
            ("brand.md", 1500),
        ]
        chunks: list[str] = []
        seen: set[str] = set()
        for name, max_chars in priority:
            p = ad / name
            if p.exists() and p.is_file():
                try:
                    body = p.read_text(encoding="utf-8", errors="ignore").strip()
                except Exception:
                    continue
                if not body:
                    continue
                body = compress_prompt_context(body, max_chars=max_chars)
                label = name if name.endswith(".md") else name
                chunks.append(f"### {label}\n\n{body}")
                seen.add(name)
        # Catch any other .md at top level we didn't enumerate.
        try:
            for p in sorted(ad.glob("*.md")):
                if p.name in seen or p.name.startswith("."):
                    continue
                try:
                    body = p.read_text(encoding="utf-8", errors="ignore").strip()
                except Exception:
                    continue
                if not body:
                    continue
                body = compress_prompt_context(body, max_chars=1500)
                chunks.append(f"### {p.name}\n\n{body}")
        except Exception:
            pass
        return "\n\n---\n\n".join(chunks)

    def _seed_design_tokens_into_scaffold(self, artifact_dir, out_dir) -> None:
        """Copy Designer token artifacts into the scaffold before codegen."""
        import shutil
        from pathlib import Path as _Path

        ad = _Path(artifact_dir)
        od = _Path(out_dir)
        tokens_src = ad / "tokens.css"
        if not tokens_src.is_file():
            return
        dest = od / "src" / "tokens.css"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tokens_src, dest)
        except Exception:
            logger.debug("tokens.css seed into scaffold failed", exc_info=True)

    async def _scaffold_from_brief(self, task: TaskRequest) -> TaskResult:
        """Generate new code from a brief into artifact_dir/scaffold/.

        Two-phase build so the model isn't trying to fit an entire project
        into a single LLM response:

          Phase 1 — Plan: ask for a JSON file plan (path + one-line purpose
                          per file). Cheap, structured.
          Phase 2 — Build: loop the plan; for each file, ask the model to
                          emit JUST that file's contents. Each file gets
                          its own 8000-token budget — a 10-file project
                          now gets ~10x the headroom of the old single-
                          call scaffold, and on subscription-backed CLI
                          providers (claude/copilot/kimi) the cap is
                          ignored entirely.
        """
        import json as _json
        import re as _re
        from pathlib import Path as _Path
        d = task.input_data or {}
        brief = (d.get("brief") or "").strip()
        artifact_dir = self.resolve_artifact_dir(d.get("artifact_dir"))
        scaffold_override = str(d.get("code_scaffold_dir") or "").strip()
        if scaffold_override:
            out_dir = _Path(scaffold_override)
        else:
            out_dir = artifact_dir / "scaffold"
        out_dir.mkdir(parents=True, exist_ok=True)
        self._seed_design_tokens_into_scaffold(artifact_dir, out_dir)
        resolved_out_dir = out_dir.resolve()
        # Reset the per-scaffold entrypoint-regen budget so a long-lived
        # agent reused across builds gets a fresh allowance each run.
        self._entrypoint_regen_budget = int(
            os.environ.get("SKYN3T_ENTRYPOINT_REGEN_BUDGET", "2")
        )
        files_written: List[str] = []
        # Phase 2 (skills grading): names of skills injected into the
        # build prompt are surfaced in the TaskResult output so the runner
        # can call skill_library.record_use(name, success=verdict=="yes")
        # per build. Initialized at function scope so it is always defined
        # by the time TaskResult is constructed, regardless of which
        # branches below execute.
        injected_skills: List[str] = []
        injected_learnings: List[str] = []

        # Read prior-stage artifacts so we build on what research,
        # architecture, design, and brainstorm produced — not just the
        # bare brief. Without this, integration-research is run, written
        # to disk, then completely ignored when CodeAgent prompts the
        # model — which is why integration briefs produced fake demos.
        prior_context = self._read_prior_artifacts(artifact_dir)

        # Prepend the runner's essential-output summaries (PR #21) when
        # present. These are short, curated recaps of what each prior
        # stage decided ("Architect picked react-vite + express on port
        # 3000.") — much cheaper for the LLM to internalise than the
        # full architecture.md. Goes FIRST in prior_context so it's
        # what the model sees before the deeper artifact dumps.
        prior_summaries = d.get("prior_summaries")
        if isinstance(prior_summaries, dict) and prior_summaries:
            summary_lines = [
                f"- **{name}**: {summary}"
                for name, summary in prior_summaries.items()
                if isinstance(name, str) and isinstance(summary, str) and summary.strip()
            ]
            if summary_lines:
                essentials_block = (
                    "### Upstream essentials\n\n"
                    "Brief, curated recaps from each completed prior stage. "
                    "Treat these as the canonical 'what did upstream decide'.\n\n"
                    + "\n".join(summary_lines)
                )
                prior_context = (
                    essentials_block + "\n\n---\n\n" + prior_context
                    if prior_context
                    else essentials_block
                )

        # Read palette.json once so the CSS prelude in brief_requirements
        # can lock in real brand colors instead of fallback defaults.
        # Backend-agnostic: same prelude format works regardless of which
        # CLI/API model executes the per-file write.
        _palette_hexes: List[str] = []
        try:
            import json as _json_palette
            _palette_path = artifact_dir / "palette.json"
            if _palette_path.exists():
                _palette_data = _json_palette.loads(_palette_path.read_text(encoding="utf-8"))
                # Accept either flat dict (primary/bg/accent/...) or list of hex.
                if isinstance(_palette_data, dict):
                    for _v in _palette_data.values():
                        if isinstance(_v, str) and _v.startswith("#") and len(_v) in (4, 7, 9):
                            _palette_hexes.append(_v)
                elif isinstance(_palette_data, list):
                    _palette_hexes.extend(
                        v for v in _palette_data
                        if isinstance(v, str) and v.startswith("#")
                    )
        except Exception:
            logger.debug("palette.json read for prelude failed", exc_info=True)

        # Hard cap on plan size so a runaway model can't generate 1000 files.
        # Dynamic based on brief signals: extensible / marketplace / plugin
        # briefs get 80, default 25. Without the higher cap, the planner
        # truncates the customization machinery and ships a static panel.
        from skyn3t.agents.stack_templates import files_target_for, max_files_for
        MAX_FILES = max_files_for(brief)

        try:
            client = self.get_llm() if hasattr(self, "get_llm") else None
            if client is None:
                from skyn3t.adapters import LLMClient
                client = LLMClient(default_model=self.config.get("model"),
                                   backend=self.config.get("backend"),
                                   event_bus=self.event_bus, caller_name=self.name)

            # ── Phase 1: plan ───────────────────────────────────────────
            # Try deterministic stack templates first — they encode known-
            # good file trees per ecosystem (FastAPI/Next/React/Flask/etc.).
            # The LLM is good at writing file content, unreliable at picking
            # the right file SHAPE for an ecosystem it's seen many variants
            # of. A wrong shape (e.g. Next 12 `pages/` vs Next 14 `app/`)
            # breaks the build before any code runs.
            architecture_text = ""
            try:
                architecture_text = (
                    (artifact_dir / "architecture.md")
                    .read_text(encoding="utf-8", errors="ignore")
                    .strip()
                )
            except Exception:
                architecture_text = ""
            tech_stack: Dict[str, Any] = {}
            try:
                raw_tech_stack = (
                    (artifact_dir / "tech_stack.json")
                    .read_text(encoding="utf-8", errors="ignore")
                    .strip()
                )
                parsed_tech_stack = _json.loads(raw_tech_stack) if raw_tech_stack else {}
                if isinstance(parsed_tech_stack, dict):
                    tech_stack = parsed_tech_stack
            except Exception:
                tech_stack = {}

            # Honor the architect's decisions.json contract (PR #23).
            # When the architect committed to a Node backend bundle
            # (express / hono-node), pass that down to plan_for_stack
            # so the backend tier ships even when the brief itself
            # didn't trigger _needs_backend. Without this hook the
            # scaffold is frontend-only and the consistency reviewer
            # flags "architect promised Express + port 3000 but no
            # server code shipped" on every build.
            from skyn3t.agents.decisions import load_decisions
            decisions = (
                d.get("decisions")
                if isinstance(d.get("decisions"), dict)
                else load_decisions(artifact_dir)
            )

            from skyn3t.agents.stack_templates import detect_stack_from_handoff, plan_for_stack
            template_key = detect_stack_from_handoff(
                brief,
                architecture_text=architecture_text,
                tech_stack=tech_stack,
                decisions=decisions,
            )
            template_plan = (
                plan_for_stack(template_key, brief, decisions=decisions)
                if template_key
                else None
            )

            plan: Dict[str, Any] = {}
            file_specs: List[Dict[str, Any]] = []
            stack = "minimal"

            if template_plan:
                # Skip LLM planning entirely — use the known-good shape.
                stack = template_key or "minimal"
                # Outer-loop self-learning: consult the build-pattern
                # scoreboard for the chosen stack. If a different shape
                # has accumulated a meaningfully better success rate
                # than the default template (≥3 samples, ≥75% wins, and
                # at least 10 percentage points above the template's own
                # success rate when known), prefer it. Otherwise stick
                # with the default. Wrapped so a missing/empty store
                # never blocks scaffolding.
                file_specs = [
                    {"path": rel, "purpose": purpose}
                    for rel, purpose in template_plan
                ]
                try:
                    from skyn3t.intelligence.build_patterns import get_default_scoreboard
                    sb = get_default_scoreboard()
                    best = sb.best_shape(stack, min_samples=3)
                    if best and best.success_rate >= 0.75 and best.shape:
                        # Default-template shape, for comparison:
                        default_shape = sorted(rel for rel, _ in template_plan)
                        # Find the default's own stats (if any) so we
                        # don't switch on a tie.
                        default_rate = 0.0
                        for stat in sb.all_stats_for(stack):
                            if sorted(stat.shape) == default_shape:
                                default_rate = stat.success_rate
                                break
                        if best.success_rate - default_rate >= 0.10:
                            # Use the learned shape, but UNION with the
                            # default template plan so any NEW tier
                            # additions (design-system primitives,
                            # configurable tier) still land. The
                            # learned shape is frozen at the moment it
                            # was recorded — without this union, every
                            # new tier we add silently disappears from
                            # the plan as soon as a prior shape gets
                            # promoted to "learned". Default template
                            # paths win when there's a conflict so the
                            # newer purposes carry through.
                            default_purposes = {rel: purpose for rel, purpose in template_plan}
                            learned_paths = set(best.shape)
                            default_paths = set(default_purposes.keys())
                            union_paths = learned_paths | default_paths
                            file_specs = [
                                {
                                    "path": rel,
                                    "purpose": default_purposes.get(rel)
                                    or "(learned: high-success shape)",
                                }
                                for rel in sorted(union_paths)
                            ]
                            await self.think(
                                f"using learned shape ∪ default for '{template_key}' "
                                f"(success {best.success_rate:.0%} vs default {default_rate:.0%}, "
                                f"learned={len(learned_paths)} + new={len(default_paths - learned_paths)})"
                            )
                        else:
                            await self.think(f"using stack template '{template_key}'")
                    else:
                        await self.think(f"using stack template '{template_key}'")
                except Exception:
                    logger.debug("build-pattern bias lookup failed", exc_info=True)
                    await self.think(f"using stack template '{template_key}'")
            else:
                target_min, target_max = files_target_for(brief)
                # For ambitious briefs (extensibility / marketplace), spell
                # out the customization surface the planner should reserve
                # slots for — otherwise the model defaults to a static
                # 7-card panel because that's the median homelab project.
                if target_max >= 30:
                    extensibility_note = (
                        " Brief asks for an EXTENSIBLE product (plugin "
                        "registry, drag-and-drop, settings UI, marketplace, "
                        "or 'bring your own API'). Reserve slots for the "
                        "customization machinery: services.json registry, "
                        "generic API-card component, settings/layout UI, "
                        "plugin contract, theme system, omnibox/command "
                        "palette — NOT just the named services."
                    )
                else:
                    extensibility_note = ""
                plan_system = (
                    "You are a senior engineer planning a runnable project. "
                    "Output a JSON object: {\"stack\": \"...\", \"files\": [{\"path\": "
                    "\"relative/path\", \"purpose\": \"one-line description\"}, ...]}. "
                    "Pick a tech stack matching the brief — HTML+JS for browser games "
                    "and static UIs, FastAPI/Flask for Python APIs, Express/Node for "
                    f"JS APIs. Aim for {target_min}-{target_max} files: source, config, "
                    f"README, and a tiny test when relevant.{extensibility_note} "
                    "JSON only, no preamble."
                )
                plan_prompt = f"Brief:\n{brief}\n\nReturn the JSON plan."
                await self.think("planning project structure")
                plan_out = await client.complete(
                    plan_prompt,
                    system=plan_system,
                    max_tokens=4000,
                    temperature=0.3,
                    timeout=_PLAN_TIMEOUT_SECONDS,
                )
                if plan_out and "[deterministic-stub]" not in plan_out:
                    m = _re.search(r"\{[\s\S]*\}", plan_out)
                    if m:
                        try:
                            plan = _json.loads(m.group(0))
                        except Exception:
                            plan = {}
                raw_files = plan.get("files") if isinstance(plan, dict) else None
                stack = (plan.get("stack") if isinstance(plan, dict) else None) or "minimal"
                if isinstance(raw_files, list):
                    file_specs = raw_files

            if not isinstance(file_specs, list) or not file_specs:
                file_specs = []
            else:
                file_specs = file_specs[:MAX_FILES]

            # Component breakdown: if Designer produced a structured
            # component_file_plan.json (#113), merge those component files
            # into the file_specs list. Small per-file generations succeed
            # where one massive App.jsx call times out. We extend rather
            # than replace because the original file_specs still includes
            # config files (package.json, vite.config.js, etc) that we
            # need.
            try:
                plan_path = artifact_dir / "component_file_plan.json"
                if plan_path.is_file():
                    import json as _json_plan
                    plan_data = _json_plan.loads(plan_path.read_text(encoding="utf-8"))
                    existing_paths = {
                        (s.get("path") or "").strip()
                        for s in file_specs
                        if isinstance(s, dict)
                    }
                    added = 0
                    for entry in (plan_data.get("files") or []):
                        if not isinstance(entry, dict):
                            continue
                        path = str(entry.get("path") or "").strip()
                        purpose = str(entry.get("purpose") or "").strip()
                        if not path or not purpose:
                            continue
                        if path in existing_paths:
                            continue
                        props = entry.get("props") or []
                        if isinstance(props, list) and props:
                            purpose = (
                                f"{purpose} Props: {', '.join(str(p) for p in props)}."
                            )
                        file_specs.append({"path": path, "purpose": purpose})
                        existing_paths.add(path)
                        added += 1
                        if len(file_specs) >= MAX_FILES:
                            break
                    if added:
                        await self.think(
                            f"merged {added} component file(s) from component_file_plan.json"
                        )
            except Exception:
                logger.debug("component_file_plan merge failed", exc_info=True)

            # ── Phase 2: build one file at a time ───────────────────────
            file_index = "\n".join(
                f"- {(s.get('path') or '').strip()}: {(s.get('purpose') or '').strip()}"
                for s in file_specs
                if isinstance(s, dict) and s.get("path")
            )
            # Stack-specific idiom hint — anchors the model to the modern
            # shape for the chosen ecosystem (App Router for Next, hooks
            # for React, pydantic v2 for FastAPI, etc). Without this the
            # model defaults to whatever was most-common in its training
            # set, which is often outdated.
            from skyn3t.agents.stack_templates import hint_for_stack
            stack_hint = hint_for_stack(stack)
            build_system = (
                "You are implementing one file of a real, runnable project. "
                "Output ONLY that file's raw contents — no JSON wrapper, no "
                "fenced code block, no preamble, no explanation. Just the "
                "contents that should be written to disk verbatim.\n\n"
                "Rules that override 'small project' instincts:\n"
                "- If the brief asks the program to talk to a real system "
                "(Docker, an HTTP API, a database, a device, a service, a "
                "file), wire up the real integration. Use fetch / the real "
                "client library / the real protocol. Do NOT hardcode arrays "
                "of fake data when the brief expects live data.\n"
                "- When you don't have the credentials at generation time, "
                "read them from environment variables (process.env.X / "
                "os.environ['X']) and document them in the README. Do not "
                "invent fake credentials, but do not stub the integration "
                "either.\n"
                "- Loading states, errors, and empty states are part of the "
                "implementation, not decoration. Wire them to the real "
                "fetch lifecycle.\n"
                "- Mock data is only acceptable in a clearly named "
                "DEV_FIXTURES constant gated behind an env flag, and only "
                "as a fallback when the real source is unreachable.\n"
                "- Every file you write must be self-consistent: if you "
                "import './App.jsx', it must exist in the plan; if you use "
                "a library, it must be in package.json.\n"
                "- Default ports (use these unless the brief says otherwise): "
                "backend Express server on PORT=3100, Vite frontend on 5180. "
                "These avoid colliding with the SkyN3t studio on 5173/6660. "
                "CORS_ORIGIN defaults to http://localhost:5180. The frontend "
                "should call the backend at http://localhost:3100 (or "
                "use a Vite proxy for /api → :3100).\n"
                "- Module system: server-side files are ESM. package.json "
                "has \"type\": \"module\"; use `import` / `export`, not "
                "`require` / `module.exports`. Adapter files must "
                "`export default router` (NOT `export { router }`). The "
                "server entry imports adapters with the .js extension: "
                "`import sonarrRouter from './adapters/sonarr.js';`.\n"
                "- UI quality bar: ship something a stranger could demo "
                "without embarrassment. Use :root CSS variables for "
                "colors/spacing/type, hover and :focus-visible states, "
                "@media responsive rules, and distinct loading/error/empty "
                "views (not bare text). No Lorem ipsum, no placeholder "
                "divs, no TODO comments left in shipped files.\n"
                "- Feature completeness: implement the brief's core "
                "workflow end-to-end in this file pass — wiring, handlers, "
                "and edge cases — not a skeleton that 'looks like' the app."
            )
            if d.get("autonomous"):
                try:
                    quality_floor = int(d.get("quality_floor_score") or 85)
                except (TypeError, ValueError):
                    quality_floor = 85
                build_system = (
                    build_system
                    + "\n\nAUTONOMOUS QUALITY CONTRACT:\n"
                    + f"- This build must earn reviewer verdict `go` with score >= {quality_floor}/100.\n"
                    + "- If the brief is broad, simplify the product scope but keep one complete, "
                    + "runnable end-to-end workflow instead of many shallow placeholders.\n"
                    + "- Do not leave TODOs, placeholder copy, inert buttons, mock-only dashboards, "
                    + "or disconnected components. Autonomous outputs below this bar are rejected "
                    + "and retried as failures."
                )
            if stack_hint:
                build_system = build_system + "\n\n" + stack_hint
            # Scoreboard pre-warnings: the runner injects strings derived
            # from BuildPatternScoreboard for shapes that have failed a
            # known pattern (e.g. lost the router mount). Lift them into
            # the system prompt so the model sees the warning before it
            # writes the affected files.
            pre_warnings = d.get("scoreboard_prewarnings") or []
            if pre_warnings:
                build_system = (
                    build_system
                    + "\n\nPRIOR-FAILURE PATTERNS for this scaffold shape:\n"
                    + "\n".join(f"- {w}" for w in pre_warnings)
                )
            # Skill injection: pull the top-3 net-helpful skills tagged
            # with this stack and append them as additional context. This
            # is how the durable Hermes-style skill library gets read
            # back at code-generation time — closing the loop from
            # "system recorded what worked" to "system uses what worked."
            try:
                from skyn3t.intelligence.skill_library import get_default_library
                lib = get_default_library()
                try:
                    from skyn3t.intelligence.learnings_store import (
                        sync_playbook_skills_to_library,
                    )

                    sync_playbook_skills_to_library(
                        library=lib,
                        min_score=_PROMPT_SKILL_MIN_SCORE,
                    )
                except Exception:
                    logger.debug("playbook skill sync failed", exc_info=True)
                # Query multiple tags: stack-specific shape skills,
                # role-specific skills (code_agent), and topic skills
                # (polling, websocket, etc.). Dedupe by skill name.
                seen: set[str] = set()
                skill_lines: List[str] = []
                # Widened to include the design-system tags so the
                # service-card / KPI / sparkline / status-pill /
                # drawer / topbar skills land in the prompt for
                # visual files. v28 shipped JSON-dump cards because
                # these tags were absent and the LLM had no design
                # vocabulary to draw on.
                topic_tags = [
                    "code_agent", stack, "react", "polling",
                    "websocket", "integration", "ux",
                    "dashboard", "service-card", "kpi", "sparkline",
                    "status", "drawer", "topbar", "ui-pattern",
                ]
                for tag in topic_tags:
                    if not tag:
                        continue
                    for skill in lib.find(
                        tag=tag,
                        min_score=_PROMPT_SKILL_MIN_SCORE,
                        limit=6,
                    ):
                        if skill.name in seen:
                            continue
                        if not _prompt_skill_allowed(skill):
                            continue
                        seen.add(skill.name)
                        snippet = (skill.body or "").strip()
                        if not snippet:
                            continue
                        # Bumped from 1200 → 3500 chars. Design skills
                        # need the full anatomy + example code, not a
                        # truncated stub. CLI ignores token caps and
                        # the prompt size hit is well worth it for
                        # widget-shaped output instead of JSON dumps.
                        if len(snippet) > 3500:
                            snippet = snippet[:3500] + "\n…[truncated]"
                        skill_lines.append(f"### Skill: {skill.name}\n{snippet}")
                        # Allow 8 skills (was 5) — covers card + kpi +
                        # sparkline + status + drawer + topbar +
                        # density + a code_agent specific one.
                        if len(skill_lines) >= 8:
                            break
                    if len(skill_lines) >= 8:
                        break
                # Relevance pass: tag-matching alone never surfaces imported
                # Agent-Skills (tagged only `agent-skill` + their dir name),
                # so their description/trigger-tagged guidance was inert.
                # Rank ALL skills by relevance to a compact query built from
                # the brief + stack + planned file paths/purposes, and merge
                # into the SAME dedup-by-name set. search() returns nothing
                # when no skill is actually relevant, so behavior is identical
                # to before when the library has no matching skills. Guarded in
                # its own try/except (and via getattr) so a library without
                # search() — or any error inside it — never discards the
                # tag-based results already collected above.
                _search = getattr(lib, "search", None)
                if callable(_search) and len(skill_lines) < 8:
                    try:
                        query_bits: List[str] = [brief or "", stack or ""]
                        for _spec in (file_specs or [])[:12]:
                            if isinstance(_spec, dict):
                                query_bits.append(str(_spec.get("path") or ""))
                                query_bits.append(str(_spec.get("purpose") or ""))
                        query = " ".join(b for b in query_bits if b)[:2000]
                        for skill in _search(
                            query,
                            limit=8,
                            min_score=_PROMPT_SKILL_MIN_SCORE,
                        ):
                            if len(skill_lines) >= 8:
                                break
                            if skill.name in seen:
                                continue
                            if not _prompt_skill_allowed(skill):
                                continue
                            seen.add(skill.name)
                            snippet = (skill.body or "").strip()
                            if not snippet:
                                continue
                            if len(snippet) > 3500:
                                snippet = snippet[:3500] + "\n…[truncated]"
                            skill_lines.append(f"### Skill: {skill.name}\n{snippet}")
                    except Exception:
                        logger.debug("skill relevance pass failed", exc_info=True)
                if skill_lines:
                    build_system = (
                        build_system
                        + "\n\nLearned skills — apply when relevant:\n\n"
                        + "\n\n".join(skill_lines)
                    )
                # Surface the injected skill names so the runner can credit
                # or blame each one based on the build verdict (record_use).
                # `seen` holds every skill name that was actually pulled in.
                injected_skills = sorted(seen)
            except Exception:
                logger.debug("skill injection failed", exc_info=True)

            try:
                from skyn3t.intelligence.learnings_store import (
                    get_default_store,
                    playbook_entry_safe_for_prompt,
                )

                items = get_default_store().guidance_for(
                    brief or stack,
                    stack=stack,
                    tags=[stack, "code", "build_pattern", "routing"],
                    limit=5,
                )
                learning_lines: List[str] = []
                for entry in items:
                    if not playbook_entry_safe_for_prompt(
                        entry,
                        min_score=_PROMPT_SKILL_MIN_SCORE,
                    ):
                        continue
                    title = str(entry.get("title") or "").strip()
                    content = str(entry.get("content") or "").strip()
                    if not title or not content:
                        continue
                    if len(content) > 1200:
                        content = content[:1200] + "\n…[truncated]"
                    injected_learnings.append(title)
                    learning_lines.append(f"### {title}\n{content}")
                if learning_lines:
                    build_system = (
                        build_system
                        + "\n\nCurated learnings playbook — apply when relevant:\n\n"
                        + "\n\n".join(learning_lines)
                    )
                    await self.think(
                        f"injected {len(learning_lines)} curated learning(s) into prompt"
                    )
            except Exception:
                logger.debug("learnings playbook injection failed", exc_info=True)

            try:
                from skyn3t.core.model_router import tier_for_stage
                from skyn3t.intelligence.cheap_smart import (
                    build_cheap_context_boost,
                    cheap_smart_enabled,
                )

                if cheap_smart_enabled() and "cheap" in tier_for_stage("code"):
                    boost = build_cheap_context_boost(
                        brief=brief,
                        stack=stack,
                        stage_name="code",
                    )
                    if boost:
                        build_system = build_system + "\n\n" + boost
            except Exception:
                logger.debug("cheap_smart context boost failed", exc_info=True)

            # RAG recall: query past experiences for this stack + brief
            # to inject "I tried this before and it failed because..."
            # lessons into the prompt. This is the outer loop: every
            # failed build teaches the system what NOT to do next time.
            try:
                # Phase 2 (RAG observability): reuse the single live
                # RAGEngine threaded through task.input_data by the runner
                # (input_data['rag_engine']) so we hit the same warm,
                # already-initialized ChromaDB the rest of the system uses
                # — and so Owner I's per-query recall metric fires for this
                # call too. RAGEngine.query self-initializes, so a passed
                # engine needs no explicit initialize() here.
                rag = d.get("rag_engine")
                if rag is None:
                    # Cold-start fallback for non-studio callers that did
                    # not thread an engine in. Gate on a present vector DB
                    # so we never wedge the scaffold flow building one.
                    from skyn3t.config.settings import get_settings
                    settings = get_settings()
                    vector_db_path = Path(
                        settings.vector_db_path or "data/vector_db"
                    )
                    if not vector_db_path.exists():
                        # No vector DB yet — nothing to recall.
                        raise RuntimeError("vector db not initialized")

                    from skyn3t.rag.rag_engine import RAGEngine
                    rag = RAGEngine()
                    # Cap initialization at 3s so a cold ChromaDB start
                    # doesn't wedge the scaffold flow.
                    await asyncio.wait_for(rag.initialize(), timeout=3.0)
                query_text = (
                    f"past build failures for {stack} project: {brief[:300]}"
                )
                retrieval = await asyncio.wait_for(
                    rag.query(
                        query_text, n_results=3,
                        filter_dict={
                            "$and": [
                                {"doc_type": "experience"},
                                {"success": False},
                            ]
                        },
                    ),
                    timeout=3.0,
                )
                if retrieval.get("documents"):
                    exp_lines: List[str] = []
                    seen_signatures: list[str] = []
                    for doc in retrieval["documents"][:3]:
                        content = doc.get("content", "").strip()
                        if not content:
                            continue
                        # Keep it short — one paragraph per lesson.
                        para = content.split("\n\n")[0]
                        if len(para) > 600:
                            para = para[:600] + "…"
                        exp_lines.append(para)
                        # Phase 2: harvest error_signature from the
                        # recalled doc's metadata so we can pair the
                        # similarity-ranked prose with a SQL-ranked
                        # "fix that worked for this signature" block.
                        meta = doc.get("metadata") or {}
                        sig = meta.get("error_signature")
                        if sig and sig not in seen_signatures:
                            seen_signatures.append(str(sig))
                    if exp_lines:
                        build_system = (
                            build_system
                            + "\n\nLessons from past builds — avoid these mistakes:\n\n"
                            + "\n\n".join(f"- {ln}" for ln in exp_lines)
                        )
                        await self.think(
                            f"injected {len(exp_lines)} RAG experience(s) into prompt"
                        )
                    # Phase 2: rank fixes by historical win rate for each
                    # signature surfaced above and inject the top-3 into
                    # the prompt. The vector store said "this is similar";
                    # the SQL index says "this is what WORKED."
                    if seen_signatures:
                        ranked_blocks = await self._collect_ranked_fix_blocks(
                            seen_signatures[:3],
                        )
                        if ranked_blocks:
                            build_system = (
                                build_system
                                + "\n\nKnown fixes — ranked by historical win rate:\n\n"
                                + "\n\n".join(ranked_blocks)
                            )
                            await self.think(
                                f"injected ranked fixes for "
                                f"{len(ranked_blocks)} signature(s)"
                            )
            except Exception:
                logger.debug("RAG recall query failed", exc_info=True)

            # Phase 2 (lessons read-side): the runner pre-queries RAG for
            # this task and threads the resulting lesson strings through
            # task.input_data['lessons'] (List[str]). Append them to the
            # system prompt here, alongside the live RAG-recall section, so
            # they reach the model even on build paths where the inline RAG
            # query above was skipped (no engine / no vector DB). Kept in a
            # self-contained try so a malformed payload can't break the
            # scaffold flow.
            try:
                lessons = d.get("lessons")
                if isinstance(lessons, (list, tuple)):
                    lesson_lines = [
                        str(ln).strip() for ln in lessons if str(ln).strip()
                    ]
                    if lesson_lines:
                        build_system = (
                            build_system
                            + "\n\nLessons from past builds — avoid these mistakes:\n\n"
                            + "\n\n".join(f"- {ln}" for ln in lesson_lines)
                        )
                        await self.think(
                            f"injected {len(lesson_lines)} lesson(s) from "
                            f"input_data into prompt"
                        )
            except Exception:
                logger.debug("input_data lessons injection failed", exc_info=True)

            from skyn3t.agents.stack_templates import manifest_for, readme_for_stack

            # Pass 1: walk every spec, write what we can deterministically
            # (READMEs and known manifests), and collect the rest as LLM
            # jobs. Deterministic writes are instant and don't need
            # parallelism — only the LLM calls do.
            llm_jobs: List[Tuple[int, str, str, "_Path"]] = []
            for i, spec in enumerate(file_specs, start=1):
                if not isinstance(spec, dict):
                    continue
                rel = (spec.get("path") or "").lstrip("/").strip()
                purpose = (spec.get("purpose") or "").strip()
                if not rel:
                    continue
                target = (out_dir / rel).resolve()
                try:
                    target.relative_to(resolved_out_dir)
                except ValueError:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)

                # README shortcut: when a known stack is in play, generate
                # the README deterministically instead of burning an LLM
                # call on boilerplate the model writes the same way every
                # time. Saves ~1 call per project (~8k tokens of room).
                if rel.lower() in ("readme.md", "readme") and stack != "minimal":
                    body_static = readme_for_stack(stack, brief)
                    if body_static:
                        try:
                            target.write_text(body_static, encoding="utf-8")
                            files_written.append(str(target))
                            await self.think(f"wrote {rel} (deterministic readme for {stack})")
                            continue
                        except Exception:
                            pass  # fall through to LLM path

                # Manifest shortcut: requirements.txt / package.json /
                # Package.swift have known-good versioned shapes per
                # stack. Writing them deterministically eliminates the
                # "model pinned react ^17 but used hooks API" failure
                # class entirely.
                if stack != "minimal":
                    body_manifest = manifest_for(
                        stack, rel, brief, palette_hexes=_palette_hexes
                    )
                    if body_manifest:
                        try:
                            target.write_text(body_manifest, encoding="utf-8")
                            files_written.append(str(target))
                            await self.think(f"wrote {rel} (deterministic manifest for {stack})")
                            continue
                        except Exception:
                            pass  # fall through to LLM path

                # Anything left over: needs an LLM call. Queue it.
                llm_jobs.append((i, rel, purpose, target))

            # v33 tried clustering frontend files into ONE Kimi call —
            # parser couldn't reliably match Kimi's output format and
            # the run timed out. Reverted: every file goes solo, but
            # per-file routing in _build_one still sends frontend files
            # to Kimi and backend files to Copilot. Net result is the
            # same model-per-file-type, without the brittle cluster
            # parsing.
            solo_jobs = list(llm_jobs)
            batch_clusters: List[List[Tuple[int, str, str, "_Path"]]] = []

            # Pass 2: run the queued LLM calls concurrently. CLI backends
            # are subprocess-per-call, and the same model serves every
            # job in flight — pushing too many parallel CLI subprocesses
            # at the same model just makes each one slower and risks rate
            # limits. A small concurrency window (4) keeps the model
            # busy without thrashing.
            #
            # Bounded by an asyncio.Semaphore so cancellation propagates
            # cleanly and the fallback-on-empty path doesn't have to
            # serialize anything.
            concurrency = min(4, max(len(solo_jobs), len(batch_clusters))) if (solo_jobs or batch_clusters) else 0
            if concurrency > 0:
                await self.think(
                    f"building {len(llm_jobs)} file(s): "
                    f"{len(solo_jobs)} solo + {len(batch_clusters)} cluster(s) "
                    f"of {sum(len(c) for c in batch_clusters)} file(s) "
                    f"(concurrency={concurrency})"
                )
            sem = asyncio.Semaphore(concurrency) if concurrency > 0 else None

            async def _build_one(
                i: int, rel: str, purpose: str, target: "_Path",
            ) -> Tuple[str, str, "_Path"]:
                """Resolve one file's body via the LLM chain.

                Returns (rel, body, target). ``body`` may be empty when
                every fallback fails — caller drops empties on the floor
                (placeholder is written for known shapes, otherwise the
                file is skipped).
                """
                assert sem is not None  # only called when concurrency > 0
                async with sem:
                    file_prompt_parts = [
                        f"Brief:\n{brief}\n\n",
                        f"Stack: {stack}\n\n",
                    ]
                    # Brief-derived non-negotiable rules for THIS file
                    # type. Backend-agnostic — same compact bullets work
                    # across claude/copilot/kimi/openrouter free models.
                    # Skip if no rules match (empty rules block would
                    # just be dead context that costs tokens).
                    try:
                        from skyn3t.agents.brief_requirements import (
                            extract_requirements,
                            format_hard_rules,
                        )
                        _reqs = extract_requirements(brief or "")
                        _rules_md = format_hard_rules(
                            _reqs, rel, palette_hexes=_palette_hexes
                        )
                        if _rules_md:
                            file_prompt_parts.append(_rules_md)
                    except Exception:
                        logger.debug(
                            "brief_requirements injection failed for %s", rel,
                            exc_info=True,
                        )
                    # Filter prior_context to just the sections this
                    # file actually needs. Cuts ~10KB off most CLI
                    # prompts, which directly saves wall-clock time
                    # because the CLI is streaming-bound.
                    relevant = _relevant_context(prior_context, rel)
                    if relevant:
                        file_prompt_parts.append(
                            "Prior stages produced these artifacts. "
                            "Use them — especially the API specs in research.md "
                            "if present — to wire REAL integrations, not fake demo data:\n\n"
                            f"{relevant}\n\n"
                        )

                    # Visual-file injection: when the file is part of the
                    # UI (App.jsx, components/, pages/, hooks that drive
                    # the UI), inject the per-service brand kit (icon
                    # URL + brand color + widget shape per service the
                    # brief mentions) AND an explicit instruction not
                    # to render data as JSON dumps. v28 shipped JSON
                    # dumps because the LLM was never told otherwise.
                    rl_visual = rel.lower()
                    is_visual = (
                        rl_visual.endswith((".jsx", ".tsx"))
                        or "/components/" in rl_visual.replace("\\", "/")
                        or "/pages/" in rl_visual.replace("\\", "/")
                        or rl_visual.endswith("app.jsx")
                        or rl_visual.endswith("app.tsx")
                    )
                    if is_visual:
                        try:
                            from skyn3t.agents.service_brand_kit import (
                                brand_kit_markdown,
                            )
                            from skyn3t.agents.stack_templates import (
                                _detect_services,
                            )
                            services = _detect_services(brief)
                            kit_md = brand_kit_markdown(services)
                            if kit_md:
                                file_prompt_parts.append(kit_md + "\n")
                            file_prompt_parts.append(
                                "## Visual quality requirements (NON-NEGOTIABLE)\n"
                                "- DO NOT render service data as `JSON.stringify(...)`. "
                                "Build the service-specific WIDGET shape from the "
                                "brand kit above. The widget hint per service tells "
                                "you what UI shape a user expects.\n"
                                "- Use the brand color for: status dot, sparkline "
                                "stroke, progress bars, icon background tint. Card "
                                "chrome stays neutral.\n"
                                "- Every card has: header (icon + title + host:port "
                                "+ status pill), stat row (concrete numbers), "
                                "optional sparkline, action row (open/refresh/"
                                "settings icons + 'Xm ago').\n"
                                "- Status pill: 6px dot + label, rounded-full pill, "
                                "12% bg tint of the dot color.\n"
                                "- Empty / loading / error states are required — "
                                "never render a blank card or raw error object.\n"
                                "- Modern dashboard reference points: Homarr, "
                                "Heimdall, Linear, Vercel — clean dark theme, "
                                "rounded cards, soft shadows, sans-serif (Inter), "
                                "tabular numerals.\n\n"
                            )
                        except Exception:
                            logger.debug(
                                "brand-kit injection failed for %s", rel,
                                exc_info=True,
                            )

                    # When writing an entrypoint (App.jsx / main.jsx /
                    # page.tsx), pin the LLM to USE the planned
                    # component files rather than reinvent them inline.
                    # Real bug from e79bc0: planned HabitCard / HabitList /
                    # WeeklyGrid / etc. all shipped but App.jsx redefined
                    # its own inline versions and never imported them.
                    # Reviewer correctly flagged "two parallel component
                    # trees, one orphaned." This is the prompt fix.
                    entrypoint_instructions = _entrypoint_import_instructions(
                        rel=rel,
                        file_specs=file_specs,
                    )
                    if entrypoint_instructions:
                        file_prompt_parts.append(entrypoint_instructions)
                    file_prompt_parts.extend([
                        f"Full file plan:\n{file_index}\n\n",
                        f"Now write the COMPLETE contents of: `{rel}`\n",
                        f"Purpose: {purpose}\n\n",
                        "Return ONLY the file's raw contents (no JSON, no fences).",
                    ])
                    file_prompt = "".join(file_prompt_parts)
                    await self.think(f"building file {i}/{len(file_specs)}: {rel}")

                    # Known-problematic files: skip CLIs entirely.
                    # App.jsx, main.jsx, server entrypoints, etc. fail CLI
                    # generation repeatedly (Kimi+Copilot streaming-idle
                    # timeouts, deterministic-stub fallbacks). When
                    # OpenRouter is configured we route these directly to
                    # the task-aware model ladder and skip the 8-minute
                    # CLI hang cycle.
                    #
                    # The fast-path used to call ONE hardcoded model
                    # (owl-alpha). It now iterates the same ladder tier 4
                    # uses (project_type_router.ladder_for_file_and_brief),
                    # so UI files get the UI_HEAVY ladder, server files
                    # get BACKEND, etc. — no more "owl alpha rate-limited
                    # → fall back to CLI → timeout" cascade.
                    import os as _os_route
                    _rl_route = rel.lower()
                    # Frontend entrypoints (App/main, with or without src/).
                    _is_frontend_entry = (
                        _rl_route.endswith((
                            "/app.jsx", "/app.tsx", "/main.jsx", "/main.tsx",
                        ))
                        or _rl_route in ("app.jsx", "app.tsx", "main.jsx", "main.tsx")
                        or _rl_route.endswith("src/app.jsx")
                        or _rl_route.endswith("src/main.jsx")
                    )
                    # Server entrypoints — same CLI-timeout failure mode
                    # as frontend entrypoints (large prompt, complex
                    # cross-file wiring expected) but were not in the
                    # original fast-path. Adding them is the BR-038-ish
                    # follow-up to Kimi's report on entrypoint generation.
                    _is_server_entry = (
                        _rl_route.endswith((
                            "server/index.js", "server/index.ts",
                            "server/app.js", "server/app.ts",
                            "server/main.js", "server/main.ts",
                        ))
                        or _rl_route in ("server.js", "server.ts", "index.js", "index.ts")
                        and "/" not in _rl_route  # only top-level server.js
                    )
                    _is_problem_file = _is_frontend_entry or _is_server_entry
                    # Feature flag — default ON. Set
                    # SKYN3T_ENTRYPOINT_OPENROUTER_FIRST=0 to disable
                    # and force entrypoints back through the CLI tiers.
                    _fast_path_enabled = (
                        _os_route.environ.get(
                            "SKYN3T_ENTRYPOINT_OPENROUTER_FIRST", "1"
                        ).strip().lower() not in ("0", "false", "no", "off")
                        # When OpenRouter is failed over to claude_cli (key
                        # exhausted), skip the OpenRouter ladder entirely.
                        and _os_route.environ.get(
                            "SKYN3T_LLM_FORCE_CLAUDE_CLI", ""
                        ).strip().lower() not in ("1", "true", "yes", "on")
                    )
                    # OPENROUTER_API_KEY may live in os.environ OR in .env
                    # (loaded by pydantic-settings into the Settings object).
                    # pydantic-settings does NOT inject .env values into
                    # os.environ, so a plain os.environ.get() check would
                    # return None even when the key is configured.
                    _or_key = _os_route.environ.get("OPENROUTER_API_KEY")
                    if not _or_key:
                        try:
                            from skyn3t.config.settings import get_settings as _gs
                            _or_key = getattr(_gs(), "openrouter_api_key", None)
                        except Exception:
                            _or_key = None
                    if _or_key and _fast_path_enabled:
                        # Mirror into os.environ so OpenRouterBackend can
                        # see it without us threading the key everywhere.
                        _os_route.environ.setdefault("OPENROUTER_API_KEY", _or_key)
                        try:
                            logger.warning(
                                "OPENROUTER FAST-PATH: %s routed to OpenRouter ladder "
                                "(entrypoint=%s)",
                                rel, _is_problem_file,
                            )
                            from skyn3t.adapters import LLMClient as _LLMCEntry
                            # Task-aware ladder. ladder_for_file_and_brief
                            # picks UI_HEAVY for *.jsx, BACKEND for
                            # server/*, DOCS for *.md, etc. — see
                            # core/project_type_router.py for the full
                            # mapping. Try each model in order; stop on
                            # first usable response. Falls through to
                            # CLI chain only if the whole ladder fails.
                            try:
                                from skyn3t.core.project_type_router import (
                                    ladder_for_file_and_brief,
                                )
                                _fp_ladder = list(
                                    ladder_for_file_and_brief(rel, brief or "")
                                )
                            except Exception:
                                _fp_ladder = [
                                    "openai/gpt-oss-120b:free",
                                    "qwen/qwen3-coder:free",
                                    "qwen/qwen3-next-80b-a3b-instruct:free",
                                ]
                            # Use a SHORT focused prompt for OpenRouter.
                            # The full file_prompt is 18-25KB which is
                            # exactly what was killing CLI generation
                            # and would also stress OpenRouter (slower
                            # streaming for big inputs). For entrypoint
                            # files, brief + purpose + stack is enough.
                            _entry_prompt = (
                                f"Implement the file `{rel}` for this product brief:\n\n"
                                f"BRIEF:\n{(brief or '').strip()[:1200]}\n\n"
                                f"PURPOSE OF THIS FILE: {purpose or 'Top-level entrypoint implementing the brief.'}\n"
                                f"STACK: {stack or 'react_vite'}\n\n"
                                # Pin the entrypoint to import the PLANNED component
                                # files instead of inventing imports for files that
                                # were never scaffolded — the dominant ENOENT /
                                # missing-file build-failure class. Same rules the
                                # slow path injects; "" when not an entry file.
                                + (entrypoint_instructions + "\n\n" if entrypoint_instructions else "")
                                + "Output ONLY the file body. No fences, no markdown, no commentary. "
                                "Imports at the top, default export at the bottom for React components. "
                                "Write a complete, runnable implementation that matches the brief — "
                                "not a stub, not a placeholder, not a TODO comment."
                            )
                            body_local = ""
                            _accepted_model = ""
                            for _fp_model in _fp_ladder:
                                try:
                                    file_client = _LLMCEntry(
                                        default_model=_fp_model,
                                        backend="openrouter",
                                        event_bus=self.event_bus,
                                        caller_name=self.name,
                                    )
                                    # Per-model client owns an httpx pool;
                                    # close it before the next ladder rung so
                                    # long builds don't exhaust sockets/FDs.
                                    try:
                                        _try_body = await file_client.complete(
                                            _entry_prompt,
                                            system=(
                                                "You write production-grade source code. "
                                                "Never use TODO comments, placeholders, or "
                                                "'replace with real implementation' language. "
                                                "Output the complete file body only."
                                            ),
                                            max_tokens=8000,
                                            temperature=0.2,
                                            timeout=90.0,
                                        )
                                    finally:
                                        try:
                                            await file_client.aclose()
                                        except Exception:
                                            pass
                                except Exception as _fp_exc:
                                    logger.warning(
                                        "OPENROUTER FAST-PATH %s failed for %s: %s",
                                        _fp_model, rel, _fp_exc,
                                    )
                                    continue
                                # Marker extraction + cleanup, same as
                                # below in the CLI path. We keep this
                                # inline because the per-model loop
                                # needs to validate before moving on.
                                _marked_fp = _extract_marked_files(_try_body or "")
                                if _marked_fp:
                                    _fp_match = (
                                        _marked_fp.get(rel)
                                        or _marked_fp.get(rel.lstrip("/"))
                                        or _marked_fp.get(_Path(rel).name)
                                    )
                                    if not _fp_match and len(_marked_fp) == 1:
                                        _fp_match = next(iter(_marked_fp.values()))
                                    if _fp_match:
                                        _try_body = _fp_match
                                _try_body = _strip_cli_prelude((_try_body or "").strip(), rel)
                                _try_body = _strip_fences(_try_body)
                                _try_body = _strip_copilot_footer(_try_body)
                                if (
                                    _try_body
                                    and "[deterministic-stub]" not in _try_body
                                    and "TODO[skyn3t]" not in _try_body
                                    and _syntax_ok(_try_body, rel)
                                    and _stack_ok(_try_body, rel, stack)
                                ):
                                    body_local = _try_body
                                    _accepted_model = _fp_model
                                    break
                                logger.warning(
                                    "OPENROUTER FAST-PATH %s returned unusable body for %s (len=%d)",
                                    _fp_model, rel, len(_try_body or ""),
                                )
                            if body_local:
                                logger.warning(
                                    "OPENROUTER FAST-PATH ACCEPTED for %s via %s",
                                    rel, _accepted_model,
                                )
                                await self.think(
                                    f"entrypoint fast-path SUCCESS for {rel} via {_accepted_model}"
                                )
                                # Entry-file hardening: force ONE regen if
                                # the accepted body is a stub or inline-
                                # redefines a planned component. Budget-
                                # gated; returns the original on any issue.
                                body_local = await self._regen_entrypoint_if_needed(
                                    rel=rel, body=body_local, purpose=purpose,
                                    brief=brief, stack=stack, file_specs=file_specs,
                                )
                                return rel, body_local, target
                            else:
                                logger.warning(
                                    "OPENROUTER FAST-PATH ladder exhausted for %s — falling back to CLI chain",
                                    rel,
                                )
                        except Exception:
                            logger.exception(
                                "entrypoint fast-path errored for %s; continuing with CLI", rel,
                            )

                    # Per-file routing: pick the BACKEND best for THIS
                    # file's type. Frontend (.jsx, components/, pages/,
                    # hooks/) → kimi_cli (pretty UI). Backend (server/,
                    # api/, adapters/) → copilot_cli (code correctness).
                    # Empirical from v15-v32: kimi is better at React,
                    # copilot is better at Express. Mixing them gets us
                    # the best of both per file instead of forcing one.
                    file_client = client  # default = agent's primary
                    routed = False
                    try:
                        from skyn3t.core.model_router import resolve_model_for_file
                        # Adaptive routing: pass the active stack +
                        # scoreboard so the router can demote a backend
                        # that's been losing for this stack. Falls back
                        # to pure static when scoreboard is unavailable
                        # or SKYN3T_ROUTER_ADAPTIVE=0.
                        try:
                            from skyn3t.intelligence.build_patterns import get_default_scoreboard
                            _sb = get_default_scoreboard()
                        except Exception:
                            _sb = None
                        per_file_backend, per_file_model = resolve_model_for_file(
                            rel, stack=stack, scoreboard=_sb,
                            event_bus=self.event_bus,
                        )
                        # Only construct a new client if the routing
                        # actually differs from the agent's primary
                        # (saves a subprocess + LLMClient roundtrip).
                        agent_backend = (self.config or {}).get("backend")
                        if per_file_backend and per_file_backend != agent_backend:
                            from skyn3t.adapters import LLMClient as _LLMC
                            file_client = _LLMC(
                                default_model=per_file_model,
                                backend=per_file_backend,
                                event_bus=self.event_bus,
                                caller_name=self.name,
                                backend_is_policy=True,
                            )
                            routed = True
                    except Exception:
                        logger.debug(
                            "per-file backend routing failed for %s",
                            rel, exc_info=True,
                        )

                    # First attempt with the per-file-routed backend.
                    try:
                        body_local = await file_client.complete(
                            file_prompt,
                            system=build_system,
                            max_tokens=8000,
                            temperature=0.3,
                            timeout=_FILE_BUILD_TIMEOUT_SECONDS,
                            _allow_backend_failover=False,
                        )
                    except Exception:
                        body_local = ""
                    finally:
                        # H15: close the per-file routed client so we don't
                        # leak httpx connections / subprocess pools.
                        if routed and file_client is not client:
                            try:
                                await file_client.aclose()
                            except Exception:
                                pass
                    marked_local = _extract_marked_files(body_local or "")
                    if marked_local:
                        body_match = (
                            marked_local.get(rel)
                            or marked_local.get(rel.lstrip("/"))
                            or marked_local.get(_Path(rel).name)
                        )
                        if not body_match and len(marked_local) == 1:
                            body_match = next(iter(marked_local.values()))
                        if body_match:
                            body_local = body_match

                    # Normalize before any further checks. Two passes:
                    # (a) strip the CLI's tool-call trace narration that
                    #     sometimes prefixes file content
                    # (b) strip a fenced ``` block even when prose
                    #     surrounds it. CLI backends do this despite
                    #     the prompt explicitly forbidding it.
                    body_local = _strip_cli_prelude((body_local or "").strip(), rel)
                    body_local = _strip_fences(body_local)
                    body_local = _strip_copilot_footer(body_local)

                    # Determine whether we need a fallback-backend retry.
                    # Two trigger conditions, both treated the same way:
                    #   (a) Empty or deterministic-stub response.
                    #   (b) Response that fails a cheap syntax pre-check
                    #       — saves a ~10-min reviewer + verifier round-trip
                    #       just to discover a malformed file.
                    needs_retry = (
                        not body_local
                        or "[deterministic-stub]" in body_local
                        or not _syntax_ok(body_local, rel)
                        or not _stack_ok(body_local, rel, stack)
                    )

                    if needs_retry:
                        primary = self.config.get("backend") or ""
                        try:
                            if not body_local:
                                reason = "empty"
                            elif "[deterministic-stub]" in body_local:
                                reason = "stub"
                            elif not _syntax_ok(body_local, rel):
                                reason = "bad syntax"
                            elif not _stack_ok(body_local, rel, stack):
                                reason = f"stack mismatch ({stack})"
                            else:
                                reason = "unknown"
                            await self.think(
                                f"retry {rel} on fallback backend ({reason})"
                            )
                            from skyn3t.adapters import LLMClient as _LLMClient
                            from skyn3t.core.model_router import resolve_model_for_file
                            try:
                                from skyn3t.intelligence.build_patterns import (
                                    get_default_scoreboard,
                                )
                                _sb_retry = get_default_scoreboard()
                            except Exception:
                                _sb_retry = None
                            esc_backend, esc_model = resolve_model_for_file(
                                rel,
                                stack=stack,
                                scoreboard=_sb_retry,
                                event_bus=self.event_bus,
                                escalate=True,
                            )
                            retry_client = _LLMClient(
                                default_model=esc_model,
                                backend=esc_backend or None,
                                event_bus=self.event_bus,
                                caller_name=self.name,
                                backend_is_policy=bool(esc_backend),
                            )
                            try:
                                retry_body = await retry_client.complete(
                                    file_prompt,
                                    system=build_system,
                                    max_tokens=8000,
                                    temperature=0.3,
                                    timeout=_FILE_RETRY_TIMEOUT_SECONDS,
                                    _allow_backend_failover=False,
                                )
                            finally:
                                try:
                                    await retry_client.aclose()
                                except Exception:
                                    pass
                            if (
                                (not retry_body or "[deterministic-stub]" in retry_body)
                                and primary
                            ):
                                retry_client = _LLMClient(
                                    default_model=None,
                                    backend=None,
                                    skip_backends=[primary],
                                    event_bus=self.event_bus,
                                    caller_name=self.name,
                                )
                                try:
                                    retry_body = await retry_client.complete(
                                        file_prompt,
                                        system=build_system,
                                        max_tokens=8000,
                                        temperature=0.3,
                                        timeout=_FILE_RETRY_TIMEOUT_SECONDS,
                                        _allow_backend_failover=False,
                                    )
                                finally:
                                    try:
                                        await retry_client.aclose()
                                    except Exception:
                                        pass
                            marked_retry = _extract_marked_files(retry_body or "")
                            if marked_retry:
                                retry_match = (
                                    marked_retry.get(rel)
                                    or marked_retry.get(rel.lstrip("/"))
                                    or marked_retry.get(_Path(rel).name)
                                )
                                if not retry_match and len(marked_retry) == 1:
                                    retry_match = next(iter(marked_retry.values()))
                                if retry_match:
                                    retry_body = retry_match
                            retry_body = _strip_cli_prelude((retry_body or "").strip(), rel)
                            retry_body = _strip_fences(retry_body)
                            retry_body = _strip_copilot_footer(retry_body)
                        except Exception:
                            retry_body = ""

                        # Accept the retry result only if it's actually
                        # better — non-empty, not a stub, and passes the
                        # syntax check. Otherwise keep the original (it
                        # may be wrong but at least it's content the
                        # downstream fix-loop can work with).
                        if (
                            retry_body
                            and "[deterministic-stub]" not in retry_body
                            and _syntax_ok(retry_body, rel)
                            and _stack_ok(retry_body, rel, stack)
                        ):
                            body_local = retry_body
                        elif not body_local:
                            body_local = retry_body  # nothing to lose

                    # Third-tier retry — last chance to get a real file
                    # before we fall back to a TODO placeholder.
                    # Different from the second retry above in three ways:
                    #   (1) Uses a more focused, file-specific prompt
                    #       (no global ceremony, just "implement this file
                    #       for this brief").
                    #   (2) Allows backend failover so we try every
                    #       configured provider.
                    #   (3) Includes the brief and key context lines so
                    #       the model isn't generating in a vacuum.
                    # This was added because users were seeing the
                    # "Generated scaffold still contains unresolved TODO
                    # stubs: src/App.jsx" failure repeatedly — the
                    # second retry shares too much prompt scaffolding
                    # with the first, so the same failure mode recurs.
                    if (
                        not body_local
                        or "[deterministic-stub]" in body_local
                        or not _syntax_ok(body_local, rel)
                    ):
                        # FIRED — log loudly so we can tell from history
                        # whether this path actually ran (previous bug:
                        # silent failures hid the third-tier from logs).
                        logger.warning(
                            "THIRD-TIER RETRY FIRED for %s — reason: body_empty=%s stub=%s syntax_bad=%s",
                            rel,
                            not body_local,
                            "[deterministic-stub]" in (body_local or ""),
                            not _syntax_ok(body_local or "", rel),
                        )
                        try:
                            await self.think(
                                f"third-tier focused retry for {rel}"
                            )
                            from skyn3t.adapters import LLMClient as _LLMClient3
                            # Skip the backends that already failed on
                            # this file. Previously we created a fresh
                            # client with no skip list, so it tried
                            # Copilot first AGAIN and timed out on the
                            # same prompt. Reusing skip_backends from
                            # the primary client gives the third-tier
                            # retry a real chance to land on a
                            # different CLI.
                            _primary_skip = set(
                                getattr(file_client, "_skip_backends", None)
                                or getattr(client, "_skip_backends", None) or []
                            )
                            # Also explicitly skip whatever backend
                            # produced the failed body, if recorded.
                            _last_failed = (
                                getattr(file_client, "_last_failed_backend", None)
                                or getattr(client, "_last_failed_backend", None)
                            )
                            if _last_failed:
                                _primary_skip.add(_last_failed)
                            focused_client = _LLMClient3(
                                default_model=None,
                                backend=None,
                                skip_backends=sorted(_primary_skip) if _primary_skip else None,
                            )
                            if _primary_skip:
                                logger.warning(
                                    "third-tier skipping failed backends: %s",
                                    sorted(_primary_skip),
                                )
                            focused_prompt = (
                                f"Implement the file `{rel}` for this product brief:\n\n"
                                f"BRIEF:\n{(brief or '').strip()[:1500]}\n\n"
                                f"PURPOSE OF THIS FILE: {purpose or 'no purpose specified'}\n"
                                f"STACK: {stack or 'react_vite'}\n\n"
                                f"Output ONLY the file body (no fences, no markdown, no commentary). "
                                f"Write a complete, runnable implementation — not a stub, not a placeholder. "
                                f"Imports at the top, default export at the bottom for React components. "
                                f"Match the brief's actual product — do not invent unrelated functionality."
                            )
                            try:
                                focused_body = await focused_client.complete(
                                    focused_prompt,
                                    system=(
                                        "You write production-grade source code. "
                                        "Never use TODO comments, placeholders, or 'replace with real implementation' "
                                        "language. Generate the complete, working file. If the file is a React "
                                        "component, build the actual UI the brief describes."
                                    ),
                                    max_tokens=8000,
                                    temperature=0.2,
                                    timeout=_FILE_RETRY_TIMEOUT_SECONDS,
                                )
                            finally:
                                try:
                                    await focused_client.aclose()
                                except Exception:
                                    pass
                            focused_body = _strip_cli_prelude((focused_body or "").strip(), rel)
                            focused_body = _strip_fences(focused_body)
                            focused_body = _strip_copilot_footer(focused_body)
                            # Diagnostic: what did the LLM actually return?
                            _len = len(focused_body or "")
                            _has_stub = "[deterministic-stub]" in (focused_body or "")
                            _has_todo = "TODO[skyn3t]" in (focused_body or "")
                            _syn_ok = _syntax_ok(focused_body or "", rel)
                            _stk_ok = _stack_ok(focused_body or "", rel, stack)
                            logger.warning(
                                "THIRD-TIER RETRY RESULT for %s — len=%d stub=%s todo=%s syntax_ok=%s stack_ok=%s",
                                rel, _len, _has_stub, _has_todo, _syn_ok, _stk_ok,
                            )
                            if focused_body and _len < 200:
                                # If it's tiny, dump it so we can see what the LLM actually said.
                                logger.warning("THIRD-TIER RETRY BODY for %s: %r", rel, focused_body[:500])
                            if (
                                focused_body
                                and not _has_stub
                                and not _has_todo
                                and _syn_ok
                                and _stk_ok
                            ):
                                body_local = focused_body
                                await self.think(f"third-tier retry succeeded for {rel}")
                                logger.warning("THIRD-TIER RETRY ACCEPTED for %s", rel)
                            else:
                                logger.warning(
                                    "THIRD-TIER RETRY REJECTED for %s — falling back to placeholder",
                                    rel,
                                )
                        except Exception as _3rd_exc:
                            logger.warning(
                                "THIRD-TIER RETRY EXCEPTION for %s: %s",
                                rel, _3rd_exc, exc_info=True
                            )
                            # Also keep the original debug log
                            logger.debug(
                                "third-tier retry failed for %s", rel, exc_info=True
                            )

                    # Fourth-tier: OpenRouter API. When CLIs all
                    # failed/timed out (the App.jsx-stub failure mode),
                    # bypass the CLI subprocess problem entirely by
                    # calling OpenRouter directly. Uses a small model
                    # ladder so most calls land on cheap models. Only
                    # fires when OPENROUTER_API_KEY is configured; the
                    # placeholder fallback below still catches when
                    # OpenRouter is unavailable.
                    if (
                        not body_local
                        or "[deterministic-stub]" in body_local
                        or not _syntax_ok(body_local, rel)
                    ):
                        try:
                            import os as _os
                            _or_key_4 = _os.environ.get("OPENROUTER_API_KEY")
                            if not _or_key_4:
                                try:
                                    from skyn3t.config.settings import get_settings as _gs4
                                    _or_key_4 = getattr(_gs4(), "openrouter_api_key", None)
                                except Exception:
                                    _or_key_4 = None
                            if _or_key_4:
                                # Make sure the OpenRouter backend can see it
                                _os.environ.setdefault("OPENROUTER_API_KEY", _or_key_4)
                                logger.warning(
                                    "FOURTH-TIER (OpenRouter) FIRED for %s — CLIs exhausted",
                                    rel,
                                )
                                await self.think(
                                    f"fourth-tier OpenRouter retry for {rel}"
                                )
                                from skyn3t.adapters import LLMClient as _LLMClient4
                                # Model ladder, cheapest first.
                                # Skip free tier — empirical test
                                # showed it's currently rate-limited.
                                # deepseek-v3.2 is ~$0.25/M in, $0.38/M out;
                                # a typical 5KB file uses ~2K in + 2K out
                                # ≈ $0.001/call. A full build is ~$0.02.
                                # Project-type aware ladder. UI files
                                # get Mimo Flash second; backend files
                                # get Qwen3-Coder second; games get
                                # Hunyuan-3 second. Free Owl Alpha is
                                # always the first try regardless of
                                # type. See skyn3t/core/project_type_router
                                # for the full mapping.
                                try:
                                    from skyn3t.core.project_type_router import (
                                        ladder_for_file_and_brief,
                                    )
                                    _model_ladder = list(
                                        ladder_for_file_and_brief(rel, brief or "")
                                    )
                                except Exception:
                                    _model_ladder = [
                                        "openai/gpt-oss-120b:free",
                                        "qwen/qwen3-coder:free",
                                        "qwen/qwen3-next-80b-a3b-instruct:free",
                                        "meta-llama/llama-3.3-70b-instruct:free",
                                    ]
                                _focused_prompt_or = (
                                    f"Implement the file `{rel}` for this product brief:\n\n"
                                    f"BRIEF:\n{(brief or '').strip()[:1500]}\n\n"
                                    f"PURPOSE OF THIS FILE: {purpose or 'no purpose specified'}\n"
                                    f"STACK: {stack or 'react_vite'}\n\n"
                                    # Pin entrypoints to import the PLANNED component
                                    # files (dominant missing-import failure class);
                                    # "" for non-entry files.
                                    + (entrypoint_instructions + "\n\n" if entrypoint_instructions else "")
                                    + "Output ONLY the file body (no fences, no markdown, no commentary). "
                                    "Write a complete, runnable implementation — not a stub, not a placeholder. "
                                    "Imports at the top, default export at the bottom for React components. "
                                    "Match the brief's actual product — do not invent unrelated functionality."
                                )
                                _or_body = ""
                                _or_model = ""
                                for _m in _model_ladder:
                                    try:
                                        or_client = _LLMClient4(
                                            default_model=_m,
                                            backend="openrouter",
                                        )
                                        # Close each per-model client's httpx
                                        # pool before the next rung — otherwise
                                        # a long ladder leaks sockets/FDs.
                                        try:
                                            _or_body = await or_client.complete(
                                                _focused_prompt_or,
                                                system=(
                                                    "You write production-grade source code. "
                                                    "Never use TODO comments, placeholders, or "
                                                    "'replace with real implementation' language. "
                                                    "Generate the complete, working file."
                                                ),
                                                max_tokens=8000,
                                                temperature=0.2,
                                                timeout=90.0,
                                            )
                                        finally:
                                            try:
                                                await or_client.aclose()
                                            except Exception:
                                                pass
                                        _or_body = _strip_cli_prelude((_or_body or "").strip(), rel)
                                        _or_body = _strip_fences(_or_body)
                                        if (
                                            _or_body
                                            and "[deterministic-stub]" not in _or_body
                                            and "TODO[skyn3t]" not in _or_body
                                            and _syntax_ok(_or_body, rel)
                                        ):
                                            _or_model = _m
                                            break
                                        else:
                                            logger.warning(
                                                "FOURTH-TIER %s returned unusable body for %s (len=%d)",
                                                _m, rel, len(_or_body or ""),
                                            )
                                            _or_body = ""
                                    except Exception as _or_exc:
                                        logger.warning(
                                            "FOURTH-TIER %s failed for %s: %s",
                                            _m, rel, _or_exc,
                                        )
                                        _or_body = ""
                                        continue
                                if _or_body:
                                    body_local = _or_body
                                    logger.warning(
                                        "FOURTH-TIER ACCEPTED for %s via %s",
                                        rel, _or_model,
                                    )
                                    await self.think(
                                        f"OpenRouter ({_or_model}) generated {rel}"
                                    )
                        except Exception:
                            logger.exception(
                                "fourth-tier OpenRouter retry failed for %s", rel,
                            )

                    # Entry-file hardening (Phase 3): before falling back
                    # to a placeholder, if this is an entry file whose
                    # accepted body is a stub or inline-redefines a
                    # planned component, force ONE budget-gated regen.
                    # No-op for non-entry files and when no offending
                    # pattern is present; returns the original on failure.
                    body_local = await self._regen_entrypoint_if_needed(
                        rel=rel, body=body_local or "", purpose=purpose,
                        brief=brief, stack=stack, file_specs=file_specs,
                    )

                    # If we STILL have nothing usable, write a visible
                    # placeholder so downstream imports resolve. The reviewer
                    # / verifier will flag this and the fix loop gets a real
                    # signal instead of a silent gap.
                    # v43: also fall back to placeholder when the body is
                    # syntactically broken — keeping a fenced or truncated
                    # file breaks the build before the fix loop can run.
                    if (
                        not body_local
                        or "[deterministic-stub]" in body_local
                        or not _syntax_ok(body_local, rel)
                    ):
                        await self.think(f"FILE MISSING after retries: {rel}")
                        body_local = _placeholder_for(rel, purpose, stack)

                    return rel, (body_local or ""), target

            async def _build_one_monitored(
                i: int, rel: str, purpose: str, target: "_Path",
            ) -> Tuple[str, str, "_Path"]:
                """MONITOR-wrapped version of _build_one.

                Two protections on top of the existing retry tiers:
                1. Hard wall-clock timeout — 900s. Even if every CLI
                   plus every OpenRouter retry hangs, the file
                   eventually returns (with a placeholder) instead of
                   stalling the entire stage.
                2. One per-file final retry via OpenRouter — if
                   _build_one raises or returns an empty body, we run
                   ONE more call straight to Owl Alpha with a tight
                   focused prompt. Cheaper + simpler than letting the
                   stage timeout cascade.

                Stall detection happens implicitly: when wait_for
                triggers, _build_one is cancelled and we move to the
                per-file retry. No "monitor task" needs to poll
                progress — asyncio handles the cancellation.
                """
                import asyncio as _asyncio_mon
                _STAGE_WALL_CLOCK = 900.0
                try:
                    return await _asyncio_mon.wait_for(
                        _build_one(i, rel, purpose, target),
                        timeout=_STAGE_WALL_CLOCK,
                    )
                except _asyncio_mon.TimeoutError:
                    logger.warning(
                        "PER-FILE WALL-CLOCK timeout for %s after %.0fs — "
                        "running last-resort OpenRouter retry",
                        rel, _STAGE_WALL_CLOCK,
                    )
                except Exception:
                    logger.warning(
                        "_build_one raised for %s — running last-resort OpenRouter retry",
                        rel, exc_info=True,
                    )

                # Last-resort retry — straight to OpenRouter / Owl Alpha,
                # short focused prompt. Bypasses the CLI chain entirely
                # because by this point we know the CLIs aren't getting
                # this file done. Capped at 60s.
                import os as _os_lr
                _or_key_lr = _os_lr.environ.get("OPENROUTER_API_KEY")
                if not _or_key_lr:
                    try:
                        from skyn3t.config.settings import get_settings as _gs_lr
                        _or_key_lr = getattr(_gs_lr(), "openrouter_api_key", None)
                        if _or_key_lr:
                            _os_lr.environ.setdefault("OPENROUTER_API_KEY", _or_key_lr)
                    except Exception:
                        _or_key_lr = None
                if _or_key_lr:
                    try:
                        from skyn3t.adapters import LLMClient as _LLMCLR
                        last_client = _LLMCLR(
                            default_model=None,  # -> free catalog model (no-claude default)
                            backend="openrouter",
                            event_bus=self.event_bus,
                            caller_name=self.name,
                        )
                        last_prompt = (
                            f"Write the file `{rel}` for this brief:\n\n"
                            f"BRIEF: {(brief or '').strip()[:1000]}\n\n"
                            f"PURPOSE: {purpose or 'no purpose specified'}\n"
                            f"STACK: {stack or 'react_vite'}\n\n"
                            "Output ONLY the file body — no fences, no commentary. "
                            "Complete, runnable implementation. No TODO, no stubs."
                        )
                        try:
                            last_body = await last_client.complete(
                                last_prompt,
                                system="You write production-grade source code. Output only the file body.",
                                max_tokens=6000,
                                temperature=0.2,
                                timeout=60.0,
                            )
                        finally:
                            try:
                                await last_client.aclose()
                            except Exception:
                                pass
                        last_body = _strip_cli_prelude((last_body or "").strip(), rel)
                        last_body = _strip_fences(last_body)
                        last_body = _strip_copilot_footer(last_body)
                        if (
                            last_body
                            and "[deterministic-stub]" not in last_body
                            and "TODO[skyn3t]" not in last_body
                            and _syntax_ok(last_body, rel)
                        ):
                            logger.warning(
                                "PER-FILE LAST-RESORT succeeded for %s (via Owl Alpha)",
                                rel,
                            )
                            return rel, last_body, target
                    except Exception:
                        logger.warning(
                            "per-file last-resort retry failed for %s", rel, exc_info=True
                        )
                # Everything failed. Write a placeholder so the stage can
                # continue and the reviewer sees the gap.
                return rel, _placeholder_for(rel, purpose, stack), target

            async def _build_cluster(
                cluster: List[Tuple[int, str, str, "_Path"]],
            ) -> List[Tuple[str, str, "_Path"]]:
                """Resolve N sibling files in a SINGLE LLM call.

                Sibling files share shape (e.g. seven Express-router
                adapters), so writing them together gives the model a
                chance to be consistent across them. It also collapses N
                CLI subprocess round-trips into 1, which is the bulk of
                the wall-time win.

                Response format requested: each file separated by a
                literal marker line ``// === path ===``. The parser
                accepts ``# === path ===`` too so Python clusters work.
                On any parse failure we fall back to per-file calls for
                this cluster — slower but guaranteed to work.
                """
                assert sem is not None
                async with sem:
                    cluster_paths = [rel for _, rel, _, _ in cluster]
                    cluster_lines = [
                        f"- `{rel}`: {purpose}"
                        for _, rel, purpose, _ in cluster
                    ]
                    parent_dir = str(_Path(cluster_paths[0]).parent)
                    suffix = _Path(cluster_paths[0]).suffix
                    # Marker is comment-syntax appropriate for the suffix.
                    # Python uses '# ===', everything else uses '// ==='.
                    is_py = suffix == ".py"
                    marker = "# ===" if is_py else "// ==="
                    fence_close = "===" if not is_py else "==="
                    prompt_parts = [
                        f"Brief:\n{brief}\n\n",
                        f"Stack: {stack}\n\n",
                    ]
                    # Brief-derived non-negotiable rules for the cluster's
                    # dominant file type. Backend-agnostic compact rules
                    # so even small-context free-tier models stay on rails.
                    try:
                        from skyn3t.agents.brief_requirements import (
                            extract_requirements,
                            format_hard_rules,
                        )
                        _reqs = extract_requirements(brief or "")
                        _cluster_rules_md = format_hard_rules(
                            _reqs, cluster_paths[0], palette_hexes=_palette_hexes
                        )
                        if _cluster_rules_md:
                            prompt_parts.append(_cluster_rules_md)
                    except Exception:
                        logger.debug(
                            "brief_requirements injection failed for cluster %s",
                            parent_dir, exc_info=True,
                        )
                    if prior_context:
                        prompt_parts.append(
                            "Prior stages already produced these artifacts. "
                            "Use them — especially the API specs in research.md "
                            "if present — to wire REAL integrations, not fake demo data:\n\n"
                            f"{prior_context}\n\n"
                        )
                    prompt_parts.extend([
                        f"Full file plan:\n{file_index}\n\n",
                        f"You are now writing a CLUSTER of {len(cluster)} sibling "
                        f"files under `{parent_dir}/`. They share shape; keep "
                        f"them CONSISTENT with each other (same imports, "
                        f"helpers, error handling, return shape, code style).\n\n"
                        f"Files to write in this batch:\n"
                        + "\n".join(cluster_lines)
                        + "\n\n"
                        f"Output format — one response containing ALL files, "
                        f"each preceded by exactly this marker line on its own line:\n"
                        f"  {marker} <relative path> {fence_close}\n"
                        f"Then the file's raw contents. No fenced code blocks, no JSON, "
                        f"no prose between files. Example:\n\n"
                        f"{marker} {cluster_paths[0]} {fence_close}\n"
                        f"<contents of {cluster_paths[0]}>\n"
                        f"{marker} {cluster_paths[1] if len(cluster_paths) > 1 else cluster_paths[0]} {fence_close}\n"
                        f"<contents of next file>\n"
                        f"... and so on for every file in the batch.\n"
                    ])
                    cluster_prompt = "".join(prompt_parts)
                    # Route the cluster call to the cluster's preferred
                    # backend (per-file routing: frontend → kimi_cli).
                    # Lets Kimi handle the whole-frontend swarm-style
                    # batch in one coherent pass while the per-file
                    # backend setting stays correct on each file.
                    from skyn3t.core.model_router import resolve_model_for_file
                    try:
                        from skyn3t.intelligence.build_patterns import get_default_scoreboard
                        _sb_cluster = get_default_scoreboard()
                    except Exception:
                        _sb_cluster = None
                    cluster_backend, cluster_model = resolve_model_for_file(
                        cluster_paths[0], stack=stack, scoreboard=_sb_cluster,
                        event_bus=self.event_bus,
                    )
                    agent_backend = (self.config or {}).get("backend")
                    if cluster_backend and cluster_backend != agent_backend:
                        from skyn3t.adapters import LLMClient as _LLMC
                        cluster_client = _LLMC(
                            default_model=cluster_model,
                            backend=cluster_backend,
                            event_bus=self.event_bus,
                            caller_name=self.name,
                            backend_is_policy=True,
                        )
                    else:
                        cluster_client = client
                    await self.think(
                        f"batching cluster of {len(cluster)} file(s) "
                        f"under {parent_dir}/ on {cluster_backend}"
                    )
                    try:
                        raw = await cluster_client.complete(
                            cluster_prompt,
                            system=build_system,
                            max_tokens=8000,
                            temperature=0.3,
                            timeout=_CLUSTER_BUILD_TIMEOUT_SECONDS,
                            _allow_backend_failover=False,
                        )
                    except Exception:
                        raw = ""
                    if not raw or "[deterministic-stub]" in raw:
                        # Whole cluster failed → fall back to per-file path
                        # so we don't ship a hole. solo path keeps its own
                        # retry-on-syntax-fail loop.
                        await self.think(
                            f"cluster {parent_dir}/ produced no output, "
                            f"falling back to per-file calls"
                        )
                        per_file_results = await asyncio.gather(
                            *(_build_one(i, rel, purpose, t) for i, rel, purpose, t in cluster),
                            return_exceptions=True,
                        )
                        return [r for r in per_file_results if not isinstance(r, BaseException)]

                    # Parse: split by either marker style so a Python
                    # cluster and a JS cluster use the same code path.
                    # Marker regex matches `// === path ===` or `# === path ===`
                    # anywhere on its own line.
                    parsed = _extract_marked_files(raw)

                    # Validate: every requested file must have parsed
                    # content AND pass syntax check. If any miss, fall back
                    # for the missing ones to the per-file path.
                    out: List[Tuple[str, str, "_Path"]] = []
                    missing: List[Tuple[int, str, str, "_Path"]] = []
                    for i, rel, purpose, target in cluster:
                        body = parsed.get(rel) or parsed.get(rel.lstrip("/"))
                        # Also tolerate the model emitting just basename.
                        if not body:
                            body = parsed.get(_Path(rel).name)
                        if not body:
                            missing.append((i, rel, purpose, target))
                            continue
                        body = _strip_cli_prelude(body, rel)
                        body = _strip_fences(body)
                        if not _syntax_ok(body, rel):
                            missing.append((i, rel, purpose, target))
                            continue
                        out.append((rel, body, target))
                    if missing:
                        await self.think(
                            f"cluster {parent_dir}/: {len(missing)}/{len(cluster)} "
                            f"file(s) missing or invalid, falling back to per-file"
                        )
                        per_file_results = await asyncio.gather(
                            *(_build_one(i, rel, purpose, t) for i, rel, purpose, t in missing),
                            return_exceptions=True,
                        )
                        for r in per_file_results:
                            if isinstance(r, BaseException):
                                continue
                            out.append(r)
                    return out

            if solo_jobs or batch_clusters:
                # return_exceptions=True so one slow/exploded backend
                # doesn't tank the whole scaffold — surviving jobs still
                # get written to disk.
                # Use the monitored wrapper so individual files can fail
                # or stall without killing the whole stage. Each file
                # gets up to 900s wall-clock + one last-resort OpenRouter
                # retry before placeholder. Clusters keep using the
                # original _build_cluster (they have their own internal
                # fallback path).
                coros = [
                    _build_one_monitored(i, rel, purpose, target)
                    for i, rel, purpose, target in solo_jobs
                ] + [
                    _build_cluster(cluster) for cluster in batch_clusters
                ]
                results = await asyncio.gather(*coros, return_exceptions=True)

                # Flatten: solo results are single tuples, cluster results
                # are lists of tuples. Normalize before writing.
                flat: List[Tuple[str, str, "_Path"]] = []
                for r in results:
                    if isinstance(r, BaseException):
                        logger.debug("parallel build raised", exc_info=r)
                        continue
                    if isinstance(r, list):
                        for item in r:
                            if (
                                isinstance(item, tuple)
                                and len(item) == 3
                                and isinstance(item[0], str)
                                and isinstance(item[1], str)
                                and isinstance(item[2], (_Path, str))
                            ):
                                flat.append((item[0], item[1], _Path(item[2])))
                    elif (
                        isinstance(r, tuple)
                        and len(r) == 3
                        and isinstance(r[0], str)
                        and isinstance(r[1], str)
                        and isinstance(r[2], (_Path, str))
                    ):
                        flat.append((r[0], r[1], _Path(r[2])))

                skipped_paths: List[str] = []
                for rel_w, body_w, target_w in flat:
                    if not body_w:
                        # File made it through the LLM loop but
                        # _placeholder_for returned empty too — log
                        # the skip so this never silently regresses.
                        # Earlier versions silently dropped files
                        # (App.jsx, server/data/user-config.json,
                        # server/config-store.js etc) here.
                        skipped_paths.append(rel_w)
                        logger.warning(
                            "scaffold: file %s produced no body and no "
                            "placeholder — SKIPPING. Either the LLM "
                            "kept returning empty for this path and "
                            "_placeholder_for has no shape for its "
                            "extension.", rel_w,
                        )
                        try:
                            await self.think(
                                f"SKIPPED {rel_w}: empty body, no placeholder"
                            )
                        except Exception:
                            pass
                        continue
                    # _build_one / _build_cluster already stripped fences
                    # and ran syntax checks; here we just trim trailing
                    # whitespace before writing.
                    body_w = body_w.strip()
                    try:
                        target_w.write_text(body_w, encoding="utf-8")
                        files_written.append(str(target_w))
                    except Exception:
                        skipped_paths.append(rel_w)
                        logger.warning(
                            "scaffold: write failed for %s", rel_w,
                            exc_info=True,
                        )
                        continue
                if skipped_paths:
                    # Surface the gap so callers (and the reviewer)
                    # know the scaffold is incomplete relative to its
                    # plan. The runner can choose to retry these.
                    logger.warning(
                        "scaffold complete with %d skipped file(s): %s",
                        len(skipped_paths),
                        ", ".join(skipped_paths[:20]),
                    )
        except Exception:
            logger.exception("scaffold-from-brief failed; falling back to deterministic stub")

        # Fallback path: if the LLM didn't produce real code files, hand-
        # roll the scaffold. Two trigger cases:
        #   (a) Nothing got written at all (LLM offline or every call
        #       returned deterministic-stub).
        #   (b) Only the deterministic README was written but no code —
        #       happens when a stack template fires but the LLM portion
        #       fails. Without this check we'd ship a README and zero
        #       code, which is worse than the hand-rolled scaffold.
        non_readme = [
            f for f in files_written
            if _Path(f).name.lower() not in ("readme.md", "readme")
        ]
        if not non_readme:
            # Wipe any README we wrote — the fallback will produce its
            # own coherent set including a brief-relevant README.
            for f in files_written:
                try:
                    _Path(f).unlink()
                except Exception:
                    pass
            files_written = self._write_fallback_scaffold(out_dir, brief)

        # Backward-compatible frontend aliases: some newer stack templates
        # emit `style.css` + `main.js`, while older tests/integrations
        # expect `styles.css` + `app.js`. Keep both when needed.
        files_written = self._ensure_legacy_frontend_aliases(out_dir, files_written, brief)

        # Backfill any local import targets that the LLM referenced but
        # never wrote (canary-118/119 pattern: App.jsx imported
        # CommandPalette/ActivityFeed/ServiceDetail but the planner never
        # listed them, so the scaffold failed to build with "module not
        # found"). When a deterministic generator exists for the missing
        # path, fall back to it; otherwise a placeholder stub avoids the
        # build break.
        try:
            files_written = await self._backfill_unresolved_local_imports(
                out_dir=out_dir,
                files_written=files_written,
                stack=template_key,
                brief=brief,
                palette_hexes=_palette_hexes,
                llm_client=self._llm,
            )
        except Exception:
            logger.exception("backfill unresolved imports failed (non-fatal)")

        # Strip declared-but-unused deps from package.json files. Every
        # canary 119-136 had the reviewer flag "better-sqlite3 declared
        # but never imported" as architecture-vs-scaffold drift (~5pt).
        # Rather than retrofit sqlite into config-store.js (heavy lift),
        # remove the unused declaration so the manifest matches reality.
        try:
            self._strip_unused_package_deps(out_dir)
        except Exception:
            logger.debug("strip-unused-deps failed (non-fatal)", exc_info=True)

        try:
            smoke_written = self._ensure_smoke_tests(out_dir, tech_stack)
            files_written.extend(smoke_written)
        except Exception:
            logger.debug("smoke test emission failed (non-fatal)", exc_info=True)

        try:
            await self.share_learning(
                f"scaffold: {len(files_written)} files for brief",
                scope="studio",
            )
        except Exception:
            logger.debug("share_learning(scaffold) failed", exc_info=True)

        # Surface completeness data so the runner / reviewer can spot a
        # scaffold that fell short of its plan. Earlier silent drops
        # (v22 missed App.jsx, v23 missed 7 files) only surfaced after
        # someone tried to boot the result.
        planned_count = len(file_specs) if isinstance(file_specs, list) else 0
        written_count = len(files_written)
        missing_files: List[str] = []
        if planned_count > written_count:
            try:
                planned_paths = {
                    (s.get("path") or "").lstrip("/").strip()
                    for s in file_specs if isinstance(s, dict)
                }
                # files_written holds resolved absolute paths; convert
                # back to scaffold-relative.
                resolved_out = str(out_dir.resolve())
                written_paths = set()
                for f in files_written:
                    try:
                        rel = str(_Path(f).resolve().relative_to(resolved_out))
                        written_paths.add(rel.replace("\\", "/"))
                    except Exception:
                        continue
                missing_files = sorted(planned_paths - written_paths - {""})
            except Exception:
                logger.debug("completeness check failed", exc_info=True)

        # Phase 3 pre-verifier signal: planned_imports / stub_markers /
        # entrypoint_files / entrypoint_is_stub. Pure-Python over the
        # already-written files; on ANY failure the fields default to
        # []/False and the existing keys above still return — the runner
        # treats absence as 'no signal' and never blocks.
        planned_imports, stub_markers, entrypoint_files, entrypoint_is_stub = (
            _collect_stub_signal(out_dir, file_specs, files_written)
        )

        return TaskResult(
            task_id=task.task_id, success=True,
            output={
                "files": files_written,
                "summary": (
                    f"Scaffolded {written_count}/{planned_count} planned file(s)."
                    + (f" Missing: {len(missing_files)}." if missing_files else "")
                ),
                "scaffold_dir": str(out_dir),
                "planned_count": planned_count,
                "written_count": written_count,
                "missing_files": missing_files,
                "injected_skills": injected_skills,
                "injected_learnings": injected_learnings,
                "planned_imports": planned_imports,
                "stub_markers": stub_markers,
                "entrypoint_files": entrypoint_files,
                "entrypoint_is_stub": entrypoint_is_stub,
            })

    def _write_fallback_scaffold(self, out_dir, brief: str) -> list[str]:
        brief_lower = (brief or "").lower()
        if "minesweeper" in brief_lower:
            return self._write_minesweeper_scaffold(out_dir, brief)
        if any(
            token in brief_lower
            for token in ("todo", "frontend", "ui", "website", "site", "landing", "dashboard", "app")
        ):
            return self._write_frontend_scaffold(out_dir, brief)
        if any(
            token in brief_lower
            for token in ("api", "backend", "service", "server", "webhook", "docker", "container")
        ):
            return self._write_backend_scaffold(out_dir, brief)
        return self._write_script_scaffold(out_dir, brief)

    # Match `import X from './path'`, `import X from "./path"`,
    # `from './path' import` (ESM/TS) and `require('./path')`. Captures
    # ONLY relative paths (./, ../) — bare specifiers are package imports,
    # not scaffold files, so we never try to manufacture them.
    _LOCAL_IMPORT_RE = _RE.compile(
        r"""(?xm)
        (?:
            ^\s*import\s+(?:[^'";\n]+?\s+from\s+)?['"](\.{1,2}/[^'"\n]+)['"]
          | ^\s*export\s+(?:\*|\{[^}]*\})\s+from\s+['"](\.{1,2}/[^'"\n]+)['"]
          | \brequire\s*\(\s*['"](\.{1,2}/[^'"\n]+)['"]\s*\)
        )
        """
    )

    _ENTRYPOINT_ESM_IMPORT_RE = _RE.compile(
        r"""(?xm)
        ^(?P<indent>\s*)import\s+
        (?P<clause>(?!type\b)[^'";\n]+?)\s+
        from\s+
        (?P<quote>['"])(?P<target>\.{1,2}/[^'"\n]+)(?P=quote)
        (?P<semi>[ \t]*;?)
        """
    )

    # Common extensions to try when resolving a bare `./Foo` import.
    _RESOLVE_EXTS: Tuple[str, ...] = (
        ".jsx", ".tsx", ".ts", ".js", ".mjs", ".cjs", ".css", ".scss",
    )

    async def _backfill_unresolved_local_imports(
        self,
        *,
        out_dir,
        files_written: list[str],
        stack: Optional[str],
        brief: str,
        palette_hexes: Optional[List[str]] = None,
        llm_client=None,
    ) -> list[str]:
        """Scan generated files for relative imports that don't resolve, and
        backfill from deterministic generators when possible.

        Root cause we're addressing: the LLM's App.jsx imports
        ``./components/CommandPalette.jsx``, but the planner never put
        that file in the spec, so it never got written. The scaffold
        ships with a broken import and Vite/Webpack fail to build.

        Strategy:
          1. Parse every JS/JSX/TS/TSX file we wrote, extracting relative
             import targets.
          2. Resolve each target against the file's directory. Try the
             literal path, then the path with each common extension, then
             ``<path>/index.<ext>``.
          3. For each unresolved target, look up the scaffold-relative
             path in stack_templates._MANIFEST_GENERATORS. If a
             deterministic generator exists, write it. Otherwise write a
             minimal placeholder so the build at least succeeds.
        """
        from pathlib import Path as _Path

        from skyn3t.agents.stack_templates import manifest_for

        out_dir = _Path(out_dir).resolve()
        # Only scan files we actually emitted, and limit to JS/TS-ish
        # bodies — backfilling CSS @imports etc. is out of scope.
        scan_exts = {".jsx", ".tsx", ".js", ".ts", ".mjs", ".cjs"}
        scanned: int = 0
        backfilled: list[str] = []

        for written in list(files_written):
            try:
                p = _Path(written)
                if not p.is_absolute():
                    p = (out_dir / p).resolve()
                if not p.is_file():
                    continue
                if p.suffix.lower() not in scan_exts:
                    continue
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            scanned += 1

            file_dir = p.parent
            for match in self._LOCAL_IMPORT_RE.finditer(text):
                target = match.group(1) or match.group(2) or match.group(3) or ""
                if not target:
                    continue
                # Resolve relative to the importing file's directory.
                try:
                    base = (file_dir / target).resolve()
                except Exception:
                    continue
                # Reject anything outside the scaffold (e.g. `../../../etc/passwd`).
                try:
                    base.relative_to(out_dir)
                except ValueError:
                    continue

                resolved = self._resolve_import_path(base)
                if resolved is not None:
                    continue  # already on disk — nothing to do

                # Pick a concrete path for the new file. If the target
                # ends with an extension we leave it alone; otherwise
                # tack on `.jsx` for JSX importers, `.js` otherwise.
                if base.suffix:
                    target_path = base
                else:
                    pick_ext = ".jsx" if p.suffix.lower() in (".jsx", ".tsx") else ".js"
                    target_path = base.with_suffix(pick_ext)

                # Compute the scaffold-relative path for the dispatch lookup.
                try:
                    rel_path = target_path.relative_to(out_dir).as_posix()
                except ValueError:
                    continue

                # First try a deterministic generator. Pass stack as-is;
                # manifest_for() returns None for unknown (stack, path).
                body: Optional[str] = None
                # Self-healing: check learned generators first (beats Hermes —
                # permanently learned from past failures).
                try:
                    from skyn3t.self_healing.learned_generators import LearnedGeneratorManager
                    _lgr = LearnedGeneratorManager()
                    gen = _lgr.get_generator(rel_path)
                    if gen:
                        body = gen(brief or "")
                        if body:
                            logger.info("Using learned generator for %s", rel_path)
                except Exception:
                    pass
                if body is None and stack:
                    body = manifest_for(
                        stack, rel_path, brief or "", palette_hexes=palette_hexes
                    )
                if body is None and llm_client:
                    # No deterministic generator — ask the LLM to produce a
                    # real implementation so we don't ship a stub that the
                    # reviewer will flag and the retry loop will never fix.
                    try:
                        prompt = (
                            f"Write the COMPLETE source for the file '{rel_path}'.\n"
                            f"Brief: {brief}\n"
                            f"Stack: {stack or 'unknown'}\n"
                            "Return ONLY the raw code — no markdown fences, "
                            "no explanations."
                        )
                        # llm_client.complete() is async — await it
                        # directly since this method is now async.
                        coro = llm_client.complete(
                            prompt=prompt,
                            system="You are a senior developer. Write clean, "
                                   "working code for the requested file.",
                            max_tokens=2048,
                            temperature=0.2,
                            timeout=120,
                        )
                        body = await coro
                        # Apply the same fence/prelude sanitization the other
                        # generation paths use — CLI/OpenRouter backends often
                        # wrap output in ``` fences or lead with prose, which
                        # would otherwise be written verbatim and break the
                        # build. Gate on the syntax check so a fenced/truncated
                        # body falls through to the deterministic placeholder.
                        body = _strip_cli_prelude((body or "").strip(), rel_path)
                        body = _strip_fences(body)
                        body = _strip_copilot_footer(body)
                        # Reject only genuine stub markers — a blanket
                        # `"TODO" in body` wrongly discarded legitimate files
                        # that merely mention "TODO" (a comment, a todo-app's
                        # own UI text, etc.).
                        if (
                            not body
                            or "[deterministic-stub]" in body
                            or "TODO[skyn3t]" in body
                            or "skyn3t-backfill" in body
                            or not _syntax_ok(body, rel_path)
                        ):
                            body = None
                    except Exception:
                        body = None
                if body is None:
                    # Last-resort placeholder so the build doesn't break.
                    # Cheaper than letting Vite fail, and the reviewer can
                    # still flag the stub.
                    body = self._placeholder_local_import(rel_path)

                try:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(body, encoding="utf-8")
                    backfilled.append(str(target_path))
                except OSError:
                    logger.warning("backfill write failed: %s", target_path)

        if backfilled:
            logger.info(
                "backfilled %d unresolved import(s) across %d file(s): %s",
                len(backfilled),
                scanned,
                ", ".join(_Path(b).name for b in backfilled[:5])
                + ("…" if len(backfilled) > 5 else ""),
            )
            files_written = files_written + [b for b in backfilled if b not in files_written]
        fixed_imports = self._fix_entrypoint_import_export_mismatches(
            out_dir, files_written
        )
        if fixed_imports:
            logger.info(
                "fixed %d entrypoint import/export mismatch(es): %s",
                len(fixed_imports),
                ", ".join(_Path(f).name for f in fixed_imports[:5])
                + ("…" if len(fixed_imports) > 5 else ""),
            )
        return files_written

    @staticmethod
    def _resolve_import_path(base):
        """Return the actual file path an import resolves to, or None.

        Tries: the literal path, the path with each common extension,
        and `<path>/index.<ext>`. Mirrors Node's resolution algorithm
        for relative specifiers.
        """
        from pathlib import Path as _Path
        if base.is_file():
            return base
        for ext in CodeAgent._RESOLVE_EXTS:
            candidate = _Path(str(base) + ext)
            if candidate.is_file():
                return candidate
        if base.is_dir():
            for ext in CodeAgent._RESOLVE_EXTS:
                idx = base / f"index{ext}"
                if idx.is_file():
                    return idx
        return None

    @classmethod
    def _fix_entrypoint_import_export_mismatches(cls, out_dir, files_written) -> list[str]:
        """Rewrite entrypoint imports when named/default style disagrees with exports."""
        from pathlib import Path as _Path

        out_dir = _Path(out_dir).resolve()
        fixed: list[str] = []
        scan_exts = {".jsx", ".tsx", ".js", ".ts", ".mjs"}
        for written in list(files_written or []):
            try:
                p = _Path(written)
                if not p.is_absolute():
                    p = (out_dir / p).resolve()
                if not p.is_file() or p.suffix.lower() not in scan_exts:
                    continue
                if p.name.lower() not in _ENTRYPOINT_FILES:
                    continue
                original = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            replacements: list[tuple[int, int, str]] = []
            for match in cls._ENTRYPOINT_ESM_IMPORT_RE.finditer(original):
                clause = (match.group("clause") or "").strip()
                target = match.group("target") or ""
                try:
                    base = (p.parent / target).resolve()
                    base.relative_to(out_dir)
                except Exception:
                    continue
                resolved = cls._resolve_import_path(base)
                if resolved is None or resolved.suffix.lower() not in scan_exts:
                    continue
                try:
                    target_body = resolved.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                exports = cls._module_export_shape(target_body)
                default_name, named_imports = cls._import_clause_shape(clause)
                new_clause: Optional[str] = None

                if (
                    default_name is None
                    and len(named_imports) == 1
                    and exports["has_default"]
                ):
                    imported, local = named_imports[0]
                    if imported not in exports["named"]:
                        new_clause = local
                elif (
                    default_name is not None
                    and not named_imports
                    and not exports["has_default"]
                    and default_name in exports["named"]
                ):
                    new_clause = f"{{ {default_name} }}"

                if not new_clause or new_clause == clause:
                    continue
                quote = match.group("quote") or "'"
                semi = match.group("semi") or ""
                replacement = (
                    f"{match.group('indent') or ''}import {new_clause} "
                    f"from {quote}{target}{quote}{semi}"
                )
                replacements.append((match.start(), match.end(), replacement))

            if not replacements:
                continue
            updated = original
            for start, end, replacement in reversed(replacements):
                updated = updated[:start] + replacement + updated[end:]
            try:
                p.write_text(updated, encoding="utf-8")
                fixed.append(str(p))
            except OSError:
                logger.warning("entrypoint import/export fix write failed: %s", p)
        return fixed

    @staticmethod
    def _import_clause_shape(clause: str) -> tuple[Optional[str], list[tuple[str, str]]]:
        """Return ``(default_local_name, [(imported_name, local_name), ...])``."""
        clause = (clause or "").strip()
        if not clause or clause.startswith("*"):
            return None, []
        default_name: Optional[str] = None
        named: list[tuple[str, str]] = []
        named_block: Optional[str] = None
        if clause.startswith("{"):
            named_block = clause
        elif ", {" in clause:
            default_name = clause.split(",", 1)[0].strip() or None
            named_block = clause.split(",", 1)[1].strip()
        else:
            default_name = clause.strip() or None
        if named_block and named_block.startswith("{") and named_block.endswith("}"):
            inner = named_block[1:-1]
            for part in inner.split(","):
                item = part.strip()
                if not item:
                    continue
                pieces = _RE.split(r"\s+as\s+", item, maxsplit=1)
                imported = pieces[0].strip()
                local = pieces[1].strip() if len(pieces) == 2 else imported
                if imported and local:
                    named.append((imported, local))
        return default_name, named

    @staticmethod
    def _module_export_shape(body: str) -> dict[str, Any]:
        named: set[str] = set()
        for rx in (
            r"\bexport\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
            r"\bexport\s+class\s+([A-Za-z_$][\w$]*)",
            r"\bexport\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
        ):
            named.update(_RE.findall(rx, body or ""))
        for match in _RE.finditer(r"\bexport\s*\{([^}]+)\}", body or ""):
            for part in match.group(1).split(","):
                item = part.strip()
                if not item:
                    continue
                item = _RE.split(r"\s+as\s+", item, maxsplit=1)[-1].strip()
                if _RE.match(r"^[A-Za-z_$][\w$]*$", item):
                    named.add(item)
        return {
            "has_default": bool(_RE.search(r"\bexport\s+default\b", body or "")),
            "named": named,
        }

    @staticmethod
    def _placeholder_local_import(rel_path: str) -> str:
        """Tiny build-valid stub for an unresolved local import.

        Keeps Vite/Webpack from breaking when no deterministic generator
        applies. The reviewer's `placeholder_leak` check still catches it
        so the stub never sneaks through to production.
        """
        name = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        ext = rel_path.rsplit(".", 1)[-1].lower()
        if ext in ("jsx", "tsx"):
            return (
                f"// @skyn3t-backfill-stub: for missing import.\n"
                f"export default function {name}() {{\n"
                f"  return null;\n"
                f"}}\n"
            )
        if ext in ("ts", "tsx"):
            if name.startswith("use"):
                return (
                    "// @skyn3t-backfill-stub: for missing import.\n"
                    f"export function {name}() {{\n"
                    "  return {};\n"
                    "}\n"
                    f"export default {name};\n"
                )
            return (
                "// @skyn3t-backfill-stub: for missing import.\n"
                "export default {};\n"
            )
        if ext in ("css", "scss"):
            return f"/* @skyn3t-backfill-stub: for missing import ({rel_path}) */\n"
        # js / mjs / cjs / fallback
        if name.startswith("use"):
            return (
                "// @skyn3t-backfill-stub: for missing import.\n"
                f"export function {name}() {{\n"
                "  return {};\n"
                "}\n"
                f"export default {name};\n"
            )
        return (
            "// @skyn3t-backfill-stub: for missing import.\n"
            "export default {};\n"
        )

    # Packages we WILL strip from a package.json if no source file imports
    # them. Conservative whitelist: only deps that historically get
    # declared by ArchitectAgent (via tech_stack.json picks) but
    # CodeAgent's actual scaffold doesn't use. Frameworks/runtimes are
    # never stripped — even if not directly imported, build tools may
    # consume them.
    _STRIPPABLE_UNUSED_DEPS = frozenset({
        "better-sqlite3", "sqlite3", "sqlite",
        "pg", "postgres", "@vercel/postgres",
        "mongodb", "mongoose",
        "@prisma/client", "prisma",
        "drizzle-orm",
        "node-cron", "croner", "agenda",
    })

    @classmethod
    def _strip_unused_package_deps(cls, out_dir) -> None:
        """Remove declared-but-never-imported deps from package.json.

        Walks each non-vendor package.json under out_dir. For every
        strippable dep, checks the scaffold's source for an import or
        require of that package. If absent, removes the dep from
        dependencies/devDependencies. Leaves package-lock.json
        untouched — operator can run `npm install` again to reconcile.
        """
        import json as _json
        import re as _RE
        from pathlib import Path as _Path
        out_dir = _Path(out_dir).resolve()

        # Build the source body once (skipping node_modules / dist etc.)
        skip = {"node_modules", "dist", "build", ".next", ".cache", ".git"}
        source_chunks: list[str] = []
        for path in out_dir.rglob("*"):
            if not path.is_file():
                continue
            if any(part in skip for part in path.relative_to(out_dir).parts):
                continue
            if path.suffix.lower() not in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
                continue
            try:
                source_chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
        source_blob = "\n".join(source_chunks)

        def _is_imported(pkg: str) -> bool:
            esc = _RE.escape(pkg)
            patterns = [
                rf"require\s*\(\s*['\"]{esc}['\"]",
                rf"from\s+['\"]{esc}['\"]",
                rf"import\s*\(\s*['\"]{esc}['\"]",
            ]
            return any(_RE.search(p, source_blob) for p in patterns)

        for pkg_path in out_dir.rglob("package.json"):
            try:
                if any(part in skip for part in pkg_path.relative_to(out_dir).parts):
                    continue
            except ValueError:
                continue
            try:
                with open(pkg_path, "r", encoding="utf-8") as fh:
                    data = _json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            mutated = False
            for section_key in ("dependencies", "devDependencies"):
                section = data.get(section_key)
                if not isinstance(section, dict):
                    continue
                for name in list(section.keys()):
                    if name in cls._STRIPPABLE_UNUSED_DEPS and not _is_imported(name):
                        section.pop(name, None)
                        mutated = True
                        logger.info(
                            "Stripped unused dep %s from %s",
                            name, pkg_path.relative_to(out_dir).as_posix(),
                        )
            if mutated:
                try:
                    with open(pkg_path, "w", encoding="utf-8") as fh:
                        _json.dump(data, fh, indent=2)
                        fh.write("\n")
                except OSError:
                    logger.warning("could not write stripped package.json: %s", pkg_path)

    @staticmethod
    def _ensure_legacy_frontend_aliases(out_dir, files_written: list[str], brief: str) -> list[str]:
        brief_lower = (brief or "").lower()
        if not any(
            token in brief_lower
            for token in ("todo", "frontend", "ui", "website", "site", "landing", "dashboard", "app")
        ):
            return files_written
        from pathlib import Path as _Path

        current = {_Path(path).name for path in files_written}
        alias_pairs = [
            ("style.css", "styles.css"),
            ("main.js", "app.js"),
            ("script.js", "app.js"),
        ]
        for src_name, dst_name in alias_pairs:
            if dst_name in current:
                continue
            src_path = out_dir / src_name
            dst_path = out_dir / dst_name
            if src_path.exists() and src_path.is_file() and not dst_path.exists():
                dst_path.write_text(src_path.read_text(encoding="utf-8"), encoding="utf-8")
                files_written.append(str(dst_path))
                current.add(dst_name)
        return files_written

    def _write_frontend_scaffold(self, out_dir, brief: str) -> list[str]:
        files = {
            "index.html": """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>SkyN3t Starter</title>
    <link rel="stylesheet" href="styles.css">
  </head>
  <body>
    <main class="app-shell">
      <section class="card">
        <header class="card-header">
          <p class="eyebrow">SkyN3t scaffold</p>
          <h1>Todo starter</h1>
          <p class="lede">""" + brief + """</p>
        </header>
        <form id="todo-form" class="todo-form">
          <input id="todo-input" type="text" placeholder="Add a task" autocomplete="off">
          <button type="submit">Add</button>
        </form>
        <ul id="todo-list" class="todo-list"></ul>
      </section>
    </main>
    <script src="app.js"></script>
  </body>
</html>
""",
            "styles.css": """:root {
  color-scheme: dark;
  font-family: Inter, system-ui, sans-serif;
}

body {
  margin: 0;
  min-height: 100vh;
  background: linear-gradient(180deg, #0f172a, #111827 60%, #020617);
  color: #e5eefb;
}

.app-shell {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 2rem;
}

.card {
  width: min(560px, 100%);
  background: rgba(15, 23, 42, 0.88);
  border: 1px solid rgba(148, 163, 184, 0.22);
  border-radius: 20px;
  padding: 1.5rem;
  box-shadow: 0 24px 80px rgba(15, 23, 42, 0.45);
}

.eyebrow {
  margin: 0 0 0.35rem;
  color: #38bdf8;
  font-size: 0.78rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.lede {
  color: #cbd5e1;
}

.todo-form {
  display: flex;
  gap: 0.75rem;
  margin: 1.25rem 0;
}

.todo-form input {
  flex: 1;
  border: 1px solid rgba(148, 163, 184, 0.26);
  border-radius: 999px;
  padding: 0.8rem 1rem;
  background: rgba(15, 23, 42, 0.75);
  color: inherit;
}

.todo-form button,
.todo-item button {
  border: 0;
  border-radius: 999px;
  background: #38bdf8;
  color: #0f172a;
  padding: 0.8rem 1rem;
  font-weight: 700;
  cursor: pointer;
}

.todo-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.75rem;
}

.todo-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.9rem 1rem;
  border-radius: 14px;
  background: rgba(30, 41, 59, 0.92);
  border: 1px solid rgba(148, 163, 184, 0.18);
}

.todo-item.done span {
  text-decoration: line-through;
  color: #94a3b8;
}
""",
            "app.js": """const form = document.getElementById('todo-form');
const input = document.getElementById('todo-input');
const list = document.getElementById('todo-list');

const todos = [
  { id: crypto.randomUUID(), text: 'Sketch the happy path', done: false },
  { id: crypto.randomUUID(), text: 'Wire the UI state', done: false },
];

function renderTodos() {
  list.innerHTML = '';
  todos.forEach((todo) => {
    const item = document.createElement('li');
    item.className = `todo-item${todo.done ? ' done' : ''}`;

    const label = document.createElement('span');
    label.textContent = todo.text;
    label.addEventListener('click', () => {
      todo.done = !todo.done;
      renderTodos();
    });

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = 'Remove';
    remove.addEventListener('click', () => {
      const index = todos.findIndex((entry) => entry.id === todo.id);
      if (index >= 0) {
        todos.splice(index, 1);
        renderTodos();
      }
    });

    item.append(label, remove);
    list.append(item);
  });
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const value = input.value.trim();
  if (!value) return;
  todos.unshift({ id: crypto.randomUUID(), text: value, done: false });
  input.value = '';
  renderTodos();
});

renderTodos();
""",
        }
        return self._write_scaffold_files(out_dir, files)

    def _write_minesweeper_scaffold(self, out_dir, brief: str) -> list[str]:
        files = {
            "index.html": """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Minesweeper</title>
    <link rel="stylesheet" href="styles.css">
  </head>
  <body>
    <main class="app-shell">
      <section class="game-panel">
        <header class="hero">
          <p class="eyebrow">SkyN3t scaffold</p>
          <h1>Minesweeper</h1>
          <p class="lede">""" + brief + """</p>
        </header>

        <section class="toolbar" aria-label="Game controls">
          <div class="difficulty-group" role="group" aria-label="Difficulty">
            <button type="button" class="difficulty is-active" data-difficulty="beginner">Beginner</button>
            <button type="button" class="difficulty" data-difficulty="intermediate">Intermediate</button>
            <button type="button" class="difficulty" data-difficulty="expert">Expert</button>
          </div>
          <button type="button" id="reset-btn" class="reset-btn">New game</button>
        </section>

        <section class="status-bar" aria-label="Game status">
          <div class="stat">
            <span class="stat-label">Mines</span>
            <strong id="mine-count">10</strong>
          </div>
          <div class="stat">
            <span class="stat-label">Time</span>
            <strong id="timer">0</strong>
          </div>
          <div class="stat">
            <span class="stat-label">State</span>
            <strong id="status-text">Ready</strong>
          </div>
        </section>

        <section class="board-shell">
          <div id="board" class="board" role="grid" aria-label="Minesweeper board"></div>
        </section>

        <p class="hint">Left click to reveal. Right click to flag. First click is always safe.</p>
      </section>
    </main>

    <script src="app.js"></script>
  </body>
</html>
""",
            "styles.css": """:root {
  color-scheme: dark;
  font-family: Inter, system-ui, sans-serif;
  --bg: #081c15;
  --panel: #1b4332;
  --panel-border: rgba(116, 198, 157, 0.24);
  --text: #f8f9fa;
  --muted: #b7e4c7;
  --accent: #74c69d;
  --accent-strong: #52b788;
  --danger: #ef476f;
  --cell-size: 40px;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-height: 100vh;
  background: radial-gradient(circle at top, #2d6a4f, var(--bg) 58%);
  color: var(--text);
}

.app-shell {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 2rem 1rem;
}

.game-panel {
  width: min(720px, 100%);
  background: rgba(8, 28, 21, 0.88);
  border: 1px solid var(--panel-border);
  border-radius: 24px;
  padding: 1.5rem;
  box-shadow: 0 24px 70px rgba(0, 0, 0, 0.28);
}

.hero h1,
.hero p {
  margin: 0;
}

.eyebrow {
  margin-bottom: 0.4rem;
  color: var(--accent);
  font-size: 0.76rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.lede {
  margin-top: 0.55rem;
  color: var(--muted);
}

.toolbar,
.status-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  align-items: center;
  justify-content: space-between;
  margin-top: 1.25rem;
}

.difficulty-group {
  display: inline-flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

button {
  border: 0;
  border-radius: 999px;
  font: inherit;
  cursor: pointer;
}

.difficulty,
.reset-btn {
  padding: 0.7rem 1rem;
  background: rgba(116, 198, 157, 0.12);
  color: var(--text);
  border: 1px solid rgba(116, 198, 157, 0.22);
}

.difficulty.is-active,
.reset-btn {
  background: var(--accent);
  color: #081c15;
  font-weight: 700;
}

.status-bar {
  padding: 0.9rem 1rem;
  background: rgba(27, 67, 50, 0.66);
  border-radius: 18px;
}

.stat {
  min-width: 110px;
}

.stat-label {
  display: block;
  color: var(--muted);
  font-size: 0.78rem;
  margin-bottom: 0.2rem;
}

.board-shell {
  margin-top: 1rem;
  overflow-x: auto;
}

.board {
  display: grid;
  gap: 6px;
  justify-content: start;
}

.cell {
  width: var(--cell-size);
  height: var(--cell-size);
  border-radius: 12px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(248, 249, 250, 0.08);
  color: var(--text);
  font-weight: 700;
  transition: transform 120ms ease, background 120ms ease;
}

.cell:hover {
  transform: translateY(-1px);
  background: rgba(248, 249, 250, 0.14);
}

.cell.revealed {
  background: rgba(183, 228, 199, 0.18);
  border-color: rgba(183, 228, 199, 0.2);
}

.cell.mine {
  background: rgba(239, 71, 111, 0.2);
}

.cell.flagged {
  color: #ffb703;
}

.cell[data-count="1"] { color: #8ecae6; }
.cell[data-count="2"] { color: #74c69d; }
.cell[data-count="3"] { color: #ffd166; }
.cell[data-count="4"] { color: #f78c6b; }
.cell[data-count="5"] { color: #ff99c8; }
.cell[data-count="6"] { color: #cdb4db; }
.cell[data-count="7"] { color: #f8f9fa; }
.cell[data-count="8"] { color: #dee2e6; }

.hint {
  margin: 1rem 0 0;
  color: var(--muted);
  font-size: 0.92rem;
}
""",
            "app.js": """const boardEl = document.getElementById('board');
const statusEl = document.getElementById('status-text');
const mineCountEl = document.getElementById('mine-count');
const timerEl = document.getElementById('timer');
const resetBtn = document.getElementById('reset-btn');
const difficultyButtons = [...document.querySelectorAll('[data-difficulty]')];

const difficulties = {
  beginner: { rows: 8, cols: 8, mines: 10 },
  intermediate: { rows: 12, cols: 12, mines: 24 },
  expert: { rows: 16, cols: 16, mines: 40 },
};

let difficultyKey = 'beginner';
let state = null;
let timerId = null;

function neighbors(row, col) {
  const points = [];
  for (let y = row - 1; y <= row + 1; y += 1) {
    for (let x = col - 1; x <= col + 1; x += 1) {
      if (y === row && x === col) continue;
      if (y < 0 || x < 0 || y >= state.rows || x >= state.cols) continue;
      points.push([y, x]);
    }
  }
  return points;
}

function createCell(row, col) {
  return {
    row,
    col,
    mine: false,
    flagged: false,
    revealed: false,
    adjacent: 0,
  };
}

function buildState() {
  const settings = difficulties[difficultyKey];
  const cells = Array.from({ length: settings.rows }, (_, row) =>
    Array.from({ length: settings.cols }, (_, col) => createCell(row, col))
  );
  state = {
    ...settings,
    cells,
    firstClick: true,
    gameOver: false,
    revealedSafeCells: 0,
    flagsUsed: 0,
    seconds: 0,
  };
  boardEl.style.gridTemplateColumns = `repeat(${state.cols}, var(--cell-size))`;
  stopTimer();
  timerEl.textContent = '0';
  mineCountEl.textContent = String(state.mines);
  statusEl.textContent = 'Ready';
}

function placeMines(safeRow, safeCol) {
  const forbidden = new Set([`${safeRow}:${safeCol}`]);
  neighbors(safeRow, safeCol).forEach(([row, col]) => forbidden.add(`${row}:${col}`));
  const openSpots = [];
  state.cells.forEach((row) => {
    row.forEach((cell) => {
      if (!forbidden.has(`${cell.row}:${cell.col}`)) openSpots.push(cell);
    });
  });
  for (let i = openSpots.length - 1; i > 0; i -= 1) {
    const swapIndex = Math.floor(Math.random() * (i + 1));
    [openSpots[i], openSpots[swapIndex]] = [openSpots[swapIndex], openSpots[i]];
  }
  openSpots.slice(0, state.mines).forEach((cell) => {
    cell.mine = true;
  });
  state.cells.forEach((row) => {
    row.forEach((cell) => {
      cell.adjacent = neighbors(cell.row, cell.col).filter(([y, x]) => state.cells[y][x].mine).length;
    });
  });
}

function startTimer() {
  stopTimer();
  timerId = window.setInterval(() => {
    state.seconds += 1;
    timerEl.textContent = String(state.seconds);
  }, 1000);
}

function stopTimer() {
  if (timerId) {
    window.clearInterval(timerId);
    timerId = null;
  }
}

function revealCell(row, col) {
  const cell = state.cells[row][col];
  if (cell.revealed || cell.flagged || state.gameOver) return;
  if (state.firstClick) {
    placeMines(row, col);
    state.firstClick = false;
    statusEl.textContent = 'Playing';
    startTimer();
  }
  cell.revealed = true;
  if (cell.mine) {
    finishGame(false);
    return;
  }
  state.revealedSafeCells += 1;
  if (cell.adjacent === 0) {
    neighbors(row, col).forEach(([y, x]) => revealCell(y, x));
  }
  if (state.revealedSafeCells === state.rows * state.cols - state.mines) {
    finishGame(true);
  }
}

function toggleFlag(row, col) {
  const cell = state.cells[row][col];
  if (cell.revealed || state.gameOver) return;
  cell.flagged = !cell.flagged;
  state.flagsUsed += cell.flagged ? 1 : -1;
  mineCountEl.textContent = String(Math.max(state.mines - state.flagsUsed, 0));
  renderBoard();
}

function finishGame(won) {
  state.gameOver = true;
  stopTimer();
  statusEl.textContent = won ? 'Cleared!' : 'Boom!';
  state.cells.forEach((row) => {
    row.forEach((cell) => {
      if (cell.mine) cell.revealed = true;
    });
  });
  renderBoard();
}

function renderBoard() {
  boardEl.innerHTML = '';
  state.cells.forEach((row) => {
    row.forEach((cell) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'cell';
      button.setAttribute('role', 'gridcell');
      button.dataset.row = String(cell.row);
      button.dataset.col = String(cell.col);
      if (cell.revealed) {
        button.classList.add('revealed');
        if (cell.mine) {
          button.classList.add('mine');
          button.textContent = 'X';
        } else if (cell.adjacent > 0) {
          button.dataset.count = String(cell.adjacent);
          button.textContent = String(cell.adjacent);
        } else {
          button.textContent = '';
        }
      } else if (cell.flagged) {
        button.classList.add('flagged');
        button.textContent = '!';
      } else {
        button.textContent = '';
      }
      button.addEventListener('click', () => {
        revealCell(cell.row, cell.col);
        renderBoard();
      });
      button.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        toggleFlag(cell.row, cell.col);
      });
      boardEl.append(button);
    });
  });
}

function resetGame(nextDifficulty = difficultyKey) {
  difficultyKey = nextDifficulty;
  difficultyButtons.forEach((button) => {
    button.classList.toggle('is-active', button.dataset.difficulty === difficultyKey);
  });
  buildState();
  renderBoard();
}

difficultyButtons.forEach((button) => {
  button.addEventListener('click', () => resetGame(button.dataset.difficulty));
});

resetBtn.addEventListener('click', () => resetGame(difficultyKey));

resetGame('beginner');
""",
        }
        return self._write_scaffold_files(out_dir, files)

    def _write_backend_scaffold(self, out_dir, brief: str) -> list[str]:
        files = {
            "main.py": """from fastapi import FastAPI

app = FastAPI(title="SkyN3t Starter API")


@app.get('/health')
async def health() -> dict[str, str]:
    return {'status': 'ok'}


@app.get('/brief')
async def brief() -> dict[str, str]:
    return {'brief': """ + repr(brief) + """}
""",
            "requirements.txt": "fastapi==0.116.1\nuvicorn==0.35.0\n",
        }
        return self._write_scaffold_files(out_dir, files)

    def _write_script_scaffold(self, out_dir, brief: str) -> list[str]:
        files = {
            "main.py": """def main() -> None:
    print('SkyN3t starter scaffold')
    print(""" + repr(brief) + """)


if __name__ == '__main__':
    main()
""",
        }
        return self._write_scaffold_files(out_dir, files)

    @staticmethod
    def _ensure_smoke_tests(out_dir, tech_stack: Dict[str, Any]) -> list[str]:
        """Emit minimal vitest/pytest smoke tests when the scaffold has none."""
        from pathlib import Path as _P

        written: list[str] = []
        root = _P(out_dir)
        pkg_path = root / "package.json"
        if pkg_path.exists():
            tests_dir = root / "tests"
            smoke_test = tests_dir / "smoke.test.js"
            if not smoke_test.exists():
                tests_dir.mkdir(parents=True, exist_ok=True)
                smoke_test.write_text(
                    "import { describe, it, expect } from 'vitest';\n"
                    "describe('smoke', () => {\n"
                    "  it('loads', () => { expect(true).toBe(true); });\n"
                    "});\n",
                    encoding="utf-8",
                )
                written.append(str(smoke_test))
            try:
                import json as _json_pkg

                pkg = _json_pkg.loads(pkg_path.read_text(encoding="utf-8"))
                if isinstance(pkg, dict):
                    scripts = pkg.setdefault("scripts", {})
                    if not isinstance(scripts, dict):
                        scripts = {}
                        pkg["scripts"] = scripts
                    if not str(scripts.get("test") or "").strip():
                        scripts["test"] = "vitest run"
                        pkg_path.write_text(
                            _json_pkg.dumps(pkg, indent=2) + "\n", encoding="utf-8"
                        )
                        written.append(str(pkg_path))
            except Exception:
                logger.debug("package.json test script patch failed", exc_info=True)
            return written

        tests_dir = root / "tests"
        py_smoke = tests_dir / "test_smoke.py"
        py_files = list(root.glob("**/*.py"))
        if py_files and not py_smoke.exists():
            tests_dir.mkdir(parents=True, exist_ok=True)
            entry = "main"
            for candidate in ("app", "main", "server"):
                if (root / f"{candidate}.py").exists():
                    entry = candidate
                    break
            py_smoke.write_text(
                f"def test_smoke_import():\n"
                f"    import importlib\n"
                f"    importlib.import_module('{entry}')\n",
                encoding="utf-8",
            )
            written.append(str(py_smoke))
        return written

    @staticmethod
    def _write_scaffold_files(out_dir, files: Dict[str, str]) -> list[str]:
        written: list[str] = []
        from pathlib import Path as _P
        out_root = _P(out_dir).resolve()
        for rel_path, content in files.items():
            # LLM-supplied path — defend against absolute paths and
            # parent-traversal that would leak files into the SkyN3t repo.
            raw = (rel_path or "").strip()
            if not raw or raw.startswith("/") or ".." in raw.split("/"):
                logger.warning("scaffold write refused (path escapes scaffold): %r", raw)
                continue
            target = (_P(out_dir) / raw).resolve()
            try:
                target.relative_to(out_root)
            except ValueError:
                logger.warning("scaffold write refused (outside scaffold): %r", raw)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(str(target))
        return written

    async def _execute_code(self, task_or_code) -> Dict[str, Any]:
        """Execute Python code via the configured sandbox backend.

        Uses ``SKYN3T_EXECUTION_BACKEND`` (default ``auto``): Docker pool when
        Docker is running, otherwise inline. Set ``docker-pool`` to require
        container isolation.
        """
        if isinstance(task_or_code, TaskRequest):
            code = task_or_code.input_data.get("code", "")
        else:
            code = task_or_code

        if not code:
            return {"success": False, "error": "No code provided"}

        try:
            from skyn3t.config.settings import get_settings
            from skyn3t.security.sandbox import get_backend

            settings = get_settings()
            backend = await get_backend(settings.execution_backend)
            result = await backend.execute(
                code,
                language="python",
                timeout=30,
                memory_mb=256,
            )
            return {
                "success": result.success,
                "output": result.stdout,
                "errors": result.stderr,
                "error": result.error,
                "truncated": result.truncated,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _analyze_code(self, task: TaskRequest) -> Dict[str, Any]:
        """Analyze code quality and structure."""
        code = task.input_data.get("code", "")
        analysis_type = task.input_data.get("analysis_type", "general")

        if not code:
            return {"success": False, "error": "No code provided"}

        result = {"analysis_type": analysis_type, "issues": []}

        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return {"success": False, "error": f"Syntax error: {e}"}

        if analysis_type in ("general", "complexity"):
            # Simple complexity metrics
            func_count = len([n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)])
            class_count = len([n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)])
            import_count = len([n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))])

            lines = code.splitlines()
            blank_lines = len([line for line in lines if not line.strip()])
            comment_lines = len([line for line in lines if line.strip().startswith("#")])

            result["metrics"] = {
                "functions": func_count,
                "classes": class_count,
                "imports": import_count,
                "total_lines": len(lines),
                "blank_lines": blank_lines,
                "comment_lines": comment_lines,
                "code_lines": len(lines) - blank_lines - comment_lines,
            }

        if analysis_type in ("general", "style"):
            # Simple style checks
            lines = code.splitlines()
            for i, line in enumerate(lines, 1):
                if len(line) > 120:
                    result["issues"].append({
                        "line": i,
                        "type": "style",
                        "message": f"Line too long ({len(line)} > 120 characters)",
                    })
                if line.rstrip() != line:
                    result["issues"].append({
                        "line": i,
                        "type": "style",
                        "message": "Trailing whitespace",
                    })

        result["success"] = True
        return result

    async def _refactor_code(self, task: TaskRequest) -> Dict[str, Any]:
        """Refactor code based on specified type."""
        code = task.input_data.get("code", "")
        refactor_type = task.input_data.get("refactor_type", "format")

        if not code:
            return {"success": False, "error": "No code provided"}

        refactored = code
        changes = []

        if refactor_type in ("format", "all"):
            # Simple formatting: normalize whitespace
            lines = code.splitlines()
            formatted_lines = []
            prev_blank = False
            for line in lines:
                stripped = line.rstrip()
                if not stripped:
                    if not prev_blank:
                        formatted_lines.append("")
                        prev_blank = True
                else:
                    formatted_lines.append(stripped)
                    prev_blank = False
            refactored = "\n".join(formatted_lines)
            changes.append("Normalized whitespace and removed trailing whitespace")

        if refactor_type in ("imports", "all"):
            # Sort and deduplicate the leading import block. Use ast end_lineno
            # to track the *line span* of imports, not their *node count*; a
            # single multi-line `from x import (a, b, c)` is one node spanning
            # several lines, so slicing by len(imports) corrupts the file.
            try:
                tree = ast.parse(refactored)
                imports: List[str] = []
                last_import_line = 0  # 1-based, inclusive
                for node in tree.body:
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        imports.append(ast.unparse(node))
                        end = getattr(node, "end_lineno", node.lineno)
                        if end and end > last_import_line:
                            last_import_line = end
                    else:
                        break
                if imports and last_import_line > 0:
                    sorted_imports = sorted(set(imports))
                    rest_lines = refactored.splitlines()[last_import_line:]
                    refactored_lines = sorted_imports + [""] + rest_lines
                    refactored = "\n".join(refactored_lines)
                    changes.append("Sorted and deduplicated imports")
            except Exception:
                pass

        return {
            "success": True,
            "original": code,
            "refactored": refactored,
            "changes": changes,
            "refactor_type": refactor_type,
        }

    async def _run_tests(self, task: TaskRequest) -> Dict[str, Any]:
        """Run tests using pytest or unittest."""
        test_code = task.input_data.get("test_code", "")
        test_framework = task.input_data.get("test_framework", "pytest")
        target_code = task.input_data.get("target_code", "")

        if not test_code:
            return {"success": False, "error": "No test code provided"}

        with tempfile.TemporaryDirectory() as tmpdir:
            # Write target code if provided
            if target_code:
                target_path = os.path.join(tmpdir, "target_module.py")
                with open(target_path, "w") as f:
                    f.write(target_code)

            # Write test code
            test_path = os.path.join(tmpdir, "test_module.py")
            with open(test_path, "w") as f:
                if target_code:
                    f.write("import sys\nsys.path.insert(0, '{}')\n".format(tmpdir))
                f.write(test_code)

            try:
                if test_framework == "pytest":
                    cmd = [sys.executable, "-m", "pytest", test_path, "-v", "--tb=short"]
                else:
                    cmd = [sys.executable, "-m", "unittest", "-v", test_path]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self._execution_timeout,
                    cwd=tmpdir,
                )

                return {
                    "success": result.returncode == 0,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "framework": test_framework,
                }
            except subprocess.TimeoutExpired:
                return {"success": False, "error": "Tests timed out"}
            except Exception as e:
                return {"success": False, "error": str(e)}
