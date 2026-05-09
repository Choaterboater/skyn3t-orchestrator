"""Shared pytest fixtures."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def event_bus():
    from skyn3t.core.events import EventBus

    return EventBus()


@pytest.fixture
def mock_openai():
    """Mock OpenAI client."""
    mock = MagicMock()
    mock.chat.completions.create = MagicMock(return_value=asyncio.Future())
    mock.chat.completions.create.return_value.set_result(
        MagicMock(
            choices=[MagicMock(message=MagicMock(content="Test response"))],
            usage=MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
    )
    return mock


@pytest.fixture
def mock_anthropic():
    """Mock Anthropic client."""
    mock = MagicMock()
    mock.messages.create = MagicMock(return_value=asyncio.Future())
    mock.messages.create.return_value.set_result(
        MagicMock(
            content=[MagicMock(type="text", text="Test response")],
            usage=MagicMock(input_tokens=10, output_tokens=5),
        )
    )
    return mock


@pytest.fixture
def mock_github():
    """Mock GitHub client."""
    mock = MagicMock()
    mock.get_repo.return_value = MagicMock(
        name="test-repo",
        full_name="owner/test-repo",
        description="Test repository",
        stargazers_count=100,
        forks_count=20,
        open_issues_count=5,
        language="Python",
        created_at=MagicMock(isoformat=lambda: "2024-01-01T00:00:00"),
        updated_at=MagicMock(isoformat=lambda: "2024-01-01T00:00:00"),
        get_languages=lambda: {"Python": 1000},
        get_readme=lambda: MagicMock(content="VGhpcyBpcyBhIHRlc3Q="),
        get_commits=lambda: [],
        get_topics=lambda: ["test"],
        html_url="https://github.com/owner/test-repo",
    )
    mock.get_rate_limit.return_value = MagicMock()
    return mock
