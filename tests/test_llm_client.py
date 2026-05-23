import asyncio
import os
import time
from pathlib import Path

import pytest

import skyn3t.adapters.llm_client as llm_client_module
from skyn3t.adapters.llm_client import (
    LLMClient,
    _append_sandbox_artifacts,
    _collect_sandbox_artifacts,
    _drop_cross_provider_model_name,
)
from skyn3t.core.events import EventBus, EventType


def test_llm_client_reads_defaults_from_settings(monkeypatch):
    monkeypatch.setattr(
        llm_client_module,
        "_settings_fallbacks",
        lambda: {
            "llm_backend": "openrouter",
            "llm_model": "openai/gpt-4.1",
            "anthropic_api_key": "anthropic-key",
            "openrouter_api_key": "router-key",
        },
    )

    client = LLMClient()

    assert client.backend == "openrouter"
    assert client.default_model == "openai/gpt-4.1"
    assert client._anthropic_key == "anthropic-key"
    assert client._openrouter_key == "router-key"


@pytest.mark.asyncio
async def test_llm_client_auto_mode_tries_all_local_clis_before_api_keys(monkeypatch):
    order = []

    async def fake_try_cli(self, name, cls):
        order.append(name)
        return False

    monkeypatch.setattr(LLMClient, "_try_cli", fake_try_cli)

    client = LLMClient(backend="auto")
    client._anthropic_key = None
    client._openrouter_key = None

    await client._get_impl()

    assert order == ["copilot_cli", "claude_cli", "kimi_cli", "openai_cli"]
    assert client.backend == "deterministic"


@pytest.mark.asyncio
async def test_llm_client_design_hint_uses_copilot_first(monkeypatch):
    """Claude is the user's largest subscription; Kimi has shown 180s
    streaming-idle hangs on architect/design calls. Across every
    routing hint, the order is Claude first."""
    order = []

    async def fake_try_cli(self, name, cls):
        order.append(name)
        return False

    monkeypatch.setattr(LLMClient, "_try_cli", fake_try_cli)

    client = LLMClient(backend="auto", caller_name="designer")
    client._anthropic_key = None
    client._openrouter_key = None

    await client._get_impl()

    assert order == ["copilot_cli", "claude_cli", "kimi_cli", "openai_cli"]
    assert client.backend == "deterministic"


@pytest.mark.asyncio
async def test_llm_client_code_hint_uses_copilot_first(monkeypatch):
    order = []

    async def fake_try_cli(self, name, cls):
        order.append(name)
        return False

    monkeypatch.setattr(LLMClient, "_try_cli", fake_try_cli)

    client = LLMClient(backend="auto", caller_name="build_fix")
    client._anthropic_key = None
    client._openrouter_key = None

    await client._get_impl()

    assert order == ["copilot_cli", "claude_cli", "kimi_cli", "openai_cli"]
    assert client.backend == "deterministic"


@pytest.mark.asyncio
async def test_policy_backend_falls_through_to_auto_chain(monkeypatch):
    order = []

    async def fake_try_cli(self, name, cls):
        order.append(name)
        if name == "copilot_cli":
            self._backend_name = "copilot_cli"
            self._impl = object()
            return True
        return False

    monkeypatch.setattr(LLMClient, "_try_cli", fake_try_cli)

    client = LLMClient(
        backend="kimi_cli",
        caller_name="designer",
        backend_is_policy=True,
    )
    client._anthropic_key = None
    client._openrouter_key = None

    await client._get_impl()

    # Policy "kimi_cli" tries kimi first, then falls through to the
    # auto chain (claude_cli, copilot_cli, kimi_cli skipped, openai_cli).
    # copilot_cli is the second one that succeeds, so it should land first
    # after the initial kimi attempt and the claude attempt.
    # Policy "kimi_cli" tries kimi first, then falls through to the
    # auto chain (copilot_cli first since Copilot is multi-model proxy).
    assert order[:2] == ["kimi_cli", "copilot_cli"]
    assert client.backend == "copilot_cli"


@pytest.mark.asyncio
async def test_explicit_backend_override_beats_hint(monkeypatch):
    order = []

    async def fake_try_cli(self, name, cls):
        order.append(name)
        if name == "kimi_cli":
            self._backend_name = "kimi_cli"
            self._impl = object()
            return True
        return False

    monkeypatch.setattr(LLMClient, "_try_cli", fake_try_cli)

    client = LLMClient(backend="kimi_cli", caller_name="code_agent")

    await client._get_impl()

    assert order == ["kimi_cli"]
    assert client.backend == "kimi_cli"


@pytest.mark.asyncio
async def test_llm_exchange_events_redact_obvious_secrets(monkeypatch):
    class FakeBackend:
        async def complete(self, req):
            return (
                "reply with sk-ant-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
                "and demo@example.com and "
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.signature"
            )

    async def fake_get_impl():
        return FakeBackend()

    event_bus = EventBus()
    client = LLMClient(event_bus=event_bus, caller_name="tester")
    client._backend_name = "deterministic"
    monkeypatch.setattr(client, "_get_impl", fake_get_impl)

    await client.complete(
        "Prompt contains sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa and dev@example.com",
        system="Bearer abcdefghijklmnopqrstuvwxyz1234567890",
    )

    exchange = event_bus.get_history(EventType.LLM_EXCHANGE)[0]
    assert "***REDACTED***" in exchange.payload["prompt"]
    assert "***REDACTED***" in exchange.payload["response"]
    assert "***REDACTED***" in exchange.payload["system"]
    assert "dev@example.com" not in exchange.payload["prompt"]
    assert "demo@example.com" not in exchange.payload["response"]


@pytest.mark.asyncio
async def test_llm_complete_warning_includes_caller_backend_and_error(caplog, monkeypatch):
    class FakeBackend:
        async def complete(self, req):  # noqa: ARG002
            raise RuntimeError("")

    async def fake_get_impl():
        return FakeBackend()

    client = LLMClient(backend="deterministic", caller_name="planner")
    monkeypatch.setattr(client, "_get_impl", fake_get_impl)

    with caplog.at_level("WARNING", logger="skyn3t.adapters.llm_client"):
        result = await client.complete("hello")

    assert result.startswith("[deterministic-stub]")
    assert "hello" in result
    assert "caller=planner" in caplog.text
    assert "backend=deterministic" in caplog.text
    assert "error=RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_llm_complete_retries_next_backend_once(monkeypatch):
    class FailingBackend:
        async def complete(self, req):  # noqa: ARG002
            raise RuntimeError("kimi timed out")

    class SuccessBackend:
        async def complete(self, req):  # noqa: ARG002
            return "fallback backend response"

    async def fake_get_impl(self):
        if "kimi_cli" in self._skip_backends:
            self._backend_name = "copilot_cli"
            return SuccessBackend()
        self._backend_name = "kimi_cli"
        return FailingBackend()

    monkeypatch.setattr(LLMClient, "_get_impl", fake_get_impl)

    client = LLMClient(backend="kimi_cli", caller_name="designer")
    result = await client.complete("hello")

    assert result == "fallback backend response"


@pytest.mark.asyncio
async def test_llm_complete_timeout_retries_next_backend_once(monkeypatch):
    class SlowBackend:
        async def complete(self, req):  # noqa: ARG002
            await asyncio.sleep(0.2)
            return "too slow"

    class SuccessBackend:
        async def complete(self, req):  # noqa: ARG002
            return "fallback backend response"

    async def fake_get_impl(self):
        if "kimi_cli" in self._skip_backends:
            self._backend_name = "copilot_cli"
            return SuccessBackend()
        self._backend_name = "kimi_cli"
        return SlowBackend()

    monkeypatch.setattr(LLMClient, "_get_impl", fake_get_impl)

    client = LLMClient(backend="kimi_cli", caller_name="designer")
    result = await client.complete("hello", timeout=0.05)

    assert result == "fallback backend response"


@pytest.mark.parametrize(
    ("default_model", "explicit_model", "expected_retry_model"),
    [
        ("openrouter/owl-alpha", None, None),
        (None, "anthropic/claude-sonnet-4", None),
        ("kimi-code/kimi-for-coding", None, "kimi-code/kimi-for-coding"),
    ],
)
@pytest.mark.asyncio
async def test_llm_complete_failover_sanitizes_only_cross_provider_models(
    monkeypatch, default_model, explicit_model, expected_retry_model
):
    seen_retry_models = []

    class FailingBackend:
        async def complete(self, req):  # noqa: ARG002
            raise RuntimeError("primary backend rejected the request")

    class SuccessBackend:
        async def complete(self, req):
            seen_retry_models.append(req.model)
            return "fallback backend response"

    async def fake_get_impl(self):
        if "openrouter" in self._skip_backends:
            self._backend_name = "copilot_cli"
            return SuccessBackend()
        self._backend_name = "openrouter"
        return FailingBackend()

    monkeypatch.setattr(LLMClient, "_get_impl", fake_get_impl)

    client = LLMClient(backend="openrouter", default_model=default_model, caller_name="designer")
    result = await client.complete("hello", model=explicit_model)

    assert result == "fallback backend response"
    assert seen_retry_models == [expected_retry_model]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (None, None),
        ("openrouter/owl-alpha", None),
        ("anthropic/claude-sonnet-4", None),
        ("OpenRouter/Owl-Alpha", None),
        ("kimi-code/kimi-for-coding", "kimi-code/kimi-for-coding"),
        ("gpt-5.4", "gpt-5.4"),
    ],
)
def test_drop_cross_provider_model_name(model, expected):
    assert _drop_cross_provider_model_name(model) == expected


def test_collect_sandbox_artifacts_harvests_new_files(tmp_path: Path):
    root = tmp_path / "cwd"
    root.mkdir(parents=True, exist_ok=True)
    old_file = root / "sandbox" / "server" / "old.js"
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_text("old", encoding="utf-8")
    started_at = time.time()
    os.utime(old_file, (started_at - 10, started_at - 10))

    new_file = root / "sandbox" / "server" / "index.js"
    new_file.write_text("export default {};", encoding="utf-8")

    artifacts = _collect_sandbox_artifacts(str(root), started_at=started_at)
    assert ("server/index.js", "export default {};") in artifacts
    assert not any(path == "server/old.js" for path, _ in artifacts)


def test_append_sandbox_artifacts_prefers_single_file_when_stdout_empty():
    out = _append_sandbox_artifacts("", [("server/index.js", "console.log('ok');\n")])
    assert out == "console.log('ok');"
