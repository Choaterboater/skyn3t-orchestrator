"""Regression tests for the llm_client audit fixes.

1. Cross-provider model stripping must drop the live OpenRouter default
   ``xiaomi/...`` ids (or_strong / or_ui) on failover to a CLI backend, while
   preserving the CLI-local ``kimi-code/...`` id and bare CLI model names.
2. The ``stdbuf`` line-buffering prefix must be skipped when the binary is not
   on PATH, so completions don't fail AFTER the version probe passed.
"""

from __future__ import annotations

import asyncio
import shutil
import sys

import pytest

from skyn3t.adapters import llm_client
from skyn3t.adapters.llm_client import (
    LLMClient,
    _drop_cross_provider_model_name,
)

# --- Fix 1: xiaomi/ (and any new OpenRouter publisher) gets stripped -------

@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # The live or_strong / or_ui defaults — the bug.
        ("xiaomi/mimo-v2.5-pro", None),
        ("xiaomi/mimo-v2-flash", None),
        ("Xiaomi/MiMo-V2.5-Pro", None),
        # A brand-new publisher we never enumerated — robust allow-list wins.
        ("moonshotai/kimi-k2", None),
        ("some-new-vendor/model-x", None),
        # CLI-local slashed id must be preserved.
        ("kimi-code/kimi-for-coding", "kimi-code/kimi-for-coding"),
        # Bare CLI model names must be preserved.
        ("gpt-5.3-codex", "gpt-5.3-codex"),
        ("claude-sonnet-4.6", "claude-sonnet-4.6"),
        (None, None),
    ],
)
def test_drop_cross_provider_strips_xiaomi_and_unknown_publishers(model, expected):
    assert _drop_cross_provider_model_name(model) == expected


def test_failover_from_openrouter_xiaomi_to_cli_drops_stale_model(monkeypatch):
    """End-to-end: an or_strong xiaomi default must NOT leak into the CLI retry."""

    seen_retry_models: list = []

    class FailingBackend:
        async def complete(self, req):
            raise RuntimeError("openrouter 429 rate limited")

    class SuccessBackend:
        async def complete(self, req):
            seen_retry_models.append(req.model)
            return "cli backend response"

    async def fake_get_impl(self):
        if "openrouter" in self._skip_backends:
            self._backend_name = "claude_cli"
            return SuccessBackend()
        self._backend_name = "openrouter"
        return FailingBackend()

    monkeypatch.setattr(LLMClient, "_get_impl", fake_get_impl)

    client = LLMClient(
        backend="openrouter",
        default_model="xiaomi/mimo-v2.5-pro",
        caller_name="designer",
    )
    result = asyncio.run(client.complete("hello"))

    assert result == "cli backend response"
    # The stale xiaomi/ id must have been dropped (None), not leaked to the CLI.
    assert seen_retry_models == [None]


# --- Fix 2: stdbuf prefix skipped when the binary is absent ----------------

def _patch_subprocess(monkeypatch, captured_args):
    """Stub asyncio.create_subprocess_exec to capture argv and exit cleanly.

    The fake proc drains immediately (empty stdout/stderr) and exits 0 so
    _run_capture returns without touching a real CLI.
    """

    class _EmptyStream:
        async def read(self, _n):
            return b""

    class _FakeProc:
        returncode = 0

        def __init__(self):
            self.stdout = _EmptyStream()
            self.stderr = _EmptyStream()

        async def wait(self):
            return 0

        def kill(self):  # pragma: no cover - not reached (returncode is 0)
            pass

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured_args.append(list(args))
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


@pytest.mark.skipif(sys.platform == "win32", reason="stdbuf prefix is Unix-only")
def test_run_capture_skips_stdbuf_when_absent(monkeypatch):
    """When stdbuf isn't on PATH, the command must run without the prefix.

    Before the fix the prefix was unconditional, so on a host lacking stdbuf
    every completion died with FileNotFoundError after the probe passed.
    """
    captured_args: list = []
    _patch_subprocess(monkeypatch, captured_args)
    # _run_capture does `import shutil as _sh`; both resolve to this singleton.
    monkeypatch.setattr(shutil, "which", lambda name: None)

    asyncio.run(llm_client._run_capture(["echo", "hi"]))

    assert captured_args, "subprocess was never launched"
    assert captured_args[0][0] != "stdbuf"
    assert captured_args[0][:2] == ["echo", "hi"]


@pytest.mark.skipif(sys.platform == "win32", reason="stdbuf prefix is Unix-only")
def test_run_capture_uses_stdbuf_when_present(monkeypatch):
    """When stdbuf IS on PATH, the line-buffering prefix is still applied."""
    captured_args: list = []
    _patch_subprocess(monkeypatch, captured_args)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/stdbuf" if name == "stdbuf" else None,
    )

    asyncio.run(llm_client._run_capture(["echo", "hi"]))

    assert captured_args, "subprocess was never launched"
    assert captured_args[0][:3] == ["stdbuf", "-oL", "-eL"]
