"""Brief → hard-requirements extractor.

Backend-agnostic — pure regex, no LLM. Parses the user's brief once and
returns a compact list of must-have features the swarm keeps dropping
(glassmorphism without backdrop-filter, dark mode without color-scheme,
command palette without keybinding, etc.).

The output is meant to be injected into every CodeAgent/DesignerAgent
prompt as a NON-NEGOTIABLE rules block. Keep the format short — 1-2 KB
max — so small/free models on weak context windows still see it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


# Each rule: (matchers in the brief, file types it applies to, NON_NEGOTIABLE rule text).
# Matching is OR across the patterns; case-insensitive; substring (not
# token-bounded) — keep these phrases tight so we don't fire on stray
# decorative mentions.
_RULES: List[tuple] = [
    # ----- Visual / CSS rules -----
    (("glassmorphism", "glass effect", "frosted glass"),
     ("css",),
     "GLASSMORPHISM: every panel / card / modal MUST include "
     "`backdrop-filter: blur(<value>)` AND `-webkit-backdrop-filter: blur(<value>)`. "
     "Use a translucent rgba background (alpha 0.05–0.20) on top of a dark page bg. "
     "1px hairline border with low alpha. Do not ship flat opaque cards when this is in the brief."),

    (("dark mode", "dark theme", "dark-mode"),
     ("css",),
     "DARK MODE: `:root { color-scheme: dark; }`, body bg is a dark hex from the palette, "
     "text contrast ≥ AA. Do not use a light page background."),

    (("light theme toggle", "theme toggle", "theme switcher", "light/dark"),
     ("jsx", "tsx", "ts", "js"),
     "THEME TOGGLE: add a control that flips `data-theme` on `<html>` or the root element "
     "between 'dark' and 'light'. Persist the choice in localStorage."),

    # ----- Keyboard / interaction rules -----
    (("command palette", "cmd+k", "ctrl+k", "⌘k", "cmdk"),
     ("jsx", "tsx"),
     "COMMAND PALETTE: must be invoked by Cmd+K / Ctrl+K. Use `useEffect` with a "
     "keydown listener, or `react-hotkeys-hook`. Render a modal listing actions; "
     "ESC closes it. Do not skip the keybinding."),

    (("keyboard shortcut", "hotkey"),
     ("jsx", "tsx"),
     "KEYBOARD SHORTCUTS: bind via `keydown` listener. Document each shortcut "
     "visually (e.g. tooltip or footer hint)."),

    # ----- API surface rules -----
    (("health endpoint", "/health", "healthcheck", "health check"),
     ("js", "ts", "py"),
     "HEALTH ENDPOINT: backend must expose `GET /api/health` (or `/health`) returning "
     "`{ok: true}` with HTTP 200. Wire the route before listening."),

    (("persistent config", "config persist", "settings persist",
      "persistent backend config", "config store"),
     ("js", "ts", "py"),
     "PERSISTENT CONFIG: writes survive restart. Use atomic file write (write-to-temp + rename) "
     "OR sqlite. Reads must reload from disk on boot, not from memory."),

    (("secret encryption", "encrypt secret", "encrypt api key", "aes-256", "aes256"),
     ("js", "ts", "py"),
     "SECRET ENCRYPTION: API keys / tokens MUST be encrypted at rest using AES-256-GCM "
     "(node `crypto`). Do not write plaintext secrets to disk. The keyfile must be "
     "loaded from env var or `~/.config/<app>/keyfile`."),

    # ----- UI shape rules -----
    (("activity feed", "activity log", "recent activity"),
     ("jsx", "tsx"),
     "ACTIVITY FEED: render a scrollable list of recent events with relative timestamps "
     "(e.g. '5m ago'). Use the API or websocket — do not hard-code rows."),

    (("sidebar", "side nav", "side navigation"),
     ("jsx", "tsx"),
     "SIDEBAR: render a left-side nav with the listed sections as links. "
     "Wrap with `<aside>` or a `Sidebar` component."),

    (("toast", "snackbar"),
     ("jsx", "tsx"),
     "TOASTS: use `<Toaster />` (react-hot-toast / sonner) or a custom `<Toast>` component. "
     "Show on success/error of async actions. Don't drop user feedback silently."),

    # ----- Backend / server rules -----
    (("rate limit", "ratelimit", "rate-limit"),
     ("js", "ts", "py"),
     "RATE LIMITING: middleware on the API surface (`express-rate-limit` or equivalent). "
     "Return 429 on excess; don't silently drop requests."),

    # ----- Quality bar -----
    (("polished", "premium", "production"),
     ("css", "jsx", "tsx", "ts", "js"),
     "POLISH: no TODO/FIXME/placeholder strings in critical files. Empty/loading/error "
     "states are required for every data-fetching surface. Numbers use tabular figures."),
]


@dataclass
class HardRequirements:
    """Structured requirements extracted from the brief."""
    rules_by_ext: dict = field(default_factory=dict)  # ext -> List[str]

    def for_file(self, rel_path: str) -> List[str]:
        ext = rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else ""
        return list(self.rules_by_ext.get(ext, []))

    def is_empty(self) -> bool:
        return not self.rules_by_ext


def extract_requirements(brief: str) -> HardRequirements:
    """Run regex over the brief and return rules grouped by file type."""
    out = HardRequirements()
    if not brief:
        return out
    brief_lower = brief.lower()
    for matchers, exts, rule_text in _RULES:
        if not any(m in brief_lower for m in matchers):
            continue
        for ext in exts:
            out.rules_by_ext.setdefault(ext, []).append(rule_text)
    return out


def _hex_luminance(hex_str: str) -> float:
    """Approx perceived luminance (0..1) for ranking 'dark' vs 'light' hexes."""
    s = hex_str.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) < 6:
        return 0.5
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return 0.5
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _assign_palette_slots(palette_hexes: List[str]) -> dict:
    """Pick dark hex for --bg, lightest for --text, mid for --accent.

    Without this, iteration order picked --bg=orange on canary-114's
    warm palette — wrong contrast, hurts dark-mode contract. This makes
    the prelude semantically correct for any palette.
    """
    if not palette_hexes:
        return {}
    by_lum = sorted(palette_hexes, key=_hex_luminance)
    out = {}
    # Darkest is bg, slightly lighter is surface
    out["--bg"] = by_lum[0]
    out["--surface"] = by_lum[1] if len(by_lum) > 1 else by_lum[0]
    # Lightest is text
    out["--text"] = by_lum[-1]
    # Most saturated middle one is accent (proxy: middle of brightness order)
    mid = len(by_lum) // 2
    out["--accent"] = by_lum[mid]
    return out


def _css_prelude(palette_hexes: List[str], has_dark: bool, has_glass: bool) -> str:
    """Build a CSS prelude block that the LLM is told to copy verbatim.

    This is the model-agnostic equivalent of "we wrote this for you, just
    keep it." Even cheap free-tier models faithfully copy literal blocks.
    """
    lines = ["/* === LOCKED PRELUDE — do not modify === */"]
    if has_dark:
        lines.append(":root {")
        lines.append("  color-scheme: dark;")
        slots = _assign_palette_slots(palette_hexes)
        if slots:
            for slot in ("--bg", "--surface", "--accent", "--text"):
                if slot in slots:
                    lines.append(f"  {slot}: {slots[slot]};")
        else:
            lines.append("  --bg: #0b0d10;")
            lines.append("  --surface: rgba(255,255,255,0.04);")
            lines.append("  --accent: #5a8dee;")
            lines.append("  --text: #e6e8eb;")
        lines.append("  --muted: rgba(255,255,255,0.55);")
        lines.append("}")
        lines.append("body {")
        lines.append("  background: var(--bg);")
        lines.append("  color: var(--text);")
        lines.append("  margin: 0;")
        lines.append("  font-family: Inter, system-ui, sans-serif;")
        lines.append("}")
    if has_glass:
        lines.append(".glass, .panel, .card, .modal-card, .drawer {")
        lines.append("  background: rgba(255,255,255,0.06);")
        lines.append("  backdrop-filter: blur(18px) saturate(140%);")
        lines.append("  -webkit-backdrop-filter: blur(18px) saturate(140%);")
        lines.append("  border: 1px solid rgba(255,255,255,0.08);")
        lines.append("  border-radius: 14px;")
        lines.append("}")
    lines.append("/* === END LOCKED PRELUDE === */")
    return "\n".join(lines)


def format_hard_rules(
    reqs: HardRequirements,
    rel_path: str,
    *,
    palette_hexes: Optional[List[str]] = None,
) -> str:
    """Return a markdown rules block to prepend to a CodeAgent file prompt.

    For `.css` files where the brief implies glass/dark, also includes
    a literal LOCKED PRELUDE the LLM is instructed to copy verbatim as
    the file's first lines. This is the cheapest, most backend-agnostic
    way to guarantee the right tokens land in the file.

    Returns empty string if no rules apply — never insert dead headers.
    """
    rules = reqs.for_file(rel_path)
    if not rules:
        return ""
    bullets = "\n".join(f"- {r}" for r in rules)
    out = (
        "## NON-NEGOTIABLE rules for this file (from brief)\n"
        f"{bullets}\n"
        "If you cannot honor a rule, write a one-line `// SKIPPED: <why>` "
        "comment inline rather than silently dropping it.\n\n"
    )

    if rel_path.lower().endswith((".css", ".scss", ".sass")):
        # Detect glass/dark from this file's rules — already filtered by
        # extract_requirements so we just check whether they fired.
        rules_blob = " ".join(rules).lower()
        has_glass = "glassmorphism" in rules_blob
        has_dark = "dark mode" in rules_blob
        if has_glass or has_dark:
            prelude = _css_prelude(palette_hexes or [], has_dark, has_glass)
            out += (
                "### REQUIRED PRELUDE — copy these lines VERBATIM as the first "
                "lines of the file before anything else:\n\n"
                "```css\n"
                f"{prelude}\n"
                "```\n\n"
                "Add your rules BELOW the `=== END LOCKED PRELUDE ===` marker. "
                "Do not change the prelude tokens.\n\n"
            )

    return out


def format_global_summary(reqs: HardRequirements) -> str:
    """Compact summary of all rules, file-type agnostic.

    Useful for system-level prompts where the model needs to know
    everything the brief promised across files.
    """
    if reqs.is_empty():
        return ""
    seen: List[str] = []
    for ext_rules in reqs.rules_by_ext.values():
        for r in ext_rules:
            if r not in seen:
                seen.append(r)
    bullets = "\n".join(f"- {r}" for r in seen)
    return (
        "## Brief-derived requirements (apply across the scaffold)\n"
        f"{bullets}\n\n"
    )
