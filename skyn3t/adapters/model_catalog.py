"""Resolve available models per LLM backend.

Each resolver returns a list of {id, label, context_tokens?, hint?} dicts.
All resolvers must be cheap, robust, and never raise. Network/CLI calls are
optional — fall back to a sensible static list per backend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List

logger = logging.getLogger("skyn3t.adapters.model_catalog")

# ---- static fallbacks ----------------------------------------------------
STATIC: Dict[str, List[Dict[str, Any]]] = {
    "claude_cli": [
        {"id": "claude-sonnet-4-7", "label": "Claude Sonnet 4.7"},
        {"id": "claude-opus-4-7", "label": "Claude Opus 4.7"},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
        {"id": "sonnet", "label": "sonnet (alias)"},
        {"id": "opus", "label": "opus (alias)"},
        {"id": "haiku", "label": "haiku (alias)"},
    ],
    "kimi_cli": [
        {"id": "kimi-code/kimi-for-coding", "label": "Kimi K2.6 (default — managed)"},
        {"id": "(default)", "label": "Use config.toml default"},
    ],
    "copilot_cli": [
        {"id": "claude-sonnet-4.6", "label": "Claude Sonnet 4.6 (default · 1x)"},
        {"id": "claude-sonnet-4.5", "label": "Claude Sonnet 4.5 · 1x"},
        {"id": "claude-haiku-4.5",  "label": "Claude Haiku 4.5 · 0.33x"},
        {"id": "claude-opus-4.6",   "label": "Claude Opus 4.6 · 3x (premium)"},
        {"id": "claude-opus-4.5",   "label": "Claude Opus 4.5 · 3x (premium)"},
        {"id": "gpt-5.4",           "label": "GPT-5.4 · 1x"},
        {"id": "gpt-5.3-codex",     "label": "GPT-5.3 Codex · 1x"},
        {"id": "gpt-5.2-codex",     "label": "GPT-5.2 Codex · 1x"},
        {"id": "gpt-5.2",           "label": "GPT-5.2 · 1x"},
        {"id": "gpt-5.4-mini",      "label": "GPT-5.4 mini · 0.33x"},
        {"id": "gpt-5-mini",        "label": "GPT-5 mini · 0x (free)"},
        {"id": "gpt-4.1",           "label": "GPT-4.1 · 0x (free)"},
    ],
    "openai_cli": [
        {"id": "gpt-5.4", "label": "GPT-5.4"},
        {"id": "gpt-5", "label": "GPT-5"},
        {"id": "gpt-5-mini", "label": "GPT-5 mini"},
        {"id": "gpt-5-codex", "label": "GPT-5 Codex"},
        {"id": "o4-mini", "label": "o4-mini (reasoning)"},
        {"id": "o3-mini", "label": "o3-mini (reasoning)"},
        {"id": "gpt-4.1", "label": "GPT-4.1"},
        {"id": "gpt-4o", "label": "GPT-4o"},
    ],
    "anthropic": [
        {"id": "claude-sonnet-4-7", "label": "Claude Sonnet 4.7"},
        {"id": "claude-opus-4-7", "label": "Claude Opus 4.7"},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
    ],
    "openrouter": [
        {"id": "anthropic/claude-sonnet-4.5", "label": "Anthropic · Claude Sonnet 4.5"},
        {"id": "anthropic/claude-opus-4.1", "label": "Anthropic · Claude Opus 4.1"},
        {"id": "openai/gpt-4o", "label": "OpenAI · GPT-4o"},
        {"id": "openai/gpt-4.1", "label": "OpenAI · GPT-4.1"},
        {"id": "google/gemini-2.5-pro", "label": "Google · Gemini 2.5 Pro"},
        {"id": "meta-llama/llama-3.3-70b-instruct", "label": "Meta · Llama 3.3 70B"},
        {"id": "moonshotai/kimi-k2", "label": "Moonshot · Kimi K2"},
    ],
    "auto": [],
    "deterministic": [{"id": "(none)", "label": "Deterministic stub — no model used"}],
}

# in-process cache (backend → (ts, items))
_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
_CACHE_TTL = 3600  # 1h


def _cache_get(key: str) -> List[Dict[str, Any]] | None:
    v = _CACHE.get(key)
    if not v:
        return None
    ts, items = v
    if time.time() - ts > _CACHE_TTL:
        return None
    return items


def _cache_set(key: str, items: List[Dict[str, Any]]) -> None:
    _CACHE[key] = (time.time(), items)


# ---- per-backend resolvers ----------------------------------------------
async def _claude_cli() -> List[Dict[str, Any]]:
    # parse `claude --help` if available; otherwise static
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "--help",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=4.0)
        except asyncio.TimeoutError:
            proc.kill()
            return STATIC["claude_cli"]
        text = (out or b"").decode(errors="replace").lower()
        # If `--model` is documented, surface aliases known to work.
        if "--model" in text:
            return STATIC["claude_cli"]
    except (FileNotFoundError, OSError):
        pass
    return STATIC["claude_cli"]


async def _openai_cli() -> List[Dict[str, Any]]:
    # `openai api models.list -o json` returns a JSON list when authenticated.
    try:
        proc = await asyncio.create_subprocess_exec(
            "openai", "api", "models.list",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        except asyncio.TimeoutError:
            proc.kill()
            return STATIC["openai_cli"]
        if proc.returncode != 0:
            return STATIC["openai_cli"]
        # The CLI's default output isn't reliably JSON; try, else fall back.
        try:
            data = json.loads(out.decode(errors="replace"))
            ids = []
            if isinstance(data, dict) and "data" in data:
                ids = [m["id"] for m in data["data"]]
            elif isinstance(data, list):
                ids = [m.get("id") for m in data if isinstance(m, dict)]
            ids = [i for i in ids if i]
            if ids:
                return [{"id": i, "label": i} for i in sorted(set(ids))]
        except Exception:
            pass
    except (FileNotFoundError, OSError):
        pass
    return STATIC["openai_cli"]


async def _anthropic() -> List[Dict[str, Any]]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return STATIC["anthropic"]
    try:
        import httpx  # type: ignore
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                },
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                items = [
                    {
                        "id": m.get("id"),
                        "label": m.get("display_name") or m.get("id"),
                    }
                    for m in data
                    if m.get("id")
                ]
                if items:
                    return items
    except Exception:
        logger.exception("anthropic models fetch failed")
    return STATIC["anthropic"]


async def _openrouter() -> List[Dict[str, Any]]:
    try:
        from skyn3t.core.openrouter_catalog import (
            get_catalog_async,
            is_sync_enabled,
            load_catalog,
        )

        if is_sync_enabled():
            snap = await get_catalog_async()
        else:
            snap = load_catalog()
        if snap.models:
            return [
                {
                    "id": m["id"],
                    "label": m.get("name") or m["id"],
                    "context_tokens": m.get("context_length"),
                }
                for m in snap.models
            ]
    except Exception:
        logger.exception("openrouter catalog load failed")
    return STATIC["openrouter"]


# ---- public ---------------------------------------------------------------
async def list_models(backend: str) -> List[Dict[str, Any]]:
    backend = (backend or "auto").lower()
    cached = _cache_get(backend)
    if cached is not None:
        return cached
    items: List[Dict[str, Any]]
    try:
        if backend == "claude_cli":
            items = await _claude_cli()
        elif backend == "openai_cli":
            items = await _openai_cli()
        elif backend == "anthropic":
            items = await _anthropic()
        elif backend == "openrouter":
            items = await _openrouter()
        else:
            items = list(STATIC.get(backend, []))
    except Exception:
        logger.exception("list_models error for %s", backend)
        items = list(STATIC.get(backend, []))
    _cache_set(backend, items)
    return items
