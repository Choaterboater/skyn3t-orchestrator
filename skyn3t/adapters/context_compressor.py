"""Context compressor — shrink verbose prompt payloads before the LLM call.

Cheap/free models choke on (and pay for) noisy RAG/scrape/log context. This is
one chokepoint, wired into ``LLMClient.complete``, gated by
``SKYN3T_COMPRESS_CONTEXT`` (default OFF) so a lossy drop can't silently change
build behavior until it's A/B'd against build success. (Research item C —
headroom concept.)
"""

from __future__ import annotations

import os
import re
from typing import Optional, Tuple

_TRAILING_WS = re.compile(r"[ \t]+\n")
_REPEAT_WS = re.compile(r"[ \t]{3,}")
_BLANK_RUN = re.compile(r"\n[ \t]*\n[ \t]*\n+")


def compress_enabled() -> bool:
    return os.environ.get("SKYN3T_COMPRESS_CONTEXT", "").strip().lower() in {
        "1", "on", "true", "yes",
    }


def _dedupe_lines(text: str) -> str:
    """Drop exact repeats of substantial lines (log spam, duplicated chunks)."""
    seen: set = set()
    out = []
    for line in text.split("\n"):
        key = line.strip()
        if len(key) > 20:
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > 5000:
                seen.clear()
        out.append(line)
    return "\n".join(out)


def compress_text(text: str, *, max_chars: int = 24_000) -> str:
    if not text:
        return text
    t = _TRAILING_WS.sub("\n", text)
    t = _REPEAT_WS.sub("  ", t)
    t = _BLANK_RUN.sub("\n\n", t)
    t = _dedupe_lines(t)
    if len(t) > max_chars:
        head = t[: int(max_chars * 0.7)]
        tail = t[-int(max_chars * 0.25):]
        t = head + "\n\n…[context compressed]…\n\n" + tail
    return t


def compress(prompt: str, system: Optional[str]) -> Tuple[str, Optional[str]]:
    """Compress (prompt, system) when enabled; otherwise return unchanged."""
    if not compress_enabled():
        return prompt, system
    return compress_text(prompt), (compress_text(system) if system else system)
