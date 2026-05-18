"""DesignerAgent now ships real code assets alongside its markdown.

Previously a brand_kit project produced 6 .md files + palette.json — no
tokens.css, no SVG, no design-tokens-shape JSON. Receivers had to hand-
transcribe the palette into their own CSS. These tests pin down that
the agent now writes droppable assets too.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from skyn3t.agents.designer import DesignerAgent
from skyn3t.core.agent import TaskRequest

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


@pytest.mark.asyncio
async def test_llm_generate_stops_retrying_after_deterministic_fallback(monkeypatch):
    calls = {"count": 0}

    class FakeLLMClient:
        def __init__(self, *args, **kwargs):
            return None

        async def complete(self, prompt, *, max_tokens, temperature):
            calls["count"] += 1
            return "[deterministic-stub]\nbackend unavailable"

    monkeypatch.setattr("skyn3t.adapters.LLMClient", FakeLLMClient)

    agent = DesignerAgent()

    first = await agent._llm_generate(
        role_prompt="Write brand markdown.",
        brief="Build a premium dashboard.",
        fallback="fallback-one",
        max_tokens=100,
        kind="brand",
    )
    second = await agent._llm_generate(
        role_prompt="Write components markdown.",
        brief="Build a premium dashboard.",
        fallback="fallback-two",
        max_tokens=100,
        kind="components",
    )

    assert first == "fallback-one"
    assert second == "fallback-two"
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_llm_generate_skips_failed_backend_on_later_calls(monkeypatch):
    seen_skip_backends = []

    class FakeLLMClient:
        def __init__(self, *args, **kwargs):
            seen_skip_backends.append(list(kwargs.get("skip_backends") or []))
            self._last_failed_backend = ""

        async def complete(self, prompt, *, max_tokens, temperature):  # noqa: ARG002
            if len(seen_skip_backends) == 1:
                self._last_failed_backend = "kimi_cli"
                return "use copilot fallback output that is comfortably longer than eighty characters for acceptance."
            return "second successful output that also clears the minimum length threshold for acceptance."

    monkeypatch.setattr("skyn3t.adapters.LLMClient", FakeLLMClient)

    agent = DesignerAgent()

    first = await agent._llm_generate(
        role_prompt="Write brand markdown.",
        brief="Build a premium dashboard.",
        fallback="fallback-one",
        max_tokens=100,
        kind="brand",
    )
    second = await agent._llm_generate(
        role_prompt="Write components markdown.",
        brief="Build a premium dashboard.",
        fallback="fallback-two",
        max_tokens=100,
        kind="components",
    )

    assert first.startswith("use copilot fallback output")
    assert second.startswith("second successful output")
    assert seen_skip_backends == [[], ["kimi_cli"]]


@pytest.mark.asyncio
async def test_llm_generate_times_out_and_uses_fallback(monkeypatch):
    import asyncio

    calls = {"count": 0}

    class FakeLLMClient:
        def __init__(self, *args, **kwargs):
            self.backend = "kimi_cli"
            self._last_failed_backend = ""

        async def complete(self, prompt, *, max_tokens, temperature):  # noqa: ARG002
            calls["count"] += 1
            await asyncio.sleep(1)
            return "this should never be returned"

    monkeypatch.setattr("skyn3t.adapters.LLMClient", FakeLLMClient)
    monkeypatch.setattr("skyn3t.agents.designer._LLM_GENERATE_TIMEOUT_SECONDS", 0.01)

    agent = DesignerAgent()

    first = await agent._llm_generate(
        role_prompt="Write brand markdown.",
        brief="Build a premium dashboard.",
        fallback="fallback-one",
        max_tokens=100,
        kind="brand",
    )
    second = await agent._llm_generate(
        role_prompt="Write components markdown.",
        brief="Build a premium dashboard.",
        fallback="fallback-two",
        max_tokens=100,
        kind="components",
    )

    assert first == "fallback-one"
    assert second == "fallback-two"
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_execute_does_not_hang_on_post_write_side_effects(monkeypatch, tmp_path: Path):
    async def fake_pick_palette(self, brief, mood):  # noqa: ARG001
        return dict(PALETTE)

    async def fake_render_brand_md(self, brief, mood, palette_tuple, fonts, voice, logos):  # noqa: ARG001
        return "# Brand\n"

    async def fake_render_components_md(self, brief, mood, target, palette_json):  # noqa: ARG001
        return "# Components\n"

    async def slow_send_message(*args, **kwargs):  # noqa: ARG001
        await asyncio.sleep(1)

    async def slow_share_learning(*args, **kwargs):  # noqa: ARG001
        await asyncio.sleep(1)

    import asyncio

    monkeypatch.setattr("skyn3t.agents.designer._POST_WRITE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(DesignerAgent, "_pick_palette", fake_pick_palette)
    monkeypatch.setattr(DesignerAgent, "_render_brand_md", fake_render_brand_md)
    monkeypatch.setattr(DesignerAgent, "_render_components_md", fake_render_components_md)
    monkeypatch.setattr(DesignerAgent, "send_message", slow_send_message)
    monkeypatch.setattr(DesignerAgent, "share_learning", slow_share_learning)

    agent = DesignerAgent()
    task = TaskRequest(
        task_id="designer-side-effect-timeout",
        input_data={
            "brief": "Build a premium dashboard.",
            "artifact_dir": str(tmp_path),
            "next_agent": "CodeAgent",
        },
    )

    result = await agent.execute(task)

    assert result.success is True
    assert sorted(Path(p).name for p in result.output["files"]) == [
        "README.md",
        "brand.md",
        "components.md",
        "logo.svg",
        "palette.json",
        "tokens.css",
        "tokens.json",
    ]
    assert (tmp_path / "brand.md").exists()
    assert (tmp_path / "tokens.css").exists()
