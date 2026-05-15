"""Tests for skyn3t.agents.service_brand_kit.

The brand kit feeds visual context (icons, colors, widget shapes) to
CodeAgent so it builds product-shaped widgets instead of generic JSON
dumps. These tests cover the three-function public surface.
"""

from __future__ import annotations

from skyn3t.agents.service_brand_kit import (
    ServiceBrand,
    brand_for,
    brand_kit_markdown,
    known_slugs,
)

# ─── brand_for() ────────────────────────────────────────────────────────


def test_brand_for_known_slug_returns_record():
    b = brand_for("sonarr")
    assert b is not None
    assert isinstance(b, ServiceBrand)
    assert b.slug == "sonarr"
    assert b.name == "Sonarr"
    assert b.color.startswith("#")
    assert b.icon_url.startswith("https://")


def test_brand_for_is_case_insensitive():
    """The catalog is keyed by lowercase slug. Lookup must normalize."""
    assert brand_for("SONARR") is not None
    assert brand_for("Sonarr") is not None
    assert brand_for("sonarr") is not None


def test_brand_for_unknown_slug_returns_none():
    assert brand_for("never-heard-of-this") is None


def test_brand_for_empty_string_returns_none():
    assert brand_for("") is None


def test_brand_for_none_safe_for_callers_via_falsy_check():
    """The function guards against falsy input — callers pass user/LLM
    output directly without a wrapping check."""
    # Passing None would be a type error in mypy land, but the runtime
    # guard at line 231 returns None instead of raising — verify the
    # falsy-input contract holds.
    assert brand_for("") is None


# ─── brand_kit_markdown() ──────────────────────────────────────────────


def test_brand_kit_markdown_empty_list_returns_empty_string():
    assert brand_kit_markdown([]) == ""


def test_brand_kit_markdown_renders_header_when_any_slug_resolves():
    md = brand_kit_markdown(["sonarr"])
    assert "## Service brand kit" in md
    assert "Sonarr" in md


def test_brand_kit_markdown_includes_icon_color_category_widget():
    md = brand_kit_markdown(["sonarr"])
    # Each block has these four pieces — without any of them, the LLM
    # can't build a product-shaped widget.
    assert "icon:" in md
    assert "brand color:" in md
    assert "category:" in md
    assert "widget shape:" in md


def test_brand_kit_markdown_skips_unknown_slugs_silently():
    """Unknown slugs are dropped (not error-flagged) — the kit is a
    best-effort hint, not a validator."""
    md = brand_kit_markdown(["sonarr", "fake-service", "radarr"])
    assert "Sonarr" in md
    assert "Radarr" in md
    assert "fake-service" not in md


def test_brand_kit_markdown_all_unknown_still_returns_header_only():
    """When every slug is unknown, the header still gets emitted but
    no service blocks follow. Acceptable degenerate output — the
    CodeAgent's prompt won't break."""
    md = brand_kit_markdown(["fake-1", "fake-2"])
    assert "## Service brand kit" in md
    # The two known structural sections are header + integration note;
    # no service body follows.
    assert "### " not in md


def test_brand_kit_markdown_preserves_slug_order():
    """If the brief lists Sonarr first, the markdown should too —
    matters for the LLM's primary-vs-secondary attention."""
    md = brand_kit_markdown(["radarr", "sonarr"])
    radarr_pos = md.find("### Radarr")
    sonarr_pos = md.find("### Sonarr")
    assert radarr_pos != -1
    assert sonarr_pos != -1
    assert radarr_pos < sonarr_pos


# ─── known_slugs() ─────────────────────────────────────────────────────


def test_known_slugs_returns_sorted_list_of_strings():
    slugs = known_slugs()
    assert isinstance(slugs, list)
    assert all(isinstance(s, str) for s in slugs)
    assert slugs == sorted(slugs)


def test_known_slugs_includes_seed_services():
    """The minimum coverage that the codebase relies on: sonarr/radarr
    must be present (homelab dashboards are the canonical use case)."""
    slugs = set(known_slugs())
    assert "sonarr" in slugs
    assert "radarr" in slugs


def test_every_known_slug_resolves():
    """Round-trip: every slug returned by known_slugs() must resolve
    via brand_for(). Catches catalog-key drift."""
    for slug in known_slugs():
        assert brand_for(slug) is not None, f"known slug {slug} did not resolve"


def test_every_brand_has_complete_fields():
    """No silently-missing fields in catalog entries."""
    for slug in known_slugs():
        b = brand_for(slug)
        assert b is not None
        assert b.slug
        assert b.name
        assert b.icon_url.startswith("https://")
        assert b.color.startswith("#")
        assert len(b.color) in (4, 7, 9)  # #RGB, #RRGGBB, or #RRGGBBAA
        assert b.widget  # non-empty
        assert b.category  # non-empty
