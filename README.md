# SkyN3t Orchestrator

**SkyN3t** — *Just A Rather Very Intelligent System*

A multi-agent orchestrator with a **persistent collective brain**: self-healing, Retrieval-Augmented Generation (RAG), autonomous task execution, and a shared consciousness that lets agents think, learn, and remember together.

## Documentation

- [SkyN3t summary](docs/skyn3t-summary.md) — what SkyN3t does, how it works, how it differs from Hermes-style systems, and its multi-model LLM approach
- [Technical flow diagrams](docs/technical_flow_diagram.md) — deeper architecture and execution diagrams
- [Mission](docs/MISSION.md) — product direction and differentiation
- [Wishlist](docs/WISHLIST.md) — planned and parked work

## First Run

```bash
./scripts/setup.sh
source .venv/bin/activate
skyn3t init
skyn3t start
```

Then open http://localhost:6660, describe what you want in the dashboard brief box, and let the swarm choose a starting workflow for you. Use `skyn3t status` or `skyn3t repl` from another terminal when you want the CLI view.

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
PROJECTS_DIR=~/Documents/Skyn3t/Projects

# At least one API key (for non-CLI adapters)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...

# Optional: Redis for distributed mode
USE_REDIS=false
REDIS_URL=redis://localhost:6379/0
```

`PROJECTS_DIR` controls where Studio writes project folders and demo artifacts. Point it at an external location like `~/Documents/Skyn3t/Projects` if you want generated work to live outside the repo checkout.

## Quick Start

### Start the Web Server

```bash
skyn3t start
# Optional convenience wrapper:
./scripts/run.sh web
# Module entrypoint if you prefer:
python -m skyn3t.cli.main start
```

Open http://localhost:6660 for the cyberpunk dashboard.

> **Note:** `skyn3t start` runs with HTTP access logs **off** by default,
> so the orchestrator's own warning/info messages (fast-path activations,
> critique timeouts, build verifier output) stay visible. Pass
> `--access-log` if you want to see per-request lines.

### Register Agents

Via the CLI:
```bash
skyn3t agent add claude --provider anthropic
skyn3t agent list
```

### Submit a Task

```bash
skyn3t task submit claude "Say hello"
# Or use the one-off exec shortcut:
skyn3t exec claude "Say hello"
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
skyn3t repl
skyn3t status
skyn3t exec claude "Explain quantum computing"
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

### CI quality gate

Every push/PR to `main` runs the same baseline checks in GitHub Actions:

- `ruff check skyn3t tests`
- `mypy skyn3t`
- `pytest tests/ --ignore=tests/test_observability.py -q`

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

SkyN3t is released under the MIT License, a permissive open-source license that grants you, the user, broad freedom to use, copy, modify, merge, publish, distribute, sublicense, and even sell copies of this software, in whole or in part, for any purpose — commercial, academic, personal, experimental, or otherwise — without imposing royalties, restrictions on field of endeavor, or onerous obligations beyond the preservation of the original copyright notice and this permission notice in all substantial portions of the software. We chose the MIT License deliberately, because we believe that the future of artificial intelligence, multi-agent orchestration, and collective machine cognition should be built openly, transparently, and collaboratively, with knowledge flowing freely between researchers, engineers, hobbyists, students, and institutions across every continent and discipline. The software is provided "AS IS", without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and noninfringement; in no event shall the authors or copyright holders be liable for any claim, damages, or other liability, whether in an action of contract, tort, or otherwise, arising from, out of, or in connection with the software or the use or other dealings in the software. By contributing to, forking, or otherwise engaging with this codebase, you join a growing community committed to advancing the state of the art in autonomous, self-healing, memory-augmented agent systems — and we welcome you, wholeheartedly, to build, break, remix, and reimagine SkyN3t in whatever direction your curiosity carries you.

Copyright © 2024–2026 SkyN3t Contributors. All rights reserved under the terms above.

For the full text of the MIT License, see the `LICENSE` file in the root of this repository, or visit https://opensource.org/licenses/MIT for the canonical reference.

If you build something interesting with SkyN3t — a research paper, a production system, a creative experiment, a teaching tool, or anything in between — we would love to hear about it. Open an issue, send a pull request, or simply leave a star; every signal helps the project grow.

*Think together. Remember together. Build together.*
