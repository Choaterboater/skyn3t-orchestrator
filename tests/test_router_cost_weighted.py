"""Tests for cost-weighted adaptive routing.

Win-rate-only demotion fires on failures. The cost layer adds a
second case: when both the static pick AND its alternative are
working fine, prefer the cheaper one — but only when the savings
clear the noise threshold (default 25%).

Tests cover:
- both winning, alt cheaper → cost demote fires
- both winning, savings below threshold → no demote
- cost demote disabled via env var → no demote
- alt is more expensive → no demote
- env-var cost overrides honored
- failure demote and cost demote don't double-fire
- _expected_cost_per_success math
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from skyn3t.core.events import Event, EventType
from skyn3t.core.model_router import (
    _BACKEND_COST,
    _backend_cost,
    _expected_cost_per_success,
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
    """Lock router knobs to defaults; disable ε-exploration so cost
    decisions are deterministic."""
    for key in (
        "SKYN3T_ROUTER_ADAPTIVE",
        "SKYN3T_ROUTER_DEMOTE_BELOW",
        "SKYN3T_ROUTER_DEMOTE_AFTER",
        "SKYN3T_ROUTER_EXPLORATION_EPS",
        "SKYN3T_ROUTER_COST_WEIGHTED",
        "SKYN3T_ROUTER_COST_SAVINGS",
        "SKYN3T_ROUTER_BACKEND_COSTS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SKYN3T_ROUTER_EXPLORATION_EPS", "0")


class _BusRecorder:
    def __init__(self):
        self.events: List[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)


def _seed_wins(sb, stack, backend, wins, losses):
    for _ in range(wins):
        sb.record_backend(stack, ["src/App.jsx"], backend, "yes")
    for _ in range(losses):
        sb.record_backend(stack, ["src/App.jsx"], backend, "no")


# ---------------------------------------------------------------------
# _backend_cost + _expected_cost_per_success
# ---------------------------------------------------------------------


def test_backend_cost_uses_static_table():
    # claude_cli is priced 3x in the static table.
    assert _backend_cost("claude_cli") == 3.0


def test_backend_cost_unknown_returns_pessimistic_default():
    assert _backend_cost("never_heard_of_it") == 2.0


def test_backend_cost_env_override_wins(monkeypatch):
    import json
    monkeypatch.setenv(
        "SKYN3T_ROUTER_BACKEND_COSTS",
        json.dumps({"kimi_cli": 5.0}),
    )
    assert _backend_cost("kimi_cli") == 5.0


def test_backend_cost_invalid_env_falls_back(monkeypatch, caplog):
    monkeypatch.setenv("SKYN3T_ROUTER_BACKEND_COSTS", "{not json")
    # Falls back to the static table without raising.
    assert _backend_cost("kimi_cli") == _BACKEND_COST["kimi_cli"]


def test_expected_cost_per_success_basic():
    # cost 3.0 / rate 0.5 = 6.0
    assert _expected_cost_per_success("claude_cli", 0.5) == 6.0


def test_expected_cost_per_success_zero_rate_returns_none():
    """Rate 0 = infinite cost — don't divide by zero."""
    assert _expected_cost_per_success("kimi_cli", 0.0) is None


def test_expected_cost_per_success_none_rate_returns_none():
    assert _expected_cost_per_success("kimi_cli", None) is None


# ---------------------------------------------------------------------
# Cost-demote behavior end-to-end through resolve_model_for_file
# ---------------------------------------------------------------------


def test_cost_demote_prefers_cheaper_when_both_winning(scoreboard):
    """Static pick for src/App.jsx is the 'ui' tier (copilot_cli +
    gpt-5.3-codex). Seed both copilot_cli (cost 1.0) and claude_cli
    (cost 3.0) with high win rates → cost demote should NOT fire
    (the alt is more expensive, not less)."""
    # Static for src/App.jsx returns ("copilot_cli", "gpt-5.3-codex").
    backend_static, _ = _resolve_static("src/App.jsx")
    assert backend_static == "copilot_cli"

    _seed_wins(scoreboard, "react_vite", "copilot_cli", wins=8, losses=2)  # 0.8
    _seed_wins(scoreboard, "react_vite", "claude_cli", wins=8, losses=2)   # 0.8
    out = resolve_model_for_file(
        "src/App.jsx", stack="react_vite", scoreboard=scoreboard,
    )
    # Alternative for copilot_cli is claude_cli, which costs MORE.
    # Cost demote must NOT fire.
    assert out[0] == "copilot_cli"


def test_cost_demote_fires_when_alt_meaningfully_cheaper(monkeypatch, scoreboard):
    """Flip the cost table: make claude_cli cheaper than copilot_cli
    via env override, with comparable win rates → cost demote fires."""
    import json
    # Make copilot_cli artificially expensive vs claude_cli.
    monkeypatch.setenv(
        "SKYN3T_ROUTER_BACKEND_COSTS",
        json.dumps({"copilot_cli": 10.0, "claude_cli": 1.0}),
    )
    _seed_wins(scoreboard, "react_vite", "copilot_cli", wins=8, losses=2)  # 0.8
    _seed_wins(scoreboard, "react_vite", "claude_cli", wins=8, losses=2)   # 0.8
    out = resolve_model_for_file(
        "src/App.jsx", stack="react_vite", scoreboard=scoreboard,
    )
    # copilot_cli's cost/success = 10/0.8 = 12.5
    # claude_cli's cost/success = 1/0.8 = 1.25 → 90% savings, fires
    assert out[0] == "claude_cli"


def test_cost_demote_does_not_fire_below_savings_threshold(monkeypatch, scoreboard):
    """24% savings is below the 25% default → keep the original."""
    import json
    # cur cost 1.0, alt cost 0.76. Both rates 0.8.
    # savings = (1.0/0.8 - 0.76/0.8) / (1.0/0.8) = 0.24
    monkeypatch.setenv(
        "SKYN3T_ROUTER_BACKEND_COSTS",
        json.dumps({"copilot_cli": 1.0, "claude_cli": 0.76}),
    )
    _seed_wins(scoreboard, "react_vite", "copilot_cli", wins=8, losses=2)
    _seed_wins(scoreboard, "react_vite", "claude_cli", wins=8, losses=2)
    out = resolve_model_for_file(
        "src/App.jsx", stack="react_vite", scoreboard=scoreboard,
    )
    assert out[0] == "copilot_cli"


def test_cost_demote_savings_threshold_env_overridable(monkeypatch, scoreboard):
    """Lower the savings threshold to 0.1 → the 24%-saving case now fires."""
    import json
    monkeypatch.setenv(
        "SKYN3T_ROUTER_BACKEND_COSTS",
        json.dumps({"copilot_cli": 1.0, "claude_cli": 0.76}),
    )
    monkeypatch.setenv("SKYN3T_ROUTER_COST_SAVINGS", "0.1")
    _seed_wins(scoreboard, "react_vite", "copilot_cli", wins=8, losses=2)
    _seed_wins(scoreboard, "react_vite", "claude_cli", wins=8, losses=2)
    out = resolve_model_for_file(
        "src/App.jsx", stack="react_vite", scoreboard=scoreboard,
    )
    assert out[0] == "claude_cli"


def test_cost_demote_disabled_by_env(monkeypatch, scoreboard):
    """Kill switch reverts to pure win-rate routing."""
    import json
    monkeypatch.setenv("SKYN3T_ROUTER_COST_WEIGHTED", "0")
    monkeypatch.setenv(
        "SKYN3T_ROUTER_BACKEND_COSTS",
        json.dumps({"copilot_cli": 100.0, "claude_cli": 1.0}),
    )
    _seed_wins(scoreboard, "react_vite", "copilot_cli", wins=8, losses=2)
    _seed_wins(scoreboard, "react_vite", "claude_cli", wins=8, losses=2)
    out = resolve_model_for_file(
        "src/App.jsx", stack="react_vite", scoreboard=scoreboard,
    )
    # Even with extreme cost difference, kill switch forces static.
    assert out[0] == "copilot_cli"


def test_cost_demote_skips_when_alt_below_threshold(monkeypatch, scoreboard):
    """If the alt is losing on win rate, don't redirect to it just
    because it's cheap. Quality > cost when quality fails."""
    import json
    monkeypatch.setenv(
        "SKYN3T_ROUTER_BACKEND_COSTS",
        json.dumps({"copilot_cli": 10.0, "claude_cli": 1.0}),
    )
    _seed_wins(scoreboard, "react_vite", "copilot_cli", wins=8, losses=2)  # 0.8
    _seed_wins(scoreboard, "react_vite", "claude_cli", wins=1, losses=9)   # 0.1
    out = resolve_model_for_file(
        "src/App.jsx", stack="react_vite", scoreboard=scoreboard,
    )
    assert out[0] == "copilot_cli"


def test_failure_demote_takes_precedence_over_cost_demote(monkeypatch, scoreboard):
    """When the static pick is losing, failure demote fires first.
    Cost layer doesn't double-fire on the demoted backend."""
    import json
    # Make the alternative slightly cheaper too — both layers could
    # theoretically fire, but only failure demote should.
    monkeypatch.setenv(
        "SKYN3T_ROUTER_BACKEND_COSTS",
        json.dumps({"copilot_cli": 5.0, "claude_cli": 1.0}),
    )
    # copilot_cli is losing badly → failure demote fires to claude_cli.
    _seed_wins(scoreboard, "react_vite", "copilot_cli", wins=1, losses=9)  # 0.1
    _seed_wins(scoreboard, "react_vite", "claude_cli", wins=8, losses=2)   # 0.8

    bus = _BusRecorder()
    out = resolve_model_for_file(
        "src/App.jsx", stack="react_vite", scoreboard=scoreboard, event_bus=bus,
    )
    assert out[0] == "claude_cli"
    decisions = [
        e for e in bus.events
        if e.event_type == EventType.CORTEX_DECISION
    ]
    actions = [d.payload["action"] for d in decisions]
    # Exactly one decision should fire — the failure demote, not both.
    assert actions == ["demote_backend"]


def test_cost_demote_publishes_decision(monkeypatch, scoreboard):
    """The cost-demote path emits a CORTEX_DECISION with the cost
    rationale (action='cost_demote_backend')."""
    import json
    monkeypatch.setenv(
        "SKYN3T_ROUTER_BACKEND_COSTS",
        json.dumps({"copilot_cli": 10.0, "claude_cli": 1.0}),
    )
    _seed_wins(scoreboard, "react_vite", "copilot_cli", wins=8, losses=2)
    _seed_wins(scoreboard, "react_vite", "claude_cli", wins=8, losses=2)

    bus = _BusRecorder()
    resolve_model_for_file(
        "src/App.jsx", stack="react_vite", scoreboard=scoreboard, event_bus=bus,
    )
    decisions = [
        e for e in bus.events
        if e.event_type == EventType.CORTEX_DECISION
    ]
    assert len(decisions) == 1
    payload = decisions[0].payload
    assert payload["action"] == "cost_demote_backend"
    assert payload["input"]["from_backend"] == "copilot_cli"
    assert payload["input"]["to_backend"] == "claude_cli"
    assert payload["input"]["relative_savings"] > 0.25


def test_no_data_returns_static(scoreboard):
    """With no scoreboard data at all, cost path is a no-op."""
    out = resolve_model_for_file(
        "src/App.jsx", stack="react_vite", scoreboard=scoreboard,
    )
    assert out == _resolve_static("src/App.jsx")
