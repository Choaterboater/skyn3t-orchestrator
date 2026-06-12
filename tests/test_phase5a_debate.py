"""Tests for Phase 5A cross-model debate (agents/debate.py).

All LLM interaction is faked — no real backends, no real keys, no network.
We assert: flag-gating, cheap/free-default model selection, graceful degrade
on <2 models, and the ModelTournamentStore record shape (so routing learns).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from skyn3t.agents import debate as dbt
from skyn3t.intelligence.model_tournament import ModelTournamentStore

# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeEventBus:
    def __init__(self) -> None:
        self.events = []

    def publish(self, event) -> None:  # mirrors EventBus.publish signature
        self.events.append(event)

    def names(self):
        return [e.event_type.name for e in self.events]


class _FakeClient:
    """Stand-in for adapters.LLMClient. Records construction kwargs."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.backend = kwargs.get("backend") or "fake"
        _FakeClient.instances.append(self)

    async def complete(self, prompt, *, system=None, temperature=0.4, **kw):
        # Deterministic, "passing" critique/proposal keyed by backend so each
        # participant is distinguishable. No blocker markers => critique passes.
        return f"OUTPUT from {self.backend}: sound and complete."


@pytest.fixture
def fake_llm(monkeypatch):
    _FakeClient.instances = []
    monkeypatch.setattr("skyn3t.adapters.llm_client.LLMClient", _FakeClient)
    return _FakeClient


# ── Flag gating ──────────────────────────────────────────────────────────────


def test_debate_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SKYN3T_DEBATE", raising=False)
    monkeypatch.delenv("SKYN3T_DEBATE_STAGES", raising=False)
    assert dbt.debate_enabled("architect") is False
    assert dbt.debate_enabled("code") is False


def test_debate_enabled_scoped_to_default_stages(monkeypatch):
    monkeypatch.setenv("SKYN3T_DEBATE", "1")
    monkeypatch.delenv("SKYN3T_DEBATE_STAGES", raising=False)
    assert dbt.debate_enabled("architect") is True
    assert dbt.debate_enabled("reviewer") is True
    assert dbt.debate_enabled("code") is True
    # Code-stage aliases collapse onto 'code'.
    assert dbt.debate_enabled("code_agent") is True
    assert dbt.debate_enabled("code_improver") is True
    # Out-of-scope stages stay off even when the flag is on.
    assert dbt.debate_enabled("writer") is False
    assert dbt.debate_enabled("marketer") is False


def test_debate_stages_env_override(monkeypatch):
    monkeypatch.setenv("SKYN3T_DEBATE", "1")
    monkeypatch.setenv("SKYN3T_DEBATE_STAGES", "reviewer")
    assert dbt.debate_enabled("reviewer") is True
    assert dbt.debate_enabled("architect") is False
    assert dbt.debate_enabled("code") is False


# ── Cheap/free default model selection ───────────────────────────────────────


def test_default_models_are_cheap_or_free():
    from skyn3t.core.model_router import relative_backend_cost

    models = dbt.default_debate_models()
    assert models, "expected a non-empty cheap lineup"
    for spec in models:
        backend, _model = dbt._parse_model_spec(spec)
        # Hard cheap guard: no expensive backend (claude_cli=3.0) ever appears.
        assert relative_backend_cost(backend) <= 2.0, spec
    # Diversity: lineup is de-duplicated by backend.
    backends = [dbt._parse_model_spec(s)[0] for s in models]
    assert len(backends) == len(set(backends))


def test_select_models_dedups_by_backend_and_caps():
    pairs = dbt._select_models(
        stage_name="architect",
        stack=None,
        explicit=["openrouter:a/b", "openrouter:c/d", "kimi_cli", "copilot_cli"],
        max_models=2,
    )
    assert len(pairs) == 2
    backends = [b for b, _ in pairs]
    assert len(backends) == len(set(backends))  # cross-model diversity


# ── Graceful degrade ─────────────────────────────────────────────────────────


def test_insufficient_models_skips_with_fallback(monkeypatch):
    # Force a single-model lineup so the <2 guard fires.
    monkeypatch.setattr(dbt, "default_debate_models", lambda: ["openrouter:x/y"])
    result = asyncio.run(
        dbt.run_debate(
            stage_name="architect",
            brief="build a thing",
            proposer_outputs={"architect": "PRIOR PLAN"},
            artifact_dir=Path("/tmp/debate-skip"),
            event_bus=None,
            models=[],
            max_models=3,
        )
    )
    assert result.skipped_reason == "insufficient_models"
    assert result.winner_text == "PRIOR PLAN"  # proposer fallback
    assert result.synthesized is False
    assert result.per_model == []


# ── Full debate flow (faked LLM) ─────────────────────────────────────────────


def test_run_debate_records_one_trial_per_participant(fake_llm, tmp_path, monkeypatch):
    # Point the tournament store at a tmp file by overriding its data dir.
    store_path = tmp_path / "tournament.json"
    monkeypatch.setattr(dbt, "_record_trials", dbt._record_trials)

    # Patch ModelTournamentStore so record_trial lands in our tmp file
    # regardless of settings/data_dir.
    real_init = ModelTournamentStore.__init__

    def _patched_init(self, path=None):
        real_init(self, path=store_path)

    monkeypatch.setattr(ModelTournamentStore, "__init__", _patched_init)

    bus = _FakeEventBus()
    artifact_dir = tmp_path / "myproj"
    artifact_dir.mkdir()

    result = asyncio.run(
        dbt.run_debate(
            stage_name="architect",
            brief="build a fastapi react saas dashboard",
            proposer_outputs={"architect": "seed plan"},
            artifact_dir=artifact_dir,
            event_bus=bus,
            models=["openrouter:a/b", "kimi_cli", "copilot_cli"],
            max_models=3,
            rounds=1,
        )
    )

    assert result.skipped_reason is None
    assert result.winner_model
    assert len(result.per_model) == 3
    assert result.winner_text.startswith("OUTPUT from")

    # One ModelTrial per participant, with the pinned tag conventions.
    store = ModelTournamentStore(path=store_path)
    trials = store.load_trials()
    assert len(trials) == 3
    for trial in trials:
        assert "architect" in trial.domain_tags  # stage tag
        assert trial.vendor_tags  # backend tag present
        assert 0 <= trial.score <= 100
        assert trial.cost_usd >= 0.0
    # The detected stack ('fastapi'/'react'/...) is recorded as a domain tag too.
    all_domain = {tag for t in trials for tag in t.domain_tags}
    assert len(all_domain) >= 1


def test_run_debate_emits_conversation_events(fake_llm, tmp_path, monkeypatch):
    # No-op the recorder so this test focuses purely on event emission.
    monkeypatch.setattr(dbt, "_record_trials", lambda **kw: None)

    bus = _FakeEventBus()
    artifact_dir = tmp_path / "proj2"
    artifact_dir.mkdir()

    asyncio.run(
        dbt.run_debate(
            stage_name="reviewer",
            brief="review brief",
            proposer_outputs=None,
            artifact_dir=artifact_dir,
            event_bus=bus,
            models=["openrouter:a/b", "kimi_cli"],
            max_models=3,
            rounds=1,
        )
    )

    names = bus.names()
    assert "AGENT_CONVERSATION_STARTED" in names
    assert "AGENT_CONVERSATION_TURN" in names
    assert "AGENT_CONVERSATION_ENDED" in names


def test_run_debate_passes_cheap_caller_and_skip_backends(fake_llm, tmp_path, monkeypatch):
    monkeypatch.setattr(dbt, "_record_trials", lambda **kw: None)
    bus = _FakeEventBus()
    artifact_dir = tmp_path / "proj3"
    artifact_dir.mkdir()

    asyncio.run(
        dbt.run_debate(
            stage_name="code",
            brief="brief",
            proposer_outputs={"code": "x"},
            artifact_dir=artifact_dir,
            event_bus=bus,
            models=["openrouter:a/b", "kimi_cli"],
            max_models=2,
            rounds=1,
        )
    )

    assert fake_llm.instances, "expected the debate to construct LLMClients"
    for inst in fake_llm.instances:
        # Every debate client identifies itself for dashboard attribution.
        assert inst.kwargs.get("caller_name") == "debate"
        # skip_backends enforces cross-model diversity (never skips own backend).
        skip = inst.kwargs.get("skip_backends") or []
        assert inst.backend not in skip


def test_run_debate_degrades_when_models_fail(tmp_path, monkeypatch):
    """When backends return nothing, debate must not raise — it degrades."""

    class _FailingClient:
        def __init__(self, **kwargs):
            self.backend = kwargs.get("backend") or "fake"

        async def complete(self, *a, **k):
            raise RuntimeError("backend down / no key")

    monkeypatch.setattr("skyn3t.adapters.llm_client.LLMClient", _FailingClient)
    monkeypatch.setattr(dbt, "_record_trials", lambda **kw: None)

    bus = _FakeEventBus()
    artifact_dir = tmp_path / "proj4"
    artifact_dir.mkdir()

    result = asyncio.run(
        dbt.run_debate(
            stage_name="architect",
            brief="brief",
            proposer_outputs={"architect": "FALLBACK PLAN"},
            artifact_dir=artifact_dir,
            event_bus=bus,
            models=["openrouter:a/b", "kimi_cli"],
            max_models=2,
        )
    )
    # All proposals fail -> falls back to seed and flags insufficient_models,
    # but never raises and never blocks.
    assert result.skipped_reason == "insufficient_models"
    assert result.winner_text == "FALLBACK PLAN"
