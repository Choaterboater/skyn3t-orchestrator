"""TokenTracker char-count clamping and event handling.

Root cause being tested: the studio dashboard previously showed
code_agent at 11.5M response tokens for 9 calls — implausible. CLIs
sometimes emit megabytes of agent-loop trace (tool calls, file reads,
search results) to stdout before the final code body. token_tracker
was counting those trace dumps as response tokens, inflating the
per-agent and per-project rollups by 100x.

The fix clamps response_chars at 200KB (~50K tokens) and prompt_chars
at 500KB (~125K tokens) — well above any legitimate LLM output range
but below the noisy-trace tail.
"""

from __future__ import annotations

from types import SimpleNamespace

from skyn3t.observability.token_tracker import TokenTracker


def _make_event(**payload):
    return SimpleNamespace(payload=payload)


def test_normal_size_response_passes_unclamped() -> None:
    """A regular 8KB output should be counted at ~2K response tokens."""
    tracker = TokenTracker()
    tracker._on_exchange(_make_event(
        prompt_chars=4000,
        response_chars=8000,
        system_chars=500,
        agent="code_agent",
        project_slug="test-build",
        project_stage="code",
        backend="openrouter",
        model="openrouter/owl-alpha",
    ))
    agents = tracker.per_agent()
    assert len(agents) == 1
    a = agents[0]
    # 4000/4 + 500/4 = 1000 + 125 = 1125 prompt tokens
    assert a["prompt_tokens"] == 1125
    # 8000/4 = 2000 response tokens
    assert a["response_tokens"] == 2000


def test_oversized_response_gets_clamped() -> None:
    """A 5MB CLI trace dump should not produce 1.25M response tokens.
    The clamp caps at 200KB → 50K tokens.
    """
    tracker = TokenTracker()
    tracker._on_exchange(_make_event(
        prompt_chars=4000,
        response_chars=5_000_000,  # 5MB — the bad case from the dashboard
        system_chars=0,
        agent="code_agent",
    ))
    agents = tracker.per_agent()
    # 200_000 / 4 = 50_000 tokens, NOT 1_250_000
    assert agents[0]["response_tokens"] == 50_000


def test_oversized_prompt_gets_clamped() -> None:
    tracker = TokenTracker()
    tracker._on_exchange(_make_event(
        prompt_chars=2_000_000,  # 2MB prompt
        response_chars=1000,
        system_chars=0,
        agent="code_agent",
    ))
    agents = tracker.per_agent()
    # 500_000 / 4 = 125_000 prompt tokens (clamped from 500_000)
    assert agents[0]["prompt_tokens"] == 125_000


def test_zero_chars_yields_zero_tokens() -> None:
    """Empty payloads (failed calls, etc.) must not crash and must
    yield zero tokens, not "1" from the max() guard.
    """
    tracker = TokenTracker()
    tracker._on_exchange(_make_event(
        prompt_chars=0,
        response_chars=0,
        system_chars=0,
        agent="code_agent",
    ))
    agents = tracker.per_agent()
    assert agents[0]["prompt_tokens"] == 0
    assert agents[0]["response_tokens"] == 0


def test_multiple_calls_accumulate_correctly() -> None:
    """Five normal-sized calls should accumulate without weird inflation."""
    tracker = TokenTracker()
    for _ in range(5):
        tracker._on_exchange(_make_event(
            prompt_chars=4000,
            response_chars=8000,
            system_chars=500,
            agent="code_agent",
        ))
    agents = tracker.per_agent()
    a = agents[0]
    assert a["calls"] == 5
    # 5 * 1125 = 5625
    assert a["prompt_tokens"] == 5625
    # 5 * 2000 = 10000
    assert a["response_tokens"] == 10_000


def test_legacy_path_uses_preview_strings() -> None:
    """Events without prompt_chars/response_chars fall back to len of
    truncated preview fields with a min-bump for non-trivial outputs.
    """
    tracker = TokenTracker()
    tracker._on_exchange(_make_event(
        prompt="x" * 2500,    # >2000 triggers bump
        response="y" * 2500,  # >2000 triggers bump
        system="",
        agent="reviewer",
    ))
    agents = tracker.per_agent()
    a = agents[0]
    # Legacy bump: prompt_chars=max(2500,8000)=8000 → 2000 tokens
    assert a["prompt_tokens"] == 2000
    # response_chars=max(2500,4000)=4000 → 1000 tokens
    assert a["response_tokens"] == 1000
