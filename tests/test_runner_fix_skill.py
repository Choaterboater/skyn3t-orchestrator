"""Tests for the runner's _persist_fix_as_skill helper.

When the in-place build-fix loop resolves a failure, the runner writes
a Skill file capturing what worked so future scaffolds for the same
stack get the lesson in their system prompt automatically.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def fresh_lib(monkeypatch, tmp_path):
    """Fresh, isolated SkillLibrary pointed at tmp."""
    import skyn3t.intelligence.skill_library as sl
    skill_root = tmp_path / "skills"
    monkeypatch.setattr(sl, "_default_library", None)
    lib = sl.SkillLibrary(root=skill_root)
    monkeypatch.setattr(sl, "get_default_library", lambda: lib)
    return lib


@pytest.fixture
def runner(monkeypatch, tmp_path):
    """StudioRunner with the projects_root pointed at tmp_path."""
    from skyn3t.core.events import EventBus
    from skyn3t.studio.runner import StudioRunner
    return StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")


def test_persist_fix_as_skill_writes_a_tagged_skill(runner, fresh_lib):
    runner._persist_fix_as_skill(
        stack="fastapi",
        fix_round=1,
        prior_summary="py_compile failed: SyntaxError on src/main.py line 12",
    )
    skills = fresh_lib.all()
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "fastapi-fix-loop-round-1"
    assert "fastapi" in s.tags
    assert "fix-loop" in s.tags
    assert "build-success" in s.tags
    assert "py_compile failed" in s.body


def test_persist_fix_as_skill_skips_unknown_stack(runner, fresh_lib):
    """unknown / empty stacks shouldn't pollute the library."""
    runner._persist_fix_as_skill(stack="", fix_round=1)
    runner._persist_fix_as_skill(stack="unknown", fix_round=1)
    assert fresh_lib.all() == []


def test_persist_fix_as_skill_writes_without_prior_summary(runner, fresh_lib):
    """Missing prior_summary shouldn't crash — just omit that section."""
    runner._persist_fix_as_skill(stack="next", fix_round=2)
    skills = fresh_lib.all()
    assert len(skills) == 1
    assert "## Original failure" not in skills[0].body
    assert "round 2" in skills[0].body


def test_persist_fix_as_skill_accumulates_counts_on_repeated_fixes(runner, fresh_lib):
    """The same fix-round skill being persisted twice should merge cleanly
    (SkillLibrary.upsert preserves created_at + takes max of counts)."""
    runner._persist_fix_as_skill(stack="flask", fix_round=1, prior_summary="first fail")
    runner._persist_fix_as_skill(stack="flask", fix_round=1, prior_summary="second fail")
    skills = fresh_lib.all()
    assert len(skills) == 1  # same slug → merged, not duplicated
    assert skills[0].success_count >= 1
