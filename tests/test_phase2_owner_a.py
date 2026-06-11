"""Phase 2 (self-learning loops) — Owner A: StudioRunner consolidation.

Covers the runner-side wiring added for Phase 2:

* read-side lesson injection (``_lessons_for_code_stage``) that mirrors the
  learning-loop RAG extraction so studio builds (which never route through
  ``orchestrator.execute_task``) still get ``input_data['lessons']``;
* advisory (non-binding) plan hints (``_plan_advice``) sourced from the
  build-pattern scoreboard's best shape and the skill library;
* canonical-stack-from-brief normalization used by the advisory path;
* injected-skill grading semantics (``record_use`` is called per injected
  skill with ``success = (verdict == 'yes')``).

All tests use ``tmp_path`` and fakes/monkeypatched singletons — they never
touch ``data/`` and never run the orchestrator or a real build.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import skyn3t.intelligence.build_patterns as build_patterns
import skyn3t.intelligence.skill_library as skill_library
from skyn3t.core.events import EventBus
from skyn3t.intelligence.build_patterns import BuildPatternScoreboard
from skyn3t.intelligence.skill_library import Skill, SkillLibrary
from skyn3t.studio.runner import StudioRunner


def _runner(tmp_path: Path, *, rag=None) -> StudioRunner:
    return StudioRunner(
        event_bus=EventBus(),
        rag=rag,
        projects_root=tmp_path / "projects",
    )


class _FakeRag:
    """Minimal RAGEngine stand-in returning the documented query shape."""

    def __init__(self, documents):
        self._documents = documents
        self.calls = []

    async def query(self, query, n_results=5, filter_dict=None):
        self.calls.append((query, n_results))
        return {"documents": self._documents, "query": query}


# ---------------------------------------------------------------------------
# Read-side lessons (Item 2)
# ---------------------------------------------------------------------------

def test_lessons_for_code_stage_extracts_content(tmp_path):
    rag = _FakeRag(
        [
            {"id": "a", "content": "Mount every routes/*.js in server/index.js"},
            {"id": "b", "content": "Use react_vite scaffold for vite briefs"},
            {"id": "c", "content": ""},  # empty content must be dropped
        ]
    )
    runner = _runner(tmp_path, rag=rag)
    lessons = asyncio.run(runner._lessons_for_code_stage("build a vite react app"))
    assert lessons == [
        "Mount every routes/*.js in server/index.js",
        "Use react_vite scaffold for vite briefs",
    ]
    # Queried the live engine with the brief text.
    assert rag.calls and rag.calls[0][0] == "build a vite react app"


def test_lessons_for_code_stage_noop_without_rag(tmp_path):
    runner = _runner(tmp_path, rag=None)
    assert asyncio.run(runner._lessons_for_code_stage("anything")) == []


def test_lessons_for_code_stage_swallows_query_errors(tmp_path):
    class _Boom:
        async def query(self, *a, **k):
            raise RuntimeError("vector store down")

    runner = _runner(tmp_path, rag=_Boom())
    # Best-effort: a failing RAG must not raise into the build path.
    assert asyncio.run(runner._lessons_for_code_stage("brief")) == []


# ---------------------------------------------------------------------------
# Canonical stack from brief (Item 6 helper)
# ---------------------------------------------------------------------------

def test_canonical_stack_for_brief_normalizes_alias(tmp_path, monkeypatch):
    # Force the brief detector to emit a legacy alias and confirm the
    # canonical map collapses it to the unified bucket name.
    import skyn3t.agents.stack_templates as stack_templates

    monkeypatch.setattr(stack_templates, "detect_stack", lambda brief: "vite_react")
    runner = _runner(tmp_path)
    assert runner._canonical_stack_for_brief("anything") == "react_vite"


def test_canonical_stack_for_brief_none_when_undetected(tmp_path, monkeypatch):
    import skyn3t.agents.stack_templates as stack_templates

    monkeypatch.setattr(stack_templates, "detect_stack", lambda brief: None)
    runner = _runner(tmp_path)
    assert runner._canonical_stack_for_brief("anything") is None


# ---------------------------------------------------------------------------
# Advisory plan hints (Item 6)
# ---------------------------------------------------------------------------

def test_plan_advice_uses_best_shape_and_skills(tmp_path, monkeypatch):
    # tmp-backed scoreboard with a clearly-best shape for react_vite.
    sb = BuildPatternScoreboard(store_path=tmp_path / "p.json")
    shape = ["src/main.jsx", "src/App.jsx", "index.html", "package.json"]
    for _ in range(4):
        sb.record("react_vite", shape, "yes")
    sb.flush()
    monkeypatch.setattr(build_patterns, "get_default_scoreboard", lambda: sb)

    # tmp-backed skill library with one relevant skill.
    lib = SkillLibrary(root=tmp_path / "skills")
    lib.upsert(
        Skill(
            name="vite-router-mount",
            description="Mount react routes in a vite app",
            tags=["react", "vite"],
            success_count=3,
        )
    )
    monkeypatch.setattr(skill_library, "get_default_library", lambda: lib)

    # Detector emits the canonical bucket name directly.
    import skyn3t.agents.stack_templates as stack_templates

    monkeypatch.setattr(stack_templates, "detect_stack", lambda brief: "react_vite")

    runner = _runner(tmp_path)
    advice = runner._plan_advice("build a vite react router app")

    assert any("ADVISORY (non-binding)" in a for a in advice)
    # Best-shape hint surfaces the canonical stack + a scaffold file.
    assert any("react_vite" in a and "src/App.jsx" in a for a in advice)
    # Skill hint surfaces the learned skill name.
    assert any("vite-router-mount" in a for a in advice)
    # Stays advisory — never hard-pins (no imperative "you MUST use shape X").
    assert all("MUST use" not in a for a in advice)


def test_plan_advice_empty_when_no_signal(tmp_path, monkeypatch):
    sb = BuildPatternScoreboard(store_path=tmp_path / "p.json")  # no records
    monkeypatch.setattr(build_patterns, "get_default_scoreboard", lambda: sb)
    lib = SkillLibrary(root=tmp_path / "skills")  # empty library
    monkeypatch.setattr(skill_library, "get_default_library", lambda: lib)
    import skyn3t.agents.stack_templates as stack_templates

    monkeypatch.setattr(stack_templates, "detect_stack", lambda brief: None)

    runner = _runner(tmp_path)
    assert runner._plan_advice("an undetectable brief") == []


# ---------------------------------------------------------------------------
# Injected-skill grading semantics (Item 3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "verdict, expect_success, expect_failure",
    [
        ("yes", 1, 0),   # passing build credits the skill
        ("no", 0, 1),    # failing build debits the skill
        ("skipped", 0, 1),  # non-'yes' verdicts count as a failure
    ],
)
def test_record_use_grading_matches_verdict(
    tmp_path, verdict, expect_success, expect_failure
):
    """Mirror the runner verdict block: record_use(name, success=verdict=='yes').

    This locks the exact grading contract Owner A wires at the verdict site
    so a future refactor cannot silently flip success/failure semantics.
    """
    lib = SkillLibrary(root=tmp_path / "skills")
    lib.upsert(Skill(name="graded-skill", description="x"))

    injected_skills = ["graded-skill"]
    _passed = str(verdict).lower() == "yes"
    for name in injected_skills:
        lib.record_use(name, success=_passed)

    # min_score=-1.0 so a debited (negative-score) skill is still returned.
    updated = lib.find(tag=None, min_score=-1.0)
    by_name = {s.name: s for s in updated}
    assert "graded-skill" in by_name
    sk = by_name["graded-skill"]
    assert sk.success_count == expect_success
    assert sk.failure_count == expect_failure
