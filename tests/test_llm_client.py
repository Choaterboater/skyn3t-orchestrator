import os
import time
from pathlib import Path

import pytest

from skyn3t.adapters.llm_client import (
    LLMClient,
    _append_sandbox_artifacts,
    _collect_sandbox_artifacts,
)
from skyn3t.core.events import EventBus, EventType


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

    assert order == ["claude_cli", "copilot_cli", "openai_cli", "kimi_cli"]
    assert client.backend == "deterministic"


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
