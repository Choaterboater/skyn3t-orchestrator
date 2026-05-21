"""Tests for adaptive routing in ``skyn3t.core.model_router``.

The static decisions are exercised by the existing test suite (and by
``test_build_patterns.py`` indirectly). These tests cover the new
scoreboard-aware demotion path that lets the router learn which
backend keeps losing on a given stack.

Each test seeds a real ``BuildPatternScoreboard`` (no mocks) so the
end-to-end contract — record per-backend → query backend_rate →
demote — is verified rather than the wiring shape.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from skyn3t.core.model_router import (
    _BACKEND_ALTERNATIVES,
    _resolve_static,
    resolve_model_for_file,
)
from skyn3t.intelligence.build_patterns import BuildPatternScoreboard

# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def scoreboard(tmp_path: Path) -> BuildPatternScoreboard:
    return BuildPatternScoreboard(store_path=tmp_path / "patterns.json")


@pytest.fixture(autouse=True)
def _stable_router_env(monkeypatch):
    """Lock the env knobs to defaults; clear adaptive disable."""
    for key in (
        "SKYN3T_ROUTER_ADAPTIVE",
        "SKYN3T_ROUTER_DEMOTE_BELOW",
        "SKYN3T_ROUTER_DEMOTE_AFTER",
        "SKYN3T_ROUTER_EXPLORATION_EPS",
    ):
        monkeypatch.delenv(key, raising=False)
    # Disable epsilon-greedy exploration for determinism in tests that
    # don't explicitly toggle it.
    monkeypatch.setenv("SKYN3T_ROUTER_EXPLORATION_EPS", "0")


def _seed_backend_outcomes(
    sb: BuildPatternScoreboard,
    stack: str,
    backend: str,
    *,
    wins: int = 0,
    losses: int = 0,
) -> None:
    """Helper: shape doesn't matter for stack-aggregate ``backend_rate``,
    but ``record_backend`` needs a non-empty shape. Use a stable one."""
    shape = ["src/App.jsx", "package.json"]
    for _ in range(wins):
        sb.record_backend(stack, shape, backend, "yes")
    for _ in range(losses):
        sb.record_backend(stack, shape, backend, "no")


# ---------------------------------------------------------------------
# Default static behavior preserved when no scoreboard / no stack
# ---------------------------------------------------------------------


def test_returns_static_when_no_scoreboard_provided():
    assert resolve_model_for_file("src/components/X.jsx") == _resolve_static(
        "src/components/X.jsx"
    )


def test_returns_static_when_no_stack_provided(scoreboard):
    # Even if the scoreboard is loaded with data, no `stack` means the
    # adaptive branch can't run — we don't know what to query.
    _seed_backend_outcomes(scoreboard, "react_vite", "openrouter", losses=20)
    out = resolve_model_for_file(
        "src/components/X.jsx", scoreboard=scoreboard,
    )
    assert out == _resolve_static("src/components/X.jsx")


def test_returns_static_when_adaptive_disabled(monkeypatch, scoreboard):
    monkeypatch.setenv("SKYN3T_ROUTER_ADAPTIVE", "0")
    _seed_backend_outcomes(scoreboard, "react_vite", "openrouter", losses=20)
    assert resolve_model_for_file(
        "src/components/X.jsx", stack="react_vite", scoreboard=scoreboard,
    ) == _resolve_static("src/components/X.jsx")


# ---------------------------------------------------------------------
# Demotion path
# ---------------------------------------------------------------------


def test_demotes_when_static_choice_keeps_losing(scoreboard):
    # Static pick for src/components/*.jsx is openrouter (or_ui tier).
    # Seed enough losses to drop its rate well below the 0.4 default.
    _seed_backend_outcomes(scoreboard, "react_vite", "openrouter", wins=1, losses=9)
    out = resolve_model_for_file(
        "src/components/X.jsx", stack="react_vite", scoreboard=scoreboard,
    )
    assert out == _BACKEND_ALTERNATIVES["openrouter"]


def test_no_demotion_below_min_samples(scoreboard):
    # 3 losses, 0 wins → rate would be 0.0, but min_samples (default 5)
    # blocks the decision. Stick with static.
    _seed_backend_outcomes(scoreboard, "react_vite", "openrouter", wins=0, losses=3)
    out = resolve_model_for_file(
        "src/components/X.jsx", stack="react_vite", scoreboard=scoreboard,
    )
    assert out == _resolve_static("src/components/X.jsx")


def test_no_demotion_above_threshold(scoreboard):
    # 7/10 = 0.7 > 0.4: leave the static pick alone.
    _seed_backend_outcomes(scoreboard, "react_vite", "openrouter", wins=7, losses=3)
    out = resolve_model_for_file(
        "src/components/X.jsx", stack="react_vite", scoreboard=scoreboard,
    )
    assert out == _resolve_static("src/components/X.jsx")


def test_no_demotion_when_alternative_also_losing(scoreboard):
    """If we'd demote openrouter→copilot_cli but copilot_cli is ALSO bad,
    keep the original — flipping to a worse option is regret, not progress."""
    _seed_backend_outcomes(scoreboard, "react_vite", "openrouter", wins=1, losses=9)
    _seed_backend_outcomes(scoreboard, "react_vite", "copilot_cli", wins=1, losses=9)
    out = resolve_model_for_file(
        "src/components/X.jsx", stack="react_vite", scoreboard=scoreboard,
    )
    assert out == _resolve_static("src/components/X.jsx")


def test_threshold_env_var_tightens_demotion(monkeypatch, scoreboard):
    # With default 0.4, a 5/10 = 0.5 win rate stays static. Tighten to
    # 0.6 and the same data triggers a demote.
    _seed_backend_outcomes(scoreboard, "react_vite", "openrouter", wins=5, losses=5)
    assert resolve_model_for_file(
        "src/components/X.jsx", stack="react_vite", scoreboard=scoreboard,
    ) == _resolve_static("src/components/X.jsx")

    monkeypatch.setenv("SKYN3T_ROUTER_DEMOTE_BELOW", "0.6")
    assert resolve_model_for_file(
        "src/components/X.jsx", stack="react_vite", scoreboard=scoreboard,
    ) == _BACKEND_ALTERNATIVES["openrouter"]


def test_min_samples_env_var_lowers_floor(monkeypatch, scoreboard):
    # Defaults require 5 graded attempts; with 2 losses we'd normally
    # leave the static pick alone. Drop the floor to 1 and the demotion fires.
    _seed_backend_outcomes(scoreboard, "react_vite", "openrouter", wins=0, losses=2)
    monkeypatch.setenv("SKYN3T_ROUTER_DEMOTE_AFTER", "1")
    out = resolve_model_for_file(
        "src/components/X.jsx", stack="react_vite", scoreboard=scoreboard,
    )
    assert out == _BACKEND_ALTERNATIVES["openrouter"]


# ---------------------------------------------------------------------
# Exploration (ε-greedy)
# ---------------------------------------------------------------------


def test_exploration_overrides_demotion_when_coin_lands(monkeypatch, scoreboard):
    """With ε=1.0 we ALWAYS keep the losing backend (full exploration).
    Verifies the explore path is wired correctly."""
    _seed_backend_outcomes(scoreboard, "react_vite", "openrouter", wins=1, losses=9)
    monkeypatch.setenv("SKYN3T_ROUTER_EXPLORATION_EPS", "1.0")
    out = resolve_model_for_file(
        "src/components/X.jsx", stack="react_vite", scoreboard=scoreboard,
    )
    assert out == _resolve_static("src/components/X.jsx")


def test_exploration_seeded_for_partial_eps(monkeypatch, scoreboard):
    """With ε=0.5 and a controlled random seed, the outcome is stable
    across runs — verifies we're actually consulting ``random.random``."""
    _seed_backend_outcomes(scoreboard, "react_vite", "openrouter", wins=1, losses=9)
    monkeypatch.setenv("SKYN3T_ROUTER_EXPLORATION_EPS", "0.5")

    # Force the RNG to "exploit" (no exploration) — random.random() == 0.99 > 0.5
    monkeypatch.setattr(random, "random", lambda: 0.99)
    assert resolve_model_for_file(
        "src/components/X.jsx", stack="react_vite", scoreboard=scoreboard,
    ) == _BACKEND_ALTERNATIVES["openrouter"]

    # Force the RNG to "explore" — random.random() == 0.01 < 0.5
    monkeypatch.setattr(random, "random", lambda: 0.01)
    assert resolve_model_for_file(
        "src/components/X.jsx", stack="react_vite", scoreboard=scoreboard,
    ) == _resolve_static("src/components/X.jsx")


# ---------------------------------------------------------------------
# Safety: scoreboard exceptions don't break routing
# ---------------------------------------------------------------------


def test_scoreboard_exception_falls_back_to_static():
    class _BrokenScoreboard:
        def backend_rate(self, *_a, **_k):
            raise RuntimeError("scoreboard on fire")

    out = resolve_model_for_file(
        "src/components/X.jsx", stack="react_vite", scoreboard=_BrokenScoreboard(),
    )
    assert out == _resolve_static("src/components/X.jsx")


def test_object_without_backend_rate_ignored(scoreboard):
    """A scoreboard-shaped object lacking ``backend_rate`` must not
    crash the router — duck-typed guard."""
    class _LegacyScoreboard:
        pass

    out = resolve_model_for_file(
        "src/components/X.jsx",
        stack="react_vite",
        scoreboard=_LegacyScoreboard(),
    )
    assert out == _resolve_static("src/components/X.jsx")


# ---------------------------------------------------------------------
# BuildPatternStats / BuildPatternScoreboard schema
# ---------------------------------------------------------------------


def test_record_backend_partitions_by_backend(scoreboard):
    sb = scoreboard
    shape = ["src/App.jsx"]
    sb.record_backend("react_vite", shape, "openrouter", "yes")
    sb.record_backend("react_vite", shape, "openrouter", "no")
    sb.record_backend("react_vite", shape, "copilot_cli", "yes")
    sb.record_backend("react_vite", shape, "copilot_cli", "yes")

    rate_kimi = sb.backend_rate("react_vite", "openrouter", min_samples=1)
    rate_copilot = sb.backend_rate("react_vite", "copilot_cli", min_samples=1)
    assert rate_kimi == 0.5
    assert rate_copilot == 1.0


def test_record_backend_ignores_empty_inputs(scoreboard):
    sb = scoreboard
    sb.record_backend("", ["src/App.jsx"], "openrouter", "yes")
    sb.record_backend("react_vite", [], "openrouter", "yes")
    sb.record_backend("react_vite", ["src/App.jsx"], "", "yes")
    # Nothing should have landed
    assert sb.backend_rate("react_vite", "openrouter", min_samples=1) is None


def test_backend_rate_aggregates_across_shapes(scoreboard):
    sb = scoreboard
    sb.record_backend("react_vite", ["src/App.jsx"], "openrouter", "yes")
    sb.record_backend("react_vite", ["src/components/Foo.jsx"], "openrouter", "no")
    sb.record_backend("react_vite", ["src/styles.css"], "openrouter", "no")
    # 1 win / (1 win + 2 loss) = 0.333…
    rate = sb.backend_rate("react_vite", "openrouter", min_samples=3)
    assert rate is not None
    assert abs(rate - (1 / 3)) < 1e-6


def test_backend_rate_returns_none_below_min_samples(scoreboard):
    sb = scoreboard
    sb.record_backend("react_vite", ["a.js"], "openrouter", "no")
    sb.record_backend("react_vite", ["b.js"], "openrouter", "no")
    assert sb.backend_rate("react_vite", "openrouter", min_samples=5) is None


def test_by_backend_roundtrips_through_persistence(tmp_path):
    path = tmp_path / "patterns.json"
    sb = BuildPatternScoreboard(store_path=path, flush_every=1)
    sb.record_backend("react_vite", ["src/App.jsx"], "openrouter", "yes")
    sb.record_backend("react_vite", ["src/App.jsx"], "openrouter", "no")
    sb.flush()

    sb2 = BuildPatternScoreboard(store_path=path)
    assert sb2.backend_rate("react_vite", "openrouter", min_samples=1) == 0.5


def test_by_backend_field_absent_on_legacy_rows_loads_clean(tmp_path):
    """Rows persisted before by_backend existed must load with an
    empty dict and not crash from_dict."""
    import json

    path = tmp_path / "patterns.json"
    legacy = {
        "react_vite": {
            "abc123": {
                "stack": "react_vite",
                "shape": ["src/App.jsx"],
                "success": 1,
                "failure": 2,
                "skipped": 0,
                "tags": {},
                "last_seen_at": 1700000000.0,
            }
        }
    }
    path.write_text(json.dumps(legacy))

    sb = BuildPatternScoreboard(store_path=path)
    assert sb.backend_rate("react_vite", "openrouter", min_samples=1) is None
    stats = sb.all_stats_for("react_vite")[0]
    assert stats.by_backend == {}
