"""Shared pytest fixtures."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_state(tmp_path_factory, monkeypatch):
    runtime_root = tmp_path_factory.mktemp("runtime-state")
    data_dir = runtime_root / "data"
    logs_dir = runtime_root / "logs"
    vector_dir = data_dir / "vector_db"
    secrets_path = data_dir / "secrets.json"
    audit_dir = logs_dir / "audit"

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("LOGS_DIR", str(logs_dir))
    monkeypatch.setenv("VECTOR_DB_PATH", str(vector_dir))
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("SECRET_STORAGE_PATH", str(secrets_path))
    monkeypatch.setenv("AUDIT_LOG_DIR", str(audit_dir))

    import skyn3t.memory.database as memory_database
    from skyn3t.config.settings import get_settings
    from skyn3t.core.models import init_db
    from skyn3t.memory.database import close_engine

    get_settings.cache_clear()
    close_engine()
    memory_database._async_session_maker = None
    asyncio.get_event_loop().run_until_complete(init_db())
    try:
        import skyn3t.cortex.proposals as proposals_module

        proposals_module._store = None
    except Exception:
        pass

    yield Path(runtime_root)

    asyncio.get_event_loop().run_until_complete(close_engine())
    memory_database._async_session_maker = None
    get_settings.cache_clear()
    try:
        import skyn3t.cortex.proposals as proposals_module

        proposals_module._store = None
    except Exception:
        pass


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
