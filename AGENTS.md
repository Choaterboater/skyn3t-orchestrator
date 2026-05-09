# SkyN3t Agent Guide

This document is for AI agents working on the SkyN3t orchestrator codebase.

## Architecture Overview

SkyN3t is an async, event-driven multi-agent system built on Python 3.10+.

- **Event Bus** — In-memory pub/sub for inter-agent communication (`skyn3t.core.events`)
- **Orchestrator** — Central manager for agent registration, task routing, and monitoring (`skyn3t.core.orchestrator`)
- **BaseAgent** — Abstract class that all agents extend (`skyn3t.core.agent`)
- **Self-Healing Manager** — Automatic recovery when agents fail (`skyn3t.core.self_healing`)
- **RAG Engine** — Semantic search over knowledge documents (`skyn3t.rag.rag_engine`)
- **Web Layer** — FastAPI application with WebSocket streaming (`skyn3t.web.app`)

### 🧠 Brain Layer (New)

| Component | File | Purpose |
|-----------|------|---------|
| **MemoryStore** | `skyn3t/memory/store.py` | Persistent SQLite storage for tasks, agents, messages, lessons, logs |
| **CollectiveConsciousness** | `skyn3t/memory/consciousness.py` | Shared working memory: KV store with TTL, sessions, agent insights |
| **ExperienceIngestor** | `skyn3t/memory/ingestor.py` | Auto-ingests task outcomes into RAG vector store |
| **SelfTuningEngine** | `skyn3t/memory/tuner.py` | Applies reflection suggestions to live agent configs |
| **MetaAgent** | `skyn3t/memory/meta_agent.py` | Autonomous cortex that observes and improves the system |

## Installation & Setup

```bash
# Quick setup (creates venv, installs deps, creates .env)
./scripts/setup.sh

# Or manual:
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env with your API keys

# Start
./scripts/run.sh web     # Web server at http://localhost:6660
./scripts/run.sh cli     # Interactive CLI
python -m skyn3t.cli.main start
```

## Key Files and Their Purposes

| File | Purpose |
|------|---------|
| `skyn3t/core/events.py` | Event types, Event dataclass, EventBus implementation |
| `skyn3t/core/agent.py` | BaseAgent, TaskRequest, TaskResult, AgentCapability |
| `skyn3t/core/orchestrator.py` | Orchestrator — agent registry, task submission, monitoring loops |
| `skyn3t/core/self_healing.py` | SelfHealingManager with pluggable healing actions |
| `skyn3t/core/models.py` | SQLAlchemy async ORM models (Agent, Task, Message, KnowledgeDocument) |
| `skyn3t/memory/store.py` | Persistent memory: SQLite CRUD for all runtime data |
| `skyn3t/memory/consciousness.py` | Shared blackboard: working memory, sessions, insights |
| `skyn3t/memory/ingestor.py` | Experience → RAG pipeline: auto-ingest task outcomes |
| `skyn3t/memory/tuner.py` | Self-tuning: apply reflection suggestions to agent configs |
| `skyn3t/memory/meta_agent.py` | Autonomous meta-agent cortex |
| `skyn3t/rag/vector_store.py` | ChromaDB wrapper for embeddings and similarity search |
| `skyn3t/rag/document_processor.py` | Text, markdown, and code chunking |
| `skyn3t/rag/rag_engine.py` | High-level RAG: ingest → chunk → embed → query → answer |
| `skyn3t/agents/github_explorer.py` | GitHub repository analysis agent |
| `skyn3t/adapters/openai_adapter.py` | OpenAI GPT agent adapter |
| `skyn3t/adapters/anthropic_adapter.py` | Anthropic Claude agent adapter |
| `skyn3t/web/app.py` | FastAPI app with REST API and WebSocket dashboard |
| `skyn3t/config/settings.py` | Pydantic Settings with env var loading |

## How the Brain Works

1. **Task execution** → Orchestrator saves to MemoryStore + Consciousness
2. **ExperienceIngestor** → Auto-adds task outcome to RAG vector store
3. **ReflectionEngine** → Analyzes success/failure, publishes KNOWLEDGE_UPDATED
4. **SelfTuningEngine** → Listens to knowledge updates, applies config changes
5. **MetaAgent** → Observes system trends, generates improvement hypotheses
6. **Next task** → Gets `collective_context` injected with past experiences + insights

## How to Add a New Agent

1. Create a new file in `skyn3t/agents/` or `skyn3t/adapters/`
2. Subclass `BaseAgent` from `skyn3t.core.agent`
3. Implement the three abstract methods:
   - `initialize(self) -> None` — set up API clients
   - `execute(self, task: TaskRequest) -> TaskResult` — main task logic
   - `health_check(self) -> bool` — verify connectivity
4. Add capabilities via `self.add_capability(AgentCapability(...))`
5. Import and export in `skyn3t/agents/__init__.py` or `skyn3t/adapters/__init__.py`
6. Write tests in `tests/test_agents.py` using mocks for external APIs
7. Update `AGENTS.md` and `README.md`

### Minimal Agent Template

```python
from skyn3t.core.agent import BaseAgent, TaskRequest, TaskResult, AgentCapability
from skyn3t.core.events import EventBus


class MyAgent(BaseAgent):
    def __init__(self, name: str, event_bus: EventBus, config=None):
        super().__init__(name, "my_type", "my_provider", event_bus, config)
        self.add_capability(
            AgentCapability(name="my_skill", description="...")
        )

    async def initialize(self):
        self.metadata["ready"] = True

    async def execute(self, task: TaskRequest) -> TaskResult:
        return TaskResult(
            task_id=task.task_id, success=True, output={"done": True}
        )

    async def health_check(self) -> bool:
        return True
```

## Testing Guidelines

- **Framework** — pytest with `asyncio.run()` for async code (see `tests/test_memory.py`)
- **Mock external APIs** with `unittest.mock.patch` or `unittest.mock.AsyncMock`
- **Mock `get_settings()`** via environment variables and `get_settings.cache_clear()`
- **Use temporary paths** for vector DB to avoid polluting the real data directory
- **Clean up agents** in tests: call `await agent.shutdown()` or `await orchestrator.stop()`
- **Event bus tests** should verify both type-specific and global subscribers
- Run tests:
  ```bash
  pytest tests/ -v
  pytest tests/ --ignore=tests/test_observability.py -q  # skip flaky singleton tests
  ```

### Common Fixture Pattern

```python
@pytest.fixture(autouse=True)
def mock_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("VECTOR_DB_PATH", str(tmp_path / "vectors"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
```

## Code Style

- **Formatter** — `black` (line length 100)
- **Linter** — `ruff`
- **Type checker** — `mypy` (`disallow_untyped_defs = true`)
- **Docstrings** — Google style or PEP 257
- **Imports** — sorted with ruff/isort
- Run before committing:
  ```bash
  black skyn3t tests
  ruff check skyn3t tests
  mypy skyn3t
  pytest
  ```
