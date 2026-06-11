"""Phase 2 — Owner H: build-pattern graduation (auto-promote).

MetaAgent._check_build_pattern_biases auto-promotes a winning scaffold shape
without operator approval ONLY when it clears the strict graduation bar
(>=90% success over >=20 graded builds). Below the bar, the path stays
approval-gated and never writes the live skill library on its own.

Covers Item 8: graduation branch, edge thresholds, per-stack cooldown dedup,
and the audit fix (no every-tick library write below graduation).
"""

from __future__ import annotations

import json
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
def graded_env(monkeypatch, tmp_path):
    """MetaAgent + tmp BuildPatternScoreboard + fake cortex store + tmp skill
    library + tmp prefs path. Returns (meta, sb, store, skill_root, prefs)."""
    import skyn3t.intelligence.build_patterns as bp
    monkeypatch.setattr(bp, "_default_scoreboard", None)
    sb = bp.BuildPatternScoreboard(store_path=tmp_path / "p.json", flush_every=1)
    monkeypatch.setattr(bp, "_default_scoreboard", sb)

    store = _FakeCortexStore()
    import skyn3t.cortex as cortex
    monkeypatch.setattr(cortex, "get_store", lambda: store)

    # Tmp-path the live skill library so we NEVER touch data/skills.
    import skyn3t.intelligence.skill_library as sl
    skill_root = tmp_path / "skills"
    monkeypatch.setattr(sl, "_default_library", None)
    monkeypatch.setattr(sl, "get_default_library", lambda: sl.SkillLibrary(root=skill_root))

    # Tmp-path the stack-preference file so we NEVER touch data/.
    prefs_path = tmp_path / "build_pattern_preferences.json"
    monkeypatch.setattr(
        "skyn3t.cortex.build_pattern_bias.PREFS_PATH", prefs_path
    )

    from skyn3t.core.events import EventBus
    from skyn3t.memory.meta_agent import MetaAgent
    meta = MetaAgent(event_bus=EventBus())
    return meta, sb, store, skill_root, prefs_path


def _seed_winner_loser(sb, stack, winner_shape, loser_shape, *, wins, losses):
    """Winner gets `wins` successes + 1 loss; loser gets `losses` failures."""
    for _ in range(wins):
        sb.record(stack, winner_shape, "yes")
    sb.record(stack, winner_shape, "no")
    for _ in range(losses):
        sb.record(stack, loser_shape, "no")


def test_graduation_auto_promotes_above_bar(graded_env):
    """>=90% over >=20 samples -> auto-promote (skill written, NO proposal)."""
    meta, sb, store, skill_root, prefs_path = graded_env
    # Winner: 20 wins, 0 loss across the winning shape -> 100% over 20.
    for _ in range(20):
        sb.record("react_vite", ["src/main.tsx", "vite.config.ts", "package.json"], "yes")
    # Loser: clear failure shape so the contrast logic finds a loser too.
    for _ in range(5):
        sb.record("react_vite", ["src/main.tsx", "package.json"], "no")

    meta._check_build_pattern_biases()

    # Graduation supersedes the approval-gated proposal — none should be filed.
    assert store.proposals == []

    # Skill was activated directly (auto-promote), and prefs were written.
    import skyn3t.intelligence.skill_library as sl
    lib = sl.get_default_library()
    names = [s.name for s in lib.find(tag="react_vite", min_score=-1.0, limit=5)]
    assert "react_vite-winning-shape" in names
    assert prefs_path.exists()
    prefs = json.loads(prefs_path.read_text())
    assert "react_vite" in prefs

    # The action is recorded as an auto-promotion.
    grads = [a for a in meta._actions if a.get("type") == "build_pattern_graduated"]
    assert len(grads) == 1
    assert grads[0]["result"] == "auto_promoted"
    assert grads[0]["stack"] == "react_vite"
    assert grads[0]["winner_samples"] >= 20


def test_below_sample_bar_stays_approval_gated(graded_env):
    """86% over 7 samples (below 20-sample bar) -> proposal only, NO skill."""
    meta, sb, store, skill_root, prefs_path = graded_env
    _seed_winner_loser(
        sb, "fastapi",
        ["src/main.py", "tests/test_health.py", "requirements.txt"],
        ["src/main.py", "requirements.txt"],
        wins=6, losses=5,
    )
    meta._check_build_pattern_biases()

    # Approval-gated path: a proposal is filed.
    assert len(store.proposals) == 1
    assert store.proposals[0]["payload"]["kind"] == "build_pattern_bias"

    # AUDIT FIX: the every-tick library write is gone — nothing graduated,
    # so the skill library must remain empty (no bypass-approval write).
    assert not skill_root.exists() or list(skill_root.glob("**/*.md")) == []
    assert not prefs_path.exists()
    assert [a for a in meta._actions if a.get("type") == "build_pattern_graduated"] == []


def test_below_rate_bar_stays_approval_gated(graded_env):
    """85% over 20+ samples (below the 90% rate bar) -> proposal only."""
    meta, sb, store, skill_root, prefs_path = graded_env
    # 17 wins / 3 losses = 85% over 20 graded -> meets samples, misses rate.
    for _ in range(17):
        sb.record("next", ["app/page.tsx", "package.json"], "yes")
    for _ in range(3):
        sb.record("next", ["app/page.tsx", "package.json"], "no")
    for _ in range(5):
        sb.record("next", ["app/page.tsx"], "no")

    meta._check_build_pattern_biases()

    assert len(store.proposals) == 1  # approval-gated, not graduated
    assert [a for a in meta._actions if a.get("type") == "build_pattern_graduated"] == []
    assert not prefs_path.exists()


def test_graduation_cooldown_dedups_per_stack(graded_env):
    """Two scans in quick succession auto-promote at most once per stack."""
    meta, sb, store, skill_root, prefs_path = graded_env
    for _ in range(22):
        sb.record("react_vite", ["src/main.tsx", "vite.config.ts"], "yes")
    for _ in range(5):
        sb.record("react_vite", ["src/main.tsx"], "no")

    meta._check_build_pattern_biases()
    meta._check_build_pattern_biases()

    grads = [a for a in meta._actions if a.get("type") == "build_pattern_graduated"]
    assert len(grads) == 1  # cooldown prevents the second auto-promotion
