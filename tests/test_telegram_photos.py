"""Tests for the photo / design-reference pipeline.

Covers:
- Auto-tag extraction from caption
- Library load/save round-trip
- Pick-largest-photo-variant heuristic
- Match-references-to-brief by tag overlap
- Canonical brand registration (no double-add)
- Designer integration: loading and using attached references

External dependencies (Telegram getFile/download, Claude vision) are
mocked — these tests run offline.
"""

from __future__ import annotations

import json

import pytest

from skyn3t.agents.design_vision import DesignReference, PaletteEntry
from skyn3t.integrations import telegram_photos as photos

# ---------------------------------------------------------------------------
# Per-test isolation: redirect data dir to a tmp_path
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_refs_dir(tmp_path, monkeypatch):
    """Redirect ``data_dir`` to a tmp path so each test gets a clean
    library + extraction cache."""
    monkeypatch.setattr(photos, "_refs_dir", lambda: tmp_path / "design_references")

    # Same redirection for the design_vision extraction cache.
    from skyn3t.agents import design_vision
    monkeypatch.setattr(
        design_vision, "_extractions_dir",
        lambda: tmp_path / "design_references" / "extractions",
    )
    return tmp_path / "design_references"


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_auto_tag_from_caption_hashtag_form():
    assert photos._auto_tag_from_caption("#warm #dense #indigo") == ["warm", "dense", "indigo"]


def test_auto_tag_from_caption_space_form():
    assert photos._auto_tag_from_caption("warm dense indigo") == ["warm", "dense", "indigo"]


def test_auto_tag_from_caption_mixed():
    assert photos._auto_tag_from_caption("#warm, soft, dense indigo") == ["warm", "soft", "dense", "indigo"]


def test_auto_tag_caps_at_8():
    caption = " ".join(f"tag{i}" for i in range(20))
    assert len(photos._auto_tag_from_caption(caption)) == 8


def test_auto_tag_skips_short_and_long_words():
    # 1-char words and >24-char words should be excluded.
    caption = "a really longword " + ("x" * 25)
    tags = photos._auto_tag_from_caption(caption)
    assert "a" not in tags
    assert ("x" * 25) not in tags


def test_pick_largest_photo_variant_by_file_size():
    photos_payload = [
        {"file_id": "a", "file_size": 100, "width": 50, "height": 50},
        {"file_id": "b", "file_size": 5000, "width": 200, "height": 200},
        {"file_id": "c", "file_size": 500, "width": 100, "height": 100},
    ]
    picked = photos._pick_largest_photo_variant(photos_payload)
    assert picked["file_id"] == "b"


def test_pick_largest_photo_variant_fallback_to_dimensions():
    photos_payload = [
        {"file_id": "a", "width": 50, "height": 50},
        {"file_id": "b", "width": 1000, "height": 800},
        {"file_id": "c", "width": 200, "height": 200},
    ]
    picked = photos._pick_largest_photo_variant(photos_payload)
    assert picked["file_id"] == "b"


def test_pick_largest_photo_variant_empty():
    assert photos._pick_largest_photo_variant([]) is None


# ---------------------------------------------------------------------------
# Library tests
# ---------------------------------------------------------------------------


def _make_entry(library, *, id="abc12345", sha="a" * 64, path="/tmp/x.jpg",
                user_id="u1", tags=None, uploaded_at=100.0,
                extraction_ok=False, caption=""):
    entry = photos.LibraryEntry(
        id=id, sha=sha, path=path, user_id=user_id,
        uploaded_at=uploaded_at, tags=list(tags or []),
        extraction_ok=extraction_ok, caption=caption,
    )
    library[id] = entry
    return entry


def test_library_save_load_roundtrip(isolated_refs_dir):
    library = {}
    _make_entry(library, id="a1", uploaded_at=100.0, tags=["warm"])
    _make_entry(library, id="b2", uploaded_at=200.0, tags=["dense", "indigo"])
    photos._save_library(library)
    loaded = photos._load_library()
    assert set(loaded.keys()) == {"a1", "b2"}
    assert loaded["b2"].tags == ["dense", "indigo"]


def test_list_references_filters_by_user(isolated_refs_dir):
    library = {}
    _make_entry(library, id="a1", user_id="u1", uploaded_at=100.0)
    _make_entry(library, id="b2", user_id="u2", uploaded_at=200.0)
    photos._save_library(library)
    u1_refs = photos.list_references(user_id="u1")
    assert len(u1_refs) == 1 and u1_refs[0].id == "a1"


def test_list_references_sorts_newest_first(isolated_refs_dir):
    library = {}
    _make_entry(library, id="old", uploaded_at=100.0)
    _make_entry(library, id="new", uploaded_at=300.0)
    _make_entry(library, id="mid", uploaded_at=200.0)
    photos._save_library(library)
    out = photos.list_references()
    assert [e.id for e in out] == ["new", "mid", "old"]


def test_update_tags_add(isolated_refs_dir):
    library = {}
    _make_entry(library, id="a1", tags=["warm"])
    photos._save_library(library)
    updated = photos.update_tags("a1", add=["dense", "indigo"])
    assert set(updated.tags) == {"warm", "dense", "indigo"}


def test_update_tags_remove(isolated_refs_dir):
    library = {}
    _make_entry(library, id="a1", tags=["warm", "dense", "indigo"])
    photos._save_library(library)
    updated = photos.update_tags("a1", remove=["dense"])
    assert "dense" not in updated.tags
    assert "warm" in updated.tags


def test_update_tags_unknown_returns_none(isolated_refs_dir):
    assert photos.update_tags("missing", add=["x"]) is None


def test_recent_uploads_within_window(isolated_refs_dir):
    import time as _time
    library = {}
    now = _time.time()
    _make_entry(library, id="fresh", user_id="u1", uploaded_at=now - 60)
    _make_entry(library, id="old", user_id="u1", uploaded_at=now - 1000)
    photos._save_library(library)
    recent = photos.recent_uploads("u1", window_seconds=300)
    assert [e.id for e in recent] == ["fresh"]


def test_match_references_to_brief_by_tag_overlap(isolated_refs_dir):
    library = {}
    _make_entry(library, id="indigo-ref", user_id="u1", tags=["indigo", "dense"])
    _make_entry(library, id="warm-ref", user_id="u1", tags=["warm", "airy"])
    _make_entry(library, id="other-user", user_id="u2", tags=["indigo"])
    photos._save_library(library)
    matches = photos.match_references_to_brief("build an indigo dashboard", "u1")
    assert matches[0].id == "indigo-ref"
    assert all(m.user_id == "u1" for m in matches)


def test_match_returns_empty_when_no_overlap(isolated_refs_dir):
    library = {}
    _make_entry(library, id="a1", user_id="u1", tags=["unrelated"])
    photos._save_library(library)
    assert photos.match_references_to_brief("build a calendar app", "u1") == []


# ---------------------------------------------------------------------------
# Canonical brand registration
# ---------------------------------------------------------------------------


def test_register_canonical_references_no_dir(isolated_refs_dir):
    # Directory doesn't exist yet → no-op
    assert photos.register_canonical_references() == []


def test_register_canonical_references_picks_up_files(isolated_refs_dir):
    isolated_refs_dir.mkdir(parents=True, exist_ok=True)
    (isolated_refs_dir / "canonical_brand.png").write_bytes(b"\x89PNG fakefile1")
    (isolated_refs_dir / "canonical_favicon.png").write_bytes(b"\x89PNG fakefile2")
    (isolated_refs_dir / "unrelated.png").write_bytes(b"\x89PNG ignore_me")
    added = photos.register_canonical_references()
    assert len(added) == 2
    assert all("canonical" in e.tags for e in added)


def test_register_canonical_idempotent(isolated_refs_dir):
    isolated_refs_dir.mkdir(parents=True, exist_ok=True)
    (isolated_refs_dir / "canonical_brand.png").write_bytes(b"\x89PNG fakefile1")
    photos.register_canonical_references()
    photos.register_canonical_references()  # second run
    assert len(photos.list_canonical_references()) == 1


def test_list_canonical_references_only(isolated_refs_dir):
    library = {}
    _make_entry(library, id="canon", tags=["canonical", "default"])
    _make_entry(library, id="user-upload", tags=["warm"])
    photos._save_library(library)
    canonical = photos.list_canonical_references()
    assert len(canonical) == 1 and canonical[0].id == "canon"


# ---------------------------------------------------------------------------
# Project attachment
# ---------------------------------------------------------------------------


def test_attach_references_writes_design_references_md(isolated_refs_dir, tmp_path):
    library = {}
    _make_entry(
        library, id="ref1", sha="x" * 64, path="/tmp/x.jpg",
        caption="warm mood", tags=["warm"],
        extraction_ok=True,
    )
    photos._save_library(library)
    # Pre-populate the extraction cache so the markdown fragment has body
    extraction_dir = isolated_refs_dir / "extractions"
    extraction_dir.mkdir(parents=True, exist_ok=True)
    (extraction_dir / f"{'x' * 64}.json").write_text(json.dumps({
        "image_path": "/tmp/x.jpg",
        "image_sha": "x" * 64,
        "palette": [{"name": "midnight", "hex": "#0A0A0A", "role": "bg"},
                    {"name": "ember", "hex": "#E05C1A", "role": "accent"}],
        "typography_vibe": "warm serif",
        "layout_density": "balanced",
        "mood": ["techno-artisan", "warm"],
        "notable_elements": ["thick stroke borders"],
        "forbidden_words": ["cold", "glassy"],
        "verdict_one_liner": "Warm artisan workshop terminal feel.",
        "extracted_at": 100.0,
    }), encoding="utf-8")

    project_dir = tmp_path / "projects" / "test-proj"
    photos.attach_references_to_project(project_dir, ["ref1"])

    refs_md = project_dir / "design_references.md"
    assert refs_md.exists()
    content = refs_md.read_text(encoding="utf-8")
    assert "Reference `ref1`" in content
    assert "warm mood" in content
    assert "Warm artisan workshop terminal feel" in content
    assert "#E05C1A" in content
    assert "warm serif" in content
    assert "cold" in content  # forbidden words listed


def test_attach_references_skips_unknown_ids(isolated_refs_dir, tmp_path):
    photos._save_library({})
    project_dir = tmp_path / "projects" / "empty"
    photos.attach_references_to_project(project_dir, ["nope"])
    assert not (project_dir / "design_references.md").exists()


def test_project_has_attached_references(isolated_refs_dir, tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir(parents=True)
    assert not photos.project_has_attached_references(project_dir)
    (project_dir / "design_references.md").write_text("hi")
    assert photos.project_has_attached_references(project_dir)


# ---------------------------------------------------------------------------
# Designer integration
# ---------------------------------------------------------------------------


def test_designer_loads_attached_references(isolated_refs_dir, tmp_path):
    """DesignerAgent._load_attached_references reads design_references.md
    and resolves back to DesignReference objects via the cache."""
    library = {}
    _make_entry(library, id="r1", sha="z" * 64, path="/tmp/z.jpg", extraction_ok=True)
    photos._save_library(library)
    ext_dir = isolated_refs_dir / "extractions"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / f"{'z' * 64}.json").write_text(json.dumps({
        "image_path": "/tmp/z.jpg",
        "image_sha": "z" * 64,
        "palette": [{"name": "n", "hex": "#000000", "role": "bg"}],
        "mood": ["cyberpunk", "synthwave"],
    }), encoding="utf-8")
    proj = tmp_path / "p"
    photos.attach_references_to_project(proj, ["r1"])

    from skyn3t.agents.designer import DesignerAgent
    agent = DesignerAgent.__new__(DesignerAgent)
    refs = agent._load_attached_references(proj)
    assert len(refs) == 1
    assert refs[0].mood == ["cyberpunk", "synthwave"]


def test_designer_palette_from_references():
    """Palette extraction maps roles correctly with sensible fallbacks."""
    from skyn3t.agents.designer import DesignerAgent
    agent = DesignerAgent.__new__(DesignerAgent)
    ref = DesignReference(
        image_path="x", image_sha="x",
        palette=[
            PaletteEntry(name="black", hex="#000000", role="bg"),
            PaletteEntry(name="cyan", hex="#0FF0FC", role="accent"),
            PaletteEntry(name="white", hex="#FFFFFF", role="text"),
        ],
    )
    palette = agent._palette_from_references([ref])
    assert palette is not None
    assert palette["bg"] == "#000000"
    assert palette["accent"] == "#0FF0FC"
    assert palette["text"] == "#FFFFFF"


def test_designer_palette_returns_none_with_no_palette():
    from skyn3t.agents.designer import DesignerAgent
    agent = DesignerAgent.__new__(DesignerAgent)
    ref = DesignReference(image_path="x", image_sha="x", palette=[])
    assert agent._palette_from_references([ref]) is None
