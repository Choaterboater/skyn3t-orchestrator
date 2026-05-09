# SkyN3t Orchestrator

**SkyN3t** — *Just A Rather Very Intelligent System*

A multi-agent orchestrator with a **persistent collective brain**: self-healing, Retrieval-Augmented Generation (RAG), autonomous task execution, and a shared consciousness that lets agents think, learn, and remember together.

## Features

- **Multi-Agent Orchestration** — Register and coordinate heterogeneous agents (Claude, Kimi, Copilot, OpenAI, GitHub, Slack, Discord, Email)
- **Persistent Collective Memory** — Every task, lesson, and insight is stored in SQLite + RAG. Agents remember across restarts.
- **Collective Consciousness** — Real-time shared blackboard where agents read each other's insights and session history
- **Experience → RAG Pipeline** — Task outcomes auto-ingest into the vector store for semantic recall
- **Self-Tuning** — Reflection suggestions automatically apply to live agent configs
- **Autonomous Meta-Agent** — A cortex that watches the system, detects problems, and proposes improvements
- **Dynamic Replanning** — Plans adapt to runtime learning and priority shifts
- **Event-Driven Architecture** — Publish/subscribe event bus for loose coupling
- **Self-Healing** — Automatic agent recovery with configurable healing strategies
- **Real-Time Dashboard** — Built-in FastAPI web app with WebSocket event streaming
- **Pipeline Engine** — Chain agents with automatic output forwarding
- **Security Layer** — macOS seatbelt sandbox, permission engine, audit log, secret encryption

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     SkyN3t Brain Architecture                    │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │  Web API    │  │  Event Bus   │  │  Meta-Agent (Cortex) │   │
│  │ (FastAPI)   │  │(pub/sub)     │  │                      │   │
│  └──────┬──────┘  └──────┬───────┘  └──────────────────────┘   │
│         │                │                                       │
│  ┌──────┴────────────────┴──────────────────────────────────┐  │
│  │              Collective Consciousness                     │  │
│  │         (shared working memory + sessions)                │  │
│  └──────┬────────────────┬──────────────────────────────────┘  │
│         │                │                                       │
│  ┌──────┴──────┐  ┌──────┴──────┐  ┌──────────────────────┐   │
│  │ MemoryStore │  │ Experience  │  │  Self-Tuning Engine  │   │
│  │  (SQLite)   │  │ Ingestor →  │  │                      │   │
│  │             │  │   RAG       │  │                      │   │
│  └─────────────┘  └─────────────┘  └──────────────────────┘   │
│         │                │                                       │
│  ┌──────┴────────────────┴──────────────────────────────────┐  │
│  │              Agents (Claude, Kimi, Copilot...)            │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- **Python 3.10+**
- **CLI tools** (at least one): `claude`, `kimi`, `copilot` — installed and authenticated
- (Optional) **Redis** for distributed event bus
- (Optional) **Docker & Docker Compose**

Install the CLI tools:
```bash
# Claude
npm install -g @anthropic-ai/claude-code

# Kimi
pip install kimi-cli

# Copilot (via GitHub CLI)
gh auth login
gh extension install github/copilot
```

## Installation

### Quick Setup (Recommended)

```bash
git clone <repo-url>
cd skyn3t
./scripts/setup.sh
```

This creates a virtual environment, installs dependencies, creates data directories, and copies `.env.example` to `.env`.

### Manual Setup

```bash
git clone <repo-url>
cd skyn3t

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install SkyN3t
pip install -e ".[dev]"

# Or install from requirements only
pip install -r requirements.txt
```

### Configuration

Edit `.env` in the project root:

```bash
cp .env.example .env
```

Key settings:
```bash
DEBUG=false
SECRET_KEY=change-me-in-production

# At least one API key (for non-CLI adapters)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...

# Optional: Redis for distributed mode
USE_REDIS=false
REDIS_URL=redis://localhost:6379/0
```

## Quick Start

### Start the Web Server

```bash
./scripts/run.sh web
# Or directly:
python -m skyn3t.cli.main start
```

Open http://localhost:6660 for the cyberpunk dashboard.

### Register Agents

Via dashboard or API:
```bash
curl -X POST http://localhost:6660/api/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "claude", "provider": "claude"}'

curl -X POST http://localhost:6660/api/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "kimi", "provider": "kimi"}'

curl -X POST http://localhost:6660/api/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "copilot", "provider": "copilot"}'
```

### Submit a Task

```bash
curl -X POST http://localhost:6660/api/agents/claude/task \
  -H "Content-Type: application/json" \
  -d '{"title": "Hello", "input": {"message": "Say hello"}}'
```

### Run a Pipeline

```bash
curl -X POST http://localhost:6660/api/pipeline \
  -H "Content-Type: application/json" \
  -d '{
    "name": "code-review-pipeline",
    "agents": ["claude", "copilot"],
    "prompts": ["Write a Python function", "Review this code"]
  }'
```

### CLI Mode

```bash
# Start interactive CLI
./scripts/run.sh cli

# Or use Typer commands
python -m skyn3t.cli.main status
python -m skyn3t.cli.main exec claude "Explain quantum computing"
```

## Brain API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | Full system status with brain layers |
| GET | `/api/memory/stats` | Persistent memory statistics |
| GET | `/api/memory/sessions` | Active swarm sessions |
| GET | `/api/memory/sessions/{id}` | Session context + history |
| GET | `/api/memory/insights` | Recent agent insights |
| GET | `/api/memory/experiences` | Semantic search over past tasks |
| GET | `/api/memory/tuning` | Self-tuning engine status |
| GET | `/api/consciousness/status` | Collective consciousness status |
| GET | `/api/meta/status` | Meta-agent observations & actions |
| POST | `/api/meta/pause` | Pause autonomous cortex |
| POST | `/api/meta/resume` | Resume autonomous cortex |
| POST | `/api/orchestrator/reorder` | Trigger task reordering |
| POST | `/api/fallback` | Register fallback chains |
| WS | `/ws` | Real-time event stream |

## Docker Deployment

```bash
docker-compose up --build
```

This starts SkyN3t + Redis. The web dashboard is at http://localhost:6660.

## Development

```bash
# Run tests
pytest tests/ --ignore=tests/test_observability.py -q

# Format code
black skyn3t tests
ruff check skyn3t tests

# Type check
mypy skyn3t
```

## Project Structure

```
skyn3t/
├── core/               # Orchestrator, event bus, agent base, pipeline
│   ├── orchestrator.py
│   ├── agent.py
│   ├── events.py
│   ├── pipeline.py
│   └── models.py       # SQLAlchemy models
├── memory/             # 🧠 BRAIN LAYER
│   ├── store.py        # Persistent SQLite memory
│   ├── consciousness.py # Shared working memory
│   ├── ingestor.py     # Experience → RAG pipeline
│   ├── tuner.py        # Self-tuning engine
│   └── meta_agent.py   # Autonomous cortex
├── intelligence/       # Agent selector, planner, reflection, decomposer
├── adapters/           # CLI adapters (claude, kimi, copilot)
├── rag/                # ChromaDB vector store, document processor
├── security/           # Sandbox, permissions, audit log, secrets
├── observability/      # Metrics, tracing, health checks
├── web/                # FastAPI app + dashboard
├── integrations/       # Slack, Discord, GitHub, email
├── cli/                # Typer CLI interface
└── config/             # Settings
```

## License

MIT License
HELLO
