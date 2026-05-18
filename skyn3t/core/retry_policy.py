"""Per-failure-class retry policy.

The old policy was uniform: every failure → exponential backoff
(``asyncio.sleep(2 ** retry_count)``) up to ``max_retries``. That's
wrong for the actual failure shapes we see in production:

  * **Auth errors** (401, 403, bad API key, expired token) — never
    going to succeed on retry. Today's policy waits 2/4/8/16 seconds
    between attempts of the same doomed call.
  * **Rate-limit / quota** (429, "rate limit exceeded", "quota") —
    succeeds on retry but needs a longer wait than the default. The
    server is often telling us how long via Retry-After.
  * **Transient network / timeout** (ECONNRESET, asyncio.TimeoutError,
    socket hangup) — short backoff + retry usually fixes it.
  * **Syntax / validation errors** — already passed the syntax gate
    once. If a retry comes back with the SAME error type, stop early.
  * **Capacity / no-fallback** — fail terminal immediately. There's
    nothing to retry against.

This module classifies a raised exception or error string into a
``FailureClass``, then picks a backoff and a "should we retry at all"
verdict per class.

Used by:
  * ``core/orchestrator.py`` → ``_handle_task_failure`` to decide
    whether/how to retry.
  * ``adapters/llm_client.py`` (future) → for the cross-model retry path
    to skip auth-class failures without burning a fallback slot.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Optional, Pattern


class FailureClass(str, enum.Enum):
    """Mutually-exclusive bucket for any task/agent/llm error."""

    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    SYNTAX = "syntax"
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    CAPACITY = "capacity"
    UNKNOWN = "unknown"


# Regex patterns matched against the lowercased error message. Order
# matters — first match wins, so put narrower patterns earlier. Each
# entry is (pattern, class). Compiled once at import.
_CLASSIFIERS: list[tuple[Pattern[str], FailureClass]] = [
    # Auth — irrecoverable without operator action
    (re.compile(r"\b(401|403)\b|unauthor[iz]ed|forbidden|"
                r"authentication\s*(failed|required|error)|"
                r"invalid\s*(api\s*key|token|credentials?)|"
                r"expired\s*(api\s*key|token|credentials?)|"
                r"bad\s*api\s*key|api\s*key\s*invalid|"
                r"permission\s*denied"), FailureClass.AUTH),

    # Rate limit — retry with longer backoff
    (re.compile(r"\b429\b|rate\s*limit|too\s*many\s*requests|"
                r"throttl(ed|ing)"), FailureClass.RATE_LIMIT),

    # Quota — like rate limit but usually billing-period bounded;
    # short retries won't recover, longer ones might (or might not).
    (re.compile(r"quota\s*(exceeded|exhausted|limit)|"
                r"insufficient\s*(credit|balance|tokens|quota)|"
                r"billing"), FailureClass.QUOTA),

    # Timeout — short backoff
    (re.compile(r"timed?\s*out|timeoutexception|deadline\s*exceeded|"
                r"connection\s*timed?\s*out"), FailureClass.TIMEOUT),

    # Transient network — short backoff
    (re.compile(r"econnreset|econnrefused|enetunreach|ehostunreach|"
                r"connection\s*reset|connection\s*refused|"
                r"broken\s*pipe|temporary\s*failure"), FailureClass.TRANSIENT),

    # Capacity / no-fallback — fail terminal
    (re.compile(r"no\s*(fallback|capacity|available\s*agent)|"
                r"circuit\s*(breaker\s*)?open|"
                r"all\s*backends\s*(failed|unavailable)"),
     FailureClass.CAPACITY),

    # Not-found (route, resource, agent)
    (re.compile(r"\b404\b|not\s*found|does\s*not\s*exist|"
                r"no\s*such\s*(file|directory|agent|task)"),
     FailureClass.NOT_FOUND),

    # Syntax — usually re-prompting won't help unless we ALSO
    # change context. Treat as retry-once-then-fail.
    (re.compile(r"syntax\s*error|unexpected\s*token|"
                r"invalid\s*syntax|parse\s*error|"
                r"malformed\s*json|json\.?decode(error)?"),
     FailureClass.SYNTAX),

    # Validation (typed input rejected by schema, pydantic, etc.)
    (re.compile(r"validation\s*error|invalid\s*value|"
                r"expected\s+\S+\s+got|type\s*error|"
                r"value\s*error"), FailureClass.VALIDATION),
]


def classify(error: object) -> FailureClass:
    """Bucket an exception or error message into a FailureClass.

    Accepts:
      * BaseException instances — uses both ``type(exc).__name__`` and
        ``str(exc)`` for matching.
      * Strings — matched directly.
      * Other types — coerced via ``str(...)``.

    Always returns SOMETHING — UNKNOWN when nothing matches.
    """
    if isinstance(error, BaseException):
        candidate = f"{type(error).__name__}: {error}"
    elif isinstance(error, str):
        candidate = error
    else:
        candidate = str(error)

    if not candidate:
        return FailureClass.UNKNOWN

    text = candidate.lower()

    # Native asyncio.TimeoutError doesn't always include "timed out"
    # in str(), so check the type-name path explicitly.
    if "timeouterror" in text:
        return FailureClass.TIMEOUT

    for pattern, cls in _CLASSIFIERS:
        if pattern.search(text):
            return cls

    return FailureClass.UNKNOWN


@dataclass(frozen=True)
class RetryDecision:
    """What to do after a failure of a given class.

    Attributes:
      should_retry: False means stop immediately; the orchestrator's
        fallback chain skips ahead to ``_finalize_task_failure``.
      backoff_seconds: how long to wait before the next attempt.
        Already includes the per-class bias and the current attempt
        number (no exponential math at the call site).
      reason: human-readable diagnostic the caller can log/emit.
    """

    should_retry: bool
    backoff_seconds: float
    reason: str


# Per-class retry budget. ``max_attempts`` is the total number of
# attempts INCLUDING the first. Set to 1 for failure classes where the
# retry never succeeds (auth) — first attempt counts, no further ones.
#
# ``base_backoff`` is multiplied by ``2 ** (attempt - 1)`` for
# exponential backoff (matches the old behavior on transient classes).
# Some classes use a flat backoff because exponential makes no sense
# (rate limit usually needs a fixed minimum wait the server hints).
_BUDGETS: dict = {
    # Class:                    max_attempts, base_backoff, cap_backoff, exponential
    FailureClass.AUTH:          (1,  0.0,   0.0,  False),
    FailureClass.QUOTA:         (1,  0.0,   0.0,  False),
    FailureClass.CAPACITY:      (1,  0.0,   0.0,  False),
    FailureClass.NOT_FOUND:     (1,  0.0,   0.0,  False),
    FailureClass.SYNTAX:        (2,  0.5,   2.0,  False),
    FailureClass.VALIDATION:    (2,  0.5,   2.0,  False),
    FailureClass.RATE_LIMIT:    (4, 10.0,  60.0,  False),  # flat 10s; cap at 60
    FailureClass.TIMEOUT:       (3,  2.0,  16.0,  True),
    FailureClass.TRANSIENT:     (4,  1.0,   8.0,  True),
    FailureClass.UNKNOWN:       (3,  2.0,   8.0,  True),
}


def decide(
    error: object,
    *,
    attempt: int,
    max_attempts_override: Optional[int] = None,
) -> RetryDecision:
    """Return a RetryDecision for an error after the Nth attempt.

    Args:
        error: the exception or error string.
        attempt: the attempt number that JUST FAILED (1-indexed). If
            the call returns ``should_retry=True`` it means attempt
            N+1 should run after the backoff.
        max_attempts_override: caller can tighten the budget (e.g. a
            task with a low timeout might want max_attempts=1 even on
            a class that normally allows 3).
    """
    cls = classify(error)
    max_attempts, base, cap, exponential = _BUDGETS[cls]
    if max_attempts_override is not None:
        max_attempts = min(max_attempts, max(1, int(max_attempts_override)))

    if attempt >= max_attempts:
        return RetryDecision(
            should_retry=False,
            backoff_seconds=0.0,
            reason=(
                f"{cls.value}: attempt {attempt}/{max_attempts} — "
                f"budget exhausted, stop"
            ),
        )

    if exponential:
        backoff = min(cap, base * (2 ** (attempt - 1)))
    else:
        backoff = min(cap, base)

    return RetryDecision(
        should_retry=True,
        backoff_seconds=float(backoff),
        reason=(
            f"{cls.value}: attempt {attempt}/{max_attempts} — "
            f"retry in {backoff:.1f}s"
        ),
    )
