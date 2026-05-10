"""Designer Agent - brand, palette, and component scaffolding.

LLM-first. The agent reads the brief, asks an LLM to infer the aesthetic intent
(cyberpunk HUD, minimal SaaS, luxury, warm/organic, etc.), and emits
``brand.md``, ``palette.json`` and ``components.md`` that match that intent.

If the LLM is offline or returns garbage, the agent falls back to a curated
hash-keyed palette table per mood so output remains deterministic.
"""

from __future__ import annotations

import hashlib
import json
import re
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
    # Cyberpunk / Skynet / Terminator HUD aesthetic - dark gunmetal +
    # blood-red accents. Deterministic fallback when LLM is offline.
    "cyber": [
        ("#B11A1A", "#3A3F44", "#FF2A2A", "#0A0B0D", "#E5E7EB"),
        ("#8B0000", "#1F2937", "#EF4444", "#0B0F14", "#F1F5F9"),
        ("#A30000", "#2C2F36", "#FF3838", "#0D1117", "#E2E8F0"),
    ],
}


_FONTS_BY_MOOD: Dict[str, Dict[str, str]] = {
    "minimal": {"heading": "Inter", "body": "Inter", "mono": "JetBrains Mono"},
    "playful": {"heading": "Fraunces", "body": "Inter", "mono": "Fira Code"},
    "luxury": {"heading": "Playfair Display", "body": "Source Serif Pro", "mono": "IBM Plex Mono"},
    "techy": {"heading": "Space Grotesk", "body": "Inter", "mono": "JetBrains Mono"},
    "warm": {"heading": "DM Serif Display", "body": "DM Sans", "mono": "DM Mono"},
    "cyber": {"heading": "Orbitron", "body": "Rajdhani", "mono": "JetBrains Mono"},
}


_VOICE_BY_MOOD: Dict[str, List[str]] = {
    "minimal": ["clear", "calm", "precise"],
    "playful": ["lively", "warm", "candid"],
    "luxury": ["refined", "confident", "understated"],
    "techy": ["sharp", "honest", "specific"],
    "warm": ["welcoming", "earnest", "human"],
    "cyber": ["clinical", "ominous", "uncompromising"],
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
    "cyber": [
        "Reticle/crosshair glyph with the wordmark in a tactical monospace.",
        "Glitched wordmark: subtle RGB-split offset on a single character.",
        "All-caps wordmark inside a thin red HUD bracket pair.",
    ],
}


# Heuristic mood detection used when the LLM is unavailable. Order matters
# because we pick the first match; cyber should beat techy for Skynet briefs.
_MOOD_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("cyber", [
        "skynet", "terminator", "cyberpunk", "hud", "tactical", "military",
        "dystopian", "neon noir", "matrix", "blade runner", "war room",
    ]),
    ("luxury", ["luxury", "premium", "high-end", "elegant", "boutique", "couture"]),
    ("playful", ["playful", "friendly", "fun", "kids", "casual", "whimsical"]),
    ("warm", ["warm", "organic", "human", "earthy", "natural", "wellness"]),
    ("techy", ["developer", "engineer", "technical", "ai", "ml", "infra", "devtool"]),
    ("minimal", ["minimal", "clean", "saas", "simple", "modern"]),
]


_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}\b")


class DesignerAgent(BaseAgent):
    """Brand + palette + components scaffolder."""

    def __init__(
        self,
        name: str = "designer",
        event_bus: EventBus | None = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="designer",
            provider="local",
            event_bus=event_bus or EventBus(),
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

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        await self.think(f"{self.name} starting on {task.task_id}")

        data = task.input_data or {}
        brief: str = (data.get("brief") or "").strip() or "Untitled project"
        explicit_mood = (data.get("mood") or "").strip().lower()
        # If caller didn't specify a mood, infer it from the brief itself
        # rather than defaulting to "minimal".
        mood: str = explicit_mood or self._infer_mood(brief)
        if mood not in _PALETTES:
            mood = "minimal"
        target: str = (data.get("target") or "saas").lower()
        artifact_dir = Path(data.get("artifact_dir") or ".")
        next_agent: Optional[str] = data.get("next_agent")

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=f"artifact_dir error: {e}")

        # Palette: LLM-first, hash-keyed table fallback.
        palette_json = await self._pick_palette(brief, mood)
        await self.think(f"selected palette for mood='{mood}'")
        fonts = _FONTS_BY_MOOD.get(mood, _FONTS_BY_MOOD["minimal"])
        voice = _VOICE_BY_MOOD.get(mood, _VOICE_BY_MOOD["minimal"])
        logos = _LOGO_CONCEPTS_BY_MOOD.get(mood, _LOGO_CONCEPTS_BY_MOOD["minimal"])

        palette_tuple: Tuple[str, str, str, str, str] = (
            palette_json["primary"],
            palette_json["secondary"],
            palette_json["accent"],
            palette_json["bg"],
            palette_json["text"],
        )

        brand_md = await self._render_brand_md(brief, mood, palette_tuple, fonts, voice, logos)
        brand_path = artifact_dir / "brand.md"
        brand_path.write_text(brand_md, encoding="utf-8")
        await self.think(f"wrote {brand_path.name}")

        palette_path = artifact_dir / "palette.json"
        palette_path.write_text(json.dumps(palette_json, indent=2), encoding="utf-8")
        await self.think(f"wrote {palette_path.name}")

        components_md = await self._render_components_md(brief, mood, target, palette_json)
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
            f"Designer mood='{mood}' palette wired via LLM with deterministic fallback.",
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

    # ------------------------------------------------------------------
    # LLM helper
    # ------------------------------------------------------------------
    async def _llm_generate(
        self,
        *,
        role_prompt: str,
        brief: str,
        fallback: str,
        max_tokens: int = 2500,
        kind: Optional[str] = None,
    ) -> str:
        try:
            client = self.get_llm() if hasattr(self, "get_llm") else None
            if client is None:
                from skyn3t.adapters import LLMClient
                client = LLMClient(
                    default_model=self.config.get("model"),
                    backend=self.config.get("backend"),
                    event_bus=self.event_bus,
                    caller_name=self.name,
                )
            cot_preamble = (
                "Think step-by-step:\n"
                "1. Read the brief carefully — what aesthetic intent does the USER want? "
                "(Skynet/Terminator? minimal? warm? luxury?)\n"
                "2. Don't impose a generic 'minimal' default — match the brief.\n"
                "3. Pick palette colors that EVOKE the intent (e.g. Skynet → deep gunmetal + blood-red, "
                "NOT cyan/magenta).\n"
                "4. Pair fonts to the palette mood.\n"
                "THEN produce the artifact.\n\n"
            )
            prompt = (
                f"{cot_preamble}{role_prompt}\n\nBrief from user:\n{brief}\n\n"
                "Produce ONLY the markdown (or JSON if asked) - no code fences, no preamble."
            )
            # For brand artifacts, prepend few-shot examples from prior projects.
            if kind == "brand":
                try:
                    from skyn3t.adapters.few_shot import few_shot_block
                    shots = few_shot_block("brand", count=2)
                    if shots:
                        prompt = shots + "\n\n# Now the new task:\n" + prompt
                except Exception:
                    pass
            out = await client.complete(prompt, max_tokens=max_tokens, temperature=0.7)
            if out and "[deterministic-stub]" not in out and len(out.strip()) > 80:
                return out.strip()
        except Exception:
            pass
        return fallback

    # ------------------------------------------------------------------
    # Mood + palette selection
    # ------------------------------------------------------------------
    def _infer_mood(self, brief: str) -> str:
        b = (brief or "").lower()
        for mood, keywords in _MOOD_KEYWORDS:
            for kw in keywords:
                if kw in b:
                    return mood
        return "minimal"

    async def _pick_palette(self, brief: str, mood: str) -> Dict[str, str]:
        """LLM-first palette picker. Falls back to hashed curated table."""
        fallback_palette = self._fallback_palette(brief, mood)
        fallback_json = json.dumps({
            "primary": fallback_palette[0],
            "secondary": fallback_palette[1],
            "accent": fallback_palette[2],
            "bg": fallback_palette[3],
            "text": fallback_palette[4],
        })

        role = (
            "You are a brand designer. Given the user's brief, infer the "
            "aesthetic intent (cyberpunk HUD, minimal SaaS, warm/organic, "
            "luxury, etc.) and produce a JSON palette with keys: primary, "
            "secondary, accent, bg, text. Use real hex colors that fit the "
            "aesthetic. Return ONLY the JSON object, nothing else.\n\n"
            "Examples of intent: 'Skynet/Terminator' -> dark gunmetal bg, "
            "blood-red primary. 'Minimal SaaS' -> white bg, single blue "
            "accent. 'Luxury' -> cream bg, gold accent."
        )
        raw = await self._llm_generate(
            role_prompt=role,
            brief=brief,
            fallback=fallback_json,
            max_tokens=400,
        )

        parsed = self._parse_palette_json(raw)
        if parsed is not None:
            return parsed

        # If parse failed, fall back to deterministic table.
        return {
            "primary": fallback_palette[0],
            "secondary": fallback_palette[1],
            "accent": fallback_palette[2],
            "bg": fallback_palette[3],
            "text": fallback_palette[4],
        }

    def _fallback_palette(self, brief: str, mood: str) -> Tuple[str, str, str, str, str]:
        rows = _PALETTES.get(mood, _PALETTES["minimal"])
        digest = hashlib.sha256(f"{brief}|{mood}".encode("utf-8")).hexdigest()
        idx = int(digest[:8], 16) % len(rows)
        return rows[idx]

    def _parse_palette_json(self, raw: str) -> Optional[Dict[str, str]]:
        """Best-effort JSON parse. Tolerates code fences and trailing prose."""
        if not raw:
            return None
        s = raw.strip()
        # Strip code fences if the model added them despite instructions.
        if s.startswith("```"):
            s = s.strip("`")
            if s.lower().startswith("json"):
                s = s[4:].lstrip()
        # Find the first `{` and the matching closing `}`.
        start = s.find("{")
        end = s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            obj = json.loads(s[start:end + 1])
        except Exception:
            return None
        required = ("primary", "secondary", "accent", "bg", "text")
        out: Dict[str, str] = {}
        for k in required:
            v = obj.get(k)
            if not isinstance(v, str):
                return None
            v = v.strip()
            if not _HEX_RE.fullmatch(v):
                # Allow #RGB shorthand by expanding.
                m = re.fullmatch(r"#([0-9A-Fa-f]{3})", v)
                if m:
                    v = "#" + "".join(c * 2 for c in m.group(1))
                else:
                    return None
            out[k] = v.upper() if v.startswith("#") else v
        return out

    # ------------------------------------------------------------------
    # Brand.md
    # ------------------------------------------------------------------
    async def _render_brand_md(
        self,
        brief: str,
        mood: str,
        palette: Tuple[str, str, str, str, str],
        fonts: Dict[str, str],
        voice: List[str],
        logos: List[str],
    ) -> str:
        fallback = self._render_brand_md_fallback(brief, mood, palette, fonts, voice, logos)
        primary, secondary, accent, bg, text = palette
        role = (
            "You are a brand designer. Produce a markdown brand document for "
            "the project described in the brief. The document must include: "
            "a one-line summary of the inferred aesthetic; a Palette section "
            "that USES the exact hex values supplied below; a Typography "
            "section with concrete heading/body/mono font picks that match "
            "the aesthetic; a Voice section listing exactly 3 adjectives; a "
            "Logo concepts section with 3 distinct, brief-specific concepts "
            "(NOT generic templates). Tie every section to the brief's "
            "aesthetic intent.\n\n"
            f"Inferred mood label: {mood}\n"
            f"Palette to reference (use these exact hex codes):\n"
            f"  primary={primary}\n  secondary={secondary}\n  accent={accent}\n"
            f"  bg={bg}\n  text={text}\n"
            f"Suggested typography (you may override if it fits better): "
            f"heading={fonts['heading']}, body={fonts['body']}, mono={fonts['mono']}\n"
        )
        return await self._llm_generate(
            role_prompt=role,
            brief=brief,
            fallback=fallback,
            max_tokens=1800,
            kind="brand",
        )

    def _render_brand_md_fallback(
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

    # ------------------------------------------------------------------
    # Components.md
    # ------------------------------------------------------------------
    async def _render_components_md(
        self,
        brief: str,
        mood: str,
        target: str,
        palette: Dict[str, str],
    ) -> str:
        fallback = self._render_components_md_fallback(target, palette)
        role = (
            "You are a UI lead. Produce a markdown document recommending the "
            "core UI components for this project. Open with a one-line note "
            "on how the components should feel given the aesthetic intent. "
            "Then provide a Markdown table with columns: Component | Class "
            "hint. Include 6-10 components that are appropriate to the "
            "target platform AND the aesthetic. Use the exact hex values "
            "supplied below in class hints where a color is needed.\n\n"
            f"Target platform: {target}\n"
            f"Inferred mood label: {mood}\n"
            f"Palette tokens to reference verbatim where helpful:\n"
            f"  primary={palette['primary']}\n  secondary={palette['secondary']}\n"
            f"  accent={palette['accent']}\n  bg={palette['bg']}\n"
            f"  text={palette['text']}\n"
        )
        return await self._llm_generate(
            role_prompt=role,
            brief=brief,
            fallback=fallback,
            max_tokens=1500,
        )

    def _render_components_md_fallback(self, target: str, palette: Dict[str, str]) -> str:
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
