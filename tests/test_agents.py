"""Tests for agent implementations."""

from unittest.mock import MagicMock, patch

import pytest

from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


class TestGitHubExplorerAgent:
    @pytest.mark.asyncio
    async def test_initialization(self):
        pytest.importorskip("github", reason="PyGithub not installed")
        from skyn3t.agents.github_explorer import GitHubExplorerAgent

        with patch("github.Github") as mock_gh:
            mock_gh.return_value.get_user.return_value = MagicMock()
            agent = GitHubExplorerAgent("gh", EventBus())
            await agent.initialize()
            assert agent.metadata.get("initialized") is True

    @pytest.mark.asyncio
    async def test_health_check(self):
        pytest.importorskip("github", reason="PyGithub not installed")
        from skyn3t.agents.github_explorer import GitHubExplorerAgent

        with patch("github.Github") as mock_gh:
            mock_gh.return_value.get_rate_limit.return_value = MagicMock()
            agent = GitHubExplorerAgent("gh", EventBus())
            await agent.initialize()
            assert await agent.health_check() is True

    @pytest.mark.asyncio
    async def test_unknown_task(self):
        from skyn3t.agents.github_explorer import GitHubExplorerAgent

        agent = GitHubExplorerAgent("gh", EventBus())
        task = TaskRequest(title="Test", input_data={"task_type": "unknown"})
        result = await agent.execute(task)
        assert result.success is False


class TestCodeAgent:
    @pytest.mark.asyncio
    async def test_initialization(self):
        from skyn3t.agents.code_agent import CodeAgent

        agent = CodeAgent("code", EventBus())
        await agent.initialize()
        assert agent.metadata.get("initialized") is True

    @pytest.mark.asyncio
    async def test_code_execution(self):
        from skyn3t.agents.code_agent import CodeAgent

        agent = CodeAgent("code", EventBus())
        await agent.initialize()

        task = TaskRequest(
            title="Run code",
            input_data={
                "task_type": "code_execution",
                "code": "x = 2 + 2\nprint(x)",
            },
        )
        result = await agent.execute(task)
        assert result.success is True
        assert "4" in str(result.output)


class TestSchedulerAgent:
    @pytest.mark.asyncio
    async def test_initialization(self):
        from skyn3t.agents.scheduler_agent import SchedulerAgent

        agent = SchedulerAgent("scheduler", EventBus())
        await agent.initialize()
        assert agent.metadata.get("initialized") is True
