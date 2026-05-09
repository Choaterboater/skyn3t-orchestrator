"""Designer Agent - brand, palette, and component scaffolding.

LLM-free. Picks 5 hex colors deterministically by hashing brief+mood against
a curated palette table per mood. Emits ``brand.md``, ``palette.json`` and
``components.md``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus


# Curated palettes per mood. Each row is a 5-color set ordered as
# (primary, secondary, accent, bg, text). The hashing step picks one row.
_PALETTES: Dict[str, List[Tuple[str, str, str, str, str]]] = {
    "minimal": [
        ("#111827", "#374151", "#6366F1", "#FFFFFF", "#111827"),
        ("#0F172A", "#475569", "#10B981", "#F8FAFC", "#0F172A"),
        ("#18181B", "#52525B", "#3B82F6", "#FAFAFA", "#18181B"),
    ],
    "playful": [
        ("#FF5C8A", "#FFD166", "#06D6A0", "#FFF8F0", "#22223B"),
        ("#FF7AB6", "#A0E7E5", "#FBE7C6", "#FFF0F6", "#2B2D42"),
        ("#F97316", "#FACC15", "#22D3EE", "#FFFBEB", "#1F2937"),
    ],
    "luxury": [
        ("#0B0B0B", "#C9A227", "#E5E5E5", "#FFFFFF", "#0B0B0B"),
        ("#1B1B1F", "#A07A2C", "#D4AF37", "#F5F2EC", "#1B1B1F"),
        ("#0E0E10", "#8C7853", "#B08D57", "#FAF7F2", "#0E0E10"),
    ],
    "techy": [
        ("#22D3EE", "#A78BFA", "#34D399", "#0B1020", "#E2E8F0"),
        ("#06B6D4", "#8B5CF6", "#22C55E", "#0F172A", "#F8FAFC"),
        ("#38BDF8", "#F472B6", "#FACC15", "#020617", "#E0F2FE"),
    ],
    "warm": [
        ("#D97706", "#92400E", "#F59E0B", "#FFFBEB", "#78350F"),
        ("#B45309", "#7C2D12", "#F97316", "#FFF7ED", "#7C2D12"),
        ("#C2410C", "#9A3412", "#FBBF24", "#FFEDD5", "#7C2D12"),
    ],
}


_FONTS_BY_MOOD: Dict[str, Dict[str, str]] = {
    "minimal": {"heading": "Inter", "body": "Inter", "mono": "JetBrains Mono"},
    "playful": {"heading": "Fraunces", "body": "Inter", "mono": "Fira Code"},
    "luxury": {"heading": "Playfair Display", "body": "Source Serif Pro", "mono": "IBM Plex Mono"},
    "techy": {"heading": "Space Grotesk", "body": "Inter", "mono": "JetBrains Mono"},
    "warm": {"heading": "DM Serif Display", "body": "DM Sans", "mono": "DM Mono"},
}


_VOICE_BY_MOOD: Dict[str, List[str]] = {
    "minimal": ["clear", "calm", "precise"],
    "playful": ["lively", "warm", "candid"],
    "luxury": ["refined", "confident", "understated"],
    "techy": ["sharp", "honest", "specific"],
    "warm": ["welcoming", "earnest", "human"],
}


_LOGO_CONCEPTS_BY_MOOD: Dict[str, List[str]] = {
    "minimal": [
        "Single-letter monogram in the heading typeface, generous whitespace.",
        "Geometric circle + line pairing, monochrome.",
        "Lowercase wordmark with a single colored dot above the i/j.",
    ],
    "playful": [
        "Hand-lettered wordmark with a slight bounce in the baseline.",
        "Mascot silhouette (e.g. friendly rounded blob) + wordmark stack.",
        "Gradient ribbon underline beneath the wordmark.",
    ],
    "luxury": [
        "All-caps serif wordmark with letter-spacing and a hairline rule.",
        "Crest-style monogram inside a thin gold border.",
        "Wordmark with a custom ligature on the boldest pair of letters.",
    ],
    "techy": [
        "Wordmark with a bracket pair flanking the name.",
        "ASCII-art glyph (e.g. /\\, [], <>) preceding the wordmark.",
        "Monospace wordmark with a colored cursor block at the end.",
    ],
    "warm": [
        "Soft serif wordmark with a hand-drawn underline.",
        "Sun/leaf glyph paired with the wordmark.",
        "Stamp-style circular badge with the brand name and tagline.",
    ],
}


class DesignerAgent(BaseAgent):
    """Brand + palette + components scaffolder."""

    def __init__(
        self,
        name: str = "designer",
        event_bus: EventBus = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="designer",
            provider="local",
            event_bus=event_bus,
            config=config,
        )
        self.add_capability(AgentCapability(
            name="design",
            description="Produce brand.md with palette, typography, voice, and logo concepts.",
            parameters={"brief": "str", "mood": "str"},
        ))
        self.add_capability(AgentCapability(
            name="branding",
            description="Pick a 5-color palette and emit palette.json.",
            parameters={"brief": "str", "mood": "str"},
        ))
        self.add_capability(AgentCapability(
            name="ui",
            description="Recommend UI components with Tailwind class hints.",
            parameters={"target": "str"},
        ))

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return bool(_PALETTES)

    async def execute(self, task: TaskRequest) -> TaskResult:
        await self.think(f"{self.name} starting on {task.task_id}")

        data = task.input_data or {}
        brief: str = (data.get("brief") or "").strip() or "Untitled project"
        mood: str = (data.get("mood") or "minimal").lower()
        if mood not in _PALETTES:
            mood = "minimal"
        target: str = (data.get("target") or "saas").lower()
        artifact_dir = Path(data.get("artifact_dir") or ".")
        next_agent: Optional[str] = data.get("next_agent")

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=f"artifact_dir error: {e}")

        palette = self._pick_palette(brief, mood)
        await self.think(f"selected palette for mood='{mood}'")
        fonts = _FONTS_BY_MOOD.get(mood, _FONTS_BY_MOOD["minimal"])
        voice = _VOICE_BY_MOOD.get(mood, _VOICE_BY_MOOD["minimal"])
        logos = _LOGO_CONCEPTS_BY_MOOD.get(mood, _LOGO_CONCEPTS_BY_MOOD["minimal"])

        brand_md = self._render_brand_md(brief, mood, palette, fonts, voice, logos)
        brand_path = artifact_dir / "brand.md"
        brand_path.write_text(brand_md, encoding="utf-8")
        await self.think(f"wrote {brand_path.name}")

        palette_json = {
            "primary": palette[0],
            "secondary": palette[1],
            "accent": palette[2],
            "bg": palette[3],
            "text": palette[4],
        }
        palette_path = artifact_dir / "palette.json"
        palette_path.write_text(json.dumps(palette_json, indent=2), encoding="utf-8")
        await self.think(f"wrote {palette_path.name}")

        components_md = self._render_components_md(target, palette_json)
        components_path = artifact_dir / "components.md"
        components_path.write_text(components_md, encoding="utf-8")
        await self.think(f"wrote {components_path.name}")

        files = [str(brand_path), str(palette_path), str(components_path)]

        if next_agent:
            await self.send_message(
                to=next_agent,
                kind="info",
                content=f"{self.name} done; artifacts in {artifact_dir}",
                payload={"files": files, "palette": palette_json, "mood": mood},
            )

        await self.share_learning(
            f"Designer mood='{mood}' palette stable across runs (hash-keyed).",
            scope="global",
            mood=mood,
        )

        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "files": files,
                "palette": palette_json,
                "mood": mood,
                "summary": f"Brand pack written ({len(files)} files).",
            },
        )

    def _pick_palette(self, brief: str, mood: str) -> Tuple[str, str, str, str, str]:
        rows = _PALETTES[mood]
        digest = hashlib.sha256(f"{brief}|{mood}".encode("utf-8")).hexdigest()
        idx = int(digest[:8], 16) % len(rows)
        return rows[idx]

    def _render_brand_md(
        self,
        brief: str,
        mood: str,
        palette: Tuple[str, str, str, str, str],
        fonts: Dict[str, str],
        voice: List[str],
        logos: List[str],
    ) -> str:
        primary, secondary, accent, bg, text = palette
        lines = [
            f"# Brand - {brief}",
            "",
            f"_Mood: **{mood}**_",
            "",
            "## Palette",
            "",
            f"- **Primary**   `{primary}`",
            f"- **Secondary** `{secondary}`",
            f"- **Accent**    `{accent}`",
            f"- **Background**`{bg}`",
            f"- **Text**      `{text}`",
            "",
            "## Typography",
            "",
            f"- **Heading**: {fonts['heading']}",
            f"- **Body**:    {fonts['body']}",
            f"- **Mono**:    {fonts['mono']}",
            "",
            "## Voice",
            "",
            f"Three adjectives that describe how the brand sounds in writing: "
            f"**{voice[0]}**, **{voice[1]}**, **{voice[2]}**.",
            "",
            "Use the active voice. Prefer short sentences. Avoid hype words.",
            "",
            "## Logo concepts",
            "",
        ]
        for i, c in enumerate(logos, start=1):
            lines.append(f"{i}. {c}")
        lines.append("")
        lines.append("## Usage notes")
        lines.append("")
        lines.append(
            "- Maintain a 4.5:1 contrast ratio between text and background.\n"
            "- Use the accent color for one CTA per screen, never two.\n"
            "- Reserve the secondary color for supporting UI (borders, dividers)."
        )
        lines.append("")
        return "\n".join(lines)

    def _render_components_md(self, target: str, palette: Dict[str, str]) -> str:
        primary = palette["primary"]
        accent = palette["accent"]
        bg = palette["bg"]
        components_by_target: Dict[str, List[Dict[str, str]]] = {
            "saas": [
                {"name": "AppShell", "tw": "min-h-screen flex bg-[VAR_BG] text-[VAR_TEXT]"},
                {"name": "TopNav", "tw": "h-14 border-b flex items-center px-4 gap-3"},
                {"name": "PrimaryButton", "tw": "px-4 py-2 rounded-md bg-[VAR_PRIMARY] text-white hover:opacity-90"},
                {"name": "Card", "tw": "rounded-xl border bg-white/50 dark:bg-zinc-900/50 p-4 shadow-sm"},
                {"name": "Table", "tw": "w-full text-sm [&_th]:font-medium [&_td]:py-2"},
                {"name": "Modal", "tw": "fixed inset-0 grid place-items-center bg-black/40"},
                {"name": "Toast", "tw": "fixed bottom-4 right-4 rounded-md bg-zinc-900 text-white px-3 py-2"},
            ],
            "site": [
                {"name": "Hero", "tw": "max-w-5xl mx-auto px-6 py-20 text-center"},
                {"name": "FeatureGrid", "tw": "grid md:grid-cols-3 gap-6 max-w-5xl mx-auto px-6"},
                {"name": "CTA", "tw": "rounded-md bg-[VAR_ACCENT] text-white px-5 py-3"},
                {"name": "Footer", "tw": "border-t mt-24 py-10 text-sm text-zinc-500"},
            ],
            "mobile": [
                {"name": "Screen", "tw": "(NativeWind) flex-1 bg-[VAR_BG]"},
                {"name": "ListItem", "tw": "(NativeWind) flex-row items-center gap-3 p-3"},
                {"name": "PrimaryButton", "tw": "(NativeWind) bg-[VAR_PRIMARY] rounded-2xl py-3 px-5"},
                {"name": "TabBar", "tw": "(NativeWind) h-14 border-t flex-row justify-around"},
            ],
            "cli": [
                {"name": "Banner", "tw": "(Rich) Panel(title=, border_style='cyan')"},
                {"name": "Spinner", "tw": "(Rich) Status('Working...', spinner='dots')"},
                {"name": "Table", "tw": "(Rich) Table(show_lines=True)"},
                {"name": "Prompt", "tw": "(Rich) Prompt.ask('> ')"},
            ],
        }
        comps = components_by_target.get(target, components_by_target["saas"])

        out = [
            f"# Components - {target} target",
            "",
            "Recommended primitives. Replace `VAR_*` tokens with the values from",
            "`palette.json`:",
            "",
            f"- `VAR_PRIMARY` -> `{palette['primary']}`",
            f"- `VAR_SECONDARY` -> `{palette['secondary']}`",
            f"- `VAR_ACCENT` -> `{palette['accent']}`",
            f"- `VAR_BG` -> `{palette['bg']}`",
            f"- `VAR_TEXT` -> `{palette['text']}`",
            "",
            "| Component | Class hint |",
            "| --- | --- |",
        ]
        for c in comps:
            tw = (c["tw"]
                  .replace("VAR_PRIMARY", primary)
                  .replace("VAR_ACCENT", accent)
                  .replace("VAR_BG", bg)
                  .replace("VAR_TEXT", palette["text"]))
            out.append(f"| `{c['name']}` | `{tw}` |")
        out.append("")
        return "\n".join(out)
