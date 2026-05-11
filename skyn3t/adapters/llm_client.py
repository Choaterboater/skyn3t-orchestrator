from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

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


# Subprocess timeouts (seconds)
_AVAILABLE_TIMEOUT = 3.0
# Real-world ceiling: copilot CLI doing MCP web search across 6-7
# services plus emitting a multi-section spec OR a large per-file
# scaffold legitimately takes 5-10 minutes. 300s killed real work in
# v5/v6 of homelab. 600s lets the model finish while still capping
# the worst case so a stuck call doesn't hang forever.
_COMPLETE_TIMEOUT = 600.0


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
                 skip_backends: Optional[List[str]] = None):
        self.default_model = default_model or os.environ.get("SKYN3T_LLM_MODEL")
        self._backend_name = (backend or os.environ.get("SKYN3T_LLM_BACKEND") or "auto").lower()
        # Cross-model debate: callers can list backends to skip (e.g. the
        # retry path passes the backend the prior attempt used). The auto
        # chain then naturally falls through to a different model.
        self._skip_backends: set = set(skip_backends or [])
        self._anthropic_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._openrouter_key = openrouter_api_key or os.environ.get("OPENROUTER_API_KEY")
        self._impl = None  # lazy
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
                       temperature: float = 0.4) -> str:
        req = LLMRequest(prompt=prompt, system=system, model=model or self.default_model,
                         max_tokens=max_tokens, temperature=temperature)
        elapsed = 0.0
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
            out: str = await impl.complete(req)
            elapsed = time.monotonic() - start
        except Exception as e:
            logger.warning(
                "llm complete failed; caller=%s backend=%s error=%s",
                self._caller_name or "llm",
                self._backend_name or "unknown",
                str(e).strip() or type(e).__name__,
            )
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
                    prompt_trunc = redact_text((req.prompt or "")[:2000])
                    response_trunc = redact_text((out or "")[:2000])
                    system_trunc = redact_text((req.system or "")[:500]) if req.system else ""
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
                            "duration_ms": int(elapsed * 1000),
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
            if name == "claude_cli":
                if await self._try_cli("claude_cli", _ClaudeCLIBackend):
                    return self._impl
            elif name == "kimi_cli":
                if await self._try_cli("kimi_cli", _KimiCLIBackend):
                    return self._impl
            elif name == "copilot_cli":
                if await self._try_cli("copilot_cli", _CopilotCLIBackend):
                    return self._impl
            elif name == "openai_cli":
                if await self._try_cli("openai_cli", _OpenAICLIBackend):
                    return self._impl
            elif name == "anthropic":
                try:
                    self._impl = _AnthropicBackend(self._anthropic_key)
                    self._backend_name = "anthropic"
                    return self._impl
                except Exception:
                    logger.warning("anthropic backend init failed", exc_info=True)
            elif name == "openrouter":
                try:
                    from skyn3t.adapters.openrouter import OpenRouterBackend
                    self._impl = OpenRouterBackend(self._openrouter_key)
                    self._backend_name = "openrouter"
                    return self._impl
                except Exception:
                    logger.warning("openrouter backend init failed", exc_info=True)
            # explicit choice failed → fall through to deterministic
            self._impl = _DeterministicBackend()
            self._backend_name = "deterministic"
            return self._impl

        # ------------------------------------------------------------------
        # 2. Auto: subscription-CLIs first, then API keys, then deterministic.
        # ------------------------------------------------------------------
        # 2a. claude CLI (most common Pro/Max subscription).
        if await self._try_cli("claude_cli", _ClaudeCLIBackend):
            return self._impl
        # 2b. copilot CLI (common coding/general subscription).
        if await self._try_cli("copilot_cli", _CopilotCLIBackend):
            return self._impl
        # 2c. OpenAI CLI (subscription/local auth).
        if await self._try_cli("openai_cli", _OpenAICLIBackend):
            return self._impl
        # 2d. kimi CLI (specialist subscription).
        if await self._try_cli("kimi_cli", _KimiCLIBackend):
            return self._impl
        # 2e. Anthropic API key (metered).
        if self._anthropic_key:
            try:
                self._impl = _AnthropicBackend(self._anthropic_key)
                self._backend_name = "anthropic"
                return self._impl
            except Exception:
                logger.warning("anthropic backend init failed", exc_info=True)
        # 2f. OpenRouter API key (metered).
        if self._openrouter_key:
            try:
                from skyn3t.adapters.openrouter import OpenRouterBackend
                self._impl = OpenRouterBackend(self._openrouter_key)
                self._backend_name = "openrouter"
                return self._impl
            except Exception:
                logger.warning("openrouter backend init failed", exc_info=True)
        # 2g. Deterministic stub.
        self._impl = _DeterministicBackend()
        self._backend_name = "deterministic"
        return self._impl

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
        return False
    except (FileNotFoundError, OSError):
        return False
    return proc.returncode == 0


async def _run_capture(args: list[str]) -> str:
    """Run a subprocess and return its stdout (raises on non-zero)."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_COMPLETE_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} cli failed: {stderr.decode(errors='replace')[:300]}")
    return stdout.decode(errors="replace").strip()


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
        return await _run_capture(args)


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
        return await _run_capture(args)


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
        return await _run_capture(args)


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
        return await _run_capture(args)


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
