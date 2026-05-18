"""Tests for CLI-based agents."""

import asyncio
from unittest.mock import patch

import pytest

from skyn3t.adapters.claude_cli import ClaudeCLIAgent
from skyn3t.adapters.cli_agent import CLIAgent
from skyn3t.adapters.copilot_cli import CopilotCLIAgent
from skyn3t.adapters.kimi_cli import KimiCLIAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


class MockCLIAgent(CLIAgent):
    """Concrete CLIAgent for testing the base class."""

    def __init__(self, name: str, event_bus: EventBus, config=None, command: str = "echo"):
        self._cli_command = command
        super().__init__(
            name=name,
            agent_type="test",
            provider="test_cli",
            event_bus=event_bus,
            command=command,
            config=config,
        )

    def build_args(self, task: TaskRequest) -> list:
        return [task.input_data.get("message", "hello")]


@pytest.mark.asyncio
class TestCLIAgentRunCLI:
    async def test_run_cli_success(self, event_bus):
        agent = MockCLIAgent("test", event_bus)
        with patch("shutil.which", return_value="/usr/bin/echo"):
            await agent.initialize()

        returncode, stdout, stderr = await agent.run_cli(["hello world"])
        assert returncode == 0
        assert "hello world" in stdout
        await agent.shutdown()

    async def test_run_cli_with_stdin(self, event_bus):
        agent = MockCLIAgent("test", event_bus, command="cat")
        with patch("shutil.which", return_value="/bin/cat"):
            await agent.initialize()

        returncode, stdout, stderr = await agent.run_cli([], stdin="piped input")
        assert returncode == 0
        assert stdout == "piped input"
        await agent.shutdown()

    async def test_run_cli_timeout(self, event_bus):
        agent = MockCLIAgent("test", event_bus, config={"cli_timeout": 1}, command="sleep")
        with patch("shutil.which", return_value="/bin/sleep"):
            await agent.initialize()

        with pytest.raises(asyncio.TimeoutError):
            await agent.run_cli(["5"], timeout=0.5)
        await agent.shutdown()

    async def test_run_cli_non_zero_exit(self, event_bus):
        agent = MockCLIAgent("test", event_bus, command="false")
        with patch("shutil.which", return_value="/usr/bin/false"):
            await agent.initialize()

        returncode, stdout, stderr = await agent.run_cli([])
        assert returncode == 1
        await agent.shutdown()


@pytest.mark.asyncio
class TestCLIAgentExecute:
    async def test_execute_success(self, event_bus):
        agent = MockCLIAgent("test", event_bus)
        with patch("shutil.which", return_value="/usr/bin/echo"):
            await agent.initialize()

        task = TaskRequest(
            title="Test",
            input_data={"message": "success"},
        )
        result = await agent.execute(task)
        assert result.success is True
        assert "success" in result.output.get("response", "")
        await agent.shutdown()

    async def test_execute_non_zero_exit(self, event_bus):
        agent = MockCLIAgent("test", event_bus, command="false")
        with patch("shutil.which", return_value="/usr/bin/false"):
            await agent.initialize()

        task = TaskRequest(title="Test", input_data={"message": "fail"})
        result = await agent.execute(task)
        assert result.success is False
        assert result.error is not None
        await agent.shutdown()

    async def test_execute_timeout(self, event_bus):
        agent = MockCLIAgent("test", event_bus, config={"cli_timeout": 0})
        # Force timeout by mocking run_cli
        with patch.object(
            agent, "run_cli", side_effect=asyncio.TimeoutError
        ):
            task = TaskRequest(title="Test", input_data={"message": "slow"})
            result = await agent.execute(task)
            assert result.success is False
            assert "timed out" in result.error.lower()


class TestClaudeCLIAgent:
    def test_build_args(self, event_bus):
        agent = ClaudeCLIAgent("claude_cli", event_bus)
        task = TaskRequest(
            title="Generate code",
            input_data={"message": "Write a Python function"},
        )
        args = agent.build_args(task)
        assert args == ["-p", "Write a Python function"]

    def test_build_args_falls_back_to_description(self, event_bus):
        agent = ClaudeCLIAgent("claude_cli", event_bus)
        task = TaskRequest(
            title="Generate code",
            description="Write a Python class",
            input_data={},
        )
        args = agent.build_args(task)
        assert args == ["-p", "Write a Python class"]

    def test_build_args_falls_back_to_title(self, event_bus):
        agent = ClaudeCLIAgent("claude_cli", event_bus)
        task = TaskRequest(title="Write docs", input_data={})
        args = agent.build_args(task)
        assert args == ["-p", "Write docs"]


class TestKimiCLIAgent:
    def test_build_args(self, event_bus):
        agent = KimiCLIAgent("kimi_cli", event_bus)
        task = TaskRequest(
            title="Translate",
            input_data={"message": "Hello in Chinese"},
        )
        args = agent.build_args(task)
        assert args == ["--print", "-p", "Hello in Chinese"]

    def test_build_args_falls_back_to_description(self, event_bus):
        agent = KimiCLIAgent("kimi_cli", event_bus)
        task = TaskRequest(
            title="Translate",
            description="Translate to Chinese",
            input_data={},
        )
        args = agent.build_args(task)
        assert args == ["--print", "-p", "Translate to Chinese"]


class TestCopilotCLIAgent:
    def test_build_args(self, event_bus):
        agent = CopilotCLIAgent("copilot_cli", event_bus)
        task = TaskRequest(
            title="Complete function",
            input_data={"message": "def fib(n):"},
        )
        args = agent.build_args(task)
        assert args == ["-p", "def fib(n):"]

    def test_build_args_falls_back_to_description(self, event_bus):
        agent = CopilotCLIAgent("copilot_cli", event_bus)
        task = TaskRequest(
            title="Complete function",
            description="def factorial(n):",
            input_data={},
        )
        args = agent.build_args(task)
        assert args == ["-p", "def factorial(n):"]


@pytest.mark.asyncio
class TestCLIAgentIntegration:
    async def test_claude_cli_agent_execute_mocked(self, event_bus):
        agent = ClaudeCLIAgent("claude_cli", event_bus)
        with patch.object(
            agent,
            "run_cli",
            return_value=(
                0,
                "Generated Python function",
                "",
            ),
        ):
            task = TaskRequest(
                title="Generate code",
                input_data={"message": "Write a Python function"},
            )
            result = await agent.execute(task)
            assert result.success is True
            assert result.output["response"] == "Generated Python function"

    async def test_kimi_cli_agent_execute_mocked(self, event_bus):
        agent = KimiCLIAgent("kimi_cli", event_bus)
        with patch.object(
            agent,
            "run_cli",
            return_value=(
                0,
                "这是一个Python函数",
                "",
            ),
        ):
            task = TaskRequest(
                title="Translate",
                input_data={"message": "Hello"},
            )
            result = await agent.execute(task)
            assert result.success is True
            assert "Python函数" in result.output["response"]

    async def test_copilot_cli_agent_execute_mocked(self, event_bus):
        agent = CopilotCLIAgent("copilot_cli", event_bus)
        with patch.object(
            agent,
            "run_cli",
            return_value=(
                0,
                "def fib(n): return n if n < 2 else fib(n-1) + fib(n-2)",
                "",
            ),
        ):
            task = TaskRequest(
                title="Complete code",
                input_data={"message": "def fib(n):"},
            )
            result = await agent.execute(task)
            assert result.success is True
            assert "fib" in result.output["response"]
