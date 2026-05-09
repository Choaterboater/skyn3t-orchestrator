from __future__ import annotations
import logging, os
from typing import Optional
from skyn3t.adapters.llm_client import LLMRequest

logger = logging.getLogger("skyn3t.adapters.openrouter")

DEFAULT_MODEL = "anthropic/claude-3.5-sonnet"
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
        self._client = httpx.AsyncClient(base_url=base_url, timeout=120.0,
                                         headers={"Authorization": f"Bearer {self.api_key}",
                                                  "HTTP-Referer": "https://skyn3t.local",
                                                  "X-Title": "skyn3t-orchestrator"})

    async def complete(self, req: LLMRequest) -> str:
        model = req.model or os.environ.get("SKYN3T_OPENROUTER_MODEL") or DEFAULT_MODEL
        messages = []
        if req.system: messages.append({"role": "system", "content": req.system})
        messages.append({"role": "user", "content": req.prompt})
        payload = {"model": model, "messages": messages,
                   "max_tokens": req.max_tokens, "temperature": req.temperature}
        r = await self._client.post("/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("openrouter: empty choices")
        msg = choices[0].get("message") or {}
        return (msg.get("content") or "").strip()

    async def aclose(self):
        try: await self._client.aclose()
        except Exception: pass
