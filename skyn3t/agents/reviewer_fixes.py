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

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

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


# Regex catches issues of the form:
#   "src/App.jsx" - issue text
#   `src/App.jsx`: issue text
#   App.jsx imports nonexistent ...
# We're conservative — only act when the file path is unambiguously named.
_FILE_REF_RE = re.compile(
    r"""(?:^|[\s\(\[])
        (?:`|"|')?
        (
          (?:src/|server/|app/|pages/|components/|hooks/|adapters/|lib/)
          [\w\-/]+\.(?:jsx?|tsx?|css|json|md)
        )
        (?:`|"|')?
        (?:\s*[—:\-]\s*|\s+)
        ([^\n]+)
    """,
    re.MULTILINE | re.VERBOSE,
)


def parse_review_for_fixes(review_md_text: str, scaffold_dir: Path) -> List[FixCandidate]:
    """Pull (file, issue) pairs out of a review.md.

    Returns at most ``MAX_FILES_FIXED`` candidates — picks the FIRST
    occurrence of each unique file path so we don't try to fix the same
    file 5 times in one pass.
    """
    if not review_md_text:
        return []
    seen_files: set = set()
    out: List[FixCandidate] = []
    for match in _FILE_REF_RE.finditer(review_md_text):
        rel = match.group(1).strip()
        issue = match.group(2).strip()
        if rel in seen_files:
            continue
        target = scaffold_dir / rel
        # Only act on files we can actually load + rewrite.
        if not target.is_file():
            continue
        # Cap issue length so the prompt doesn't get huge.
        if len(issue) > 400:
            issue = issue[:400] + "…"
        out.append(FixCandidate(file_path=rel, issue=issue))
        seen_files.add(rel)
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
            _strip_cli_prelude, _strip_fences, _strip_copilot_footer, _syntax_ok,
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
        except OSError as e:
            return FixResult(
                candidate=candidate, model_used=model,
                body_len=len(body), error=f"write failed: {e}",
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
