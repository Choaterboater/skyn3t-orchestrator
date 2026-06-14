from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from skyn3t.core.event_context import current_event_context, current_event_correlation_id
from skyn3t.security.secrets import redact_text

logger = logging.getLogger("skyn3t.adapters.llm_client")


# Module-level fallback event bus. The first agent that creates an LLMClient
# with an event_bus seeds this for all later ad-hoc constructions, so even
# clients built outside the BaseAgent.llm path can publish LLM_EXCHANGE events.
_default_event_bus = None

# Module-level fallback RAG. Mirrors the event_bus pattern: the first agent
# that creates an LLMClient with a `rag=` kwarg seeds this for all later
# ad-hoc constructions, so provider-aware prompt augmentation works even when
# callers don't thread RAG through every code path.
_default_rag = None

# Provider-qualified model-id prefixes that are valid only on a specific
# metered/API backend (OpenRouter, Anthropic, OpenAI, ...). Kept for
# documentation / observability; the actual drop logic below is allow-list
# based so we catch *any* new publisher prefix (e.g. xiaomi/, moonshotai/)
# without having to enumerate every OpenRouter vendor.
_CROSS_PROVIDER_MODEL_PREFIXES = (
    "openrouter/",
    "anthropic/",
    "openai/",
    "google/",
    "meta-llama/",
    "mistralai/",
    "deepseek/",
    "qwen/",
    "nvidia/",
    "tencent/",
    "stepfun/",
    "xai/",
    "xiaomi/",
)

# "/"-qualified model ids that ARE valid on a local CLI backend and must be
# preserved across failover. The kimi CLI's managed model is the only known
# CLI-local id that embeds a slash; everything else with a slash is a
# provider-qualified id (OpenRouter/API) that a CLI backend would reject.
_CLI_LOCAL_MODEL_PREFIXES = (
    "kimi-code/",
)


def _try_register_default(eb) -> None:
    global _default_event_bus
    if eb is not None and _default_event_bus is None:
        _default_event_bus = eb


def _try_register_default_rag(rag) -> None:
    global _default_rag
    if rag is not None and _default_rag is None:
        _default_rag = rag


def install_default_event_bus(bus) -> None:
    """Explicit setter for the module-level fallback event bus."""
    global _default_event_bus
    _default_event_bus = bus


def install_default_rag(rag) -> None:
    """Explicit setter for the module-level fallback RAG instance."""
    global _default_rag
    _default_rag = rag


def _drop_cross_provider_model_name(model: Optional[str]) -> Optional[str]:
    """Drop provider-qualified model IDs before cross-backend failover.

    ``openrouter/foo``, ``anthropic/bar`` or ``xiaomi/baz`` are valid only for
    the metered/API backend that publishes them. When we retry on a different
    backend (typically a local CLI), passing them through makes the second
    backend fail immediately on model validation instead of using its own
    default model — which silently degrades to the deterministic stub.

    Allow-list approach: any "/"-qualified id is dropped UNLESS it matches a
    known CLI-local prefix (``kimi-code/``). This catches new OpenRouter
    publisher prefixes (xiaomi/, moonshotai/, ...) without enumerating them,
    while preserving the lone CLI-local slashed id.
    """
    if not model:
        return None
    lower = model.lower()
    if lower.startswith(_CLI_LOCAL_MODEL_PREFIXES):
        return model
    return None if "/" in model else model


@dataclass
class LLMRequest:
    prompt: str
    system: Optional[str] = None
    model: Optional[str] = None     # override default
    # Default 4000: SkyN3t is overwhelmingly operated on subscription-backed
    # CLIs (claude/copilot/kimi) where there's no per-token cost and a small
    # cap mostly just truncates good output. The CLI backends ignore this
    # value entirely; only metered API backends (Anthropic API, OpenRouter,
    # OpenAI direct) actually apply it.
    max_tokens: int = 4000
    temperature: float = 0.4
    metadata: dict = field(default_factory=dict)
    # When set, CLI backends will use this directory as their subprocess
    # CWD instead of an empty per-call sandbox. Use case: reviewer / QA
    # callers need the LLM CLI's tool calls (Read/glob) to see real
    # scaffold files; otherwise the LLM "sees" an empty directory and
    # reports "scaffold root is empty" as a blocker. Caller is responsible
    # for ensuring the path exists. CLI artifact harvesting is skipped
    # when this is set so we never pollute the caller's directory.
    cwd: Optional[str] = None


# Subprocess timeouts (seconds)
_AVAILABLE_TIMEOUT = 3.0
# Hard ceiling on total CLI wall time. Kimi streaming a 15k-token
# .jsx file legitimately approaches 10min; 600s tripped on v34 with
# zero recovery. 1200s gives slow streamers headroom; idle-timeout
# below catches truly-hung calls without waiting for the hard cap.
_COMPLETE_TIMEOUT = 1200.0
# Idle timeout: if the CLI hasn't emitted ANY output for this long,
# treat it as hung and kill it. Real generation emits tokens
# continuously, so 180s of silence ≈ stuck. This catches hangs much
# faster than _COMPLETE_TIMEOUT alone, while still letting a slow
# streamer make progress on a long file.
# Bumped to 240s after repo-side buffering fixes (PYTHONUNBUFFERED)
# because some CLI runtimes still exhibit occasional multi-minute
# pauses on very large codegen tasks.
_IDLE_TIMEOUT = 240.0

_DESIGN_ROUTING_CALLERS = {
    "brainstorm",
    "designer",
    "marketer",
    "business_analyst",
    "writer",
}
_CODE_ROUTING_CALLERS = {
    "code",
    "code_agent",
    "code_improver",
    "build_fix",
    "integration_fix",
    "consistency_fix",
    "code_critique_fix",
    "vite_dryrun_fix",
}


def _settings_fallbacks() -> dict[str, Optional[str]]:
    try:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
        return {
            "llm_backend": getattr(settings, "llm_backend", None),
            "llm_model": getattr(settings, "llm_model", None),
            "anthropic_api_key": getattr(settings, "anthropic_api_key", None),
            "openrouter_api_key": getattr(settings, "openrouter_api_key", None),
        }
    except Exception:
        return {
            "llm_backend": None,
            "llm_model": None,
            "anthropic_api_key": None,
            "openrouter_api_key": None,
        }


# ── Exact-match response cache ───────────────────────────────────────────────
# Deterministic (temperature==0) completions are replayed verbatim on retries,
# re-runs, and parallel agents. Caching them short-circuits duplicate network
# spend — the cheapest cost win for the cheap/free directive. Bounded FIFO.
_RESPONSE_CACHE: Dict[str, str] = {}
_RESPONSE_CACHE_ORDER: List[str] = []
_RESPONSE_CACHE_MAX = 512


def _response_cache_enabled() -> bool:
    return os.environ.get("SKYN3T_LLM_RESPONSE_CACHE", "1").strip().lower() not in {
        "0", "off", "false", "no",
    }


def _response_cache_key(
    backend: Any, model: Any, system: Any, prompt: Any,
    max_tokens: Any, temperature: Any,
) -> str:
    import hashlib

    h = hashlib.sha256()
    for part in (str(backend or ""), str(model or ""), str(system or ""),
                 str(prompt or ""), str(max_tokens), str(temperature)):
        h.update(part.encode("utf-8", "ignore"))
        h.update(b"\x00")
    return h.hexdigest()


def _response_cache_get(key: str) -> Optional[str]:
    return _RESPONSE_CACHE.get(key)


def _response_cache_put(key: str, value: str) -> None:
    if key in _RESPONSE_CACHE:
        return
    _RESPONSE_CACHE[key] = value
    _RESPONSE_CACHE_ORDER.append(key)
    if len(_RESPONSE_CACHE_ORDER) > _RESPONSE_CACHE_MAX:
        _RESPONSE_CACHE.pop(_RESPONSE_CACHE_ORDER.pop(0), None)


class LLMClient:
    """Unified LLM facade with graceful fallbacks.

    Subscription-first backend resolution. Local CLIs (claude, kimi, ...) are
    tried before metered API keys, so users on Claude Pro/Max or other
    subscription plans aren't billed against a metered API key by default.

    Backend selection is driven by env vars:
      - SKYN3T_LLM_BACKEND=auto (default): try local CLIs first, then API keys.
      - SKYN3T_LLM_BACKEND=claude_cli | kimi_cli | copilot_cli | openai_cli
                          | anthropic | openrouter | deterministic
    Fallback: deterministic stub.
    """

    # All explicit backend names we know about
    _EXPLICIT_BACKENDS = {
        "claude_cli", "kimi_cli", "copilot_cli", "openai_cli",
        "anthropic", "openrouter", "deterministic",
    }

    def __init__(self, *, default_model: Optional[str] = None,
                 backend: Optional[str] = None,
                 anthropic_api_key: Optional[str] = None,
                 openrouter_api_key: Optional[str] = None,
                 event_bus: Optional[Any] = None,
                 caller_name: Optional[str] = None,
                 rag: Optional[Any] = None,
                 skip_backends: Optional[List[str]] = None,
                 backend_is_policy: bool = False,
                 routing_hint: Optional[str] = None):
        settings_fallbacks = _settings_fallbacks()
        self.default_model = (
            default_model
            or os.environ.get("SKYN3T_LLM_MODEL")
            or settings_fallbacks.get("llm_model")
        )
        self._backend_name = (
            backend
            or os.environ.get("SKYN3T_LLM_BACKEND")
            or settings_fallbacks.get("llm_backend")
            or "auto"
        ).lower()
        # Global failover: when the OpenRouter key is exhausted (HTTP 403 "Key
        # limit exceeded"), SKYN3T_LLM_FORCE_CLAUDE_CLI=1 reroutes every
        # OpenRouter/auto caller to the authenticated Claude Code CLI so the
        # system keeps running. Reversible — unset once OpenRouter has budget.
        if self._backend_name in {"openrouter", "auto"} and os.environ.get(
            "SKYN3T_LLM_FORCE_CLAUDE_CLI", ""
        ).strip().lower() in {"1", "true", "yes", "on"}:
            self._backend_name = "claude_cli"
            # An OpenRouter-style model id ("vendor/model") is meaningless to
            # the Claude CLI — fall back to its sonnet default.
            if self.default_model and "/" in self.default_model:
                self.default_model = "sonnet"
        # No-Claude / OpenRouter-only policy (owner 2026-06-12): never use Claude
        # — CLI or API — and never the other subscription CLIs (copilot/kimi/
        # openai) with their stale pinned models. Even when a caller passes an
        # explicit CLI backend (agent fallback chains, e.g.
        # CodeImprover.DEFAULT_FALLBACK_CHAIN, do), coerce to OpenRouter so NO
        # path can spawn `claude -p`, hit the Anthropic API, or use an old model.
        # Single chokepoint every backend flows through. Reversible via
        # SKYN3T_NO_CLAUDE=0.
        if os.environ.get("SKYN3T_NO_CLAUDE", "").strip().lower() in {
            "1", "true", "yes", "on"
        } and self._backend_name in {
            "claude_cli", "anthropic", "copilot_cli", "kimi_cli", "openai_cli", "auto"
        }:
            self._backend_name = "openrouter"
            # A bare CLI model id ("sonnet"/"opus"/"haiku"/"gpt-5.4-mini") is
            # meaningless to OpenRouter — drop it so a live catalog model loads.
            if self.default_model and "/" not in self.default_model:
                self.default_model = None
        # Free-first default: a model-less OpenRouter caller (the consistency /
        # targeted-fix repair loop, etc.) would otherwise hit the backend's PAID
        # default model and 403 on the over-limit key — which is why generated
        # TODO stubs never got repaired. Default it to a free catalog model.
        if (
            not self.default_model
            and self._backend_name == "openrouter"
            and os.environ.get("SKYN3T_NO_CLAUDE", "").strip().lower()
            in {"1", "true", "yes", "on"}
        ):
            try:
                from skyn3t.core.openrouter_catalog import pick_free_model

                self.default_model = pick_free_model()
            except Exception:
                pass
        # Cross-model debate: callers can list backends to skip (e.g. the
        # retry path passes the backend the prior attempt used). The auto
        # chain then naturally falls through to a different model.
        self._skip_backends: set = set(skip_backends or [])
        self._anthropic_key = (
            anthropic_api_key
            or os.environ.get("ANTHROPIC_API_KEY")
            or settings_fallbacks.get("anthropic_api_key")
        )
        self._openrouter_key = (
            openrouter_api_key
            or os.environ.get("OPENROUTER_API_KEY")
            or settings_fallbacks.get("openrouter_api_key")
        )
        self._impl: Optional[Any] = None  # lazy
        # Seed module fallback if this is the first explicit bus we've seen.
        _try_register_default(event_bus)
        # Same pattern for RAG: first explicit instance becomes the fallback
        # for any subsequent ad-hoc LLMClient constructions.
        _try_register_default_rag(rag)
        # Resolve final bus: explicit > module fallback. caller_name is
        # used in published LLM_EXCHANGE events so dashboards can attribute
        # prompts/responses to the requesting agent.
        self._event_bus = event_bus or _default_event_bus
        self._caller_name = caller_name
        # Resolve final RAG: explicit > module fallback. Used by complete()
        # to retrieve provider-aware doc snippets and prepend them to the
        # system prompt.
        self._rag = rag or _default_rag
        self._last_failed_backend: Optional[str] = None
        self._backend_is_policy = bool(backend_is_policy)
        self._routing_hint = self._normalize_routing_hint(routing_hint) or self._infer_routing_hint(
            caller_name
        )

    @staticmethod
    def _normalize_routing_hint(hint: Optional[str]) -> Optional[str]:
        value = (hint or "").strip().lower()
        if value in {"design", "code"}:
            return value
        return None

    @staticmethod
    def _infer_routing_hint(caller_name: Optional[str]) -> Optional[str]:
        caller = (caller_name or "").strip().lower()
        if caller in _DESIGN_ROUTING_CALLERS:
            return "design"
        if caller in _CODE_ROUTING_CALLERS:
            return "code"
        return None

    def _auto_cli_order(self) -> list[str]:
        # claude_cli is the ONLY CLI in the auto chain: it is the one CLI
        # reliably authenticated on this box (Claude Code subscription,
        # zero marginal cost). copilot/kimi pass the --version probe but
        # are NOT logged in, so every attempt burned a multi-minute
        # timeout before failover — this stalled server startup ~3min.
        # openai_cli needs an API key we don't set. Re-add a CLI here
        # ONLY after `<cli> -p "ok"` works in a fresh shell.
        return ["claude_cli"]

    async def _try_named_backend(self, name: str) -> bool:
        if name in getattr(self, "_skip_backends", ()):
            return False
        if name == "claude_cli":
            return await self._try_cli("claude_cli", _ClaudeCLIBackend)
        if name == "kimi_cli":
            return await self._try_cli("kimi_cli", _KimiCLIBackend)
        if name == "copilot_cli":
            return await self._try_cli("copilot_cli", _CopilotCLIBackend)
        if name == "openai_cli":
            return await self._try_cli("openai_cli", _OpenAICLIBackend)
        if name == "anthropic":
            try:
                self._impl = _AnthropicBackend(self._anthropic_key)
                self._backend_name = "anthropic"
                return True
            except Exception:
                logger.warning("anthropic backend init failed", exc_info=True)
                return False
        if name == "openrouter":
            try:
                from skyn3t.adapters.openrouter import OpenRouterBackend
                self._impl = OpenRouterBackend(self._openrouter_key)
                self._backend_name = "openrouter"
                return True
            except Exception:
                logger.warning("openrouter backend init failed", exc_info=True)
                return False
        if name == "deterministic":
            self._impl = _DeterministicBackend()
            self._backend_name = "deterministic"
            return True
        return False

    async def _resolve_auto_backend(self):
        if os.environ.get("SKYN3T_NO_CLAUDE", "").strip().lower() in {
            "1", "true", "yes", "on"
        }:
            if self._openrouter_key and await self._try_named_backend("openrouter"):
                return self._impl
            self._impl = _DeterministicBackend()
            self._backend_name = "deterministic"
            return self._impl

        # "Not claude at all for coding": code-generation callers resolve
        # to the OpenRouter API first — the Claude subscription is
        # reserved for reasoning stages (planner/architect/reviewer).
        # claude_cli stays as the LAST resort for code callers so a
        # transient OpenRouter outage degrades to a real model instead
        # of the deterministic stub.
        if self._routing_hint == "code":
            if self._openrouter_key and await self._try_named_backend("openrouter"):
                return self._impl
            for name in self._auto_cli_order():
                if await self._try_named_backend(name):
                    logger.warning(
                        "code caller %s using %s — OpenRouter unavailable",
                        self._caller_name or "llm", self._backend_name,
                    )
                    return self._impl
            self._impl = _DeterministicBackend()
            self._backend_name = "deterministic"
            return self._impl
        for name in self._auto_cli_order():
            if await self._try_named_backend(name):
                return self._impl
        if self._anthropic_key and await self._try_named_backend("anthropic"):
            return self._impl
        if self._openrouter_key and await self._try_named_backend("openrouter"):
            return self._impl
        self._impl = _DeterministicBackend()
        self._backend_name = "deterministic"
        return self._impl

    async def aclose(self) -> None:
        """Release any resources held by the backing implementation.

        Some backends (notably OpenRouter) own an httpx.AsyncClient that
        must be closed to avoid `Unclosed connection` warnings + connection
        pool leaks. Safe to call multiple times.
        """
        impl = self._impl
        if impl is None:
            return
        closer = getattr(impl, "aclose", None)
        if closer is None:
            return
        try:
            await closer()
        except Exception:
            logger.debug("LLMClient aclose failed", exc_info=True)

    async def complete(self, prompt: str, *, system: Optional[str] = None,
                       model: Optional[str] = None, max_tokens: int = 4000,
                       temperature: float = 0.4, timeout: Optional[float] = None,
                       cwd: Optional[str] = None,
                       _allow_backend_failover: bool = True) -> str:
        self._last_failed_backend = None
        req = LLMRequest(prompt=prompt, system=system, model=model or self.default_model,
                         max_tokens=max_tokens, temperature=temperature, cwd=cwd)
        elapsed = 0.0
        # Optional context compression (env-gated, default off) — shrink noisy
        # RAG/scrape/log payloads before the call. Done before the cache key so
        # identical compressed prompts still cache-hit.
        try:
            from skyn3t.adapters.context_compressor import compress as _compress

            req.prompt, req.system = _compress(req.prompt, req.system)
        except Exception:
            pass
        # Exact-match cache: only deterministic calls (temp==0), so we never
        # short-circuit intentional sampling (self-consistency / varied retries).
        _cache_key: Optional[str] = None
        if temperature == 0 and _response_cache_enabled():
            _cache_key = _response_cache_key(
                self._backend_name, req.model, req.system, req.prompt,
                max_tokens, temperature,
            )
            cached = _response_cache_get(_cache_key)
            if cached is not None:
                return cached
        try:
            impl = await self._get_impl()
            # Provider-aware augmentation: prepend retrieved docs notes to the
            # system prompt so the active backend gets formatting hints tailored
            # to its provider. Done after _get_impl() so self._backend_name has
            # resolved out of "auto" if needed.
            try:
                from skyn3t.adapters.prompt_builder import augmentation_for
                rag = self._rag if self._rag is not None else _default_rag
                aug = await augmentation_for(self._backend_name, rag) if rag is not None else ""
                if aug:
                    req.system = aug + "\n\n" + req.system if req.system else aug
            except Exception:
                pass
            start = time.monotonic()
            if timeout is not None:
                out = cast(str, await asyncio.wait_for(impl.complete(req), timeout=timeout))
            else:
                out = cast(str, await impl.complete(req))
            elapsed = time.monotonic() - start
            if (
                _cache_key is not None
                and out
                and (self._backend_name or "") != "deterministic"
            ):
                _response_cache_put(_cache_key, out)
        except Exception as e:
            logger.warning(
                "llm complete failed; caller=%s backend=%s error=%s",
                self._caller_name or "llm",
                self._backend_name or "unknown",
                str(e).strip() or type(e).__name__,
            )
            failed_backend = (self._backend_name or "").strip().lower()
            self._last_failed_backend = failed_backend or None
            if (
                _allow_backend_failover
                and failed_backend
                and failed_backend != "deterministic"
            ):
                failover_default_model = _drop_cross_provider_model_name(self.default_model)
                failover_explicit_model = _drop_cross_provider_model_name(model)
                try:
                    retry_client = LLMClient(
                        default_model=failover_default_model,
                        backend=None,
                        anthropic_api_key=self._anthropic_key,
                        openrouter_api_key=self._openrouter_key,
                        event_bus=self._event_bus,
                        caller_name=self._caller_name,
                        rag=self._rag,
                        skip_backends=sorted(self._skip_backends | {failed_backend}),
                        routing_hint=self._routing_hint,
                    )
                    logger.info(
                        "llm failover retry; caller=%s skip_backends=%s",
                        self._caller_name or "llm",
                        sorted(self._skip_backends | {failed_backend}),
                    )
                    return await retry_client.complete(
                        prompt,
                        system=system,
                        model=failover_explicit_model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        timeout=timeout,
                        cwd=cwd,
                        _allow_backend_failover=False,
                    )
                except Exception:
                    logger.warning("llm failover retry failed", exc_info=True)
            return self._fallback(req)

        # Publish an LLM_EXCHANGE event for dashboards/observability. We
        # truncate prompt/response to keep payloads compact; failures here
        # must never break the LLM call so everything is wrapped in try/except.
        if self._event_bus is not None:
            try:
                from skyn3t.core.events import Event, EventType
                et = getattr(EventType, "LLM_EXCHANGE", None)
                if et is not None:
                    context = current_event_context()
                    # Preview fields (truncated, redacted) for the
                    # dashboard's LLM-exchange log — humans skim these,
                    # they don't need full 30KB payloads.
                    prompt_trunc = redact_text((req.prompt or "")[:2000])
                    response_trunc = redact_text((out or "")[:2000])
                    system_trunc = redact_text((req.system or "")[:500]) if req.system else ""
                    # Accurate length fields for the token tracker —
                    # the truncated preview field made the dashboard
                    # report cap-times-bump constants instead of real
                    # token counts. These are pre-truncation lengths
                    # in characters (no redaction effect on length).
                    prompt_chars_full = len(req.prompt or "")
                    response_chars_full = len(out or "")
                    system_chars_full = len(req.system or "")
                    usage_fields: Dict[str, int] = {}
                    impl = getattr(self, "_impl", None)
                    last_usage = getattr(impl, "_last_usage", None)
                    if isinstance(last_usage, dict):
                        pt = int(last_usage.get("prompt_tokens") or 0)
                        rt = int(last_usage.get("response_tokens") or 0)
                        if pt or rt:
                            usage_fields = {
                                "prompt_tokens": pt,
                                "response_tokens": rt,
                                "total_tokens": pt + rt,
                            }
                    self._event_bus.publish(Event(
                        event_type=et,
                        source=self._caller_name or "llm",
                        payload={
                            "agent": self._caller_name or "llm",
                            "backend": self._backend_name,
                            "model": req.model or self.default_model or "",
                            "prompt": prompt_trunc,
                            "response": response_trunc,
                            "system": system_trunc,
                            "prompt_chars": prompt_chars_full,
                            "response_chars": response_chars_full,
                            "system_chars": system_chars_full,
                            "duration_ms": int(elapsed * 1000),
                            **usage_fields,
                            **{
                                key: value
                                for key, value in context.items()
                                if key in {"project_slug", "project_stage", "project_template", "task_id"}
                                and value is not None
                            },
                        },
                        correlation_id=current_event_correlation_id(),
                    ))
            except Exception:
                pass

        return out

    @property
    def backend(self) -> str:
        return self._backend_name

    def describe(self) -> dict:
        """Return current backend info for dashboards / observability."""
        return {"backend": self._backend_name, "default_model": self.default_model}

    async def _try_cli(self, name: str, cls) -> bool:
        """Instantiate `cls` and check availability; cache and return True on success.

        Honors the per-instance ``_skip_backends`` set so a caller (e.g. the
        runner's retry path) can route around a backend that just failed —
        the cross-model debate pattern. The skip list is consulted BEFORE
        the subprocess probe so we don't pay startup cost on a backend we're
        going to discard anyway.
        """
        if name in getattr(self, "_skip_backends", ()):
            return False
        try:
            backend = cls()
            if await backend.available():
                self._impl = backend
                self._backend_name = name
                return True
        except Exception:
            logger.warning("%s backend init failed", name, exc_info=True)
        return False

    async def _get_impl(self):
        if self._impl is not None:
            return self._impl

        name = self._backend_name

        # ------------------------------------------------------------------
        # 1. Explicit backend selection — honor exactly what the user asked for.
        # ------------------------------------------------------------------
        if name in self._EXPLICIT_BACKENDS:
            if await self._try_named_backend(name):
                return self._impl
            if self._backend_is_policy and name != "deterministic":
                self._skip_backends.add(name)
                self._backend_name = "auto"
                return await self._resolve_auto_backend()
            self._impl = _DeterministicBackend()
            self._backend_name = "deterministic"
            return self._impl

        # ------------------------------------------------------------------
        # 2. Auto: subscription-CLIs first, then API keys, then deterministic.
        # ------------------------------------------------------------------
        return await self._resolve_auto_backend()

    def _fallback(self, req: LLMRequest) -> str:
        return _DeterministicBackend.synthesize(req.prompt, req.system)


class _DeterministicBackend:
    async def complete(self, req: LLMRequest) -> str:
        return self.synthesize(req.prompt, req.system)

    @staticmethod
    def synthesize(prompt: str, system: Optional[str]) -> str:
        # extract a few keywords; emit a structured "stub" that downstream agents can still consume
        head = (prompt or "").strip().splitlines()[0][:160] if prompt else ""
        return (
            "[deterministic-stub]\n"
            f"context: {head}\n"
            "thoughts: working without an LLM backend; returning a minimal scaffold.\n"
            "set ANTHROPIC_API_KEY, install `claude` CLI, or set OPENROUTER_API_KEY for real generation."
        )


# ---------------------------------------------------------------------------
# CLI subprocess backends.
# Each implements:
#   async def available(self) -> bool      # quick `<bin> --version` probe
#   async def complete(self, req) -> str    # full completion via subprocess
# Errors propagate to LLMClient.complete which catches and falls back.
# ---------------------------------------------------------------------------


async def _probe_version(binary: str) -> bool:
    """Return True if `<binary> --version` exits 0 within _AVAILABLE_TIMEOUT."""
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError):
        return False
    try:
        await asyncio.wait_for(proc.wait(), timeout=_AVAILABLE_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        # H16: reap the child after killing it to avoid zombie processes.
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        return False
    except (FileNotFoundError, OSError):
        return False
    return proc.returncode == 0


def _make_llm_cli_sandbox_cwd() -> str:
    """Return a fresh sandbox directory for one LLM CLI subprocess call.

    Without an explicit cwd, ``claude``/``kimi``/``copilot``/``openai`` CLIs
    inherit the backend's CWD — which is the SkyN3t repo root in
    production. Some CLIs (notably ``claude -p``) can use Edit/Write tools
    that operate relative to CWD, leaking generated files (web/, server/,
    src/) into the repo. Pin every CLI subprocess to a sandbox dir so the
    blast radius is /tmp, not the source tree.

    A fresh dir per call (vs. a process-wide singleton) means concurrent
    calls can't double-harvest each other's artifacts, and the dir gets
    cleaned up in a finally block once we're done.
    """
    import tempfile
    return tempfile.mkdtemp(prefix="skyn3t-llm-cwd-")


def _normalize_sandbox_relpath(sandbox_root: Path, file_path: Path) -> str:
    """Normalize a harvested sandbox file path to a scaffold-relative path."""
    rel = file_path.relative_to(sandbox_root).as_posix().lstrip("/")
    # Some CLIs create files under a top-level "sandbox/" folder. Strip it
    # so route/file matching can align with scaffold-relative plan paths.
    if rel.startswith("sandbox/"):
        rel = rel[len("sandbox/") :]
    return rel


def _collect_sandbox_artifacts(sandbox_cwd: str, started_at: float) -> list[tuple[str, str]]:
    """Harvest text files created/updated during the current CLI call."""
    root = Path(sandbox_cwd)
    artifacts: list[tuple[str, str]] = []
    if not root.exists():
        return artifacts

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < started_at:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = _normalize_sandbox_relpath(root, path)
        if not rel:
            continue
        artifacts.append((rel, text))

    artifacts.sort(key=lambda it: it[0])
    return artifacts


def _append_sandbox_artifacts(stdout_text: str, artifacts: list[tuple[str, str]]) -> str:
    """Attach harvested artifacts to stdout in a parser-friendly marker format."""
    text = stdout_text.strip()
    if not artifacts:
        return text
    if not text and len(artifacts) == 1:
        # Per-file generation path: if a single file was generated via
        # sandbox write and stdout is empty, return the raw file body.
        return artifacts[0][1].strip()

    sections: list[str] = []
    for rel, body in artifacts:
        sections.append(f"// === {rel} ===\n{body.strip()}")
    merged = "\n\n".join(sections).strip()
    if not text:
        return merged
    return f"{text}\n\n{merged}"


async def _run_capture(args: list[str], cwd: Optional[str] = None) -> str:
    """Run a subprocess and return its stdout (raises on non-zero).

    Uses two-tier timeout:
      * IDLE: if no bytes arrive on stdout+stderr for _IDLE_TIMEOUT, kill.
        Real generation emits tokens continuously; long silence ≈ hang.
      * HARD: total wall time can't exceed _COMPLETE_TIMEOUT.

    The stream-then-idle pattern matters for Kimi, which can take
    8–15min on a single large file but never stops emitting for more
    than ~30s while it's actually working. A flat 600s cap killed v34.

    Buffering hardening:
      * PYTHONUNBUFFERED=1 is injected for Python-based CLIs (kimi, openai).
      * stdbuf -oL -eL is prepended on Unix to force line-buffering at
        the C stdio level as a safety net for other runtimes.
      * Idle detection watches BOTH stdout and stderr so progress
        logged to either stream resets the idle timer.

    CWD:
      * If ``cwd`` is supplied and exists, the subprocess runs there and
        artifact harvesting is skipped. Use for reviewer/QA callers that
        need the CLI's Read/glob tools to see the real scaffold.
      * Otherwise a fresh /tmp sandbox is created so code-generating
        CLI tools can't leak into the SkyN3t repo root.
    """
    import shutil as _sh
    import sys as _sys
    start_wall = time.time()
    use_caller_cwd = cwd is not None and Path(cwd).is_dir()
    if cwd and not use_caller_cwd:
        logger.warning(
            "LLM caller-provided cwd does not exist; falling back to sandbox. cwd=%r",
            cwd,
        )
    # When use_caller_cwd is True, cwd is guaranteed non-None
    # (the predicate is `cwd is not None and Path(cwd).is_dir()`).
    # Annotated explicitly so the downstream calls (harvest, rmtree)
    # don't get flagged as Optional[str].
    sandbox_cwd: str = cwd if (use_caller_cwd and cwd is not None) else _make_llm_cli_sandbox_cwd()

    # Build subprocess env with Python unbuffered mode — critical for
    # Python-based CLIs (kimi, openai) that otherwise block-buffer
    # stdout when connected to a pipe, easily exceeding the idle window.
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"

    # On Unix, prepend stdbuf to force line-buffering at the C stdio
    # level. This is a best-effort safety net for non-Python CLIs.
    # Guard on shutil.which: some hosts (notably macOS without coreutils)
    # lack stdbuf, and prepending a missing binary makes EVERY completion
    # fail with FileNotFoundError AFTER the version probe already passed —
    # a silent slide into the deterministic stub.
    exec_args = list(args)
    if _sys.platform != "win32" and _sh.which("stdbuf"):
        exec_args = ["stdbuf", "-oL", "-eL"] + exec_args

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *exec_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=sandbox_cwd,
            env=env,
        )

        stdout_buf: list[bytes] = []
        stderr_buf: list[bytes] = []
        start = asyncio.get_event_loop().time()

        async def _drain(stream, sink):
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                sink.append(chunk)

        stdout_task = asyncio.create_task(_drain(proc.stdout, stdout_buf))
        stderr_task = asyncio.create_task(_drain(proc.stderr, stderr_buf))

        def _total_len(bufs):
            return sum(len(c) for c in bufs)

        try:
            while True:
                elapsed = asyncio.get_event_loop().time() - start
                if elapsed >= _COMPLETE_TIMEOUT:
                    raise asyncio.TimeoutError(
                        f"hard timeout after {int(elapsed)}s"
                    )
                prev_len = _total_len(stdout_buf) + _total_len(stderr_buf)
                try:
                    # Wait either for proc to exit or for the idle window
                    # to elapse. We re-check progress each cycle.
                    await asyncio.wait_for(
                        proc.wait(),
                        timeout=min(_IDLE_TIMEOUT, _COMPLETE_TIMEOUT - elapsed),
                    )
                    break  # process exited
                except asyncio.TimeoutError:
                    new_len = _total_len(stdout_buf) + _total_len(stderr_buf)
                    if new_len == prev_len:
                        # No new bytes in the idle window → hung.
                        raise asyncio.TimeoutError(
                            f"idle timeout ({int(_IDLE_TIMEOUT)}s no output)"
                        )
                    # Made progress; reset and keep waiting.
                    continue
        except asyncio.TimeoutError:
            raise
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
            # Make sure drains finish flushing whatever's left in the pipes.
            for t in (stdout_task, stderr_task):
                if not t.done():
                    t.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task, return_exceptions=True),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                pass

        stdout = b"".join(stdout_buf)
        stderr = b"".join(stderr_buf)
        if proc.returncode != 0:
            raise RuntimeError(
                f"{args[0]} cli failed: {stderr.decode(errors='replace')[:300]}"
            )
        stdout_text = stdout.decode(errors="replace")
        # Skip artifact harvest when running in a caller-supplied cwd —
        # we don't own that directory and must not move files out of it.
        if use_caller_cwd:
            return stdout_text
        artifacts = _collect_sandbox_artifacts(sandbox_cwd, started_at=start_wall)
        if artifacts:
            logger.info(
                "harvested %d sandbox artifact(s) from %s",
                len(artifacts),
                args[0],
            )
        return _append_sandbox_artifacts(stdout_text, artifacts)
    finally:
        # Clean up the per-call sandbox dir on success, failure, OR
        # cancellation. ignore_errors=True so a busy file (Windows) or
        # vanished tree doesn't mask the real result/exception. Skip
        # cleanup for caller-supplied cwd — we don't own that directory.
        if not use_caller_cwd:
            _sh.rmtree(sandbox_cwd, ignore_errors=True)


class _ClaudeCLIBackend:
    """Uses the `claude` Code CLI on PATH via a subprocess (subscription)."""

    async def available(self) -> bool:
        return await _probe_version("claude")

    async def complete(self, req: LLMRequest) -> str:
        args = ["claude", "-p", req.prompt]
        if req.system:
            args.extend(["--append-system-prompt", req.system])
        if req.model:
            args.extend(["--model", req.model])
        return await _run_capture(args, cwd=req.cwd)


class _KimiCLIBackend:
    """Uses the `kimi` CLI on PATH via a subprocess (subscription).

    Mirrors the pattern in skyn3t/adapters/kimi_cli.py:
      kimi --print -p <prompt> [-m <model>]
    """

    async def available(self) -> bool:
        return await _probe_version("kimi")

    async def complete(self, req: LLMRequest) -> str:
        args = ["kimi", "--print", "--quiet", "--no-thinking"]
        if req.model:
            args.extend(["-m", req.model])
        args.extend(["-p", req.prompt])
        return await _run_capture(args, cwd=req.cwd)


class _CopilotCLIBackend:
    """Uses the `copilot` GitHub Copilot CLI on PATH (subscription).

    Copilot's `-p` non-interactive mode normally requires --allow-all-tools so
    it can run arbitrary shell/file tools. For pure text completion we don't
    enable any tools — but Copilot still requires the flag to confirm
    non-interactive use, so this backend is best-effort. Errors propagate up
    to LLMClient.complete which falls back to deterministic.
    """

    async def available(self) -> bool:
        return await _probe_version("copilot")

    async def complete(self, req: LLMRequest) -> str:
        # `--available-tools=` (empty) disables agent/tool mode so the CLI
        # returns the model's direct response. Without this, prompts come
        # back as multi-page agent transcripts that downstream parsers can't
        # interpret (and which trip our deterministic-stub fallback).
        args = ["copilot", "--available-tools=", "-p", req.prompt]
        if req.model:
            args.extend(["--model", req.model])
        return await _run_capture(args, cwd=req.cwd)


class _OpenAICLIBackend:
    """Uses the `openai` CLI for non-interactive chat completions.

    The `openai` CLI doesn't have a single-shot prompt flag; it requires a
    structured `openai api chat.completions.create` invocation. It also needs
    OPENAI_API_KEY (metered, not a subscription). Marked unavailable unless
    the key is set, so auto-mode never quietly bills a user with no key.
    """

    async def available(self) -> bool:
        if not os.environ.get("OPENAI_API_KEY"):
            return False
        return await _probe_version("openai")

    async def complete(self, req: LLMRequest) -> str:
        # Build a chat.completions.create invocation. The openai CLI accepts
        # repeated -g role content pairs to compose messages.
        model = req.model or os.environ.get("SKYN3T_OPENAI_MODEL") or "gpt-4o-mini"
        args = ["openai", "api", "chat.completions.create", "-m", model]
        if req.system:
            args.extend(["-g", "system", req.system])
        args.extend(["-g", "user", req.prompt])
        return await _run_capture(args, cwd=req.cwd)


class _AnthropicBackend:
    # Threshold (chars) at which we wrap content in cache_control blocks.
    # Anthropic's prompt-caching minimum is 1024 tokens; using 1024 chars as a
    # conservative proxy avoids tokenizing here (chars <= tokens almost always).
    _CACHE_MIN_CHARS = 1024

    def __init__(self, api_key: Optional[str]):
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise ImportError("anthropic package not installed") from e
        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        # Beta header required by older SDKs for prompt caching. Newer SDKs
        # (>=0.34) accept the feature without the header but tolerate it.
        self._extra_headers = {"anthropic-beta": "prompt-caching-2024-07-31"}

    @staticmethod
    def _user_prefix_is_cacheable(prompt: str) -> bool:
        """Heuristic: does the prompt start with / contain a stable, reusable prefix?

        We mark the user message for caching if it starts with our well-known
        augmentation/RAG section headers, or contains a fenced doc block —
        signals that the leading region is repeated across calls in a session.
        """
        if not prompt:
            return False
        head = prompt.lstrip()[:512]
        if head.startswith("# Recent successful diffs"):
            return True
        if head.startswith("# Provider notes"):
            return True
        # Provider-doc augmentation typically embeds fenced code/doc blocks.
        if "```" in prompt[:4000]:
            return True
        return False

    async def complete(self, req: LLMRequest) -> str:
        # NO_CLAUDE policy: this is an Anthropic-only backend. Under
        # SKYN3T_NO_CLAUDE the LLMClient constructor already coerces the
        # backend to openrouter, so this code path should be unreachable —
        # but guard here too so a stray direct call can't quietly hit the
        # Anthropic API / a claude model id.
        if os.environ.get("SKYN3T_NO_CLAUDE", "").strip().lower() in {
            "1", "true", "yes", "on"
        }:
            raise RuntimeError(
                "Anthropic backend disabled by SKYN3T_NO_CLAUDE; "
                "route via OpenRouter instead"
            )
        model = req.model or "claude-3-5-sonnet-latest"
        kw: dict = {"model": model, "max_tokens": req.max_tokens,
                    "temperature": req.temperature}

        # ----- system: cache if large -----
        used_cache = False
        if req.system and len(req.system) >= self._CACHE_MIN_CHARS:
            kw["system"] = [{
                "type": "text",
                "text": req.system,
                "cache_control": {"type": "ephemeral"},
            }]
            used_cache = True
        elif req.system:
            kw["system"] = req.system
        else:
            kw["system"] = ""

        # ----- user message: cache if prompt has a stable prefix worth caching -----
        if (len(req.prompt) >= self._CACHE_MIN_CHARS
                and self._user_prefix_is_cacheable(req.prompt)):
            kw["messages"] = [{
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": req.prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
            }]
            used_cache = True
        else:
            kw["messages"] = [{"role": "user", "content": req.prompt}]

        # ----- call with caching, fall back to plain on schema rejection -----
        msg = None
        if used_cache:
            try:
                msg = await self.client.messages.create(
                    **kw, extra_headers=self._extra_headers
                )
            except TypeError:
                # Older SDK: extra_headers or cache_control schema unsupported.
                msg = None
            except Exception as e:
                # Schema rejection from server (older API surface) — fall back.
                err_name = type(e).__name__
                if err_name in ("BadRequestError", "APIStatusError",
                                "InvalidRequestError", "UnprocessableEntityError"):
                    logger.info("anthropic prompt-caching unsupported, falling back: %s", e)
                    msg = None
                else:
                    raise
            if msg is None:
                # Rebuild kw without cache_control and retry as plain text.
                plain_kw: dict = {"model": model, "max_tokens": req.max_tokens,
                                  "temperature": req.temperature,
                                  "messages": [{"role": "user", "content": req.prompt}]}
                if req.system:
                    plain_kw["system"] = req.system
                msg = await self.client.messages.create(**plain_kw)
        else:
            msg = await self.client.messages.create(**kw)

        # ----- log cache usage when present -----
        usage = getattr(msg, "usage", None)
        if usage is not None:
            cache_hit = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
            if cache_hit or cache_create:
                logger.info(
                    "anthropic cache: hit=%d create=%d input=%d output=%d",
                    cache_hit, cache_create,
                    getattr(usage, "input_tokens", 0),
                    getattr(usage, "output_tokens", 0),
                )

        # concat text blocks
        return "".join(getattr(b, "text", "") for b in msg.content)
