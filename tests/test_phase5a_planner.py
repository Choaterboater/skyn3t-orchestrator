"""Phase 5A — M3_planner: ReflectionPlannerHook wiring in studio.planner.

These tests exercise the new keyword-only ``reflection`` parameter on
``plan_pipeline``. They run with ``llm_client=None`` to force the deterministic
heuristic planner (no LLM, no network) and assert:

  * ``reflection=None`` preserves the exact prior behavior (pure superset).
  * A directive whose signatures/rationale indicate a stub/entrypoint failure
    forces a real ``CodeAgent`` build and tightens the reviewer rationale.
  * ``augmented_brief`` is merged so failure-aware cues bias agent selection.
  * ``prompt_patches`` are threaded into the build/review stage rationales.
  * ``forced_stage_tier`` hints surface in the targeted stage's rationale.

The planner contract says reflection is consumed structurally; we use the real
``RetryDirective`` when importable but also verify a duck-typed stand-in works,
so a concurrent edit to reflection.py can never silently break this seam.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import pytest

from skyn3t.studio.planner import PlannedStage, plan_pipeline

try:  # real directive if M3_reflection has landed it
    from skyn3t.intelligence.reflection import RetryDirective as _RealRetryDirective
except Exception:  # pragma: no cover - reflection.py mid-edit
    _RealRetryDirective = None


@dataclass
class _SyntheticRetryDirective:
    """Duck-typed stand-in matching the ReflectionPlannerHook contract shape."""

    augmented_brief: str = ""
    prompt_patches: List[str] = field(default_factory=list)
    forced_stage_tier: Dict[str, str] = field(default_factory=dict)
    avoid_backends: List[str] = field(default_factory=list)
    rationale: str = ""
    signatures: List[str] = field(default_factory=list)


def _make_directive(**kwargs):
    """Build a RetryDirective-like object; prefers the real dataclass."""
    if _RealRetryDirective is not None:
        defaults = dict(
            augmented_brief="",
            prompt_patches=[],
            forced_stage_tier={},
            avoid_backends=[],
            rationale="",
            signatures=[],
        )
        defaults.update(kwargs)
        return _RealRetryDirective(**defaults)
    return _SyntheticRetryDirective(**kwargs)


def _agents(stages: List[PlannedStage]) -> List[str]:
    return [s.agent for s in stages]


def _by_agent(stages: List[PlannedStage], agent: str) -> PlannedStage:
    for s in stages:
        if s.agent == agent:
            return s
    raise AssertionError(f"{agent} not in plan: {[s.agent for s in stages]}")


# --- reflection=None preserves current behavior --------------------------


@pytest.mark.asyncio
async def test_reflection_none_matches_baseline_for_software_build():
    """A directive of None must be a byte-for-byte no-op vs. omitting the kwarg."""
    brief = "Build a small todo app"
    baseline = await plan_pipeline(brief=brief, llm_client=None)
    with_none = await plan_pipeline(brief=brief, llm_client=None, reflection=None)

    assert _agents(baseline) == _agents(with_none)
    assert [s.rationale for s in baseline] == [s.rationale for s in with_none]
    assert [s.expected_artifact for s in baseline] == [
        s.expected_artifact for s in with_none
    ]


@pytest.mark.asyncio
async def test_reflection_none_matches_baseline_for_docs_brief():
    brief = "Write a product spec for a todo app"
    baseline = await plan_pipeline(brief=brief, llm_client=None)
    with_none = await plan_pipeline(brief=brief, llm_client=None, reflection=None)

    assert _agents(baseline) == _agents(with_none)
    # No code stage forced for a pure docs brief on either path.
    assert "CodeAgent" not in _agents(with_none)
    assert "CodeImproverAgent" not in _agents(with_none)


# --- stub/entrypoint signatures force a real build -----------------------


@pytest.mark.asyncio
async def test_stub_signature_forces_code_stage_on_docs_plan():
    """Prior failure = empty stub scaffold; retry must add a real CodeAgent
    even though the brief alone would not force code."""
    brief = "Write a product spec for a todo app"
    baseline = await plan_pipeline(brief=brief, llm_client=None)
    assert "CodeAgent" not in _agents(baseline)

    directive = _make_directive(
        signatures=["stub:empty_module", "missing entrypoint main()"],
        rationale="prior attempt produced an empty stub scaffold; no runnable entrypoint",
    )
    stages = await plan_pipeline(brief=brief, llm_client=None, reflection=directive)

    assert "CodeAgent" in _agents(stages)
    code = _by_agent(stages, "CodeAgent")
    assert "reflective retry" in code.rationale.lower()
    assert code.expected_artifact  # has a non-empty artifact wired


@pytest.mark.asyncio
async def test_stub_signature_strengthens_reviewer_rationale():
    brief = "Build a small todo app"
    directive = _make_directive(
        signatures=["ImportError: cannot import name 'app'"],
        rationale="entrypoint won't start",
    )
    stages = await plan_pipeline(brief=brief, llm_client=None, reflection=directive)

    reviewer = _by_agent(stages, "ReviewerAgent")
    assert "reflective retry" in reviewer.rationale.lower()
    assert "entrypoint" in reviewer.rationale.lower()


@pytest.mark.asyncio
async def test_non_stub_signature_does_not_force_code_for_docs_brief():
    """A non-stub failure (e.g. rate limit) must NOT force a code stage onto a
    docs-only brief — reflection biases by WHY, not blindly."""
    brief = "Write a product spec for a todo app"
    directive = _make_directive(
        signatures=["rate_limit:429"],
        rationale="provider throttled the request; retry with backoff",
    )
    stages = await plan_pipeline(brief=brief, llm_client=None, reflection=directive)

    assert "CodeAgent" not in _agents(stages)
    assert "CodeImproverAgent" not in _agents(stages)


# --- augmented_brief biases selection ------------------------------------


@pytest.mark.asyncio
async def test_augmented_brief_biases_agent_selection():
    """A vague brief plus an augmented brief that asks for an app should
    surface a code stage that the bare brief wouldn't have produced."""
    bare = "Help me out."
    baseline = await plan_pipeline(brief=bare, llm_client=None)
    assert "CodeAgent" not in _agents(baseline)

    directive = _make_directive(
        augmented_brief="Help me out. Specifically: build a small CLI tool app.",
    )
    stages = await plan_pipeline(brief=bare, llm_client=None, reflection=directive)
    assert "CodeAgent" in _agents(stages)


# --- prompt_patches threaded into stages ---------------------------------


@pytest.mark.asyncio
async def test_prompt_patches_threaded_into_build_and_review_stages():
    brief = "Build a small todo app"
    patch = "Add explicit output format instructions (valid JSON only)."
    directive = _make_directive(prompt_patches=[patch])
    stages = await plan_pipeline(brief=brief, llm_client=None, reflection=directive)

    code = _by_agent(stages, "CodeAgent")
    reviewer = _by_agent(stages, "ReviewerAgent")
    assert patch in code.rationale
    assert patch in reviewer.rationale


# --- forced_stage_tier surfaces in rationale -----------------------------


@pytest.mark.asyncio
async def test_forced_stage_tier_surfaces_in_targeted_stage():
    brief = "Build a small todo app"
    directive = _make_directive(forced_stage_tier={"code": "premium"})
    stages = await plan_pipeline(brief=brief, llm_client=None, reflection=directive)

    code = _by_agent(stages, "CodeAgent")
    assert "premium" in code.rationale
    assert "code" in code.rationale.lower()


# --- duck-typed stand-in works (seam robustness) -------------------------


@pytest.mark.asyncio
async def test_duck_typed_directive_is_accepted():
    """plan_pipeline consumes the directive structurally; a stand-in object
    with the contract fields must work identically (no isinstance coupling)."""
    brief = "Write a product spec for a todo app"
    directive = _SyntheticRetryDirective(
        signatures=["stub:placeholder"],
        rationale="empty module produced",
    )
    stages = await plan_pipeline(brief=brief, llm_client=None, reflection=directive)
    assert "CodeAgent" in _agents(stages)


# --- handoffs stay wired after reflection mutation -----------------------


@pytest.mark.asyncio
async def test_handoffs_remain_consistent_with_reflection():
    brief = "Build a small todo app"
    directive = _make_directive(
        signatures=["stub:empty_module"],
        prompt_patches=["use a real entrypoint"],
        forced_stage_tier={"reviewer": "premium"},
    )
    stages = await plan_pipeline(brief=brief, llm_client=None, reflection=directive)

    # Brainstorm first, Reviewer last, handoffs chained.
    assert stages[0].agent == "BrainstormAgent"
    assert stages[-1].agent == "ReviewerAgent"
    assert stages[-1].handoff_to is None
    for i in range(len(stages) - 1):
        assert stages[i].handoff_to == stages[i + 1].agent
