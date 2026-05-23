"""Designer Agent - brand, palette, and component scaffolding.

LLM-first. The agent reads the brief, asks an LLM to infer the aesthetic intent
(cyberpunk HUD, minimal SaaS, luxury, warm/organic, etc.), and emits
``brand.md``, ``palette.json`` and ``components.md`` that match that intent.

If the LLM is offline or returns garbage, the agent falls back to a curated
hash-keyed palette table per mood so output remains deterministic.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger(__name__)

_POST_WRITE_TIMEOUT_SECONDS = 5.0
_LLM_GENERATE_TIMEOUT_SECONDS = 180.0

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
        self._skip_llm_for_run = False
        self._run_skip_backends: set[str] = set()

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return bool(_PALETTES)

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        await self.think(f"{self.name} starting on {task.task_id}")
        self._skip_llm_for_run = False
        self._run_skip_backends = set()

        data = task.input_data or {}
        brief: str = (data.get("brief") or "").strip() or "Untitled project"
        explicit_mood = (data.get("mood") or "").strip().lower()
        # If caller didn't specify a mood, infer it from the brief itself
        # rather than defaulting to "minimal".
        mood: str = explicit_mood or self._infer_mood(brief)
        if mood not in _PALETTES:
            mood = "minimal"
        target: str = (data.get("target") or "saas").lower()
        artifact_dir = self.resolve_artifact_dir(data.get("artifact_dir"))
        next_agent: Optional[str] = data.get("next_agent")

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=f"artifact_dir error: {e}")

        # Design references from photos the user attached via Telegram.
        # If present, these override the default mood/palette inference —
        # the user has already shown us what they want.
        attached_refs = self._load_attached_references(artifact_dir)
        if attached_refs:
            forced_mood = self._mood_from_references(attached_refs)
            if forced_mood:
                mood = forced_mood
                await self.think(
                    f"design_references.md present — using mood='{mood}' from attached photo(s)"
                )

        # Palette: LLM-first, hash-keyed table fallback. If we have an
        # attached reference with concrete hex codes, those win over the
        # LLM's pick.
        palette_json = None
        if attached_refs:
            ref_palette = self._palette_from_references(attached_refs)
            if ref_palette:
                palette_json = ref_palette
                await self.think("using palette from attached design reference(s)")
        if palette_json is None:
            palette_json = await self._pick_palette(brief, mood)
        await self.think(f"selected palette for mood='{mood}'")
        fonts = _FONTS_BY_MOOD.get(mood, _FONTS_BY_MOOD["minimal"])
        voice = _VOICE_BY_MOOD.get(mood, _VOICE_BY_MOOD["minimal"])
        logos = _LOGO_CONCEPTS_BY_MOOD.get(mood, _LOGO_CONCEPTS_BY_MOOD["minimal"])

        # Brief-signal override: when the brief explicitly calls for
        # warm-minimal aesthetic (Linear/Vercel/glassmorphism/Homarr),
        # FORCE the font/voice/logo set to the minimal preset regardless
        # of which mood was inferred. canary-126 confirmed that a
        # "premium glassmorphism homelab dashboard" brief still ended up
        # shipping Orbitron+Rajdhani+reticle through some path —
        # forcing here closes the door on all of them at once.
        if self._brief_signals_warm_minimal(brief):
            fonts = _FONTS_BY_MOOD["minimal"]
            voice = _VOICE_BY_MOOD["minimal"]
            logos = _LOGO_CONCEPTS_BY_MOOD["minimal"]
            # Use a brief-aligned mood label rather than literal "minimal"
            # — the reviewer flags brand.md's "Mood: minimal" as a
            # mismatch when the brief says "premium glassmorphism."
            # We still keep the "minimal" preset for fonts/voice/logos
            # (those keys live in the dicts), but the label that lands
            # in brand.md reflects the brief's vocabulary.
            mood = "premium glassmorphism"
            # canary-128 (49/100) shipped #A30000 red as primary AND
            # accent despite brand.md correctly saying "Mood: minimal" —
            # _pick_palette's LLM call returned a saturated red even on
            # the explicit warm-minimal signal. Force the palette to a
            # known-good slate/indigo/cyan set so the brief's "slate +
            # cool accent" intent actually ships. Same pattern as the
            # font/voice/logos override above.
            palette_json = {
                "primary":   "#6366F1",   # indigo-500 (Linear-ish primary CTA)
                "secondary": "#94A3B8",   # slate-400 (neutral secondary, distinct hue)
                "accent":    "#22D3EE",   # cyan-400 (cool accent for highlights)
                "bg":        "#0B1220",   # slate-950 (page background)
                "text":      "#E2E8F0",   # slate-200 (high-contrast body text)
            }
            await self.think("forced palette to warm-minimal slate/indigo/cyan preset")

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

        # Deterministic post-LLM sanitizer for brand.md. Every canary's
        # reviewer LLM has flagged brand.md as ignoring the brief's
        # "avoid cyberpunk / use Inter / Linear-Vercel reference points"
        # guidance — DesignerAgent kept shipping Orbitron+Rajdhani fonts
        # with "clinical, ominous, uncompromising" voice. Prompt rules
        # are insufficient (same training-data prior problem as the
        # architect sanitizer). Strip the offending content deterministically.
        try:
            sanitized = self._sanitize_brand_md(brand_md, brief)
            if sanitized != brand_md:
                brand_path.write_text(sanitized, encoding="utf-8")
                await self.think(
                    f"sanitized {brand_path.name}: stripped brief-mismatched mood/fonts"
                )
                brand_md = sanitized
        except Exception:
            logger.exception("brand.md sanitization failed (non-fatal)")

        palette_path = artifact_dir / "palette.json"
        palette_path.write_text(json.dumps(palette_json, indent=2), encoding="utf-8")
        await self.think(f"wrote {palette_path.name}")

        components_md = await self._render_components_md(brief, mood, target, palette_json)
        components_path = artifact_dir / "components.md"
        components_path.write_text(components_md, encoding="utf-8")
        await self.think(f"wrote {components_path.name}")

        # Structured component file plan — gives CodeAgent a list of
        # small component files to generate ONE AT A TIME instead of
        # cramming the whole app into App.jsx. Best-effort: a failure
        # here just means CodeAgent keeps using its existing stack
        # template plan (a smaller set of files).
        try:
            plan = await self._render_component_file_plan(brief, mood, target, palette_json)
            if plan and plan.get("files"):
                plan_path = artifact_dir / "component_file_plan.json"
                plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
                await self.think(
                    f"wrote component_file_plan.json with {len(plan['files'])} component(s)"
                )
        except Exception:
            logger.exception("component_file_plan generation failed (non-fatal)")

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
            try:
                await asyncio.wait_for(
                    self.send_message(
                        to=next_agent,
                        kind="info",
                        content=f"{self.name} done; artifacts in {artifact_dir}",
                        payload={"files": files, "palette": palette_json, "mood": mood},
                    ),
                    timeout=_POST_WRITE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "designer handoff timed out after %.1fs; continuing with completed artifacts",
                    _POST_WRITE_TIMEOUT_SECONDS,
                )
            except Exception:
                logger.exception("designer handoff failed after asset writeout")

        try:
            await asyncio.wait_for(
                self.share_learning(
                    f"Designer mood='{mood}' palette wired via LLM with deterministic fallback.",
                    scope="global",
                    mood=mood,
                ),
                timeout=_POST_WRITE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "designer learning publish timed out after %.1fs; continuing with completed artifacts",
                _POST_WRITE_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("designer learning publish failed after asset writeout")

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
        if self._skip_llm_for_run:
            return fallback
        try:
            client = None
            if hasattr(self, "get_llm") and not self._run_skip_backends:
                client = self.get_llm()
            if client is None:
                from skyn3t.adapters import LLMClient
                client = LLMClient(
                    default_model=self.config.get("model"),
                    backend=self.config.get("backend"),
                    event_bus=self.event_bus,
                    caller_name=self.name,
                    skip_backends=sorted(self._run_skip_backends),
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
            # Inject learned design skills (palette recipes, density
            # patterns, etc.) so the agent builds on accumulated wisdom
            # instead of re-deriving every time.
            try:
                skills_block = self.load_skills_for_prompt(
                    tags=["designer", "palette", "dashboard", "ui-pattern", "dark-mode"],
                    limit=3,
                )
                if skills_block:
                    prompt = prompt + skills_block
            except Exception:
                pass
            # For brand artifacts, prepend few-shot examples from prior projects.
            if kind == "brand":
                try:
                    from skyn3t.adapters.few_shot import few_shot_block
                    shots = few_shot_block("brand", count=2)
                    if shots:
                        prompt = shots + "\n\n# Now the new task:\n" + prompt
                except Exception:
                    pass
            try:
                out = str(
                    await asyncio.wait_for(
                        client.complete(prompt, max_tokens=max_tokens, temperature=0.7),
                        timeout=_LLM_GENERATE_TIMEOUT_SECONDS,
                    )
                )
            except asyncio.TimeoutError:
                failed_backend = str(getattr(client, "backend", "") or "").strip().lower()
                if failed_backend and failed_backend != "deterministic":
                    self._run_skip_backends.add(failed_backend)
                logger.warning(
                    "designer llm_generate timed out after %.1fs for %s; using fallback",
                    _LLM_GENERATE_TIMEOUT_SECONDS,
                    kind or "artifact",
                )
                self._skip_llm_for_run = True
                return fallback
            failed_backend = str(getattr(client, "_last_failed_backend", "") or "").strip().lower()
            if failed_backend:
                self._run_skip_backends.add(failed_backend)
            if out and "[deterministic-stub]" not in out and len(out.strip()) > 80:
                return out.strip()
            self._skip_llm_for_run = True
        except Exception:
            self._skip_llm_for_run = True
        return fallback

    # ------------------------------------------------------------------
    # Mood + palette selection
    # ------------------------------------------------------------------
    # Brief signals that mean "warm-minimal Linear/Vercel aesthetic"
    # — not "cyber HUD". When ANY of these appears in the brief,
    # _execute forces fonts/voice/logos to the minimal preset
    # regardless of what _infer_mood picked. Mirrors the same trigger
    # list used by _sanitize_brand_md.
    _BRIEF_WARM_MINIMAL_SIGNALS: Tuple[str, ...] = (
        "linear",
        "vercel",
        "avoid cyberpunk",
        "avoid: cyberpunk",
        "avoid noc",
        "avoid: noc",
        "premium glassmorphism",
        "polished glassmorphism",
        "warm-minimal",
        "warm minimal",
        "homarr",
        "heimdall",
    )

    @classmethod
    def _brief_signals_warm_minimal(cls, brief: str) -> bool:
        if not brief:
            return False
        b = brief.lower()
        return any(sig in b for sig in cls._BRIEF_WARM_MINIMAL_SIGNALS)

    def _infer_mood(self, brief: str) -> str:
        b = (brief or "").lower()
        for mood, keywords in _MOOD_KEYWORDS:
            for kw in keywords:
                if kw in b:
                    return mood
        return "minimal"

    # ------------------------------------------------------------------
    # Design references (user-attached photos)
    # ------------------------------------------------------------------

    def _load_attached_references(self, artifact_dir: Path) -> list:
        """Read ``<artifact_dir>/design_references.md`` and resolve back
        to the source DesignReference objects via the persistent cache.
        Returns a list of ``DesignReference`` objects (possibly empty).
        """
        try:
            from skyn3t.agents.design_vision import load_by_sha
            from skyn3t.integrations.telegram_photos import _load_library
        except Exception:  # noqa: BLE001
            return []
        refs_md = artifact_dir / "design_references.md"
        if not refs_md.exists():
            return []
        try:
            content = refs_md.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return []
        # Extract reference IDs from "## Reference `<id>`" lines.
        import re as _re
        ids = _re.findall(r"## Reference `([^`]+)`", content)
        if not ids:
            return []
        library = _load_library()
        out: list = []
        for ref_id in ids:
            entry = library.get(ref_id)
            if entry is None:
                continue
            extraction = load_by_sha(entry.sha)
            if extraction is not None:
                out.append(extraction)
        return out

    def _mood_from_references(self, references: list) -> str:
        """Derive a designer mood label from the extracted reference
        mood adjectives. We use simple keyword overlap against the
        existing ``_MOOD_KEYWORDS`` table — whatever mood has the most
        matching adjectives wins. Falls back to ``""`` (no override) if
        nothing matches."""
        if not references:
            return ""
        adjectives: list[str] = []
        for ref in references:
            adjectives.extend(getattr(ref, "mood", []) or [])
            adjectives.extend(getattr(ref, "notable_elements", []) or [])
        if not adjectives:
            return ""
        adj_text = " ".join(adjectives).lower()
        best = ("", 0)
        for mood, keywords in _MOOD_KEYWORDS:
            score = sum(1 for kw in keywords if kw in adj_text)
            if score > best[1]:
                best = (mood, score)
        return best[0]

    def _palette_from_references(self, references: list) -> Optional[Dict[str, str]]:
        """Build a palette dict from the first reference that has a
        usable palette. The DesignerAgent's downstream code expects
        keys (primary, secondary, accent, bg, text) — NOT the vision
        extractor's natural shape (bg, surface, accent, text, muted).
        We map between them: bg→bg, accent→accent (also doubles as
        primary), text→text. Surface and muted are absorbed as
        secondary. Returns None if the reference can't satisfy the
        minimum required keys."""
        for ref in references:
            palette_entries = getattr(ref, "palette", []) or []
            if not palette_entries:
                continue
            by_role: Dict[str, str] = {}
            for entry in palette_entries:
                role = (getattr(entry, "role", "") or "").lower()
                hex_code = getattr(entry, "hex", "") or ""
                if not hex_code.startswith("#") or len(hex_code) not in (4, 7):
                    continue
                if role and role not in by_role:
                    by_role[role] = hex_code

            # Backfill any missing required roles from unused entries.
            unused = [
                hex_val
                for e in palette_entries
                for hex_val in [getattr(e, "hex", "")]
                if (getattr(e, "role", "") or "").lower() not in by_role
                and hex_val
            ]
            for needed in ("bg", "accent", "text", "surface", "muted"):
                if needed not in by_role and unused:
                    by_role[needed] = unused.pop(0)

            if not all(role in by_role for role in ("bg", "accent", "text")):
                continue  # try next reference

            # Map vision-shape → designer-shape. primary defaults to
            # accent (it's the brand color CTAs use); secondary uses
            # surface or muted if available so the gradient/hover
            # branches in brand.md have something distinct to render.
            accent = by_role["accent"]
            return {
                "primary":   accent,
                "secondary": by_role.get("surface") or by_role.get("muted") or accent,
                "accent":    accent,
                "bg":        by_role["bg"],
                "text":      by_role["text"],
            }
        return None

    async def _pick_palette(self, brief: str, mood: str) -> Dict[str, str]:
        """Palette picker with three tiers:

        1. **OpenRouter (Owl Alpha)** — free, fast, fresh inference per
           project. Picks colors from the brief, not from memorized
           tables. This is the new primary path — fixes the bug where
           every habit/finance/notes project shipped the same warm
           amber palette regardless of context.
        2. **Agent CLI** — original path. Uses whatever backend the
           agent was configured with. Often hangs.
        3. **Deterministic hashed table** — pre-curated 5-color sets
           keyed by `(brief, mood)`. Same fallback as before.
        """
        fallback_palette = self._fallback_palette(brief, mood)
        fallback_json = json.dumps({
            "primary": fallback_palette[0],
            "secondary": fallback_palette[1],
            "accent": fallback_palette[2],
            "bg": fallback_palette[3],
            "text": fallback_palette[4],
        })

        # Tier 1: OpenRouter palette inference.
        or_palette = await self._pick_palette_openrouter(brief, mood)
        if or_palette is not None:
            await self.think(
                f"palette inferred via OpenRouter Owl Alpha for mood='{mood}'"
            )
            return or_palette

        # Tier 2: existing agent-CLI path (LLM-first via configured backend).
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

        # Tier 3: deterministic hashed table.
        return {
            "primary": fallback_palette[0],
            "secondary": fallback_palette[1],
            "accent": fallback_palette[2],
            "bg": fallback_palette[3],
            "text": fallback_palette[4],
        }

    async def _pick_palette_openrouter(
        self, brief: str, mood: str,
    ) -> Optional[Dict[str, str]]:
        """OpenRouter-driven palette inference. Returns ``None`` on any
        failure so the caller can fall through to the next tier."""
        import os as _os
        api_key = _os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            try:
                from skyn3t.config.settings import get_settings
                api_key = getattr(get_settings(), "openrouter_api_key", None)
                if api_key:
                    _os.environ.setdefault("OPENROUTER_API_KEY", api_key)
            except Exception:  # noqa: BLE001
                api_key = None
        if not api_key:
            return None

        prompt = (
            f"BRIEF FROM USER: {brief.strip()[:600]}\n\n"
            f"Mood hint (rough): {mood}\n\n"
            "Pick a 5-color palette that fits the brief's product domain "
            "and emotional tone. Habit trackers, finance apps, and todo "
            "apps each have distinct conventions — match the convention.\n\n"
            "Return ONLY this JSON shape, no commentary, no fences:\n"
            "{\n"
            '  "primary":   "#RRGGBB",\n'
            '  "secondary": "#RRGGBB",\n'
            '  "accent":    "#RRGGBB",\n'
            '  "bg":        "#RRGGBB",\n'
            '  "text":      "#RRGGBB"\n'
            "}\n\n"
            "Rules:\n"
            "- bg + text must have 4.5:1 contrast minimum.\n"
            "- accent should pop against bg but not overwhelm text.\n"
            "- secondary should harmonize with primary (analogous or muted variant).\n"
            "- AVOID generic 'warm amber on graphite' unless the brief specifically "
            "calls for that aesthetic — pick a fresh palette per project."
        )
        try:
            from skyn3t.adapters import LLMClient
            client = LLMClient(
                default_model="openrouter/owl-alpha",
                backend="openrouter",
                event_bus=self.event_bus,
                caller_name=self.name,
            )
            try:
                raw = await client.complete(
                    prompt,
                    system=(
                        "You are a senior brand designer. Read the brief, "
                        "match the convention, output strict JSON only."
                    ),
                    max_tokens=300,
                    temperature=0.4,
                    timeout=30.0,
                )
            finally:
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            logger.debug("openrouter palette inference failed", exc_info=True)
            return None
        return self._parse_palette_json(raw)

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

    # Terms that indicate the LLM picked the wrong aesthetic when the
    # brief explicitly forbids them. Mirrors ArchitectAgent's sentence-
    # drop sanitizer pattern — every canary 113–125 had brand.md ignoring
    # the brief's "Linear/Vercel, avoid cyberpunk/NOC console" direction.
    # Drop sentences mentioning these whenever the brief signals the
    # warm-minimal aesthetic.
    _BRAND_CYBER_DROP_TERMS: List[str] = [
        # Font families the brief calls out as forbidden
        "Orbitron", "Rajdhani", "Eurostile", "Bank Gothic", "Audiowide",
        # Mood/voice terms opposite to "warm-minimal"
        "cyber", "cyberpunk", "tactical", "ominous", "clinical",
        "uncompromising", "matrix terminal", "NOC console", "HUD",
        "reticle", "crosshair", "bracket mark", "tactical ops",
        "warm gunmetal",  # the warm-gunmetal-themed shipment we keep getting
    ]

    # Brief signals that mean "warm-minimal aesthetic, NOT cyberpunk."
    # When ANY of these appear in the brief, the cyber-drop terms above
    # become active. Conservative: silent pass-through when the brief
    # doesn't signal warm-minimal (e.g. a real cybersecurity-tool brief).
    _BRAND_WARM_SIGNALS: List[str] = [
        "linear",       # the Linear app — explicit warm reference
        "vercel",       # ditto
        "avoid cyberpunk", "avoid: cyberpunk",
        "avoid noc", "avoid: noc",
        "premium glassmorphism", "polished glassmorphism",
        "warm-minimal", "warm minimal",
        "homarr", "heimdall",  # the HomeLab Dashboard reference brands
    ]

    @classmethod
    def _sanitize_brand_md(cls, body: str, brief: str) -> str:
        """Strip brief-mismatched mood/font/voice mentions from brand.md.

        Used post-LLM because the DesignerAgent (regardless of model)
        consistently writes "cyber mood" + Orbitron/Rajdhani + "clinical/
        ominous" voice for any brief that mentions "homelab" or
        "service dashboard" — training-data prior dominates the prompt.

        Strategy: sentence-drop (same pattern as the architect sanitizer).
        Only fires when the brief explicitly signals warm-minimal
        aesthetic (Linear/Vercel/glassmorphism/Homarr/etc.), so a real
        cybersecurity-tool brief won't have its aesthetic stripped.
        """
        if not body or not brief:
            return body

        brief_lower = brief.lower()
        if not any(sig in brief_lower for sig in cls._BRAND_WARM_SIGNALS):
            return body  # not a warm-minimal brief — leave the design alone

        # Reuse the architect's sentence-drop + empty-section + renumber
        # passes for consistent behavior across both sanitizers.
        from skyn3t.agents.architect import ArchitectAgent

        out = body
        for term in cls._BRAND_CYBER_DROP_TERMS:
            out = ArchitectAgent._strip_sentences_mentioning(out, term)
        out = re.sub(r"\n{3,}", "\n\n", out)
        out = ArchitectAgent._drop_empty_sections(out)
        out = ArchitectAgent._renumber_lists(out)
        return out

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

    async def _render_component_file_plan(
        self,
        brief: str,
        mood: str,
        target: str,
        palette: Dict[str, str],
    ) -> Optional[Dict[str, Any]]:
        """Produce a structured component file plan.

        CodeAgent's per-file generation produces better output when
        each call has a small, focused scope. The default stack
        template plan for ``react_vite`` only lists App.jsx — which
        means the LLM has to fit every component, hook, and util into
        one ~5KB file. Breaking that into 4-6 named component files
        (HabitCard.jsx, StreakBadge.jsx, AddHabitForm.jsx, ...) lets
        each generation be 1-2KB instead of 5-8KB. Smaller calls
        succeed where huge ones time out.

        Returns a dict like::

            {"files": [
                {"path": "src/components/HabitCard.jsx",
                 "purpose": "Single habit card with streak badge",
                 "props": ["habit", "onCheckIn"]},
                ...
            ]}

        or ``None`` if generation failed.
        """
        # Only emit a plan when the target is a UI build — backend-only
        # / docs builds don't need component breakdowns.
        target_norm = (target or "").lower()
        if target_norm not in ("saas", "site", "app", "spa", "dashboard", ""):
            return None

        import os as _os_plan
        api_key = _os_plan.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            try:
                from skyn3t.config.settings import get_settings
                api_key = getattr(get_settings(), "openrouter_api_key", None)
                if api_key:
                    _os_plan.environ.setdefault("OPENROUTER_API_KEY", api_key)
            except Exception:  # noqa: BLE001
                api_key = None
        if not api_key:
            return None

        prompt = (
            f"BRIEF: {(brief or '').strip()[:800]}\n"
            f"Inferred mood: {mood}\n"
            f"Target: {target}\n\n"
            "Break this product into 4-8 small React components. For each, "
            "give: path (under src/components/), purpose (one sentence), "
            "and props (list of prop names, NOT types).\n\n"
            "Rules:\n"
            "- App.jsx is the shell — DON'T include it in the list. It "
            "  composes the components you list.\n"
            "- Each component does ONE thing. If it would be larger than "
            "  ~80 lines of JSX, break it further.\n"
            "- Use specific names tied to the product domain "
            "  (e.g. 'HabitCard' not 'Card', 'StreakBadge' not 'Badge').\n"
            "- Use src/components/<Name>.jsx paths (PascalCase filenames).\n"
            "- Include hooks if logic is reused (src/hooks/<useFoo>.js).\n\n"
            "Return STRICT JSON, no commentary, no fences:\n"
            "{\n"
            '  "files": [\n'
            '    {"path": "src/components/HabitCard.jsx", "purpose": "...", "props": ["habit", "onCheckIn"]}\n'
            "  ]\n"
            "}"
        )

        try:
            from skyn3t.adapters import LLMClient
            client = LLMClient(
                default_model="openrouter/owl-alpha",
                backend="openrouter",
                event_bus=self.event_bus,
                caller_name=self.name,
            )
            try:
                raw = await client.complete(
                    prompt,
                    system=(
                        "You are a React UI architect. Output strict JSON "
                        "only — no preamble, no fences."
                    ),
                    max_tokens=900,
                    temperature=0.3,
                    timeout=45.0,
                )
            finally:
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            logger.debug("component file plan generation failed", exc_info=True)
            return None

        # Tolerate code fences if the model added them.
        body = (raw or "").strip()
        if body.startswith("```"):
            body = body.strip("`")
            if body.startswith("json"):
                body = body[4:]
            body = body.strip()
            if body.endswith("```"):
                body = body[:-3].strip()
        # Find the first { and last } in case there's lingering prose.
        start = body.find("{")
        end = body.rfind("}")
        if start >= 0 and end > start:
            body = body[start : end + 1]

        try:
            parsed = json.loads(body)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(parsed, dict):
            return None
        files = parsed.get("files")
        if not isinstance(files, list) or not files:
            return None

        # Validate + normalize entries. Reject anything that doesn't
        # have a sensible path/purpose pair. Drop dupes.
        seen_paths: set = set()
        cleaned: List[Dict[str, Any]] = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "").strip().lstrip("/")
            purpose = str(entry.get("purpose") or "").strip()
            if not path or not purpose:
                continue
            # Reject paths that try to escape src/ — keep it scoped.
            if not (
                path.startswith("src/components/")
                or path.startswith("src/hooks/")
                or path.startswith("src/lib/")
                or path.startswith("src/utils/")
            ):
                continue
            # Reject filename extensions outside our generator scope.
            if not path.endswith((".jsx", ".tsx", ".js", ".ts")):
                continue
            if path in seen_paths:
                continue
            seen_paths.add(path)
            props = entry.get("props") or []
            if isinstance(props, list):
                props_clean = [str(p).strip() for p in props if str(p).strip()]
            else:
                props_clean = []
            cleaned.append({
                "path": path,
                "purpose": purpose,
                "props": props_clean,
            })
            if len(cleaned) >= 8:
                break

        if not cleaned:
            return None
        return {"files": cleaned}

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
        """Emit CSS custom properties keyed by role. Safe to @import anywhere.

        Beyond the 5 base colors + 3 fonts, also emits surface/glass/status/
        radius tokens. canary-119-136 all had the reviewer flag "tokens.css
        is skinnier than brand.md claims — no glass, no status, no radius."
        Shipping the semantic tokens closes that deduction.
        """
        heading = fonts.get("heading", "system-ui")
        body = fonts.get("body", "system-ui")
        mono = fonts.get("mono", "ui-monospace")
        accent = palette["accent"]
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
            "  /* Glass / surface tokens for the dark-mode glassmorphism layer */\n"
            "  --brand-surface: rgba(255, 255, 255, 0.04);\n"
            "  --brand-surface-2: rgba(255, 255, 255, 0.07);\n"
            "  --brand-border: rgba(255, 255, 255, 0.08);\n"
            "  --brand-border-strong: rgba(255, 255, 255, 0.14);\n"
            "  --brand-glass-blur: blur(18px) saturate(140%);\n"
            "  --brand-text-dim: rgba(226, 232, 240, 0.65);\n"
            "  --brand-muted: rgba(226, 232, 240, 0.45);\n"
            "\n"
            "  /* Status tokens (semantic, not brand-tinted) */\n"
            "  --brand-ok: #4ADE80;\n"
            "  --brand-warning: #F59E0B;\n"
            "  --brand-danger: #F87171;\n"
            "  --brand-info: " + accent + ";\n"
            "\n"
            "  /* Radii + motion */\n"
            "  --brand-radius-sm: 8px;\n"
            "  --brand-radius-md: 12px;\n"
            "  --brand-radius-lg: 18px;\n"
            "  --brand-motion-fast: 120ms cubic-bezier(0.2, 0.7, 0.2, 1);\n"
            "  --brand-motion-base: 180ms cubic-bezier(0.2, 0.7, 0.2, 1);\n"
            "\n"
            f"  --brand-font-heading: {heading};\n"
            f"  --brand-font-body: {body};\n"
            f"  --brand-font-mono: {mono};\n"
            "}\n"
        )

    @staticmethod
    def _render_design_tokens(palette: Dict[str, str], fonts: Dict[str, str]) -> Dict[str, Any]:
        """Emit a W3C design-tokens-shape JSON dict. Compatible with Style
        Dictionary, Penpot token plugins, Tailwind plugins, etc."""
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
                # Semantic status colors (mirror tokens.css)
                "ok": color_token("#4ADE80"),
                "warning": color_token("#F59E0B"),
                "danger": color_token("#F87171"),
                "info": color_token(palette["accent"]),
            },
            "surface": {
                "base": color_token("#FFFFFF0A"),
                "elevated": color_token("#FFFFFF12"),
                "border": color_token("#FFFFFF14"),
                "border-strong": color_token("#FFFFFF24"),
                "text-dim": color_token("#E2E8F0A6"),
                "muted": color_token("#E2E8F073"),
            },
            "radius": {
                "sm": {"value": "8px", "type": "dimension"},
                "md": {"value": "12px", "type": "dimension"},
                "lg": {"value": "18px", "type": "dimension"},
            },
            "motion": {
                "fast": {"value": "120ms cubic-bezier(0.2, 0.7, 0.2, 1)", "type": "duration"},
                "base": {"value": "180ms cubic-bezier(0.2, 0.7, 0.2, 1)", "type": "duration"},
            },
            "glass": {
                "blur": {"value": "blur(18px) saturate(140%)", "type": "filter"},
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
            "| `tokens.json` | W3C design-tokens shape — feed into Style Dictionary, Tokens Studio, Penpot token plugins, or Tailwind tooling. |",
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
            "## Penpot handoff",
            "",
            "- Import `logo.svg` directly into Penpot for a starting mark.",
            "- Load `tokens.json` through a Penpot token plugin if you want color/text tokens to land faster.",
            "- Keep `brand.md` and `components.md` open while building Penpot components and variants.",
            "",
        ]
        return "\n".join(out)
