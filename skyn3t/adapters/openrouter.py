from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any, Dict, Optional

from skyn3t.adapters.llm_client import LLMRequest

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
    }


def _get_request_semaphore() -> "asyncio.Semaphore":
    global _request_semaphore, _request_semaphore_limit, _request_semaphore_loop_id
    limit = openrouter_max_concurrency()
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
            timeout=120.0,
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
        max_attempts = 6
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
                    raise
                delay = min(2 ** (attempt - 1), 8) + random.random()
                await asyncio.sleep(delay)
                continue
            if r.status_code in (429,) or 500 <= r.status_code < 600:
                if attempt == max_attempts:
                    r.raise_for_status()
                # Honor Retry-After when present.
                ra = r.headers.get("retry-after")
                try:
                    delay = float(ra) if ra else min(2 ** (attempt - 1), 8) + random.random()
                except ValueError:
                    delay = min(2 ** (attempt - 1), 8) + random.random()
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
        # Unreachable, but satisfies the type checker.
        raise RuntimeError(f"openrouter: exhausted retries ({last_exc})")

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            logger.debug("httpx aclose failed", exc_info=True)
