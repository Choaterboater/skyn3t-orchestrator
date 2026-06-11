"""MetaAgent rule D: build-pattern bias proposal.

When the BuildPatternScoreboard has clear winning + losing shapes for
the same stack, MetaAgent should file a Cortex proposal explaining
the bias so the operator sees what the system learned.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


class _FakeCortexStore:
    """Captures create() calls so tests can inspect proposals filed."""

    def __init__(self) -> None:
        self.proposals: List[Dict[str, Any]] = []

    def create(self, **kwargs) -> Dict[str, Any]:
        self.proposals.append(kwargs)
        return {"id": f"prop-{len(self.proposals)}"}


@pytest.fixture
def fresh_meta(monkeypatch, tmp_path):
    """A MetaAgent + a tmp-pathed BuildPatternScoreboard + a fake
    cortex store, all wired together. Returns (meta, sb, store)."""
    import skyn3t.intelligence.build_patterns as bp
    monkeypatch.setattr(bp, "_default_scoreboard", None)
    sb = bp.BuildPatternScoreboard(store_path=tmp_path / "p.json", flush_every=1)
    monkeypatch.setattr(bp, "_default_scoreboard", sb)

    store = _FakeCortexStore()
    # Patch the cortex import resolution that _file_threshold_proposal uses.
    import skyn3t.cortex as cortex
    monkeypatch.setattr(cortex, "get_store", lambda: store)

    # Build a minimal MetaAgent without a real orchestrator.
    from skyn3t.core.events import EventBus
    from skyn3t.memory.meta_agent import MetaAgent
    meta = MetaAgent(event_bus=EventBus())
    return meta, sb, store


def test_no_proposal_when_no_data(fresh_meta):
    meta, sb, store = fresh_meta
    meta._check_build_pattern_biases()
    assert store.proposals == []


def test_no_proposal_when_only_one_shape(fresh_meta):
    """Need at least two shapes to contrast — single-shape stack stays silent."""
    meta, sb, store = fresh_meta
    for _ in range(10):
        sb.record("next", ["app/page.tsx", "package.json"], "yes")
    meta._check_build_pattern_biases()
    assert store.proposals == []


def test_no_proposal_without_clear_winner_and_loser(fresh_meta):
    """Two shapes both ~50% — not a bias signal."""
    meta, sb, store = fresh_meta
    for _ in range(5):
        sb.record("next", ["app/a.tsx"], "yes")
    for _ in range(5):
        sb.record("next", ["app/a.tsx"], "no")
    for _ in range(5):
        sb.record("next", ["app/b.tsx"], "yes")
    for _ in range(5):
        sb.record("next", ["app/b.tsx"], "no")
    meta._check_build_pattern_biases()
    assert store.proposals == []


def test_proposal_filed_when_clear_winner_vs_loser(fresh_meta):
    meta, sb, store = fresh_meta
    # Winner: 6 wins, 1 loss → 86%.
    for _ in range(6):
        sb.record("fastapi", ["src/main.py", "tests/test_health.py", "requirements.txt"], "yes")
    sb.record("fastapi", ["src/main.py", "tests/test_health.py", "requirements.txt"], "no")
    # Loser: 1 win, 5 losses → 17%.
    sb.record("fastapi", ["src/main.py", "requirements.txt"], "yes")
    for _ in range(5):
        sb.record("fastapi", ["src/main.py", "requirements.txt"], "no")
    meta._check_build_pattern_biases()
    assert len(store.proposals) == 1
    p = store.proposals[0]
    assert p["kind"] == "feature"
    assert "fastapi" in p["title"].lower()
    payload = p["payload"]
    assert payload["kind"] == "build_pattern_bias"
    assert payload["stack"] == "fastapi"
    assert "tests/test_health.py" in payload["distinguishing_files"]
    assert payload["winner_success_rate"] > payload["loser_success_rate"] + 0.30


def test_proposal_dedup_prevents_spam(fresh_meta):
    """Running the check twice in quick succession should only file once."""
    meta, sb, store = fresh_meta
    for _ in range(6):
        sb.record("next", ["a", "b", "c"], "yes")
    for _ in range(5):
        sb.record("next", ["a", "b"], "no")
    meta._check_build_pattern_biases()
    meta._check_build_pattern_biases()
    assert len(store.proposals) == 1


def test_distinguishing_files_lists_extras_in_winner(fresh_meta):
    meta, sb, store = fresh_meta
    # Winner has tsconfig.json, loser doesn't.
    for _ in range(6):
        sb.record("next", ["app/page.tsx", "package.json", "tsconfig.json"], "yes")
    sb.record("next", ["app/page.tsx", "package.json", "tsconfig.json"], "no")
    for _ in range(5):
        sb.record("next", ["app/page.tsx", "package.json"], "no")
    meta._check_build_pattern_biases()
    assert len(store.proposals) == 1
    assert "tsconfig.json" in store.proposals[0]["payload"]["distinguishing_files"]


def _isolate_skill_library(monkeypatch, tmp_path):
    """Point the default skill library + graduation prefs at tmp_path so the
    live data/ directory is never touched."""
    import skyn3t.cortex.build_pattern_bias as bpb
    import skyn3t.intelligence.skill_library as sl
    skill_root = tmp_path / "skills"
    monkeypatch.setattr(sl, "_default_library", None)
    monkeypatch.setattr(sl, "get_default_library",
                        lambda: sl.SkillLibrary(root=skill_root))
    monkeypatch.setattr(bpb, "PREFS_PATH", tmp_path / "build_pattern_preferences.json")
    return sl


def test_below_graduation_files_proposal_not_skill(fresh_meta, monkeypatch, tmp_path):
    """Below the graduation bar (high rate but few samples), the scan files an
    approval-gated proposal and must NOT write to the live skill library."""
    sl = _isolate_skill_library(monkeypatch, tmp_path)
    meta, sb, store = fresh_meta
    # Winner 86% over 7 samples — clears the 75% contrast bar but NOT the
    # graduation bar (needs >=90% over >=20 samples).
    for _ in range(6):
        sb.record("fastapi", ["src/main.py", "tests/test_health.py", "requirements.txt"], "yes")
    sb.record("fastapi", ["src/main.py", "tests/test_health.py", "requirements.txt"], "no")
    sb.record("fastapi", ["src/main.py", "requirements.txt"], "yes")
    for _ in range(5):
        sb.record("fastapi", ["src/main.py", "requirements.txt"], "no")
    meta._check_build_pattern_biases()

    # A contrast proposal is filed for operator approval...
    assert len(store.proposals) == 1
    assert store.proposals[0]["payload"]["kind"] == "build_pattern_bias"
    # ...but nothing is written to the live library yet.
    assert sl.get_default_library().find(tag="fastapi", min_score=-1.0, limit=5) == []


def test_graduation_auto_promotes_skill(fresh_meta, monkeypatch, tmp_path):
    """At/above the graduation bar (>=90% over >=20 samples vs a clear loser),
    the winning shape is auto-promoted into the skill library without waiting
    for approval, and the contrast proposal is suppressed for that stack."""
    sl = _isolate_skill_library(monkeypatch, tmp_path)
    meta, sb, store = fresh_meta
    # Winner: 19 wins, 1 loss → 95% over 20 samples (clears graduation).
    for _ in range(19):
        sb.record("fastapi", ["src/main.py", "tests/test_health.py", "requirements.txt"], "yes")
    sb.record("fastapi", ["src/main.py", "tests/test_health.py", "requirements.txt"], "no")
    # Loser: 1 win, 7 losses → 12.5% over 8 samples.
    sb.record("fastapi", ["src/main.py", "requirements.txt"], "yes")
    for _ in range(7):
        sb.record("fastapi", ["src/main.py", "requirements.txt"], "no")
    meta._check_build_pattern_biases()

    skills = sl.get_default_library().find(tag="fastapi", min_score=-1.0, limit=5)
    names = [s.name for s in skills]
    assert "fastapi-winning-shape" in names
    target = next(s for s in skills if s.name == "fastapi-winning-shape")
    assert "tests/test_health.py" in target.body
    assert "scaffold-shape" in target.tags
    assert "build-success" in target.tags
    # Graduation supersedes the approval-gated contrast proposal for this stack.
    assert len(store.proposals) == 0
