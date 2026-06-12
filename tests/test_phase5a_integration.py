"""Phase 5A integration wiring tests (studio/runner.py + agents/__init__.py).

These exercise the INTEGRATOR's job: wiring debate / A2A conversation /
reflection->planner / predictive routing / asset generation into the Studio
pipeline. Every Phase 5A hook is flag-gated + graceful, so each test asserts:

  * Flags OFF  -> the hook is a no-op and the runner behaves like today.
  * Flags ON   -> the hook fires through the contracted leaf API, using fakes
                  so there is NO real LLM spend, NO network, and NO full build.

We never run the orchestrator or a full pipeline; we drive the individual
integration helpers directly with tmp dirs + fakes.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from skyn3t.core.events import EventBus
from skyn3t.studio.runner import StudioRunner

# ── Fixtures / fakes ─────────────────────────────────────────────────────────


@pytest.fixture
def runner(tmp_path) -> StudioRunner:
    return StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")


class _Stage:
    """Minimal stage stand-in (mirrors templates.StageSpec attrs we read)."""

    def __init__(self, name: str, agent: str) -> None:
        self.name = name
        self.agent = agent
        self.capability = "x"
        self.input_extra: Dict[str, Any] = {}
        self.handoff_to = None


def _clear_phase5a_flags(monkeypatch) -> None:
    for flag in (
        "SKYN3T_DEBATE",
        "SKYN3T_DEBATE_STAGES",
        "SKYN3T_A2A_CONVERSATION",
        "SKYN3T_AUTO_ROUTE",
        "SKYN3T_ASSET_GEN",
        "SKYN3T_REFLECTIVE_RETRY",
    ):
        monkeypatch.delenv(flag, raising=False)


# ── 1. Debate critique wiring (DebateAPI) ────────────────────────────────────


def test_debate_critique_noop_when_flag_off(runner, monkeypatch, tmp_path):
    """Flag OFF: _maybe_debate_critique returns None so the caller falls back
    to ReviewerAgent — identical-to-today behavior."""
    _clear_phase5a_flags(monkeypatch)
    stage = _Stage("architect", "ArchitectAgent")
    out = asyncio.run(
        runner._maybe_debate_critique(
            stage=stage,
            brief="build a react dashboard",
            artifact_dir=tmp_path,
            rounds=2,
            timeout_s=5.0,
            proposer_summary="proposal",
        )
    )
    assert out is None


def test_debate_critique_fires_when_flag_on(runner, monkeypatch, tmp_path):
    """Flag ON: the runner routes critique through agents.debate.run_debate and
    maps the DebateResult into the {has_issues, issues, critique_text} shape —
    no real LLM calls (run_debate is faked)."""
    _clear_phase5a_flags(monkeypatch)
    monkeypatch.setenv("SKYN3T_DEBATE", "1")

    import skyn3t.agents.debate as dbt

    captured: Dict[str, Any] = {}

    class _Verdict:
        def __init__(self, model, critiques):
            self.model = model
            self.backend = model
            self.critiques = critiques

    class _Result:
        skipped_reason = None
        winner_model = "winner"
        consensus_score = 0.66
        per_model = [
            _Verdict("winner", ["looks sound and complete"]),
            _Verdict("loser", ["BLOCKER: missing error handling"]),
        ]

    async def _fake_run_debate(**kwargs):
        captured.update(kwargs)
        return _Result()

    monkeypatch.setattr(dbt, "run_debate", _fake_run_debate)

    stage = _Stage("architect", "ArchitectAgent")
    out = asyncio.run(
        runner._maybe_debate_critique(
            stage=stage,
            brief="build a react dashboard",
            artifact_dir=tmp_path,
            rounds=2,
            timeout_s=5.0,
            proposer_summary="proposal",
        )
    )
    assert out is not None
    assert out["debate"] is True
    assert out["has_issues"] is True
    # The loser's blocker critique became an issue; the winner's "sound"
    # confirmation did NOT.
    assert len(out["issues"]) == 1
    assert "missing error handling" in out["issues"][0]["issue"]
    # run_debate was called with the contracted kwargs (cheap/free defaults).
    assert captured["stage_name"] == "architect"
    assert captured["models"] is None  # default CHEAP/FREE lineup
    assert captured["record"] is True  # tournament trials recorded (M1 writer)


def test_debate_critique_degrades_on_skip(runner, monkeypatch, tmp_path):
    """A skipped debate (insufficient models) returns None -> ReviewerAgent
    fallback. Never blocks."""
    _clear_phase5a_flags(monkeypatch)
    monkeypatch.setenv("SKYN3T_DEBATE", "1")

    import skyn3t.agents.debate as dbt

    class _Skipped:
        skipped_reason = "insufficient_models"
        winner_model = ""
        consensus_score = 0.0
        per_model: List[Any] = []

    async def _fake_run_debate(**kwargs):
        return _Skipped()

    monkeypatch.setattr(dbt, "run_debate", _fake_run_debate)

    out = asyncio.run(
        runner._maybe_debate_critique(
            stage=_Stage("code", "CodeAgent"),
            brief="b",
            artifact_dir=tmp_path,
            rounds=1,
            timeout_s=5.0,
        )
    )
    assert out is None


# ── 2. Predictive routing wiring (PredictiveRoutingSelector) ─────────────────


def test_resolve_stage_route_static_when_auto_route_off(monkeypatch):
    """Flag OFF: _resolve_stage_route returns the static resolve_model route —
    identical to today's selection."""
    _clear_phase5a_flags(monkeypatch)
    from skyn3t.core import model_router as mr

    static = mr.resolve_model("reviewer", brief="build a react app")
    backend, model = StudioRunner._resolve_stage_route(
        "reviewer", brief="build a react app"
    )
    assert (backend, model) == static


def test_resolve_stage_route_prefers_predictive_when_auto_route_on(monkeypatch):
    """Flag ON: _resolve_stage_route consults select_best_model/best_model_for
    and threads stack+features through (faked evidence, no spend)."""
    _clear_phase5a_flags(monkeypatch)
    monkeypatch.setenv("SKYN3T_AUTO_ROUTE", "1")

    seen: Dict[str, Any] = {}

    import skyn3t.intelligence.routing_recommendations as rr

    def _fake_best_model_for(*, stage, stack, features=None):
        seen["stage"] = stage
        seen["stack"] = stack
        seen["features"] = features
        return {
            "backend": "openrouter",
            "model": "openrouter/owl-alpha",
            "tier": "or_cheap",
            "source": "predictive",
            "score": 87.0,
            "rationale": "fake winner",
        }

    monkeypatch.setattr(rr, "best_model_for", _fake_best_model_for)

    backend, model = StudioRunner._resolve_stage_route(
        "reviewer", brief="build a react dashboard with auth and payments"
    )
    assert backend == "openrouter"
    assert model == "openrouter/owl-alpha"
    # stack + features were detected from the brief and passed to the ranker.
    assert seen["stack"] == "react_vite"
    assert "auth" in (seen["features"] or [])


# ── 3. Asset agent wiring (AssetAgentAPI) ────────────────────────────────────


def test_asset_agent_noop_when_flag_off(runner, monkeypatch, tmp_path):
    """Flag OFF: no AssetAgent run, manifest untouched."""
    _clear_phase5a_flags(monkeypatch)
    manifest: Dict[str, Any] = {}
    asyncio.run(
        runner._maybe_run_asset_agent(
            brief="build a react dashboard",
            artifact_dir=tmp_path,
            manifest=manifest,
            prior_summaries={},
        )
    )
    assert "assets" not in manifest


def test_asset_agent_runs_and_skips_gracefully_when_flag_on(runner, monkeypatch, tmp_path):
    """Flag ON + no provider key: AssetAgent runs but emits skipped entries —
    never blocks, records into the manifest."""
    _clear_phase5a_flags(monkeypatch)
    monkeypatch.setenv("SKYN3T_ASSET_GEN", "1")
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)

    manifest: Dict[str, Any] = {}
    prior: Dict[str, str] = {}
    asyncio.run(
        runner._maybe_run_asset_agent(
            brief="build a react dashboard with a polished UI",
            artifact_dir=tmp_path,
            manifest=manifest,
            prior_summaries=prior,
        )
    )
    assert "assets" in manifest
    # No key -> all entries skipped, but the run completed.
    assert manifest["assets"]["total"] == 2
    assert manifest["assets"]["generated"] == 0
    assert all(a.get("skipped") for a in manifest["assets"]["entries"])


def test_asset_agent_skips_non_visual_brief(runner, monkeypatch, tmp_path):
    """Flag ON but a non-visual brief: AssetAgent is not run."""
    _clear_phase5a_flags(monkeypatch)
    monkeypatch.setenv("SKYN3T_ASSET_GEN", "1")
    manifest: Dict[str, Any] = {}
    asyncio.run(
        runner._maybe_run_asset_agent(
            brief="a command line tool that parses csv files",
            artifact_dir=tmp_path,
            manifest=manifest,
            prior_summaries={},
        )
    )
    assert "assets" not in manifest


# ── 4. A2A conversation wiring (ConversationLoopAPI) ─────────────────────────


def test_a2a_conversation_noop_when_flag_off(runner, monkeypatch, tmp_path):
    """Flag OFF: no orchestrator built, no converse, manifest untouched —
    pipeline stays purely linear."""
    _clear_phase5a_flags(monkeypatch)
    stages = [_Stage("designer", "DesignerAgent"), _Stage("reviewer", "ReviewerAgent")]
    manifest: Dict[str, Any] = {"slug": "s"}
    asyncio.run(
        runner._maybe_a2a_conversation(
            stages=stages,
            brief="build a react dashboard",
            artifact_dir=tmp_path,
            manifest=manifest,
            prior_summaries={},
        )
    )
    assert "a2a_conversation" not in manifest


def test_a2a_conversation_fires_when_flag_on(runner, monkeypatch, tmp_path):
    """Flag ON + triad present: converse() is driven via a real Orchestrator,
    participants get inbox pumps started, result recorded — no real LLM spend
    (converse is faked)."""
    _clear_phase5a_flags(monkeypatch)
    monkeypatch.setenv("SKYN3T_A2A_CONVERSATION", "1")

    import skyn3t.core.orchestrator as orch

    started_pumps: List[str] = []

    class _FakeAgent:
        def __init__(self, name):
            self.name = name
            self.id = name
            self.agent_type = "fake"
            self.provider = "local"
            self.role = None
            self.reports_to = None
            self.lifecycle = "active"
            self.capabilities = []

        def start_inbox_pump(self):
            started_pumps.append(self.name)

        async def stop_inbox_pump(self):
            return None

    name_map = {
        "DesignerAgent": "designer",
        "ReviewerAgent": "reviewer",
        "CodeAgent": "code_agent",
    }

    def _fake_get_agent(cls_name, **kw):
        return _FakeAgent(name_map[cls_name])

    # Patch get_agent as referenced inside runner.
    monkeypatch.setattr("skyn3t.studio.runner.get_agent", _fake_get_agent)

    captured: Dict[str, Any] = {}

    class _ConvResult:
        converged = True
        rounds_used = 2
        turns = [1, 2, 3]
        final_payload = {"summary": "agreed on plan"}

    async def _fake_converse(self, **kwargs):
        captured.update(kwargs)
        return _ConvResult()

    # Keep register_agent's pump-start path but neuter the async background
    # bookkeeping by patching converse on the class.
    monkeypatch.setattr(orch.Orchestrator, "converse", _fake_converse)

    stages = [
        _Stage("designer", "DesignerAgent"),
        _Stage("reviewer", "ReviewerAgent"),
        _Stage("code", "CodeAgent"),
    ]
    manifest: Dict[str, Any] = {"slug": "s"}
    prior: Dict[str, str] = {}

    asyncio.run(
        runner._maybe_a2a_conversation(
            stages=stages,
            brief="build a react dashboard",
            artifact_dir=tmp_path,
            manifest=manifest,
            prior_summaries=prior,
        )
    )

    assert manifest.get("a2a_conversation", {}).get("converged") is True
    assert manifest["a2a_conversation"]["rounds_used"] == 2
    # The triad's three agents were registered + pumped.
    assert set(started_pumps) == {"designer", "reviewer", "code_agent"}
    assert set(captured["participants"]) == {"designer", "reviewer", "code_agent"}
    # Converged summary was seeded for downstream stages.
    assert prior.get("a2a_conversation") == "agreed on plan"


def test_a2a_conversation_skips_without_triad(runner, monkeypatch, tmp_path):
    """Flag ON but <2 triad members present: no conversation."""
    _clear_phase5a_flags(monkeypatch)
    monkeypatch.setenv("SKYN3T_A2A_CONVERSATION", "1")
    stages = [_Stage("research", "ResearchAgent")]
    manifest: Dict[str, Any] = {"slug": "s"}
    asyncio.run(
        runner._maybe_a2a_conversation(
            stages=stages,
            brief="x",
            artifact_dir=tmp_path,
            manifest=manifest,
            prior_summaries={},
        )
    )
    assert "a2a_conversation" not in manifest


# ── 5. Reflection -> planner wiring (ReflectionPlannerHook) ──────────────────


def test_reflective_retry_stashes_directive_and_escalates(runner, monkeypatch, tmp_path):
    """Flag ON (default): _maybe_auto_retry builds a RetryDirective, stashes it
    keyed by retry-slug for plan_pipeline, and applies forced strong tiers via
    _maybe_escalate_cheap_smart. The retry itself is neutered (start patched)
    so no full build runs."""
    _clear_phase5a_flags(monkeypatch)  # SKYN3T_REFLECTIVE_RETRY default ON

    # Force a known directive so we can assert the wiring, not the heuristics.
    import skyn3t.intelligence.reflection as refl

    class _Directive:
        augmented_brief = "ORIGINAL\n\nPRIOR FAILURE CONTEXT"
        prompt_patches: List[str] = []
        forced_stage_tier = {"code": "strong"}
        avoid_backends = ["kimi_cli"]
        rationale = "stub failure -> escalate code"
        signatures: List[str] = []

    monkeypatch.setattr(refl, "reflective_retry_enabled", lambda: True)
    monkeypatch.setattr(refl, "build_retry_directive", lambda **kw: _Directive())

    escalations: List[Dict[str, str]] = []
    monkeypatch.setattr(
        runner,
        "_maybe_escalate_cheap_smart",
        lambda **kw: escalations.append(kw),
    )

    started: Dict[str, Any] = {}

    async def _fake_start(template_key, brief, **kw):
        started["template"] = template_key
        started["brief"] = brief
        started["slug"] = kw.get("slug")
        return {}

    monkeypatch.setattr(runner, "start", _fake_start)

    manifest: Dict[str, Any] = {
        "template": "auto",
        "error": "code generation failed: entrypoint stub",
        "stages": [],
        "build_verification": {"backend": "kimi_cli"},
    }
    asyncio.run(runner._maybe_auto_retry(manifest, "build a dashboard", "proj"))

    # Wait for the fire-and-forget retry task to run.
    async def _drain():
        await asyncio.sleep(0)
        for _ in range(5):
            if started:
                break
            await asyncio.sleep(0)

    asyncio.run(_drain())

    retry_slug = "proj-retry"
    # The directive was stashed for the retry's plan_pipeline call.
    assert runner._pending_reflection.get(retry_slug) is not None
    assert runner._pending_reflection[retry_slug].augmented_brief.startswith("ORIGINAL")
    # The forced strong tier escalated code via cheap_smart.
    assert any(e.get("stage_name") == "code" for e in escalations)


def test_reflective_retry_falls_back_when_flag_off(runner, monkeypatch):
    """Flag OFF: legacy lesson-block retry path (no directive stash). Brief
    still carries the failure forward."""
    _clear_phase5a_flags(monkeypatch)
    monkeypatch.setenv("SKYN3T_REFLECTIVE_RETRY", "off")

    started: Dict[str, Any] = {}

    async def _fake_start(template_key, brief, **kw):
        started["brief"] = brief
        started["slug"] = kw.get("slug")
        return {}

    monkeypatch.setattr(runner, "start", _fake_start)

    manifest: Dict[str, Any] = {
        "template": "auto",
        "error": "boom at code stage",
        "stages": [],
    }
    asyncio.run(runner._maybe_auto_retry(manifest, "build a dashboard", "proj2"))

    async def _drain():
        for _ in range(5):
            if started:
                break
            await asyncio.sleep(0)

    asyncio.run(_drain())

    # No directive stashed on the legacy path.
    assert "proj2-retry" not in runner._pending_reflection
    # Legacy lesson block still threaded the failure into the retry brief.
    assert "build a dashboard" in started["brief"]
    assert "boom at code stage" in started["brief"]


def test_pending_reflection_initialized_empty(runner):
    """Non-retry path: the reflection stash starts empty (graceful no-op)."""
    assert runner._pending_reflection == {}
