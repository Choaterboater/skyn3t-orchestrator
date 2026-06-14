from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Any, Dict, Optional

from skyn3t.adapters.llm_client import LLMRequest, TransientLLMError

logger = logging.getLogger("skyn3t.adapters.openrouter")

# Last-resort fallback model. Keep this default free-safe even when the normal
# tier router is allowed to use cheap paid models; it is used only when no
# caller-specific model/tier is supplied. Use SKYN3T_OPENROUTER_MODEL to
# override globally, or pass model= per call for fine-grained routing.
DEFAULT_MODEL = "openai/gpt-oss-120b:free"
BASE_URL = "https://openrouter.ai/api/v1"

# Process-wide cap on CONCURRENT OpenRouter requests. Without it, parallel file
# generation (up to 4/build) x stages x the autonomous fleet all hit the API at
# once and burst past provider rate limits — builds then die in a wall of 429s
# (observed: 224 in one build). Bounding total in-flight requests + the existing
# retry-after backoff lets builds actually complete. Tunable; raise once you know
# the key's real rate ceiling. Created lazily so it binds to the running loop.
DEFAULT_MAX_CONCURRENCY = 4
_MAX_CONCURRENCY_SETTING = "SKYN3T_OPENROUTER_MAX_CONCURRENCY"
_request_semaphore: Optional["asyncio.Semaphore"] = None
_request_semaphore_limit: Optional[int] = None
_request_semaphore_loop_id: Optional[int] = None

# PHASE 3: adaptive 429-aware concurrency. On a 429 we MULTIPLICATIVELY shrink
# the EFFECTIVE in-flight cap (halve, floor 1) for a cooldown window so the
# build self-throttles to the provider's real ceiling instead of bursting; once
# the window expires the configured cap is restored in full. We never mutate a
# live asyncio.Semaphore (unsupported — Semaphores can't be resized): we only
# lower the `limit` that `_get_request_semaphore` computes, and its EXISTING
# lazy per-loop rebind swaps in a smaller Semaphore. Simple, safe, self-
# restoring; no custom async primitive. Set the cooldown to 0 to disable.
_THROTTLE_COOLDOWN_SECONDS = float(
    os.environ.get("SKYN3T_OPENROUTER_THROTTLE_COOLDOWN", "20")
)
_throttle_floor: int = 0  # 0 = no active cooldown; >0 = current reduced cap
_throttle_until: float = 0.0  # monotonic deadline for the active cooldown


def _now() -> float:
    """Monotonic clock for cooldown bookkeeping (no event loop required)."""
    return time.monotonic()


def _note_throttle_if_429(status_code: int) -> None:
    """Record a 429 → halve the effective concurrency cap (floor 1) for the
    cooldown window. Idempotent within a window: repeated 429s keep shrinking
    (down to the floor of 1) and refresh the deadline.
    """
    global _throttle_floor, _throttle_until
    if status_code != 429 or _THROTTLE_COOLDOWN_SECONDS <= 0:
        return
    configured = openrouter_max_concurrency()
    current = _effective_concurrency()
    new_floor = max(1, min(current, configured) // 2)
    _throttle_floor = new_floor
    _throttle_until = _now() + _THROTTLE_COOLDOWN_SECONDS
    logger.warning(
        "openrouter 429 → cooldown: effective concurrency capped at %d for %.0fs",
        new_floor,
        _THROTTLE_COOLDOWN_SECONDS,
    )


def _effective_concurrency() -> int:
    """The configured concurrency cap, reduced to the throttle floor during an
    active 429 cooldown. Once the cooldown expires it clears the floor and
    restores the full configured cap (multiplicative-decrease / full restore).
    """
    global _throttle_floor, _throttle_until
    configured = openrouter_max_concurrency()
    if _throttle_floor and _now() < _throttle_until:
        return max(1, min(_throttle_floor, configured))
    # Cooldown expired (or never active) → clear and restore.
    _throttle_floor = 0
    return configured


def openrouter_max_concurrency() -> int:
    """Return the effective process-wide OpenRouter concurrency cap."""
    try:
        from skyn3t.config.settings import get_settings

        raw: Any = getattr(
            get_settings(), "openrouter_max_concurrency", DEFAULT_MAX_CONCURRENCY
        )
    except Exception:
        raw = os.environ.get(_MAX_CONCURRENCY_SETTING, DEFAULT_MAX_CONCURRENCY)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid %s; using default %d",
            _MAX_CONCURRENCY_SETTING,
            DEFAULT_MAX_CONCURRENCY,
        )
        value = DEFAULT_MAX_CONCURRENCY
    if value < 1:
        logger.warning("%s must be >= 1; using 1", _MAX_CONCURRENCY_SETTING)
        value = 1
    return value


def openrouter_runtime_status() -> Dict[str, Any]:
    """Non-secret OpenRouter runtime settings for health/debug surfaces."""
    limit = openrouter_max_concurrency()
    if os.environ.get(_MAX_CONCURRENCY_SETTING):
        source = "environment"
    elif limit != DEFAULT_MAX_CONCURRENCY:
        source = "settings"
    else:
        source = "default"
    return {
        "max_concurrency": limit,
        "setting": _MAX_CONCURRENCY_SETTING,
        "source": source,
        "default_max_concurrency": DEFAULT_MAX_CONCURRENCY,
        "active_semaphore_limit": _request_semaphore_limit,
        # PHASE 3 observability: the cap actually in force right now (reduced
        # during a 429 cooldown) and the active throttle floor (None when idle).
        "effective_concurrency": _effective_concurrency(),
        "throttle_floor": _throttle_floor or None,
        "throttle_cooldown_seconds": _THROTTLE_COOLDOWN_SECONDS,
    }


def _get_request_semaphore() -> "asyncio.Semaphore":
    global _request_semaphore, _request_semaphore_limit, _request_semaphore_loop_id
    # PHASE 3: use the EFFECTIVE cap (reduced during a 429 cooldown). The
    # rebind-on-limit-change logic below swaps in a smaller Semaphore when the
    # cooldown shrinks the cap, and a larger one when it restores. In-flight
    # requests keep their slot on the OLD Semaphore object (captured at
    # ``async with`` time); new acquirers use the new one — safe and self-
    # correcting within one request cycle.
    limit = _effective_concurrency()
    loop_id = id(asyncio.get_running_loop())
    if (
        _request_semaphore is None
        or _request_semaphore_limit != limit
        or _request_semaphore_loop_id != loop_id
    ):
        _request_semaphore = asyncio.Semaphore(limit)
        _request_semaphore_limit = limit
        _request_semaphore_loop_id = loop_id
        logger.info("openrouter concurrency limit=%d", limit)
    return _request_semaphore


class OpenRouterBackend:
    def __init__(self, api_key: Optional[str] = None, *, base_url: str = BASE_URL):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self._last_usage: Dict[str, int] = {"prompt_tokens": 0, "response_tokens": 0}
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        try:
            import httpx
        except ImportError as e:
            raise ImportError("httpx required for openrouter backend") from e
        self._httpx = httpx
        self._client = httpx.AsyncClient(
            base_url=base_url,
            # Granular timeout: fail FAST on a dead connection (connect=15s) but
            # give slow models a generous READ window — a flat 120s was cutting
            # off legitimate-but-slow completions mid-response (httpx.ReadTimeout),
            # which the retry loop below then exhausted, starving codegen and the
            # build-fix loop. Tunable via SKYN3T_OPENROUTER_READ_TIMEOUT.
            timeout=httpx.Timeout(
                connect=15.0,
                read=float(os.environ.get("SKYN3T_OPENROUTER_READ_TIMEOUT", "300")),
                write=60.0,
                pool=15.0,
            ),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://skyn3t.local",
                "X-Title": "skyn3t-orchestrator",
            },
        )

    async def complete(self, req: LLMRequest) -> str:
        model = req.model or os.environ.get("SKYN3T_OPENROUTER_MODEL") or DEFAULT_MODEL
        messages = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        messages.append({"role": "user", "content": req.prompt})
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        # Retry on 429 + 5xx with exponential backoff and jitter. Never retry
        # on 4xx other than 429 — those are caller errors and won't fix on retry.
        # PHASE 1: deeper backoff. The old ceiling (8s x 6 attempts ~= 23s) was
        # shorter than a real provider throttle window, so a sustained 429 fell
        # through to the deterministic-stub fallback and permanently failed the
        # build. 8 attempts capped at 30s waits out ~90s of throttling. Tunable.
        max_attempts = int(os.environ.get("SKYN3T_OPENROUTER_MAX_RETRIES", "8"))
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                # Bound concurrent in-flight requests process-wide so parallel
                # files/stages/builds can't burst past the rate limit.
                async with _get_request_semaphore():
                    r = await self._client.post("/chat/completions", json=payload)
            except Exception as exc:  # network-level
                last_exc = exc
                if attempt == max_attempts:
                    # PHASE 2: a connect/read timeout or connection error that
                    # exhausted all retries is TRANSIENT. Raise the typed error
                    # so callers bounded-retry instead of letting it fall
                    # through to the deterministic-stub sentinel.
                    raise TransientLLMError(
                        f"openrouter network error after {max_attempts} "
                        f"attempts: {exc}"
                    ) from exc
                delay = min(2 ** (attempt - 1), 30) + random.random()
                await asyncio.sleep(delay)
                continue
            if r.status_code in (429,) or 500 <= r.status_code < 600:
                # PHASE 3: record EVERY 429 (not just the final attempt) so the
                # adaptive cooldown shrinks the effective concurrency promptly.
                _note_throttle_if_429(r.status_code)
                if attempt == max_attempts:
                    # PHASE 2: a 429 throttle / 5xx overload that survived all
                    # retries is TRANSIENT — raise the typed error instead of
                    # r.raise_for_status() (whose httpx.HTTPStatusError used to
                    # be swallowed into the written-as-code stub sentinel).
                    raise TransientLLMError(
                        f"openrouter {r.status_code} after {max_attempts} attempts"
                    )
                # Honor Retry-After when present.
                ra = r.headers.get("retry-after")
                try:
                    delay = float(ra) if ra else min(2 ** (attempt - 1), 30) + random.random()
                except ValueError:
                    delay = min(2 ** (attempt - 1), 30) + random.random()
                await asyncio.sleep(delay)
                continue
            r.raise_for_status()
            data = r.json()
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            self._last_usage = {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "response_tokens": int(
                    usage.get("completion_tokens") or usage.get("response_tokens") or 0
                ),
            }
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("openrouter: empty choices")
            msg = choices[0].get("message") or {}
            return (msg.get("content") or "").strip()
        # Unreachable, but satisfies the type checker. Treat as transient for
        # consistency with the exhausted-retry exits above.
        raise TransientLLMError(f"openrouter: exhausted retries ({last_exc})")

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            logger.debug("httpx aclose failed", exc_info=True)
