#!/usr/bin/env python3
"""Report duplicate CSS rule blocks inside dashboard.html.

The dashboard's <style> block has been through 6+ "redesign passes,"
each one re-declaring the same selectors (.btn, :root, .studio-list li,
#convoFilter, etc.) without removing the older definitions. This script
parses the CSS, finds blocks that share a selector list, and prints a
report so we can decide which copies to delete.

This is REPORT-ONLY by default. Run with:

    python3 scripts/dedup_dashboard_css.py [--remove-older]

Add --remove-older to actually delete the duplicate copies (keeping
only the LAST occurrence — that's the one currently winning thanks to
CSS cascade order).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1] / "skyn3t" / "web" / "dashboard.html"

STYLE_OPEN = re.compile(r"<style[^>]*>", re.IGNORECASE)
STYLE_CLOSE = re.compile(r"</style>", re.IGNORECASE)


def extract_style_block(html: str):
    """Return (start, end, body) for the first <style>…</style> block."""
    m = STYLE_OPEN.search(html)
    if not m:
        return None
    start = m.end()
    close = STYLE_CLOSE.search(html, start)
    if not close:
        return None
    return start, close.start(), html[start : close.start()]


def iter_rules(css: str):
    """Yield (selector, full_block_including_braces, start, end) for each
    top-level CSS rule. Handles nested @media via depth tracking."""
    depth = 0
    in_string = None
    pos = 0
    n = len(css)
    rule_start = 0
    in_block = False
    selector = ""
    while pos < n:
        c = css[pos]
        if in_string:
            if c == in_string and css[pos - 1] != "\\":
                in_string = None
            pos += 1
            continue
        if c in ('"', "'"):
            in_string = c
            pos += 1
            continue
        if c == "/" and css[pos : pos + 2] == "/*":
            end = css.find("*/", pos + 2)
            pos = end + 2 if end != -1 else n
            continue
        if c == "{":
            if depth == 0:
                selector = css[rule_start:pos].strip()
                in_block = True
            depth += 1
            pos += 1
            continue
        if c == "}":
            depth -= 1
            if depth == 0 and in_block:
                yield selector, css[rule_start : pos + 1], rule_start, pos + 1
                in_block = False
                rule_start = pos + 1
            pos += 1
            continue
        pos += 1


def normalize_selector(sel: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", sel, flags=re.DOTALL)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--remove-older",
        action="store_true",
        help="Actually delete duplicate copies (keep the LAST one only).",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=2,
        help="Report selectors with at least this many duplicate blocks.",
    )
    args = parser.parse_args()

    html = DASHBOARD.read_text(encoding="utf-8")
    block = extract_style_block(html)
    if block is None:
        print("Could not locate <style> block")
        return 1
    style_start, style_end, css = block

    by_selector = defaultdict(list)
    for sel, body, s, e in iter_rules(css):
        key = normalize_selector(sel)
        if not key or key.startswith("@"):
            continue
        by_selector[key].append((s, e, body))

    dups = {k: v for k, v in by_selector.items() if len(v) >= args.min_count}
    if not dups:
        print("No duplicate selectors found.")
        return 0

    print(f"Found {len(dups)} duplicate selectors (>={args.min_count} occurrences):")
    for key in sorted(dups.keys(), key=lambda k: -len(dups[k])):
        occurrences = dups[key]
        print(f"  [{len(occurrences)}x]  {key[:120]}")

    if not args.remove_older:
        print()
        print("Dry run -- pass --remove-older to delete older copies.")
        return 0

    deletions = []
    for key, occs in dups.items():
        for s, e, _body in occs[:-1]:
            deletions.append((s, e))
    deletions.sort(reverse=True)

    new_css = css
    removed_blocks = 0
    removed_bytes = 0
    for s, e in deletions:
        if e < len(new_css) and new_css[e : e + 1] == "\n":
            e += 1
        new_css = new_css[:s] + new_css[e:]
        removed_blocks += 1
        removed_bytes += (e - s)

    new_html = html[:style_start] + new_css + html[style_end:]
    DASHBOARD.write_text(new_html, encoding="utf-8")
    print()
    print(f"Removed {removed_blocks} duplicate blocks ({removed_bytes:,} bytes).")
    print(f"dashboard.html: {len(html):,} -> {len(new_html):,} bytes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
