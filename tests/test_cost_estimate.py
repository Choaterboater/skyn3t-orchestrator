from __future__ import annotations

from skyn3t.observability.cost_estimate import estimate_cost_usd
from skyn3t.observability.token_tracker import TokenTracker


def test_token_tracker_includes_estimated_cost_usd():
    tracker = TokenTracker()
    with tracker._lock:
        tracker._by_project["demo"] = {
            "slug": "demo",
            "prompt_tokens": 1000,
            "response_tokens": 500,
            "total_tokens": 1500,
            "calls": 2,
            "first_seen_at": 1.0,
            "last_used_at": 2.0,
            "stages": {},
        }

    row = tracker.for_project("demo")
    assert row is not None
    assert "estimated_cost_usd" in row
    assert row["estimated_cost_usd"] >= 0.0
    assert estimate_cost_usd(0) == 0.0
