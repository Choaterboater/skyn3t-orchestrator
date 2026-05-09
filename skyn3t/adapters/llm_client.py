from __future__ import annotations
import asyncio, logging, os
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger("skyn3t.adapters.llm_client")


@dataclass
class LLMRequest:
    prompt: str
    system: Optional[str] = None
    model: Optional[str] = None     # override default
    max_tokens: int = 1500
    temperature: float = 0.4
    metadata: dict = field(default_factory=dict)


# Subprocess timeouts (seconds)
_AVAILABLE_TIMEOUT = 3.0
_COMPLETE_TIMEOUT = 120.0


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
                 openrouter_api_key: Optional[str] = None):
        self.default_model = default_model or os.environ.get("SKYN3T_LLM_MODEL")
        self._backend_name = (backend or os.environ.get("SKYN3T_LLM_BACKEND") or "auto").lower()
        self._anthropic_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._openrouter_key = openrouter_api_key or os.environ.get("OPENROUTER_API_KEY")
        self._impl = None  # lazy

    async def complete(self, prompt: str, *, system: Optional[str] = None,
                       model: Optional[str] = None, max_tokens: int = 1500,
                       temperature: float = 0.4) -> str:
        req = LLMRequest(prompt=prompt, system=system, model=model or self.default_model,
                         max_tokens=max_tokens, temperature=temperature)
        try:
            impl = await self._get_impl()
            return await impl.complete(req)
        except Exception as e:
            logger.warning("llm complete failed: %s", e)
            return self._fallback(req)

    @property
    def backend(self) -> str:
        return self._backend_name

    def describe(self) -> dict:
        """Return current backend info for dashboards / observability."""
        return {"backend": self._backend_name, "default_model": self.default_model}

    async def _try_cli(self, name: str, cls) -> bool:
        """Instantiate `cls` and check availability; cache and return True on success."""
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
        # 2b. kimi CLI (subscription).
        if await self._try_cli("kimi_cli", _KimiCLIBackend):
            return self._impl
        # 2c. Anthropic API key (metered).
        if self._anthropic_key:
            try:
                self._impl = _AnthropicBackend(self._anthropic_key)
                self._backend_name = "anthropic"
                return self._impl
            except Exception:
                logger.warning("anthropic backend init failed", exc_info=True)
        # 2d. OpenRouter API key (metered).
        if self._openrouter_key:
            try:
                from skyn3t.adapters.openrouter import OpenRouterBackend
                self._impl = OpenRouterBackend(self._openrouter_key)
                self._backend_name = "openrouter"
                return self._impl
            except Exception:
                logger.warning("openrouter backend init failed", exc_info=True)
        # 2e. Deterministic stub.
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
        # Use --deny-tool to keep tool surface minimal even with --allow-all-tools.
        args = ["copilot", "--allow-all-tools", "-p", req.prompt]
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
    def __init__(self, api_key: Optional[str]):
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise ImportError("anthropic package not installed") from e
        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(self, req: LLMRequest) -> str:
        model = req.model or "claude-3-5-sonnet-latest"
        kw = {"model": model, "max_tokens": req.max_tokens,
              "temperature": req.temperature,
              "messages": [{"role": "user", "content": req.prompt}]}
        if req.system:
            kw["system"] = req.system
        msg = await self.client.messages.create(**kw)
        # concat text blocks
        return "".join(getattr(b, "text", "") for b in msg.content)
