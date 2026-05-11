"""DesignerAgent now ships real code assets alongside its markdown.

Previously a brand_kit project produced 6 .md files + palette.json — no
tokens.css, no SVG, no design-tokens-shape JSON. Receivers had to hand-
transcribe the palette into their own CSS. These tests pin down that
the agent now writes droppable assets too.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from skyn3t.agents.designer import DesignerAgent


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


def test_tokens_css_includes_every_palette_role():
    css = DesignerAgent._render_tokens_css(PALETTE, FONTS)
    for role, value in PALETTE.items():
        assert f"--brand-{role}: {value};" in css, role
    assert "--brand-font-heading: Orbitron" in css
    assert "--brand-font-body: Rajdhani" in css
    assert "--brand-font-mono: JetBrains Mono" in css


def test_tokens_css_parses_as_valid_css_shape():
    """No syntax error: opens with :root and ends with }."""
    css = DesignerAgent._render_tokens_css(PALETTE, FONTS)
    assert ":root {" in css
    assert css.rstrip().endswith("}")


def test_design_tokens_w3c_shape():
    tokens = DesignerAgent._render_design_tokens(PALETTE, FONTS)
    # W3C design-tokens shape: each leaf is { value, type }.
    for role in PALETTE:
        leaf = tokens["color"][role]
        assert leaf["value"] == PALETTE[role]
        assert leaf["type"] == "color"
    for role in ("heading", "body", "mono"):
        leaf = tokens["font"][role]
        assert leaf["type"] == "fontFamily"
        assert leaf["value"] == FONTS[role]


def test_design_tokens_serializes_to_json():
    """The dict must be JSON-serializable for Style Dictionary etc."""
    tokens = DesignerAgent._render_design_tokens(PALETTE, FONTS)
    text = json.dumps(tokens)
    assert "C8102E" in text  # raw hex survives


def test_logo_svg_is_well_formed_xml():
    svg = DesignerAgent._render_logo_svg("Skyn3t — autonomous orchestration", "hud", PALETTE)
    # Parses without error and has the expected root tag.
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert root.attrib.get("viewBox") == "0 0 240 240"
    assert "C8102E" in svg or "#C8102E" in svg


def test_logo_svg_renders_warm_mood_with_serif_monogram():
    svg = DesignerAgent._render_logo_svg("Atelier coffee shop", "luxury", PALETTE)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert "serif" in svg.lower()


def test_logo_svg_handles_empty_brief():
    """No brief, no crash."""
    svg = DesignerAgent._render_logo_svg("", "minimal", PALETTE)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")


def test_readme_lists_every_asset_file():
    readme = DesignerAgent._render_kit_readme("Skyn3t", PALETTE, FONTS)
    for f in ("brand.md", "palette.json", "tokens.css", "tokens.json",
              "logo.svg", "components.md", "brand_voice_guide.md", "review.md"):
        assert f in readme, f
    # Palette values surface verbatim so the user can copy them.
    for v in PALETTE.values():
        assert v in readme
