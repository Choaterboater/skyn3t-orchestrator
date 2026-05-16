"""Tests for skyn3t.agents.brief_requirements."""

from __future__ import annotations

from skyn3t.agents.brief_requirements import (
    extract_requirements,
    format_global_summary,
    format_hard_rules,
)


def test_empty_brief_returns_empty_requirements() -> None:
    reqs = extract_requirements("")
    assert reqs.is_empty()
    assert format_hard_rules(reqs, "src/styles.css") == ""


def test_brief_with_no_known_keywords_is_empty() -> None:
    reqs = extract_requirements("Build a thing.")
    assert reqs.is_empty()


def test_glassmorphism_fires_for_css_only() -> None:
    reqs = extract_requirements("Build a dashboard with glassmorphism")
    css_rules = reqs.for_file("src/styles.css")
    js_rules = reqs.for_file("src/App.jsx")
    assert any("GLASSMORPHISM" in r for r in css_rules)
    assert not any("GLASSMORPHISM" in r for r in js_rules)


def test_command_palette_fires_for_jsx_only() -> None:
    reqs = extract_requirements("Add a command palette triggered by Cmd+K")
    jsx_rules = reqs.for_file("src/App.jsx")
    css_rules = reqs.for_file("src/styles.css")
    assert any("COMMAND PALETTE" in r for r in jsx_rules)
    assert not any("COMMAND PALETTE" in r for r in css_rules)


def test_health_endpoint_fires_for_backend_files() -> None:
    reqs = extract_requirements("The app needs a health endpoint")
    js_rules = reqs.for_file("server/index.js")
    assert any("HEALTH ENDPOINT" in r for r in js_rules)


def test_css_prelude_uses_palette_with_dark_bg() -> None:
    reqs = extract_requirements("Build a polished dashboard with glassmorphism and dark mode")
    out = format_hard_rules(
        reqs, "src/styles.css",
        palette_hexes=["#E05C1A", "#0F0D0A", "#E8DDCB"],
    )
    assert "REQUIRED PRELUDE" in out
    assert "color-scheme: dark" in out
    # darkest hex must land on --bg, lightest on --text
    assert "--bg: #0F0D0A" in out
    assert "--text: #E8DDCB" in out
    assert "backdrop-filter: blur" in out


def test_css_prelude_omitted_when_brief_has_no_glass_or_dark() -> None:
    reqs = extract_requirements("Build a CLI tool")
    out = format_hard_rules(reqs, "src/styles.css", palette_hexes=["#123456"])
    assert "REQUIRED PRELUDE" not in out


def test_polish_rule_fires_on_polished_or_premium_keyword() -> None:
    reqs = extract_requirements("ship a polished production-ready dashboard")
    css_rules = reqs.for_file("src/styles.css")
    assert any("POLISH" in r for r in css_rules)


def test_format_global_summary_dedupes_across_extensions() -> None:
    reqs = extract_requirements(
        "Build a polished dashboard with dark mode, command palette, and health endpoint"
    )
    summary = format_global_summary(reqs)
    # POLISH applies to many file types but should appear once in summary.
    assert summary.count("POLISH:") == 1
