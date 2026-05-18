"""Token usage tracker.

Subscribes to LLM_EXCHANGE events and keeps two rollups:

  - per-agent: total prompt+response tokens, call count, last-used ts
  - per-project (slug): per-stage and total tokens

Token counts are estimated from character counts at 4 chars/token —
the standard rule for English, ±15% accurate. Good enough for a
"watch the budget" view. If precise counts matter later, we can swap
in tiktoken or count from the backend's reported usage (already
captured in openai_cli/anthropic backends).

Thread-safe; singleton via ``get_default_tracker()``.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skyn3t.observability.token_tracker")

# Standard approximation. Used by every major tokenizer guide as the
# fallback when you don't want to import tiktoken / anthropic.
_CHARS_PER_TOKEN = 4.0


def _estimate_tokens(text: Any) -> int:
    """Char-count approximation. Safe on None / non-strings."""
    if text is None:
        return 0
    s = text if isinstance(text, str) else str(text)
    if not s:
        return 0
    return max(1, int(len(s) / _CHARS_PER_TOKEN))


class TokenTracker:
    """In-memory token rollups, populated from LLM_EXCHANGE events."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # agent_name → {prompt, response, total, calls, last_used_at, backend, model}
        self._by_agent: Dict[str, Dict[str, Any]] = {}
        # project_slug → {prompt, response, total, calls, stages: {stage: {prompt, response, total, calls}}}
        self._by_project: Dict[str, Dict[str, Any]] = {}

    # ── event subscription ──────────────────────────────────────────

    def subscribe(self, event_bus: Any) -> None:
        """Wire this tracker to an event bus. Idempotent — safe to call
        multiple times, only subscribes once."""
        if getattr(self, "_subscribed", False):
            return
        try:
            from skyn3t.core.events import EventType
            event_bus.subscribe(self._on_exchange, EventType.LLM_EXCHANGE)
            self._subscribed = True
            logger.info("token tracker subscribed to LLM_EXCHANGE")
        except Exception:
            logger.exception("token tracker subscribe failed")

    def _on_exchange(self, event: Any) -> None:
        try:
            payload = getattr(event, "payload", {}) or {}
            # Prefer the accurate pre-truncation length fields if the
            # client provided them (llm_client.py now does). Fall back
            # to the legacy "len of truncated preview + bump constant"
            # estimate for older event sources.
            if "prompt_chars" in payload or "response_chars" in payload:
                prompt_chars = int(payload.get("prompt_chars") or 0)
                response_chars = int(payload.get("response_chars") or 0)
                system_chars = int(payload.get("system_chars") or 0)
            else:
                # Legacy path — only the truncated preview field
                # available. Best-effort estimate.
                prompt_chars = len(payload.get("prompt") or "")
                response_chars = len(payload.get("response") or "")
                system_chars = len(payload.get("system") or "")
                if prompt_chars >= 2000:
                    prompt_chars = max(prompt_chars, 8000)
                if response_chars >= 2000:
                    response_chars = max(response_chars, 4000)

            # Clamp anomalously large responses. CLIs sometimes dump
            # their entire agent-loop trace (tool calls, file reads,
            # search results) to stdout before the final code — we
            # saw 5MB+ per call on the dashboard, producing
            # nonsense per-build totals like 11.5M response tokens.
            # The clamp keeps the legitimate output range (LLMs cap
            # at ~32KB per response in practice) while flattening
            # the trace-dump cases. A future fix can extract just
            # the code body from CLI output before length-measuring.
            _RESPONSE_CHAR_CAP = 200_000  # ~50K tokens; well above any real LLM output
            _PROMPT_CHAR_CAP = 500_000    # ~125K tokens; covers context+system+files
            if response_chars > _RESPONSE_CHAR_CAP:
                response_chars = _RESPONSE_CHAR_CAP
            if prompt_chars > _PROMPT_CHAR_CAP:
                prompt_chars = _PROMPT_CHAR_CAP
            if system_chars > _PROMPT_CHAR_CAP:
                system_chars = _PROMPT_CHAR_CAP

            # Token estimate: chars/4 (industry-standard approximation).
            # We avoid the previous "build a string of N x-chars then
            # measure it" trick which allocated up to 200KB per call
            # just to do an integer division.
            prompt_tokens = max(1, prompt_chars // 4) if prompt_chars else 0
            prompt_tokens += max(0, system_chars // 4) if system_chars else 0
            response_tokens = max(1, response_chars // 4) if response_chars else 0
            total = prompt_tokens + response_tokens

            agent_name = payload.get("agent") or "unknown"
            project_slug = payload.get("project_slug")
            stage = payload.get("project_stage") or "unknown"
            backend = payload.get("backend") or ""
            model = payload.get("model") or ""

            with self._lock:
                # ── per-agent ─────────────────────────────
                a = self._by_agent.setdefault(agent_name, {
                    "agent": agent_name,
                    "prompt_tokens": 0,
                    "response_tokens": 0,
                    "total_tokens": 0,
                    "calls": 0,
                    "last_used_at": 0.0,
                    "backend": backend,
                    "model": model,
                })
                a["prompt_tokens"] += prompt_tokens
                a["response_tokens"] += response_tokens
                a["total_tokens"] += total
                a["calls"] += 1
                a["last_used_at"] = time.time()
                # Keep the most-recent backend/model — agents can be
                # reconfigured at runtime.
                if backend:
                    a["backend"] = backend
                if model:
                    a["model"] = model

                # ── per-project ───────────────────────────
                if project_slug:
                    p = self._by_project.setdefault(project_slug, {
                        "slug": project_slug,
                        "prompt_tokens": 0,
                        "response_tokens": 0,
                        "total_tokens": 0,
                        "calls": 0,
                        "first_seen_at": time.time(),
                        "last_used_at": 0.0,
                        "stages": {},
                    })
                    p["prompt_tokens"] += prompt_tokens
                    p["response_tokens"] += response_tokens
                    p["total_tokens"] += total
                    p["calls"] += 1
                    p["last_used_at"] = time.time()
                    s = p["stages"].setdefault(stage, {
                        "stage": stage,
                        "prompt_tokens": 0,
                        "response_tokens": 0,
                        "total_tokens": 0,
                        "calls": 0,
                        "by_agent": {},
                    })
                    s["prompt_tokens"] += prompt_tokens
                    s["response_tokens"] += response_tokens
                    s["total_tokens"] += total
                    s["calls"] += 1
                    by_a = s["by_agent"].setdefault(agent_name, 0)
                    s["by_agent"][agent_name] = by_a + total
        except Exception:
            logger.exception("token tracker event handler failed")

    # ── read API ────────────────────────────────────────────────────

    def per_agent(self) -> List[Dict[str, Any]]:
        with self._lock:
            return sorted(
                (dict(v) for v in self._by_agent.values()),
                key=lambda x: x["total_tokens"],
                reverse=True,
            )

    def per_project(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows: List[Dict[str, Any]] = []
            for p in self._by_project.values():
                # Stages dict → sorted list for stable UI rendering.
                stages_list = sorted(
                    (dict(s) for s in p["stages"].values()),
                    key=lambda x: x["total_tokens"],
                    reverse=True,
                )
                rows.append({**p, "stages": stages_list})
            rows.sort(key=lambda x: x["last_used_at"], reverse=True)
            return rows

    def for_project(self, slug: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            p = self._by_project.get(slug)
            if not p:
                return None
            stages_list = sorted(
                (dict(s) for s in p["stages"].values()),
                key=lambda x: x["total_tokens"],
                reverse=True,
            )
            return {**p, "stages": stages_list}

    def totals(self) -> Dict[str, Any]:
        with self._lock:
            total = 0
            calls = 0
            for a in self._by_agent.values():
                total += a["total_tokens"]
                calls += a["calls"]
            return {
                "total_tokens": total,
                "total_calls": calls,
                "agents_tracked": len(self._by_agent),
                "projects_tracked": len(self._by_project),
            }


_DEFAULT: Optional[TokenTracker] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_tracker() -> TokenTracker:
    """Singleton accessor. Lazily created on first call."""
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                _DEFAULT = TokenTracker()
    return _DEFAULT
