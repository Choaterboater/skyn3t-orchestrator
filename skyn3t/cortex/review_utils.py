"""Helpers for turning reviewer output into actionable follow-up work."""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Tuple

_NON_ACTIONABLE_RISK_RE = re.compile(
    r"^(?:"
    r"none(?: detected)?"
    r"|n/?a"
    r"|\(none parsed\)"
    r"|no\b.*\brisks?\b.*\bdetected\b"
    r")\b",
    re.IGNORECASE,
)
_CANONICAL_VERDICT_RE = re.compile(r"^\*\*Verdict:\*\*\s*`([^`]+)`", re.MULTILINE)
_FALLBACK_VERDICT_RE = re.compile(r"^\s*Verdict:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_TOP_LEVEL_RISKS_RE = re.compile(r"^## Risks\s*$", re.MULTILINE)


def normalize_review_risks(risks: Iterable[Any]) -> List[str]:
    """Drop placeholder reviewer bullets like ``None detected``."""

    normalized: List[str] = []
    for risk in risks:
        text = str(risk or "").strip()
        if text.startswith("- "):
            text = text[2:].strip()
        if not text:
            continue
        if _NON_ACTIONABLE_RISK_RE.match(text):
            continue
        normalized.append(text)
    return normalized


def parse_review_markdown(text: str) -> Tuple[str, List[str]]:
    """Extract the canonical top-level verdict and risks from a review file."""

    verdict = ""
    canonical = _CANONICAL_VERDICT_RE.search(text)
    if canonical:
        verdict = f"Verdict: {canonical.group(1).strip()}"
    else:
        fallback = _FALLBACK_VERDICT_RE.search(text)
        if fallback:
            verdict = f"Verdict: {fallback.group(1).strip().strip('`')}"

    risks: List[str] = []
    sections = list(_TOP_LEVEL_RISKS_RE.finditer(text))
    if sections:
        start = sections[-1].end()
        remainder = text[start:]
        next_heading = re.search(r"^##\s+", remainder, re.MULTILINE)
        risk_block = remainder[: next_heading.start()] if next_heading else remainder
        for line in risk_block.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                risks.append(stripped[2:].strip())

    return verdict, normalize_review_risks(risks)
