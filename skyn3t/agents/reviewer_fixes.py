"""Reviewer-driven targeted fixes.

After the reviewer scores a project ``go-with-fixes``, this module:

1. Parses ``review.md`` for specific file references + specific
   complaints.
2. For each file with a clear issue, asks an OpenRouter model to
   regenerate that one file with the reviewer's complaint as guidance.
3. Caps total work at ``MAX_FILES_FIXED`` files and ``MAX_BUDGET_USD``
   spend so a noisy reviewer can't blow the build budget.
4. Returns a summary of what was fixed so the caller can re-run the
   reviewer or just log the audit trail.

Pure-functional shape: takes paths + strings, returns a result dict.
No coupling to the runner's stage machinery.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


MAX_FILES_FIXED = 3
MAX_BUDGET_USD = 0.20  # Hard cap — we're already inside the build-level cap.
MODEL_LADDER = (
    "openrouter/owl-alpha",       # free
    "xiaomi/mimo-v2-flash",       # cheap paid
    "deepseek/deepseek-v3.2",     # reliable
)


@dataclass
class FixCandidate:
    """One file the reviewer complained about, with the verbatim issue."""
    file_path: str   # relative to scaffold dir (e.g. "src/App.jsx")
    issue: str       # the reviewer's complaint text


@dataclass
class FixResult:
    candidate: FixCandidate
    ok: bool = False
    model_used: str = ""
    body_len: int = 0
    error: str = ""


@dataclass
class FixRunSummary:
    candidates_found: int = 0
    candidates_attempted: int = 0
    fixes_applied: int = 0
    results: List[FixResult] = field(default_factory=list)
    skipped_reason: str = ""


def _backfill_missing_local_imports(scaffold_dir: Path, file_path: str) -> None:
    """Backfill missing relative imports for a rewritten file when possible."""
    try:
        from skyn3t.agents.code_agent import CodeAgent
        from skyn3t.agents.stack_detector import detect as detect_stack
    except Exception:
        return

    artifact_dir = scaffold_dir.parent if scaffold_dir.name == "scaffold" else scaffold_dir
    stack = detect_stack(artifact_dir).stack
    if not stack:
        return

    agent = CodeAgent.__new__(CodeAgent)  # bypass __init__
    agent._backfill_unresolved_local_imports(
        out_dir=scaffold_dir,
        files_written=[str((scaffold_dir / file_path).resolve())],
        stack=stack,
        brief="",
        palette_hexes=None,
    )


# Match any filename-ish token that ends in a known code/asset extension.
# Permissive — picks up `App.jsx`, src/App.jsx, "tokens.css", brand.md, etc.
_FILE_TOKEN_RE = re.compile(
    r"""(?:^|[\s\(\[\`'"\*])
        ((?:[\w\-]+/)*[\w\-]+\.(?:jsx?|tsx?|css|scss|json|md|html|svg|svelte|vue))
        (?=[\s\)\]\.,;:\`'"\*]|$)
    """,
    re.MULTILINE | re.VERBOSE,
)

# Files we don't try to fix because they're either deterministic
# manifests, documentation, or not actually code the LLM should rewrite.
_UNFIXABLE_NAMES: set = {
    "package.json", "tsconfig.json", "vite.config.js", "vite.config.ts",
    "tokens.json", "palette.json", "tech_stack.json",
    "project.json", "brainstorm.md", "research.md",
    "architecture.md",  # architect's output — don't let the fix loop rewrite
    "review.md",        # don't recurse on the reviewer's own output
    "index.html",       # deterministic manifest
    "logo.svg",         # designer's asset
}


# Section headers that signal POSITIVE content (praise, file lists, summaries).
# We skip chunks under these headings so the parser doesn't try to "fix"
# a strength or a metadata listing.
_POSITIVE_HEADER_RE = re.compile(
    r"^#+\s+(?:\d+\.\s*)?(?:strengths?|files\s+reviewed|completeness|summary)\b",
    re.IGNORECASE,
)

# Section headers that signal ACTIONABLE complaints. Chunks under these
# headings are the ones we try to fix. Everything else: ignored.
_NEGATIVE_HEADER_RE = re.compile(
    r"^#+\s+(?:\d+\.\s*)?(?:gaps?|inconsistenc|weak\s+claims?|risks?|issues?|problems?|fixes?\s+needed|bugs?)",
    re.IGNORECASE,
)

# Lines that are pure file metadata (e.g. "`README.md` (1312 bytes)").
# These show up in the "Files reviewed" list and are never complaints.
_FILE_METADATA_RE = re.compile(
    r"^[`'\"\*]*[\w\-/]+\.\w+[`'\"\*]*\s*\(\s*\d[\d,]*\s*bytes\s*\)\s*$",
    re.IGNORECASE,
)


def _split_into_bullets(text: str) -> List[str]:
    """Split a review.md body into individual complaint chunks.

    Walks the document section by section. Only emits chunks that live
    under a "complaint" header (Gaps, Inconsistencies, Weak claims,
    Risks). Skips Strengths and file metadata lists entirely.
    """
    chunks: List[str] = []
    current: List[str] = []
    in_complaint_section = False

    def flush():
        if not in_complaint_section:
            current.clear()
            return
        if current:
            joined = " ".join(s.strip() for s in current if s.strip()).strip()
            if joined and not _FILE_METADATA_RE.match(joined):
                chunks.append(joined)
            current.clear()

    for line in text.splitlines():
        stripped = line.strip()
        # Header — possibly a section switch.
        if stripped.startswith("#"):
            flush()
            if _NEGATIVE_HEADER_RE.match(stripped):
                in_complaint_section = True
            elif _POSITIVE_HEADER_RE.match(stripped):
                in_complaint_section = False
            # Other headers don't change state — they may be sub-headers
            # within the active section.
            continue
        if not stripped:
            flush()
            continue
        if not in_complaint_section:
            continue
        if stripped.startswith(("- ", "* ", "+ ")) or re.match(r"^\d+[\.\)]\s", stripped):
            # New bullet — flush prior, start fresh with the bullet body.
            flush()
            body = re.sub(r"^[-*+]\s+|^\d+[\.\)]\s+", "", stripped)
            current.append(body)
        else:
            current.append(stripped)
    flush()
    return chunks


def _resolve_to_scaffold(rel_or_name: str, scaffold_dir: Path) -> str:
    """Given a filename token from the reviewer (which may be a bare
    name like 'App.jsx' or a path like 'src/App.jsx'), find the actual
    file in scaffold_dir and return its scaffold-relative path.

    Returns "" when no match exists.
    """
    candidate = rel_or_name.strip("`'\"*").strip()
    # Exact relative path match
    direct = scaffold_dir / candidate
    if direct.is_file():
        return candidate
    # Try with leading slash stripped
    if candidate.startswith("/"):
        return _resolve_to_scaffold(candidate.lstrip("/"), scaffold_dir)
    # Otherwise do a name lookup across the scaffold tree
    name_only = Path(candidate).name
    matches: List[Path] = []
    for p in scaffold_dir.rglob(name_only):
        if p.is_file():
            matches.append(p)
        if len(matches) > 2:
            break
    if len(matches) == 1:
        return str(matches[0].relative_to(scaffold_dir))
    # Multiple matches → ambiguous, skip to avoid fixing the wrong one.
    return ""


def parse_review_for_fixes(review_md_text: str, scaffold_dir: Path) -> List[FixCandidate]:
    """Pull (file, issue) pairs out of a review.md.

    Walks the review prose, splits into bullet/paragraph chunks. For
    each chunk, finds the file token(s) mentioned. Pairs the file with
    its chunk as the issue. Deduplicates by file (first match wins).
    Caps at ``MAX_FILES_FIXED`` so a noisy reviewer can't blow the budget.
    """
    if not review_md_text:
        return []

    chunks = _split_into_bullets(review_md_text)
    if not chunks:
        return []

    seen_files: set = set()
    out: List[FixCandidate] = []
    for chunk in chunks:
        # Skip preamble headers / summary lines that mention many files
        # at once — they aren't actionable per-file complaints.
        if chunk.lower().startswith(("summary", "files reviewed", "completeness")):
            continue
        for match in _FILE_TOKEN_RE.finditer(chunk):
            token = match.group(1).strip()
            name_only = Path(token).name
            if name_only.lower() in _UNFIXABLE_NAMES:
                continue
            resolved = _resolve_to_scaffold(token, scaffold_dir)
            if not resolved:
                continue
            if resolved in seen_files:
                continue
            issue = chunk
            if len(issue) > 400:
                issue = issue[:400].rstrip() + "…"
            out.append(FixCandidate(file_path=resolved, issue=issue))
            seen_files.add(resolved)
            break  # one file per chunk — don't burn a chunk on 3 files
        if len(out) >= MAX_FILES_FIXED:
            break
    return out


def _approx_cost_for_call(input_chars: int, output_chars: int) -> float:
    """Conservative cost estimate for a single fix call against the
    DeepSeek tier (top of our ladder above the free Owl). Used to keep
    a running budget — never let a fix run blow our overall cap."""
    # ~4 chars per token, then $0.25/M in + $0.38/M out for DeepSeek v3.2.
    in_tokens = input_chars / 4.0
    out_tokens = output_chars / 4.0
    return (in_tokens * 0.25 + out_tokens * 0.38) / 1_000_000.0


async def _try_fix_one(
    candidate: FixCandidate,
    brief: str,
    scaffold_dir: Path,
) -> FixResult:
    """Call OpenRouter to regenerate one file given the reviewer's complaint.

    Walks the model ladder until one returns a non-empty body. Returns
    a FixResult with the outcome.
    """
    target = scaffold_dir / candidate.file_path
    try:
        original = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return FixResult(candidate=candidate, error=f"read failed: {e}")

    prompt = (
        f"You are fixing one file in a generated project.\n\n"
        f"PRODUCT BRIEF:\n{(brief or '').strip()[:1200]}\n\n"
        f"FILE PATH: `{candidate.file_path}`\n\n"
        f"REVIEWER'S COMPLAINT ABOUT THIS FILE:\n{candidate.issue}\n\n"
        f"CURRENT FILE CONTENTS:\n```\n{original[:6000]}\n```\n\n"
        f"Rewrite this file to resolve the complaint. Output ONLY the new "
        f"file body — no markdown fences, no commentary, no preamble. The "
        f"new file must remain syntactically valid for its language. "
        f"Preserve everything in the original that the complaint doesn't "
        f"specifically target. Do not add TODO comments or placeholders."
    )

    last_err = ""
    for model in MODEL_LADDER:
        try:
            from skyn3t.adapters import LLMClient
            client = LLMClient(default_model=model, backend="openrouter")
            try:
                body = await client.complete(
                    prompt,
                    system=(
                        "You write production-grade source code. "
                        "Output the file body only. No fences. No commentary."
                    ),
                    max_tokens=6000,
                    temperature=0.2,
                    timeout=60.0,
                )
            finally:
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            last_err = f"{model}: {e}"
            continue
        body = (body or "").strip()
        # Strip any opening/closing fences the model may have added despite
        # the instruction.
        from skyn3t.agents.code_agent import (
            _strip_cli_prelude,
            _strip_copilot_footer,
            _strip_fences,
            _syntax_ok,
        )
        body = _strip_cli_prelude(body, candidate.file_path)
        body = _strip_fences(body)
        body = _strip_copilot_footer(body)
        if not body:
            last_err = f"{model}: empty body"
            continue
        if "[deterministic-stub]" in body or "TODO[skyn3t]" in body:
            last_err = f"{model}: stub/TODO marker present"
            continue
        if not _syntax_ok(body, candidate.file_path):
            last_err = f"{model}: syntax check failed"
            continue
        # Don't allow a fix to wholesale truncate the file — if the new
        # body is dramatically smaller than the original, treat it as
        # accidental deletion and skip.
        if len(body) < max(80, len(original) // 4):
            last_err = f"{model}: suspiciously short result ({len(body)} vs {len(original)})"
            continue
        # Write the fix.
        try:
            target.write_text(body, encoding="utf-8")
            _backfill_missing_local_imports(scaffold_dir, candidate.file_path)
        except OSError as e:
            return FixResult(
                candidate=candidate, model_used=model,
                body_len=len(body), error=f"write failed: {e}",
            )
        except Exception as e:  # noqa: BLE001
            try:
                target.write_text(original, encoding="utf-8")
            except OSError:
                pass
            return FixResult(
                candidate=candidate,
                model_used=model,
                body_len=len(body),
                error=f"post-write backfill failed: {e}",
            )
        return FixResult(
            candidate=candidate, ok=True, model_used=model, body_len=len(body),
        )
    return FixResult(candidate=candidate, error=last_err or "all models failed")


async def apply_reviewer_fixes(
    review_md_path: Path,
    scaffold_dir: Path,
    brief: str,
    budget_usd: float = MAX_BUDGET_USD,
) -> FixRunSummary:
    """Top-level entry point. Read review.md, find fixable candidates,
    run the fix loop within a budget cap. Returns a summary."""
    summary = FixRunSummary()
    if not review_md_path.is_file() or not scaffold_dir.is_dir():
        summary.skipped_reason = "missing review.md or scaffold dir"
        return summary
    try:
        review_text = review_md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        summary.skipped_reason = "review.md unreadable"
        return summary

    candidates = parse_review_for_fixes(review_text, scaffold_dir)
    summary.candidates_found = len(candidates)
    if not candidates:
        summary.skipped_reason = "no actionable file references in review"
        return summary

    spent = 0.0
    for candidate in candidates:
        if spent >= budget_usd:
            summary.skipped_reason = f"budget cap reached ({budget_usd:.2f} USD)"
            break
        summary.candidates_attempted += 1
        result = await _try_fix_one(candidate, brief, scaffold_dir)
        summary.results.append(result)
        if result.ok:
            summary.fixes_applied += 1
            # Rough cost charge — only the paid models contribute.
            if result.model_used != "openrouter/owl-alpha":
                spent += _approx_cost_for_call(
                    input_chars=len(brief or "") + len(candidate.issue) + 6000,
                    output_chars=result.body_len,
                )
    return summary
