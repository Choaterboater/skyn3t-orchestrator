"""Phase 3 owner_design_assets contracts.

Covers the three additive contract functions:
  - design_vision.score_screenshot  (graceful-None, 0-100 rubric)
  - designer.entry_css_imports_tokens (_token_contract_prompt_rule + entry CSS)
  - service_brand_kit.icon_img_snippet (<img src=icon_url> instruction)

All new functions are PURELY ADDITIVE — existing pinned outputs
(tokens.css / brand_for / brand_kit_markdown) are unchanged and covered
by their own suites.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.agents import design_vision
from skyn3t.agents.designer import DesignerAgent
from skyn3t.agents.service_brand_kit import brand_for, icon_img_snippet

PALETTE = {
    "primary": "#C8102E",
    "secondary": "#1C2B2D",
    "accent": "#4A7C59",
    "bg": "#0D0F0F",
    "text": "#D4D8D4",
}
FONTS = {
    "heading": "Orbitron, sans-serif",
    "body": "Rajdhani, sans-serif",
    "mono": "JetBrains Mono, monospace",
}


# ── designer._token_contract_prompt_rule ────────────────────────────────


def test_token_contract_rule_is_non_negotiable_and_token_driven():
    rule = DesignerAgent._token_contract_prompt_rule("techy")
    assert "NON-NEGOTIABLE" in rule
    assert "@import './tokens.css';" in rule
    assert "var(--brand-" in rule
    # Forbids hardcoded hex / Tailwind default palette.
    assert "hardcode" in rule.lower()
    assert "Tailwind" in rule
    # Frontend-design methodology: named direction + forbid generic-AI tells
    # + one distinctive detail.
    assert "aesthetic direction" in rule
    assert "generic-AI" in rule
    assert "distinctive" in rule.lower()
    # Mood seed surfaces when supplied.
    assert "techy" in rule


def test_token_contract_rule_handles_empty_mood():
    rule = DesignerAgent._token_contract_prompt_rule("")
    assert "NON-NEGOTIABLE" in rule
    assert "@import './tokens.css';" in rule
    # No stray empty-quote artifact.
    assert "mood seed: ''" not in rule


def test_token_contract_rule_is_pure_string():
    # Same input → identical output (deterministic, no external tool).
    assert (
        DesignerAgent._token_contract_prompt_rule("luxury")
        == DesignerAgent._token_contract_prompt_rule("luxury")
    )


# ── designer._render_entry_css (entry stylesheet imports tokens) ─────────


def test_entry_css_begins_with_tokens_import():
    css = DesignerAgent._render_entry_css(PALETTE, FONTS)
    assert css.lstrip().startswith("@import './tokens.css';")


def test_entry_css_consumes_brand_tokens_not_hardcoded_hex():
    css = DesignerAgent._render_entry_css(PALETTE, FONTS)
    assert "var(--brand-bg)" in css
    assert "var(--brand-text)" in css
    assert "var(--brand-font-body)" in css
    assert "var(--brand-accent)" in css
    # No raw palette hex bled into the entry stylesheet.
    for hex_value in PALETTE.values():
        assert hex_value not in css


def test_entry_css_omits_import_when_tokens_absent():
    """Degrade: no tokens.css → no broken @import, body still token-wired."""
    css = DesignerAgent._render_entry_css(PALETTE, FONTS, has_tokens=False)
    assert "@import" not in css
    assert "var(--brand-bg)" in css


# ── service_brand_kit.icon_img_snippet ──────────────────────────────────


def test_icon_img_snippet_uses_real_cdn_img_src():
    snippet = icon_img_snippet("sonarr")
    assert snippet is not None
    brand = brand_for("sonarr")
    assert brand is not None
    assert "<img" in snippet
    assert brand.icon_url in snippet
    assert f'alt="{brand.name}"' in snippet
    # Instructs AGAINST hand-rolled marks.
    assert "hand-rolled" in snippet.lower()


def test_icon_img_snippet_is_case_insensitive():
    assert icon_img_snippet("SONARR") is not None
    assert icon_img_snippet("Sonarr") is not None


def test_icon_img_snippet_unknown_slug_returns_none():
    assert icon_img_snippet("never-heard-of-this") is None


def test_icon_img_snippet_empty_returns_none():
    assert icon_img_snippet("") is None


def test_icon_img_snippet_for_every_known_service():
    from skyn3t.agents.service_brand_kit import known_slugs

    for slug in known_slugs():
        snippet = icon_img_snippet(slug)
        assert snippet is not None
        assert "<img" in snippet


# ── design_vision.score_screenshot — graceful-None contract ─────────────


def test_score_screenshot_missing_image_returns_none():
    result = asyncio.run(
        design_vision.score_screenshot(Path("/nonexistent/shot.png"))
    )
    assert result is None


def test_score_screenshot_returns_none_when_no_cli(tmp_path, monkeypatch):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    # No vision CLI available → None (never blocks the build verifier).
    monkeypatch.setattr(design_vision.shutil, "which", lambda _name: None)
    result = asyncio.run(design_vision.score_screenshot(img, brief="b", mood="m"))
    assert result is None


def test_score_screenshot_parses_cli_rubric(tmp_path, monkeypatch):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    monkeypatch.setattr(design_vision.shutil, "which", lambda name: "/usr/bin/claude")

    async def fake_run_cli(args, stdin_data=None):  # noqa: ARG001
        return (
            '```json\n{"score": 82, "verdict": "pass", '
            '"reasons": ["distinctive type scale"], '
            '"generic_ai_tells": []}\n```'
        )

    monkeypatch.setattr(design_vision, "_run_cli", fake_run_cli)

    result = asyncio.run(
        design_vision.score_screenshot(img, brief="a budgeting app", mood="warm")
    )
    assert result is not None
    assert result["score"] == 82
    assert result["verdict"] == "pass"
    assert result["reasons"] == ["distinctive type scale"]
    assert result["generic_ai_tells"] == []


def test_score_screenshot_clamps_and_derives_verdict(tmp_path, monkeypatch):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    monkeypatch.setattr(design_vision.shutil, "which", lambda name: "/usr/bin/claude")

    async def fake_run_cli(args, stdin_data=None):  # noqa: ARG001
        # Out-of-range score, missing verdict → clamp + derive fail.
        return '{"score": 250, "reasons": [], "generic_ai_tells": ["indigo gradient"]}'

    monkeypatch.setattr(design_vision, "_run_cli", fake_run_cli)
    result = asyncio.run(design_vision.score_screenshot(img))
    assert result is not None
    assert result["score"] == 100  # clamped
    assert result["verdict"] == "pass"  # derived from clamped score >= 60


def test_score_screenshot_falls_through_unusable_then_none(tmp_path, monkeypatch):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    monkeypatch.setattr(design_vision.shutil, "which", lambda name: "/usr/bin/claude")

    async def fake_run_cli(args, stdin_data=None):  # noqa: ARG001
        return "not json at all"

    monkeypatch.setattr(design_vision, "_run_cli", fake_run_cli)
    # Unparseable from every CLI → None (rubric unavailable, non-blocking).
    result = asyncio.run(design_vision.score_screenshot(img))
    assert result is None


def test_score_screenshot_cli_exception_is_swallowed(tmp_path, monkeypatch):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    monkeypatch.setattr(design_vision.shutil, "which", lambda name: "/usr/bin/claude")

    async def boom(args, stdin_data=None):  # noqa: ARG001
        raise RuntimeError("cli died")

    monkeypatch.setattr(design_vision, "_run_cli", boom)
    result = asyncio.run(design_vision.score_screenshot(img))
    assert result is None
