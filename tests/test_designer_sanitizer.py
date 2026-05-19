"""DesignerAgent._sanitize_brand_md — deterministic post-LLM cleanup.

Every canary 113–125 had brand.md ignoring the brief's "Linear/Vercel,
avoid cyberpunk" direction. The designer LLM (regardless of model)
kept shipping Orbitron+Rajdhani fonts and "clinical, ominous,
uncompromising" voice. Same pattern as architect.md drift; same fix.
"""

from __future__ import annotations

from skyn3t.agents.designer import DesignerAgent

WARM_BRIEF = (
    "Build a polished dark-mode service dashboard with a premium "
    "glassmorphism feel inspired by the HomeLab Dashboard reference. "
    "Aesthetic baseline: Linear, Vercel. Avoid cyberpunk."
)


def test_drops_orbitron_font_sentence() -> None:
    body = (
        "## Typography\n"
        "Headings use Orbitron for a tactical feel. "
        "Body uses Inter for readability.\n"
    )
    out = DesignerAgent._sanitize_brand_md(body, WARM_BRIEF)
    assert "Orbitron" not in out
    assert "Inter" in out  # the clean sentence survives


def test_drops_cyber_mood_sentence() -> None:
    body = (
        "## Mood\n"
        "Mood: cyber. Voice is clinical, ominous, uncompromising. "
        "Pacing is calm and deliberate.\n"
    )
    out = DesignerAgent._sanitize_brand_md(body, WARM_BRIEF)
    assert "cyber" not in out.lower()
    assert "clinical" not in out.lower()
    assert "ominous" not in out.lower()
    assert "calm and deliberate" in out


def test_drops_tactical_logo_concepts() -> None:
    body = (
        "## Logo concepts\n"
        "1. Reticle in a tactical mono frame.\n"
        "2. Three horizontal bars (cleaner).\n"
        "3. HUD bracket mark with crosshair accent.\n"
    )
    out = DesignerAgent._sanitize_brand_md(body, WARM_BRIEF)
    assert "reticle" not in out.lower()
    assert "tactical" not in out.lower()
    assert "HUD" not in out
    assert "crosshair" not in out.lower()
    # The clean concept (#2) survives, and gets renumbered to item 1.
    assert "Three horizontal bars" in out
    assert "1. Three horizontal bars" in out


def test_passes_through_when_brief_does_not_signal_warm_minimal() -> None:
    """A real cybersecurity-tool brief should leave brand.md alone —
    the sanitizer is gated on warm-minimal brief signals."""
    cybersec_brief = "Build a security operations dashboard for SOC analysts"
    body = "Mood: cyber. Fonts: Orbitron + JetBrains Mono. Voice: clinical."
    out = DesignerAgent._sanitize_brand_md(body, cybersec_brief)
    assert out == body


def test_passes_through_on_empty_brief() -> None:
    body = "Mood: cyber. Voice: clinical."
    assert DesignerAgent._sanitize_brand_md(body, "") == body


def test_passes_through_on_empty_body() -> None:
    assert DesignerAgent._sanitize_brand_md("", WARM_BRIEF) == ""


def test_canary_125_real_world_case() -> None:
    """canary-125 had this pattern in brand.md after three review rounds:
    Mood: cyber + Orbitron + clinical voice + reticle logo. All flagged
    by the reviewer as opposing the brief.
    """
    body = (
        "## Mood\nMood: cyber\n\n"
        "## Typography\nHeading: Orbitron. Body: Rajdhani.\n\n"
        "## Voice\nClinical, ominous, uncompromising.\n\n"
        "## Logo\nA reticle inside a tactical HUD bracket.\n\n"
        "## Glass\nTranslucent panels with backdrop-filter blur(18px).\n"
    )
    out = DesignerAgent._sanitize_brand_md(body, WARM_BRIEF)
    # All the cyber content stripped:
    assert "cyber" not in out.lower()
    assert "Orbitron" not in out
    assert "Rajdhani" not in out
    assert "clinical" not in out.lower()
    assert "ominous" not in out.lower()
    assert "reticle" not in out.lower()
    assert "tactical" not in out.lower()
    # The glass section (legitimate, matches brief) survives:
    assert "Glass" in out
    assert "backdrop-filter" in out


def test_warm_signal_homarr_or_heimdall() -> None:
    """The HomeLab Dashboard reference brands (Homarr, Heimdall) should
    trigger sanitization on their own.
    """
    body = "Mood: cyber. Headings: Orbitron."
    for sig_brief in (
        "Build a dashboard inspired by Homarr",
        "Build a dashboard inspired by Heimdall",
    ):
        out = DesignerAgent._sanitize_brand_md(body, sig_brief)
        assert "cyber" not in out.lower()
        assert "Orbitron" not in out
