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

        # ── Usable code deliverables ────────────────────────────────────
        # Markdown docs are good but not droppable into a project. Generate
        # the assets a developer would actually copy:
        #   - tokens.css   : CSS custom properties, importable anywhere
        #   - tokens.json  : W3C-style design tokens, Tailwind/Figma-friendly
        #   - logo.svg     : a real vector asset matching the first logo concept
        #   - README.md    : how to use the kit
        tokens_css_path = artifact_dir / "tokens.css"
        tokens_css_path.write_text(
            self._render_tokens_css(palette_json, fonts), encoding="utf-8"
        )
        await self.think(f"wrote {tokens_css_path.name}")

        tokens_json_path = artifact_dir / "tokens.json"
        tokens_json_path.write_text(
            json.dumps(self._render_design_tokens(palette_json, fonts), indent=2),
            encoding="utf-8",
        )
        await self.think(f"wrote {tokens_json_path.name}")

        logo_svg_path = artifact_dir / "logo.svg"
        logo_svg_path.write_text(
            self._render_logo_svg(brief, mood, palette_json),
            encoding="utf-8",
        )
        await self.think(f"wrote {logo_svg_path.name}")

        readme_path = artifact_dir / "README.md"
        readme_path.write_text(
            self._render_kit_readme(brief, palette_json, fonts),
            encoding="utf-8",
        )
        await self.think(f"wrote {readme_path.name}")

        files = [
            str(brand_path), str(palette_path), str(components_path),
            str(tokens_css_path), str(tokens_json_path),
            str(logo_svg_path), str(readme_path),
        ]

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
            max_tokens=4000,
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
            max_tokens=3500,
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

    # ─── Usable code-asset renderers ────────────────────────────────────
    # These produce real files a developer can drop into a project, not
    # markdown summaries. They cover the gap between "the agent told me
    # what to build" and "the agent gave me something I can use."

    @staticmethod
    def _render_tokens_css(palette: Dict[str, str], fonts: Dict[str, str]) -> str:
        """Emit CSS custom properties keyed by role. Safe to @import anywhere."""
        heading = fonts.get("heading", "system-ui")
        body = fonts.get("body", "system-ui")
        mono = fonts.get("mono", "ui-monospace")
        return (
            "/* Generated by SkyN3t DesignerAgent — design tokens.\n"
            " * Drop this file into your project and reference the variables\n"
            " * via var(--brand-primary), var(--brand-accent), etc.\n"
            " */\n"
            ":root {\n"
            f"  --brand-primary: {palette['primary']};\n"
            f"  --brand-secondary: {palette['secondary']};\n"
            f"  --brand-accent: {palette['accent']};\n"
            f"  --brand-bg: {palette['bg']};\n"
            f"  --brand-text: {palette['text']};\n"
            "\n"
            f"  --brand-font-heading: {heading};\n"
            f"  --brand-font-body: {body};\n"
            f"  --brand-font-mono: {mono};\n"
            "}\n"
        )

    @staticmethod
    def _render_design_tokens(palette: Dict[str, str], fonts: Dict[str, str]) -> Dict[str, Any]:
        """Emit a W3C design-tokens-shape JSON dict. Compatible with Style
        Dictionary, Tokens Studio, Tailwind plugins, etc."""
        def color_token(value: str) -> Dict[str, str]:
            return {"value": value, "type": "color"}

        def font_token(value: str) -> Dict[str, str]:
            return {"value": value, "type": "fontFamily"}

        return {
            "color": {
                "primary": color_token(palette["primary"]),
                "secondary": color_token(palette["secondary"]),
                "accent": color_token(palette["accent"]),
                "bg": color_token(palette["bg"]),
                "text": color_token(palette["text"]),
            },
            "font": {
                "heading": font_token(fonts.get("heading", "system-ui")),
                "body": font_token(fonts.get("body", "system-ui")),
                "mono": font_token(fonts.get("mono", "ui-monospace")),
            },
        }

    @staticmethod
    def _render_logo_svg(brief: str, mood: str, palette: Dict[str, str]) -> str:
        """Generate a real SVG logo based on the chosen palette and mood.

        Aesthetic-specific marks rather than a single generic shape — the
        mood drives the geometry. Falls back to an initials-roundel for
        moods we don't have explicit art for.
        """
        primary = palette["primary"]
        secondary = palette["secondary"]
        accent = palette["accent"]
        bg = palette["bg"]
        text = palette["text"]

        # Pull a 1-3 char monogram from the first significant word in the brief.
        words = [w for w in re.split(r"[^A-Za-z0-9]+", brief or "") if w]
        # If the brief contains a quoted name use that first.
        quoted = re.search(r"['\"]([A-Za-z0-9]+)['\"]", brief or "")
        if quoted:
            seed = quoted.group(1)
        elif words:
            # prefer a "name-like" capitalized or all-caps word
            named = next((w for w in words if w[:1].isupper()), words[0])
            seed = named
        else:
            seed = "X"
        mono = (seed[:3] if len(seed) <= 3 else seed[0].upper()).upper()

        if mood in {"hud", "military", "tactical", "cyberpunk"}:
            # Reticle: concentric incomplete rings + center dot, classified bar.
            return (
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 240" '
                f'role="img" aria-label="{mono} logo">\n'
                f'  <rect width="240" height="240" fill="{bg}"/>\n'
                f'  <circle cx="120" cy="120" r="88" fill="none" '
                f'stroke="{text}" stroke-width="2" stroke-dasharray="22 8"/>\n'
                f'  <circle cx="120" cy="120" r="60" fill="none" '
                f'stroke="{text}" stroke-width="1.5" opacity="0.6"/>\n'
                f'  <line x1="120" y1="20" x2="120" y2="46" stroke="{text}" stroke-width="2"/>\n'
                f'  <line x1="120" y1="194" x2="120" y2="220" stroke="{text}" stroke-width="2"/>\n'
                f'  <line x1="20" y1="120" x2="46" y2="120" stroke="{text}" stroke-width="2"/>\n'
                f'  <line x1="194" y1="120" x2="220" y2="120" stroke="{text}" stroke-width="2"/>\n'
                f'  <circle cx="120" cy="120" r="6" fill="{primary}"/>\n'
                f'  <rect x="32" y="116" width="176" height="2" fill="{primary}" opacity="0.0"/>\n'
                f'</svg>\n'
            )
        if mood in {"luxury", "editorial", "warm"}:
            # Monogram set in a thin circle, serif-feeling sans (use generic
            # SVG-default — the logo will be rasterized or replaced with type).
            return (
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 240" '
                f'role="img" aria-label="{mono} logo">\n'
                f'  <rect width="240" height="240" fill="{bg}"/>\n'
                f'  <circle cx="120" cy="120" r="92" fill="none" '
                f'stroke="{accent}" stroke-width="1"/>\n'
                f'  <text x="120" y="146" text-anchor="middle" '
                f'font-family="Georgia, serif" font-style="italic" '
                f'font-size="92" fill="{text}">{mono[0]}</text>\n'
                f'</svg>\n'
            )
        if mood in {"playful", "warm-organic"}:
            # Layered soft circles.
            return (
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 240" '
                f'role="img" aria-label="{mono} logo">\n'
                f'  <rect width="240" height="240" fill="{bg}"/>\n'
                f'  <circle cx="100" cy="120" r="58" fill="{primary}" opacity="0.85"/>\n'
                f'  <circle cx="140" cy="120" r="58" fill="{accent}" opacity="0.85"/>\n'
                f'</svg>\n'
            )
        # Minimal default: square monogram.
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 240" '
            f'role="img" aria-label="{mono} logo">\n'
            f'  <rect width="240" height="240" fill="{bg}"/>\n'
            f'  <rect x="32" y="32" width="176" height="176" fill="none" '
            f'stroke="{primary}" stroke-width="2"/>\n'
            f'  <text x="120" y="146" text-anchor="middle" '
            f'font-family="ui-sans-serif, system-ui" font-weight="700" '
            f'font-size="84" fill="{text}">{mono}</text>\n'
            f'</svg>\n'
        )

    @staticmethod
    def _render_kit_readme(brief: str, palette: Dict[str, str], fonts: Dict[str, str]) -> str:
        """A small how-to-use-this-kit doc so the receiver isn't guessing."""
        out = [
            "# Brand Kit — How to use",
            "",
            f"_Generated by SkyN3t for brief:_ **{(brief or '').strip()[:160]}**",
            "",
            "## What's here",
            "",
            "| File | Use |",
            "| --- | --- |",
            "| `brand.md` | Narrative brand guide: palette, type, voice, logo concepts. |",
            "| `palette.json` | Raw 5-color palette (primary/secondary/accent/bg/text). |",
            "| `tokens.css` | CSS custom properties — `@import` into any web project. |",
            "| `tokens.json` | W3C design-tokens shape — feed into Style Dictionary, Tokens Studio, Tailwind plugins. |",
            "| `logo.svg` | Vector logo at 240×240. Drop into a hero, app icon, or favicon pipeline. |",
            "| `components.md` | Component class hints (Tailwind-shaped) for primary surfaces. |",
            "| `brand_voice_guide.md` | Long-form voice + copy patterns. |",
            "| `review.md` | Reviewer notes — verdict + score for this kit. |",
            "",
            "## Quick drop-in",
            "",
            "```html",
            '<link rel="stylesheet" href="./tokens.css">',
            "<style>",
            "  body { background: var(--brand-bg); color: var(--brand-text); ",
            "         font-family: var(--brand-font-body); }",
            "  h1   { font-family: var(--brand-font-heading); color: var(--brand-primary); }",
            "  code { font-family: var(--brand-font-mono); }",
            "</style>",
            "```",
            "",
            "## Palette",
            "",
            f"- Primary: `{palette['primary']}`",
            f"- Secondary: `{palette['secondary']}`",
            f"- Accent: `{palette['accent']}`",
            f"- Background: `{palette['bg']}`",
            f"- Text: `{palette['text']}`",
            "",
            "## Type",
            "",
            f"- Heading: `{fonts.get('heading', 'system-ui')}`",
            f"- Body: `{fonts.get('body', 'system-ui')}`",
            f"- Mono: `{fonts.get('mono', 'ui-monospace')}`",
            "",
        ]
        return "\n".join(out)
