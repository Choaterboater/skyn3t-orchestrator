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

## PackagingAgent — runnable-product generation

Pipeline stage that runs **after `contract_verifier`** and **before `consistency_reviewer`**. Turns every generated scaffold into something a stranger can run, instead of a config-puzzle the user has to debug.

**Files:**
- `skyn3t/agents/packaging_agent.py` — the agent + per-strategy templates
- `skyn3t/agents/env_scanner.py` — finds `process.env.X` / `import.meta.env.X` / `os.getenv("X")` / pydantic Settings refs in source
- `skyn3t/agents/stack_detector.py` — classifies project family from manifests
- `tests/test_env_scanner.py`, `test_stack_detector.py`, `test_packaging_agent_{web,docker,fullstack}.py`, `test_reviewer_packaging_axis.py`

### Strategy dispatch

`StackDetector.detect(artifact_dir)` returns `family ∈ {web, server, fullstack, unknown}`. `PackagingAgent.execute` matches on family:

| Family | What gets generated |
|---|---|
| **web** (react_vite / next / svelte / etc.) | `scaffold/src/hooks/useConfig.js` (localStorage-backed config) + `scaffold/src/Settings.jsx` (auto-generated from env scanner) + first-run gate patched into `App.jsx` + `.gitignore` + slim README |
| **server** (fastapi / flask / express / etc.) | `Dockerfile` (stack-aware) + `docker-compose.yml` (app + detected services) + `.env.example` (slim, only server vars) + `.gitignore` + README with `cp .env.example .env && docker compose up` |
| **fullstack** (web + server in one repo) | Both of the above, plus: frontend added as a service in compose, `API_BASE_URL` seeded as default in `useConfig`, unified root README explaining the two-tier config model |
| **unknown** | Placeholder result — no files generated |

### Feature flag

Disable per-run via `extra={"packaging_enabled": False}` on `StudioRunner.start(...)`. Defaults to enabled.

### Reviewer scoring

`ReviewerAgent._packaging_score(artifact_dir)` returns `(score 0-10, gaps, family)`. Blended into the final review score at **10% weight**:

- With LLM score: `blend = 0.54 · llm + 0.36 · heuristic + 0.10 · packaging`
- Without:        `blend = 0.90 · heuristic + 0.10 · packaging`

Per-family rubric (all award 5 base points for README + .gitignore):

| Family | +5 specific |
|---|---|
| web | Settings.jsx (+3), useConfig hook (+2) |
| server | Dockerfile (+2), docker-compose.yml (+2), .env.example (+1) |
| fullstack | web layer (+2), server layer (+2), frontend wired into compose (+1) |
| unknown | +5 default (can't grade specifics) |

Packaging gaps surface as ⚠️ bullets in `review.md`.

### When to extend

- **New stack family** (CLI tools, iOS apps, etc.): add a `_package_<family>` method, a new `case` arm in `execute`, a `_packaging_score` branch in `reviewer.py`. Add per-stack templates next to the existing `_WEB_GITIGNORE` / `_PYTHON_DOCKERFILE` constants.
- **New infra service** for docker-compose: add an entry to `_SERVICE_STANZAS` in `packaging_agent.py`. The compose generator handles the rest.
- **New env-var idiom**: add a regex + an AST handler in `env_scanner.py`. Aim for word-boundary matching to avoid false positives.

### Safety invariants

- **Never overwrite operator's existing infra.** Dockerfile / docker-compose / .env.example are detected and left in place when present, with a note appended to the result.
- **App.jsx patching is AST-safe.** Skips when react-router is already imported, when file is over 200 lines, when no `export default` is found, or when the `@skyn3t-packaging` marker is already present (idempotent).
- **Sandbox verification is bounded.** Web tier runs `npm install + npm run build` with a 180s timeout; failure is non-fatal because the downstream BuildVerifier catches it again. Server tier skips verification entirely (BuildVerifier owns `docker compose build`).

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
