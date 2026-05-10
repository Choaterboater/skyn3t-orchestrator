"""Provider-aware prompt augmentation.

Given a backend identifier, retrieve relevant LLM docs from RAG (tagged
``kind='llm-docs'`` ``provider=<backend>``), and produce a small system-prompt
augmentation block to prepend to the user's prompt.

Caches per-provider for 1 hour to avoid hitting RAG every call.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger("skyn3t.adapters.prompt_builder")

_CACHE: dict[str, tuple[float, str]] = {}  # provider -> (ts, augmentation_text)
_TTL = 3600.0
_MAX_AUG_CHARS = 1500


# Map LLMClient backend identifiers to provider keys used in seeds.
_BACKEND_TO_PROVIDER = {
    "claude_cli": "claude",
    "anthropic": "claude",
    "copilot_cli": "copilot",
    "openai_cli": "openai",
    "kimi_cli": "kimi",
    "openrouter": None,  # mixed; skip augmentation
}


def provider_for(backend: Optional[str]) -> Optional[str]:
    if not backend:
        return None
    return _BACKEND_TO_PROVIDER.get(backend)


async def augmentation_for(backend: Optional[str], rag=None) -> str:
    """Return a short system-prompt augmentation tailored to the backend's provider.

    Empty string if no docs ingested yet, or backend not recognized.
    """
    provider = provider_for(backend)
    if provider is None or rag is None:
        return ""
    cached = _CACHE.get(provider)
    if cached and (time.time() - cached[0]) < _TTL:
        return cached[1]
    try:
        # Query RAG for the top 3 chunks for this provider. RAG implementations
        # vary; try the most common method shapes.
        hits = []
        if hasattr(rag, "query"):
            try:
                hits = await rag.query(
                    query=f"{provider} prompt format best practices",
                    top_k=3,
                    where={"kind": "llm-docs", "provider": provider},
                )
            except TypeError:
                # `where` not supported — fall back to plain top_k and filter.
                all_hits = await rag.query(
                    query=f"{provider} prompt format best practices", top_k=10
                )
                hits = [
                    h
                    for h in all_hits
                    if (
                        h.get("metadata", {}).get("provider") == provider
                        and h.get("metadata", {}).get("kind") == "llm-docs"
                    )
                ][:3]
        body_parts = []
        for h in hits[:3]:
            content = h.get("content") or ""
            if not content:
                continue
            snippet = content.strip()[:500]
            body_parts.append(snippet)
        if not body_parts:
            aug = ""
        else:
            aug = (
                f"# Provider notes ({provider})\n\n"
                + "\n\n".join(f"- {p}" for p in body_parts)
            )[:_MAX_AUG_CHARS]
        _CACHE[provider] = (time.time(), aug)
        return aug
    except Exception:
        logger.exception("augmentation_for %s failed", provider)
        return ""
