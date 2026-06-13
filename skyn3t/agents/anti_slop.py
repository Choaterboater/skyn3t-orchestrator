"""Anti-slop checks — mechanical "AI tell" patterns in generated frontend output.

A gradeable static gate that feeds the build verdict → skill grading → learnings
loop. Catches the obvious tells cheap/free models leave behind: placeholder
content, em-dashes in copy, overused default fonts, scroll-jank listeners. Tuned
to NOT fire on the dashboards/ops consoles SkyN3t often builds (Research item E,
taste-skill — rules only, not its landing-page framework defaults).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

_PLACEHOLDER = re.compile(
    r"\b(Jane Doe|John Doe|Acme(?:\s+(?:Inc|Corp|Co))?|Lorem ipsum|"
    r"example@example\.com|your-name-here|TODO: replace)\b",
    re.IGNORECASE,
)
_BANNED_FONTS = ("fraunces", "playfair display")  # overused AI-default display fonts
_SCROLL_JANK = re.compile(r"addEventListener\(\s*['\"]scroll['\"]")
_MARKUP_EXTS = (".html", ".jsx", ".tsx", ".vue", ".svelte", ".css", ".js", ".ts")
_SCAN_EXTS = (".html", ".jsx", ".tsx", ".vue", ".svelte", ".css")


def scan_text(text: str, path: str = "") -> List[Dict[str, str]]:
    """Return anti-slop findings for one file's text."""
    findings: List[Dict[str, str]] = []
    if not text or not str(path).lower().endswith(_MARKUP_EXTS):
        return findings
    low = text.lower()
    if _PLACEHOLDER.search(text):
        findings.append({
            "path": path, "rule": "placeholder_content",
            "detail": "shipped placeholder content (Jane Doe / Acme / Lorem ipsum)",
        })
    if text.count("—") >= 2:
        findings.append({
            "path": path, "rule": "em_dash_copy",
            "detail": "multiple em-dashes in copy — a common AI tell",
        })
    for font in _BANNED_FONTS:
        if font in low:
            findings.append({
                "path": path, "rule": "banned_font",
                "detail": f"overused AI-default font: {font}",
            })
    if _SCROLL_JANK.search(text):
        findings.append({
            "path": path, "rule": "scroll_listener",
            "detail": "raw scroll listener (jank; prefer IntersectionObserver)",
        })
    return findings


def scan_project(root, *, max_files: int = 400) -> List[Dict[str, str]]:
    """Scan a scaffold dir for anti-slop findings (skips node_modules)."""
    findings: List[Dict[str, str]] = []
    base = Path(root)
    count = 0
    for p in base.rglob("*"):
        if count >= max_files:
            break
        if "node_modules" in p.parts or p.suffix.lower() not in _SCAN_EXTS:
            continue
        count += 1
        try:
            rel = str(p.relative_to(base))
            findings.extend(scan_text(p.read_text(encoding="utf-8", errors="ignore"), rel))
        except Exception:
            continue
    return findings
