"""Tiny helpers for fitting long context blobs inside LLM prompts.

The previous code did inline ``body[:N] + "<marker>"`` truncations in
three slightly different forms; this module unifies them. It is not a
semantic summarizer — it does cheap whitespace collapse and then a
hard cut with a stable marker, which is what every caller relied on
before.
"""

from __future__ import annotations

import re

_BLANK_LINE_RUN = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+\n")
_TRUNCATION_MARKER = "\n…[truncated]"


def compress_prompt_context(body: str, *, max_chars: int) -> str:
    """Return ``body`` shortened to fit within ``max_chars`` characters.

    Short bodies are returned unchanged. Longer bodies are first
    lightly compressed (collapse runs of 3+ blank lines to 2, strip
    trailing whitespace on each line) and then truncated with a
    visible marker so the LLM and human readers can see the cut.

    ``max_chars`` must be > len(marker); otherwise the whole input is
    returned as-is (defensive — none of today's callers pass tiny caps).
    """
    if not isinstance(body, str) or not body:
        return body or ""
    if max_chars <= 0 or max_chars <= len(_TRUNCATION_MARKER):
        return body
    if len(body) <= max_chars:
        return body
    cleaned = _TRAILING_WS.sub("\n", body)
    cleaned = _BLANK_LINE_RUN.sub("\n\n", cleaned)
    if len(cleaned) <= max_chars:
        return cleaned
    keep = max_chars - len(_TRUNCATION_MARKER)
    return cleaned[:keep].rstrip() + _TRUNCATION_MARKER
