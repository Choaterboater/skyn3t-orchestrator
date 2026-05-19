from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Optional

from skyn3t.adapters.llm_client import LLMRequest

logger = logging.getLogger("skyn3t.adapters.openrouter")

# Current-day cheap reliable code model. ~$0.25/M in, $0.38/M out.
# Was claude-3.5-sonnet but that's ~$3/M — way more than we need for
# generic generation. Use SKYN3T_OPENROUTER_MODEL env var to override
# globally, or pass model= per call for fine-grained routing.
DEFAULT_MODEL = "deepseek/deepseek-v3.2"
BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterBackend:
    def __init__(self, api_key: Optional[str] = None, *, base_url: str = BASE_URL):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
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
        max_attempts = 4
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
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
