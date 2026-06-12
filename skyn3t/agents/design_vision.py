"""Vision-LLM extractor for design references.

CLI-FIRST DESIGN. We deliberately do NOT call any Anthropic/OpenAI API
directly — the user is on subscription-only mode after a 100M token
burn. We invoke the Claude CLI (or Copilot/Kimi as fallback) as a
subprocess. The CLIs accept image paths via their file-tool surface, so
we instruct them to read the image, analyze the design, and return a
strict JSON shape.

Results are cached per image hash in
``data/design_references/extractions/<sha>.json`` so re-attaching the
same reference to a different project doesn't burn another CLI call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


_CLI_TIMEOUT_SECONDS = 90.0  # vision is slower than text — give it room

_SYSTEM_PROMPT = (
    "You are a senior brand / UI designer extracting decision-grade "
    "direction from a reference image. Be concrete. No hedging. "
    "Output ONLY the requested JSON object — no prose around it, no "
    "fenced code block, no commentary."
)

_USER_PROMPT_TEMPLATE = """Open the {kind} at this absolute path and analyze it as a design reference for a software product:

{label}: {image_path}

{multi_page_note}Return STRICT JSON matching this shape (no prose, no markdown fences):

{{
  "palette": [
    {{"name": "...", "hex": "#RRGGBB", "role": "bg" | "surface" | "accent" | "text" | "muted" | "status"}}
  ],
  "typography_vibe": "one short phrase, e.g. 'condensed geometric sans, slight glow, futurist'",
  "layout_density": "dense" | "balanced" | "airy",
  "mood": ["3-7 short adjectives — e.g. 'techno-precision', 'autonomous machine'"],
  "notable_elements": ["bullet list — e.g. 'circuit-board fill inside shapes', 'cyan glow halo on edges'"],
  "forbidden_words": ["short list of words that would WRONG-FAMILY this brand — e.g. 'warm', 'cozy', 'earthy', 'organic'"],
  "verdict_one_liner": "one sentence summarizing the look — under 120 chars"
}}

Rules:
- The palette list must include at least one bg, one accent, one text. Aim for 5-7 entries total.
- Hex codes must be exact, not approximate names.
- Mood adjectives must be specific (not 'modern' or 'clean' — say what KIND of modern).
- forbidden_words should call out aesthetic families the user explicitly should NOT slide toward.
- Focus on VISUAL DESIGN LANGUAGE — palette, typography, layout, density, mood.
- IGNORE structural diagrams (architecture boxes/arrows, wireframe outlines, ER diagrams).
  These don't carry visual design direction — extract from finished-UI pages only.
- Output ONLY the JSON object. No preamble. No conclusion."""


_MULTI_PAGE_NOTE = (
    "This document may have multiple pages. Skim quickly across pages and "
    "extract the SHARED visual design language (palette, typography, "
    "spacing, mood) — not what any individual page says. If pages show "
    "architecture or wireframe diagrams, skip those — they're not the "
    "design reference. Focus on pages that show finished UI screenshots, "
    "hero shots, type specimens, or color swatches.\n\n"
)


def _build_user_prompt(image_path: Path) -> str:
    suffix = image_path.suffix.lower()
    if suffix == ".pdf":
        kind = "PDF document"
        label = "PDF PATH"
        multi_page_note = _MULTI_PAGE_NOTE
    else:
        kind = "image"
        label = "IMAGE PATH"
        multi_page_note = ""
    return _USER_PROMPT_TEMPLATE.format(
        image_path=str(image_path),
        kind=kind,
        label=label,
        multi_page_note=multi_page_note,
    )


@dataclass
class PaletteEntry:
    name: str
    hex: str
    role: str  # bg | surface | accent | text | muted | status


@dataclass
class DesignReference:
    image_path: str
    image_sha: str
    palette: List[PaletteEntry] = field(default_factory=list)
    typography_vibe: str = ""
    layout_density: str = ""
    mood: List[str] = field(default_factory=list)
    notable_elements: List[str] = field(default_factory=list)
    forbidden_words: List[str] = field(default_factory=list)
    verdict_one_liner: str = ""
    extracted_at: float = 0.0

    def to_brand_md_fragment(self) -> str:
        lines: list[str] = []
        lines.append("# Design reference (extracted from user-supplied image)")
        if self.verdict_one_liner:
            lines.append(f"\n> {self.verdict_one_liner}\n")
        if self.palette:
            lines.append("## Palette")
            for p in self.palette:
                lines.append(f"- **{p.name}** `{p.hex}` — {p.role}")
        if self.typography_vibe:
            lines.append("\n## Typography")
            lines.append(f"- {self.typography_vibe}")
        if self.layout_density:
            lines.append("\n## Layout density")
            lines.append(f"- {self.layout_density}")
        if self.mood:
            lines.append("\n## Mood")
            for m in self.mood:
                lines.append(f"- {m}")
        if self.notable_elements:
            lines.append("\n## Notable elements")
            for e in self.notable_elements:
                lines.append(f"- {e}")
        if self.forbidden_words:
            lines.append("\n## Forbidden words")
            lines.append(
                "Do not slide this brand toward: " + ", ".join(self.forbidden_words)
            )
        return "\n".join(lines)


def _extractions_dir() -> Path:
    try:
        from skyn3t.config.settings import get_settings
        return Path(get_settings().data_dir) / "design_references" / "extractions"
    except Exception:  # noqa: BLE001
        return Path("data/design_references/extractions")


def _sha_of_file(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _cached(sha: str) -> Optional[DesignReference]:
    p = _extractions_dir() / f"{sha}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        palette = [PaletteEntry(**e) for e in (data.get("palette") or []) if isinstance(e, dict)]
        return DesignReference(
            image_path=data.get("image_path", ""),
            image_sha=data.get("image_sha", sha),
            palette=palette,
            typography_vibe=data.get("typography_vibe", ""),
            layout_density=data.get("layout_density", ""),
            mood=list(data.get("mood") or []),
            notable_elements=list(data.get("notable_elements") or []),
            forbidden_words=list(data.get("forbidden_words") or []),
            verdict_one_liner=data.get("verdict_one_liner", ""),
            extracted_at=float(data.get("extracted_at") or 0.0),
        )
    except Exception:  # noqa: BLE001
        logger.warning("design-vision cache read failed for %s", sha, exc_info=True)
        return None


def _save(ref: DesignReference) -> None:
    out = _extractions_dir()
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{ref.image_sha}.json"
    tmp = path.with_suffix(".tmp")
    payload = asdict(ref)
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


async def _run_cli(args: List[str], stdin_data: Optional[bytes] = None) -> str:
    """Run a CLI subprocess with a timeout. Returns stdout text on success,
    raises RuntimeError on failure. Stderr is logged at debug level."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_data),
            timeout=_CLI_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"{args[0]} timed out after {_CLI_TIMEOUT_SECONDS}s")
    if proc.returncode != 0:
        err_text = (stderr or b"").decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"{args[0]} exited {proc.returncode}: {err_text}")
    return (stdout or b"").decode("utf-8", errors="replace").strip()


def _strip_json_fences(text: str) -> str:
    """The CLIs often wrap JSON in ``` fences. Strip them so json.loads works."""
    text = text.strip()
    if text.startswith("```"):
        # ```json ... ``` or just ``` ... ```
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
        # If there's a trailing ``` from an unbalanced fence
        if text.endswith("```"):
            text = text[:-3].strip()
    # Look for the first { and last } in case there's preamble/postamble
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


async def _try_claude_cli(image_path: Path) -> Optional[dict]:
    """Use Claude CLI to read the image and return JSON. Returns parsed
    dict on success, ``None`` on any failure. Claude CLI in
    non-interactive (``-p``) mode uses subscription auth — no API key
    required."""
    if not shutil.which("claude"):
        return None
    image_dir = str(image_path.parent)
    prompt = _build_user_prompt(image_path)
    args = [
        "claude", "-p",
        "--append-system-prompt", _SYSTEM_PROMPT,
        "--add-dir", image_dir,
        prompt,
    ]
    try:
        out = await _run_cli(args)
    except Exception as e:  # noqa: BLE001
        logger.warning("claude CLI vision call failed: %s", e)
        return None
    raw = _strip_json_fences(out)
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001
        logger.warning("claude CLI returned non-JSON for %s: %s", image_path.name, out[:200])
        return None


async def _try_copilot_cli(image_path: Path) -> Optional[dict]:
    """Use the Copilot CLI as a vision fallback. Same subscription model."""
    if not shutil.which("copilot"):
        return None
    image_dir = str(image_path.parent)
    prompt = _build_user_prompt(image_path)
    args = [
        "copilot",
        "--available-tools=read",  # let copilot read the image file
        "--add-dir", image_dir,
        "-p", prompt,
    ]
    try:
        out = await _run_cli(args)
    except Exception as e:  # noqa: BLE001
        logger.warning("copilot CLI vision call failed: %s", e)
        return None
    raw = _strip_json_fences(out)
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001
        logger.warning("copilot CLI returned non-JSON for %s: %s", image_path.name, out[:200])
        return None


async def _try_kimi_cli(image_path: Path) -> Optional[dict]:
    """Kimi CLI fallback. Note: Kimi may or may not support images in
    its CLI — we try and silently fall through if it can't."""
    if not shutil.which("kimi"):
        return None
    prompt = _build_user_prompt(image_path)
    args = ["kimi", "--print", "--quiet", "--no-thinking", "-p", prompt]
    try:
        out = await _run_cli(args)
    except Exception as e:  # noqa: BLE001
        logger.warning("kimi CLI vision call failed: %s", e)
        return None
    raw = _strip_json_fences(out)
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001
        logger.warning("kimi CLI returned non-JSON for %s: %s", image_path.name, out[:200])
        return None


def _parse_response(data: dict, image_path: Path, sha: str) -> DesignReference:
    palette: List[PaletteEntry] = []
    for entry in data.get("palette") or []:
        if not isinstance(entry, dict):
            continue
        try:
            palette.append(PaletteEntry(
                name=str(entry.get("name") or ""),
                hex=str(entry.get("hex") or ""),
                role=str(entry.get("role") or "accent"),
            ))
        except Exception:  # noqa: BLE001
            continue
    return DesignReference(
        image_path=str(image_path),
        image_sha=sha,
        palette=palette,
        typography_vibe=str(data.get("typography_vibe") or ""),
        layout_density=str(data.get("layout_density") or ""),
        mood=[str(x) for x in (data.get("mood") or []) if x],
        notable_elements=[str(x) for x in (data.get("notable_elements") or []) if x],
        forbidden_words=[str(x).lower() for x in (data.get("forbidden_words") or []) if x],
        verdict_one_liner=str(data.get("verdict_one_liner") or ""),
        extracted_at=time.time(),
    )


async def extract(image_path: Path) -> Optional[DesignReference]:
    """Run vision extraction on an image via the Claude CLI (with
    Copilot/Kimi fallback). Returns ``None`` on any failure (missing
    file, all CLIs unavailable, all responses unparseable)."""
    image_path = Path(image_path)
    if not image_path.exists():
        logger.warning("design-vision: image missing: %s", image_path)
        return None

    sha = _sha_of_file(image_path)
    hit = _cached(sha)
    if hit is not None:
        return hit

    # Try CLIs in subscription-cost order: Claude (largest plan), then
    # Copilot, then Kimi.
    for try_fn in (_try_claude_cli, _try_copilot_cli, _try_kimi_cli):
        data = await try_fn(image_path)
        if not isinstance(data, dict) or not data.get("palette"):
            continue
        ref = _parse_response(data, image_path, sha)
        _save(ref)
        return ref

    logger.warning("design-vision: every CLI failed for %s", image_path.name)
    return None


def load_by_sha(sha: str) -> Optional[DesignReference]:
    """Public lookup by image SHA. Used by the reference library."""
    return _cached(sha)


# ── Non-photo design-source seam (Figma / Penpot exports) ───────────────
# extract() above is the photo/image path and is left untouched. Imported
# design sources (Figma/Penpot) carry palette + typography as STRUCTURED
# data, not pixels — so they don't need vision-CLI inference. This seam
# normalizes such an export into the same DesignReference shape the rest of
# the designer pipeline already consumes. When the source IS a flat image,
# we just delegate to extract(); when it's a structured token export, we
# coerce it directly (no CLI call, no key).

# Roles we recognize in a Figma/Penpot variable/style export. Anything
# else is kept verbatim so we don't lose direction.
_TOKEN_PALETTE_ROLES = {"bg", "surface", "accent", "text", "muted", "status", "primary"}


def design_reference_from_tokens(
    tokens: dict, *, source_label: str = "design-export"
) -> Optional[DesignReference]:
    """Coerce a structured token export (Figma/Penpot variables) into a
    ``DesignReference``. Returns ``None`` when there's no usable palette.

    Expected (loose) shape::

        {"palette": [{"name","hex","role"}, ...],
         "typography_vibe": str, "layout_density": str,
         "mood": [str], "forbidden_words": [str]}

    Tolerant of partial input — missing fields default empty.
    """
    if not isinstance(tokens, dict):
        return None
    raw_palette = tokens.get("palette") or []
    palette: List[PaletteEntry] = []
    for entry in raw_palette:
        if not isinstance(entry, dict):
            continue
        hex_code = str(entry.get("hex") or "").strip()
        if not hex_code:
            continue
        role = str(entry.get("role") or "accent").strip().lower()
        palette.append(
            PaletteEntry(
                name=str(entry.get("name") or role or "color"),
                hex=hex_code,
                role=role if role in _TOKEN_PALETTE_ROLES else "accent",
            )
        )
    if not palette:
        return None
    return DesignReference(
        image_path=f"<{source_label}>",
        image_sha="",
        palette=palette,
        typography_vibe=str(tokens.get("typography_vibe") or ""),
        layout_density=str(tokens.get("layout_density") or ""),
        mood=[str(x) for x in (tokens.get("mood") or []) if x],
        notable_elements=[str(x) for x in (tokens.get("notable_elements") or []) if x],
        forbidden_words=[str(x).lower() for x in (tokens.get("forbidden_words") or []) if x],
        verdict_one_liner=str(tokens.get("verdict_one_liner") or ""),
        extracted_at=time.time(),
    )


async def extract_design_source(
    *, source: str, ref: "str | Path", tokens: Optional[dict] = None
) -> Optional[DesignReference]:
    """Unified design-import seam over heterogeneous sources.

    * ``source='image'`` (or ``'photo'``/``'screenshot'``) → delegate to
      the unchanged :func:`extract` image path.
    * ``source in ('figma','penpot','tokens','export')`` → coerce the
      already-fetched ``tokens`` dict (provided by the AssetAgent's
      provider hook) into a ``DesignReference`` with no CLI call.

    Returns ``None`` on any failure so callers degrade gracefully. The
    image path keeps its exact prior behavior.
    """
    src = (source or "").strip().lower()
    if src in ("image", "photo", "screenshot", ""):
        return await extract(Path(ref))
    if src in ("figma", "penpot", "tokens", "export"):
        if not isinstance(tokens, dict):
            logger.info(
                "extract_design_source(%s): no tokens payload supplied; skipping", src
            )
            return None
        return design_reference_from_tokens(tokens, source_label=src)
    logger.info("extract_design_source: unknown source %r", source)
    return None


# ── Screenshot scoring rubric ───────────────────────────────────────────
# build_verifier's visual gate renders the generated app, screenshots it,
# and (optionally) calls score_screenshot for a 0-100 design-quality
# rubric. Reuses the same CLI ladder + JSON-fence stripping as extract().
# Inherits extract()'s graceful-None contract: any failure → None, so the
# build verifier never blocks when vision CLIs are absent.

_SCORE_SYSTEM_PROMPT = (
    "You are a brutally honest senior product designer scoring a rendered "
    "screenshot of a freshly-generated web app. Reward distinctive, "
    "intentional design; punish generic-AI output. Output ONLY the "
    "requested JSON object — no prose, no fenced code block."
)

_SCORE_USER_PROMPT_TEMPLATE = """Open the screenshot at this absolute path and score it as a finished software UI:

IMAGE PATH: {image_path}
{context_block}
Return STRICT JSON matching this shape (no prose, no markdown fences):

{{
  "score": 0-100,
  "verdict": "pass" | "fail",
  "reasons": ["short concrete observations driving the score"],
  "generic_ai_tells": ["any generic-AI tells you can see — empty list if none"]
}}

Scoring rubric (be strict):
- 80-100: distinctive, intentional, production-grade. Considered type scale, deliberate color use, real layout rhythm, a signature detail.
- 50-79: competent but safe. Works, nothing memorable.
- 0-49: generic-AI output, broken layout, unstyled, or default-template look.

Mark these GENERIC-AI TELLS when present (each one drags the score down):
- Centered single column floating on a flat purple/indigo gradient.
- Three evenly-spaced rounded cards with a tiny icon + filler heading.
- Default system font paired with a violet/indigo accent and nothing else.
- Emoji used as primary iconography.
- Unmotivated drop shadows / uniform border-radius with no hierarchy.
- Visibly unstyled (browser-default) or horizontally-overflowing content.

verdict is "fail" when score < 60 OR any layout-breaking tell is present.
Output ONLY the JSON object. No preamble. No conclusion."""


def _build_score_prompt(image_path: Path, brief: str, mood: str) -> str:
    ctx: List[str] = []
    if brief:
        ctx.append(f"PRODUCT BRIEF (what this app is meant to be): {brief.strip()[:600]}")
    if mood:
        ctx.append(f"INTENDED AESTHETIC / MOOD: {mood.strip()[:200]}")
    context_block = ("\n" + "\n".join(ctx) + "\n") if ctx else ""
    return _SCORE_USER_PROMPT_TEMPLATE.format(
        image_path=str(image_path),
        context_block=context_block,
    )


def _coerce_score_payload(data: dict) -> Optional[dict]:
    """Normalize a parsed CLI response into the score_screenshot shape.

    Returns ``None`` when the payload doesn't carry a usable score so the
    caller can fall through to the next CLI / to heuristics-only.
    """
    if not isinstance(data, dict):
        return None
    raw_score = data.get("score")
    try:
        score = int(round(float(raw_score)))
    except (TypeError, ValueError):
        return None
    score = max(0, min(100, score))
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "fail"):
        # Derive a verdict if the model omitted/garbled it.
        verdict = "pass" if score >= 60 else "fail"
    reasons = [str(x) for x in (data.get("reasons") or []) if x]
    tells = [str(x) for x in (data.get("generic_ai_tells") or []) if x]
    return {
        "score": score,
        "verdict": verdict,
        "reasons": reasons,
        "generic_ai_tells": tells,
    }


async def _try_claude_cli_score(image_path: Path, prompt: str) -> Optional[dict]:
    if not shutil.which("claude"):
        return None
    args = [
        "claude", "-p",
        "--append-system-prompt", _SCORE_SYSTEM_PROMPT,
        "--add-dir", str(image_path.parent),
        prompt,
    ]
    try:
        out = await _run_cli(args)
    except Exception as e:  # noqa: BLE001
        logger.warning("claude CLI score call failed: %s", e)
        return None
    try:
        parsed = json.loads(_strip_json_fences(out))
    except Exception:  # noqa: BLE001
        logger.warning("claude CLI returned non-JSON score for %s: %s", image_path.name, out[:200])
        return None
    return parsed if isinstance(parsed, dict) else None


async def _try_copilot_cli_score(image_path: Path, prompt: str) -> Optional[dict]:
    if not shutil.which("copilot"):
        return None
    args = [
        "copilot",
        "--available-tools=read",
        "--add-dir", str(image_path.parent),
        "-p", prompt,
    ]
    try:
        out = await _run_cli(args)
    except Exception as e:  # noqa: BLE001
        logger.warning("copilot CLI score call failed: %s", e)
        return None
    try:
        parsed = json.loads(_strip_json_fences(out))
    except Exception:  # noqa: BLE001
        logger.warning("copilot CLI returned non-JSON score for %s: %s", image_path.name, out[:200])
        return None
    return parsed if isinstance(parsed, dict) else None


async def _try_kimi_cli_score(image_path: Path, prompt: str) -> Optional[dict]:
    if not shutil.which("kimi"):
        return None
    args = ["kimi", "--print", "--quiet", "--no-thinking", "-p", prompt]
    try:
        out = await _run_cli(args)
    except Exception as e:  # noqa: BLE001
        logger.warning("kimi CLI score call failed: %s", e)
        return None
    try:
        parsed = json.loads(_strip_json_fences(out))
    except Exception:  # noqa: BLE001
        logger.warning("kimi CLI returned non-JSON score for %s: %s", image_path.name, out[:200])
        return None
    return parsed if isinstance(parsed, dict) else None


async def score_screenshot(
    image_path: Path,
    *,
    brief: str = "",
    mood: str = "",
) -> Optional[dict]:
    """Score a rendered screenshot 0-100 via the vision CLI ladder.

    Returns ``{'score': int 0-100, 'verdict': 'pass'|'fail',
    'reasons': List[str], 'generic_ai_tells': List[str]}`` or ``None``.

    Mirrors extract()'s graceful-None contract: returns ``None`` when the
    image is missing or NO vision-capable CLI produces a usable score, so
    build_verifier treats None as 'rubric unavailable' and gates on cheap
    heuristics only — never blocking.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        logger.warning("design-vision score: image missing: %s", image_path)
        return None

    prompt = _build_score_prompt(image_path, brief, mood)
    for try_fn in (_try_claude_cli_score, _try_copilot_cli_score, _try_kimi_cli_score):
        try:
            data = await try_fn(image_path, prompt)
        except Exception:  # noqa: BLE001
            logger.warning("design-vision score CLI raised", exc_info=True)
            continue
        if not isinstance(data, dict):
            continue
        coerced = _coerce_score_payload(data)
        if coerced is not None:
            return coerced

    logger.info("design-vision score: no vision CLI produced a score for %s", image_path.name)
    return None
