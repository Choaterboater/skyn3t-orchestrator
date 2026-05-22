"""Shared pytest fixtures."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def disable_cli_backends(monkeypatch):
    """Prevent LLMClient from spawning real CLI subprocesses in tests."""

    async def _fake_probe_version(binary: str) -> bool:
        return False

    monkeypatch.setattr(
        "skyn3t.adapters.llm_client._probe_version", _fake_probe_version
    )
    yield


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
    # Prevent real OpenRouter calls in tests (scaffold entrypoint fast-path).
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    # Force inline execution backend so tests don't pull Docker images.
    monkeypatch.setenv("SKYN3T_EXECUTION_BACKEND", "inline")

    import skyn3t.memory.database as memory_database
    from skyn3t.config.settings import get_settings
    from skyn3t.core.models import init_db
    from skyn3t.memory.database import close_engine

    def _reset_proposal_store() -> None:
        try:
            import skyn3t.cortex.proposals as proposals_module

            store = getattr(proposals_module, "_store", None)
            if store is not None:
                try:
                    asyncio.run(store.cancel_inflight())
                except Exception:
                    pass
            proposals_module._store = None
        except Exception:
            pass

    get_settings.cache_clear()
    try:
        asyncio.run(close_engine())
    except Exception:
        pass
    memory_database._async_session_maker = None
    asyncio.run(init_db())
    _reset_proposal_store()

    yield Path(runtime_root)

    try:
        asyncio.run(close_engine())
    except Exception:
        pass
    memory_database._async_session_maker = None
    get_settings.cache_clear()
    _reset_proposal_store()


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
