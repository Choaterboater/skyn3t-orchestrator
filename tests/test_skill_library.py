"""Tests for skyn3t.intelligence.skill_library — durable learned-skill
files. Closes the gap to Hermes' first-class skill abstraction.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from skyn3t.intelligence.skill_library import (
    Skill,
    SkillLibrary,
    _slugify,
    get_default_library,
)


# ─── Slugify ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("FastAPI build wins", "fastapi-build-wins"),
    ("react/vite app shape", "react-vite-app-shape"),
    ("   spaces   ", "spaces"),
    ("UPPERCASE!?", "uppercase"),
    ("", "skill"),
])
def test_slugify_normalizes_to_filesystem_safe(raw, expected):
    assert _slugify(raw) == expected


def test_slugify_caps_length():
    long_name = "a" * 200
    assert len(_slugify(long_name)) <= 80


# ─── Skill (dataclass + serialization) ─────────────────────────────────


def test_skill_score_zero_with_no_signal():
    s = Skill(name="x")
    assert s.score == 0.0


def test_skill_score_one_when_all_success():
    s = Skill(name="x", success_count=5)
    assert s.score == 1.0


def test_skill_score_minus_one_when_all_failure():
    s = Skill(name="x", failure_count=5)
    assert s.score == -1.0


def test_skill_to_from_markdown_roundtrip():
    original = Skill(
        name="fastapi-tests-test-health",
        tags=["fastapi", "build-success"],
        success_count=6,
        failure_count=1,
        last_used_at=1729012345.6,
        source="build_pattern_scan",
        created_at=1729000000.0,
        body="# Always include tests/test_health.py.\n\nIt catches missing imports at smoke time.",
    )
    text = original.to_markdown()
    # Frontmatter shape sanity.
    assert text.startswith("---\n")
    assert "name: fastapi-tests-test-health" in text
    assert "tags: [build-success, fastapi]" in text
    assert "success_count: 6" in text
    # Round-trip.
    parsed = Skill.from_markdown(text)
    assert parsed.name == original.name
    assert sorted(parsed.tags) == sorted(original.tags)
    assert parsed.success_count == 6
    assert parsed.failure_count == 1
    assert "Always include" in parsed.body


def test_skill_from_markdown_handles_missing_fields():
    """Best-effort parse — never raise on a partially-written file."""
    text = "---\nname: weird\n---\n\njust the body"
    s = Skill.from_markdown(text)
    assert s.name == "weird"
    assert s.tags == []
    assert s.success_count == 0
    assert "just the body" in s.body


def test_skill_from_markdown_handles_no_frontmatter():
    text = "Just markdown body, no frontmatter"
    s = Skill.from_markdown(text)
    assert s.name == "untitled"
    assert text in s.body


# ─── SkillLibrary CRUD ─────────────────────────────────────────────────


def test_upsert_creates_file_atomically(tmp_path):
    lib = SkillLibrary(root=tmp_path / "skills")
    skill = Skill(
        name="next-app-router",
        tags=["next", "react"],
        body="# Use the app/ router.",
        success_count=3,
    )
    path = lib.upsert(skill)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "name: next-app-router" in text


def test_upsert_merges_with_existing_file(tmp_path):
    """Re-upserting the same slug preserves created_at and accumulates counts."""
    lib = SkillLibrary(root=tmp_path / "skills")
    s1 = Skill(name="my-skill", tags=["a"], success_count=2, failure_count=0,
               created_at=100.0)
    lib.upsert(s1)
    s2 = Skill(name="my-skill", tags=["b"], success_count=5, failure_count=1,
               created_at=999.0)  # new created_at — must be ignored
    lib.upsert(s2)
    [reloaded] = lib.all()
    assert reloaded.created_at == 100.0  # preserved
    # max of existing+new on each count
    assert reloaded.success_count == 5
    assert reloaded.failure_count == 1
    # tags merged
    assert sorted(reloaded.tags) == ["a", "b"]


def test_find_by_tag_filters_and_orders(tmp_path):
    lib = SkillLibrary(root=tmp_path / "skills")
    lib.upsert(Skill(name="a", tags=["next"], success_count=5, failure_count=0))
    lib.upsert(Skill(name="b", tags=["next"], success_count=1, failure_count=4))
    lib.upsert(Skill(name="c", tags=["fastapi"], success_count=3))
    next_skills = lib.find(tag="next")
    names = [s.name for s in next_skills]
    # 'a' wins (score=1), 'b' is demoted (score=-0.6) and filtered out by min_score=0
    assert "a" in names
    assert "b" not in names
    assert "c" not in names  # different tag


def test_find_respects_min_score(tmp_path):
    lib = SkillLibrary(root=tmp_path / "skills")
    lib.upsert(Skill(name="weak", tags=["x"], success_count=1, failure_count=1))
    # min_score=0 includes neutral (score=0); min_score=0.5 doesn't.
    assert lib.find(tag="x", min_score=0.0)
    assert not lib.find(tag="x", min_score=0.5)


def test_find_returns_empty_on_unknown_tag(tmp_path):
    lib = SkillLibrary(root=tmp_path / "skills")
    lib.upsert(Skill(name="a", tags=["next"]))
    assert lib.find(tag="ios") == []


def test_record_use_ticks_counts(tmp_path):
    lib = SkillLibrary(root=tmp_path / "skills")
    lib.upsert(Skill(name="practice", tags=["x"]))
    lib.record_use("practice", success=True)
    lib.record_use("practice", success=True)
    lib.record_use("practice", success=False)
    [reloaded] = lib.all()
    assert reloaded.success_count == 2
    assert reloaded.failure_count == 1


def test_record_use_on_missing_skill_returns_none(tmp_path):
    lib = SkillLibrary(root=tmp_path / "skills")
    assert lib.record_use("never-existed", success=True) is None


def test_delete_removes_file(tmp_path):
    lib = SkillLibrary(root=tmp_path / "skills")
    lib.upsert(Skill(name="goner", tags=["x"]))
    assert lib.delete("goner") is True
    assert lib.delete("goner") is False  # already gone
    assert lib.all() == []


def test_summary_aggregates_counts(tmp_path):
    lib = SkillLibrary(root=tmp_path / "skills")
    lib.upsert(Skill(name="good", tags=["next"], success_count=5))
    lib.upsert(Skill(name="bad", tags=["fastapi"], failure_count=5))
    lib.upsert(Skill(name="neutral", tags=["flask"]))
    s = lib.summary()
    assert s["total"] == 3
    assert s["net_helpful"] == 1
    assert s["demoted"] == 1
    assert sorted(s["tags"]) == ["fastapi", "flask", "next"]


def test_external_edits_are_picked_up_on_next_scan(tmp_path):
    """A human curates a skill file directly — the library reflects it
    without an explicit reload."""
    root = tmp_path / "skills"
    lib = SkillLibrary(root=root)
    # Write a hand-curated file
    (root / "manual-skill.md").write_text(
        "---\nname: hand-curated\ntags: [docs]\n---\n\n# manual content"
    )
    found = lib.find(tag="docs", min_score=-1.0)
    assert any(s.name == "hand-curated" for s in found)


def test_get_default_library_singleton(monkeypatch, tmp_path):
    import skyn3t.intelligence.skill_library as sl
    monkeypatch.setattr(sl, "_default_library", None)
    monkeypatch.chdir(tmp_path)
    a = sl.get_default_library()
    b = sl.get_default_library()
    assert a is b


def test_malformed_file_does_not_crash_scan(tmp_path):
    """A garbage .md file should be skipped, not poison the entire scan."""
    root = tmp_path / "skills"
    root.mkdir()
    (root / "broken.md").write_text("\x00\x01\x02 not valid utf or markdown")
    lib = SkillLibrary(root=root)
    lib.upsert(Skill(name="ok", tags=["x"]))
    skills = lib.all()
    names = [s.name for s in skills]
    assert "ok" in names
