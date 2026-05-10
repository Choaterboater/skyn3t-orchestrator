"""Tests for agent-layer helpers (BaseAgent.llm_complete, refactor imports,
code_improver fallback patcher, and Slack event mention stripping)."""

import asyncio
import time
from pathlib import Path

import pytest

from skyn3t.core.agent import BaseAgent, TaskRequest
from skyn3t.core.events import EventBus


class _StubBaseAgent(BaseAgent):
    """Minimal concrete BaseAgent for exercising helpers like ``llm_complete``."""

    def __init__(self, llm_obj):
        super().__init__(
            name="stub",
            agent_type="stub",
            provider="stub",
            event_bus=EventBus(),
            config={},
        )
        # Direct slot assignment short-circuits the lazy LLMClient construction
        # in BaseAgent.llm. ``get_llm`` returns whatever ``self._llm`` is set to.
        self._llm = llm_obj

    async def initialize(self) -> None:  # pragma: no cover - unused
        pass

    async def execute(self, task, stdin_data=None):  # pragma: no cover - unused
        from skyn3t.core.agent import TaskResult

        return TaskResult(task_id=task.task_id, success=True)

    async def health_check(self) -> bool:  # pragma: no cover - unused
        return True


class _LLMReturning:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        return self.value


class _LLMRaising:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        raise self.exc


class _LLMSleep:
    def __init__(self, seconds):
        self.seconds = seconds
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        await asyncio.sleep(self.seconds)
        return "should-not-arrive"


class _LLMFailThenSucceed:
    def __init__(self, success_value):
        self.success_value = success_value
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient")
        return self.success_value


class TestLLMComplete:
    @pytest.mark.asyncio
    async def test_happy_path_returns_real_response(self):
        agent = _StubBaseAgent(_LLMReturning("real response"))
        out = await agent.llm_complete("hi", fallback="FB")
        assert out == "real response"

    @pytest.mark.asyncio
    async def test_deterministic_stub_returns_fallback(self):
        agent = _StubBaseAgent(_LLMReturning("[deterministic-stub] foo"))
        out = await agent.llm_complete("hi", fallback="FB")
        assert out == "FB"

    @pytest.mark.asyncio
    async def test_exception_returns_fallback(self):
        llm = _LLMRaising(RuntimeError("boom"))
        agent = _StubBaseAgent(llm)
        # retries=0 keeps the test fast and asserts a single attempt suffices.
        out = await agent.llm_complete("hi", fallback="FB", retries=0)
        assert out == "FB"
        assert llm.calls == 1

    @pytest.mark.asyncio
    async def test_timeout_returns_fallback_quickly(self):
        agent = _StubBaseAgent(_LLMSleep(2.0))
        start = time.monotonic()
        out = await agent.llm_complete("hi", fallback="FB", timeout=0.1, retries=0)
        elapsed = time.monotonic() - start
        assert out == "FB"
        assert elapsed < 0.5, f"expected fast timeout, took {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_retry_returns_second_call_output(self, monkeypatch):
        llm = _LLMFailThenSucceed("retry-success")
        agent = _StubBaseAgent(llm)
        # Skip backoff sleep so the test is instant.
        async def _no_sleep(_):
            return None
        monkeypatch.setattr("skyn3t.core.agent.asyncio.sleep", _no_sleep)

        out = await agent.llm_complete("hi", fallback="FB", retries=1)
        assert out == "retry-success"
        assert llm.calls == 2


class TestRefactorImportsDedupAndMultiline:
    @pytest.mark.asyncio
    async def test_multi_line_from_import_dedup_and_sort(self):
        from skyn3t.agents.code_agent import CodeAgent

        # Multi-line `from x import (a, b, c)` followed by a duplicate import
        # and a body. Verifies (1) imports get sorted, (2) duplicates collapsed,
        # (3) the body after the multi-line import block is preserved (no
        # off-by-N truncation due to len(imports) vs end_lineno bug).
        code = (
            "from collections import (\n"
            "    OrderedDict,\n"
            "    defaultdict,\n"
            "    deque,\n"
            ")\n"
            "import os\n"
            "import os\n"
            "\n"
            "def hello():\n"
            "    return os.getcwd()\n"
        )

        agent = CodeAgent("code", EventBus())
        await agent.initialize()

        task = TaskRequest(
            title="Refactor imports",
            input_data={
                "task_type": "refactoring",
                "code": code,
                "refactor_type": "imports",
            },
        )
        result = await agent.execute(task)
        assert result.success is True
        refactored = result.output["refactored"]

        # Body must be preserved verbatim (def line and return statement).
        assert "def hello():" in refactored
        assert "return os.getcwd()" in refactored
        # Duplicate `import os` should have been collapsed to a single entry.
        assert refactored.count("import os") == 1
        # The multi-line `from collections import (...)` should appear
        # exactly once, sorted before `import os` (alphabetical via str sort
        # of `from collections...` vs `import os`).
        assert refactored.count("from collections import") == 1
        # No leftover dangling closing-paren import fragment from the original
        # multi-line block surviving as a body line.
        assert "    deque," not in refactored
        # "Sorted and deduplicated imports" change must be reported.
        assert "Sorted and deduplicated imports" in result.output["changes"]


class TestFallbackApplyCRLF:
    def test_fallback_apply_preserves_crlf_endings(self, tmp_path):
        from skyn3t.agents.code_improver import CodeImproverAgent

        target_rel = "src/app.py"
        target_path = tmp_path / target_rel
        target_path.parent.mkdir(parents=True)
        # Write CRLF file (Windows line endings).
        target_path.write_bytes(b"print('hello')\r\nprint('world')\r\n")

        # Tiny unified diff replacing the first line.
        patch = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-print('hello')\n"
            "+print('fixed')\n"
            " print('world')\n"
        )

        ok = CodeImproverAgent._fallback_apply(
            target_rel, patch, repo_root=tmp_path
        )
        assert ok is True

        raw = target_path.read_bytes()
        # File must still use CRLF, not have been LF-normalized on write.
        assert b"\r\n" in raw
        assert b"print('fixed')\r\n" in raw
        # Sanity: no orphan LF-only line endings introduced.
        # (Every \n in the file should be preceded by \r.)
        lf_only = raw.replace(b"\r\n", b"")
        assert b"\n" not in lf_only


class TestSlackBotMentionStrip:
    @pytest.mark.asyncio
    async def test_app_mention_strips_self_user_id(self, monkeypatch):
        from skyn3t.integrations.slack_bot import SlackBot

        bot = SlackBot(EventBus(), bot_token="test-token")
        bot._self_user_id = "U123"

        captured: list[tuple[str, str, str]] = []

        async def fake_process_message(text: str, channel: str, thread_ts: str) -> None:
            captured.append((text, channel, thread_ts))

        monkeypatch.setattr(bot, "_process_message", fake_process_message)

        await bot._handle_event(
            {
                "type": "app_mention",
                "text": "<@U123> hello there",
                "channel": "C42",
                "ts": "1700000000.000100",
            }
        )

        assert len(captured) == 1
        text, channel, thread_ts = captured[0]
        assert text == "hello there"
        assert channel == "C42"
        assert thread_ts == "1700000000.000100"
