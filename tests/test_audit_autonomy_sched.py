"""Regression tests for the autonomous scout cadence parsing (group autonomy_sched).

Bug: the default scout schedule ``"interval:12h"`` was unparseable by
``SchedulerAgent._parse_schedule`` so ``next_run`` stayed ``None`` forever, the
recurring learning-ingest never fired, and status falsely reported scheduled.
"""

from datetime import datetime, timezone

import pytest

from skyn3t.agents.scheduler_agent import SchedulerAgent
from skyn3t.cortex import autonomous_loop


def _parser() -> SchedulerAgent:
    # Build a bare instance with just the parser deps it needs (no event loop).
    return SchedulerAgent.__new__(SchedulerAgent)


def test_default_scout_schedule_parses_to_real_next_run():
    """The default ``interval:12h`` cadence must yield a concrete next_run."""
    agent = _parser()
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    next_run = agent._parse_schedule("interval:12h", base_time=base)
    assert next_run is not None, "interval:12h must be parseable"
    assert (next_run - base).total_seconds() == 12 * 3600


@pytest.mark.parametrize(
    "expr,expected_seconds",
    [
        ("interval:30s", 30),
        ("interval:5m", 5 * 60),
        ("interval:12h", 12 * 3600),
        ("interval:2d", 2 * 86400),
        ("interval: 12h", 12 * 3600),  # tolerate whitespace
    ],
)
def test_interval_prefix_units(expr, expected_seconds):
    agent = _parser()
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    next_run = agent._parse_schedule(expr, base_time=base)
    assert next_run is not None
    assert (next_run - base).total_seconds() == expected_seconds
    # The interval (used for anchor-based recurrence) must match too.
    assert agent._parse_interval_seconds(expr) == float(expected_seconds)


def test_unparseable_schedule_still_returns_none():
    """Garbage cadences must remain unparseable (so status reports not scheduled)."""
    agent = _parser()
    assert agent._parse_schedule("interval:nonsense") is None
    assert agent._parse_schedule("totally bogus") is None
    assert agent._parse_interval_seconds("interval:nonsense") is None


def test_autonomous_loop_helper_resolves_default_schedule():
    """The autonomous loop must agree the default cadence is schedulable."""
    next_run = autonomous_loop._parse_schedule_expr("interval:12h")
    assert next_run is not None
    # And a bad expression must not be treated as scheduled.
    assert autonomous_loop._parse_schedule_expr("interval:nope") is None
