# SkyN3t Technical Architecture & Flow Diagrams

> Render this file in any Mermaid-compatible viewer (GitHub, GitLab, Notion, VS Code with Mermaid extension).

---

## 1. High-Level System Architecture

```mermaid
flowchart TB
    subgraph Operators["Operators"]
        CLI["skyn3t CLI"]
        WebUI["Web Dashboard<br/>localhost:6660"]
        Telegram["Telegram Bot"]
    end

    subgraph Gateway["API Gateway"]
        FastAPI["FastAPI + WebSocket"]
        Auth["HTTP Auth / Rate Limits"]
    end

    subgraph Core["Core Orchestrator"]
        Orchestrator["Orchestrator"]
        EventBus["EventBus<br/>Pub/Sub"]
        AgentRegistry["Agent Registry<br/>19 agents"]
        TaskRouter["Task Router"]
        RetryPolicy["Retry Policy"]
        SelfHeal["Self-Healing Manager"]
    end

    subgraph Agents["Agent Swarm"]
        Brainstorm["brainstorm"]
        Architect["architect"]
        Designer["designer"]
        CodeAgent["code_agent"]
        Reviewer["reviewer"]
        BuildVerifier["build_verifier"]
        MetaAgent["meta_agent"]
        Scheduler["scheduler_agent"]
        GitHubExp["github_explorer"]
    end

    subgraph Brain["Memory & Learning"]
        SQLite[(SQLite<br/>skyn3t.db)]
        Chroma[(ChromaDB<br/>Vector Store)]
        Consciousness["CollectiveConsciousness<br/>KV + TTL"]
        RAGEngine["RAG Engine<br/>Hybrid Search"]
        Experience["Experience Index"]
        Trajectories["Trajectory Logger<br/>JSONL"]
    end

    subgraph Observability["Observability"]
        Prometheus["Prometheus Metrics"]
        TokenTracker["Token Tracker"]
        StageLatency["Stage Latency"]
        Tracer["OpenTelemetry Tracer"]
    end

    CLI --> FastAPI
    WebUI --> FastAPI
    Telegram --> FastAPI
    FastAPI --> Auth
    Auth --> Orchestrator
    Orchestrator --> EventBus
    EventBus --> Agents
    Orchestrator --> AgentRegistry
    Orchestrator --> TaskRouter
    TaskRouter --> RetryPolicy
    RetryPolicy --> SelfHeal
    Agents --> SQLite
    Agents --> Chroma
    Consciousness --> SQLite
    RAGEngine --> Chroma
    Experience --> SQLite
    Trajectories --> EventBus
    Orchestrator --> Prometheus
    Orchestrator --> TokenTracker
    Orchestrator --> StageLatency
    Orchestrator --> Tracer
```

---

## 2. Task Execution Flow

```mermaid
sequenceDiagram
    autonumber
    participant Op as Operator
    participant API as FastAPI
    participant Orc as Orchestrator
    participant Bus as EventBus
    participant Agent as Agent (e.g. code_agent)
    participant DB as SQLite
    participant Vec as ChromaDB

    Op->>API: POST /api/agents/code_agent/task<br/>{title, prompt, input_data}
    API->>Orc: submit_task(TaskRequest)
    Orc->>Orc: idempotency check (1h TTL)
    Orc->>Orc: auto-assign session_id
    Orc->>Orc: AgentSelector.select()<br/>capability + cost_budget
    Orc->>DB: MemoryStore.save_task(status=pending)
    Orc->>Bus: publish TASK_CREATED
    Orc->>Bus: publish TASK_ROUTED
    Orc->>Agent: queue TaskRequest
    Agent->>Agent: _process_tasks() loop
    Agent->>Bus: publish TASK_STARTED
    Agent->>Agent: execute(task)
    Agent->>DB: save_message(source=code_agent)
    Agent->>Vec: RAG.query() experiences
    Agent->>Bus: publish LLM_EXCHANGE
    Agent->>Bus: publish AGENT_THOUGHT
    Agent->>Agent: TaskResult(success=True)
    Agent->>Orc: task_results[task_id] = result
    Agent->>Bus: publish TASK_COMPLETED
    Orc->>DB: save_task(status=completed)
    Orc->>Bus: publish EXPERIENCE_INGEST<br/>(auto-add to vector store)
    API-->>Op: {task_id, status, result}
```

---

## 3. Studio Pipeline Flow (Project Generation)

```mermaid
flowchart LR
    subgraph Input["Project Brief"]
        Brief["'Build a homelab dashboard'"]
    end

    subgraph Pipeline["Studio Pipeline"]
        direction LR
        Brainstorm["brainstorm<br/>🧠 Ideas + constraints"]
        Architect["architect<br/>🏗️ Stack + components"]
        Designer["designer<br/>🎨 UI/UX specs"]
        Code["code_agent<br/>💻 Scaffold + code"]
        PostCode["post-code checks"]
        Reviewer["reviewer<br/>🔍 Quality gate"]
        Consistency["consistency_reviewer<br/>⚖️ Cross-check"]
        Packaging["packaging_agent<br/>📦 Docker + README"]
        BuildVerif["build_verifier<br/>✅ npm/build test"]
    end

    subgraph Output["Deliverable"]
        Artifact["projects/homelab-dashboard-v53/<br/>├── scaffold/<br/>├── Dockerfile<br/>├── docker-compose.yml<br/>├── README.md<br/>└── review.md"]
    end

    Brief --> Brainstorm
    Brainstorm -->|ideas.json| Architect
    Architect -->|architecture.md| Designer
    Designer -->|design.md| Code
    Code -->|src/| PostCode
    PostCode -->|manifest.json| Reviewer
    Reviewer -->|verdict: go-with-fixes| Consistency
    Consistency -->|pass| Packaging
    Packaging -->|Docker + compose| BuildVerif
    BuildVerif -->|build_passed| Artifact
```

### Pipeline Retry Logic

```mermaid
flowchart TD
    A[Stage fails] --> B{RetryPolicy.classify}
    B -->|AUTH / QUOTA| C[Fast-fail]
    B -->|RATE_LIMIT| D[Exponential backoff]
    B -->|TIMEOUT| E[3 attempts]
    B -->|SYNTAX| F[2 attempts]
    B -->|BUILD_ERROR| G[Targeted fix<br/>learned generators]
    D --> H[Resubmit to same agent]
    E --> H
    F --> H
    G --> I[code_agent retry<br/>with fix hint]
    H --> J{Success?}
    I --> J
    J -->|No| B
    J -->|Yes| K[Continue pipeline]
    C --> L[Publish TASK_FAILED_FINAL]
```

---

## 4. Memory & Data Flow

```mermaid
flowchart TB
    subgraph Runtime["Runtime Data"]
        Tasks[(tasks)]
        Messages[(messages)]
        Agents[(agents)]
        Logs[(system_logs)]
        Users[(users 🆕)]
        Jobs[(scheduled_jobs 🆕)]
    end

    subgraph Search["Search Layers"]
        FTS5["FTS5 Virtual Table<br/>messages + tasks + logs"]
        RAG["RAG Hybrid<br/>BM25 + Vector"]
    end

    subgraph Vector["Vector Store"]
        Chroma[(ChromaDB)]
        Embeddings["sentence-transformers"]
    end

    subgraph Learning["Learning Loop"]
        Experience[(experience_index)]
        Trajectories["trajectories/*.jsonl 🆕"]
        Lessons[(knowledge_documents)]
    end

    Tasks --> FTS5
    Messages --> FTS5
    Logs --> FTS5
    Messages --> RAG
    Lessons --> Chroma
    Chroma --> Embeddings
    Experience --> SQLite
    Trajectories --> Disk
```

---

## 5. Event Bus Architecture

```mermaid
flowchart LR
    subgraph Publishers["Event Publishers"]
        OrchestratorPub["Orchestrator"]
        AgentPub["BaseAgent"]
        LLMPub["llm_client"]
        StudioPub["StudioRunner"]
    end

    subgraph Bus["EventBus<br/>In-Memory Pub/Sub"]
        History["Bounded History<br/>maxlen=1000"]
        Subscribers["Type-Specific<br/>+ Global Subscribers"]
    end

    subgraph Consumers["Event Consumers"]
        TokenTrack["TokenTracker<br/>LLM_EXCHANGE"]
        Trajectory["TrajectoryLogger<br/>TASK_* + LLM_EXCHANGE 🆕"]
        WebSocket["WebSocket Broadcast<br/>All events"]
        ExperienceIngest["ExperienceIngestor<br/>TASK_COMPLETED"]
        MetaAgent["MetaAgent<br/>Periodic observation"]
    end

    OrchestratorPub -->|TASK_ROUTED| Bus
    OrchestratorPub -->|TASK_COMPLETED| Bus
    AgentPub -->|AGENT_THOUGHT| Bus
    LLMPub -->|LLM_EXCHANGE| Bus
    StudioPub -->|PIPELINE_STAGE_COMPLETED| Bus
    Bus --> TokenTrack
    Bus --> Trajectory
    Bus --> WebSocket
    Bus --> ExperienceIngest
    Bus --> MetaAgent
```

---

## 6. Self-Healing & Meta-Agent Loop

```mermaid
flowchart TD
    A[Task fails] --> B[RetryPolicy.decide]
    B -->|retryable| C[Resubmit]
    B -->|exhausted| D[SelfHealingManager]
    D --> E{Failure type}
    E -->|Agent crashed| F[Restart agent]
    E -->|Code error| G[TargetedFixAgent]
    E -->|Build error| H[LearnedGenerator<br/>create deterministic fix]
    E -->|Pattern detected| I[MetaAgent]
    I --> J[Analyze trends]
    J --> K[Generate hypothesis]
    K --> L[Publish CORTEX_DECISION]
    L --> M[SelfTuningEngine]
    M --> N[Apply config change]
    N --> O[Store lesson in RAG]
```

---

## 7. New Features (Phase 1 & 2)

```mermaid
flowchart LR
    subgraph Phase1["Phase 1 — Search + Insights"]
        FTS["FTS5 Session Search 🆕"]
        Insights["Insights Endpoint 🆕"]
    end

    subgraph Phase2["Phase 2 — Scheduler + User + Trajectories"]
        SchedulerDB["Scheduler + SQLite 🆕"]
        UserModel["User Profiles 🆕"]
        TrajectoryExport["Trajectory JSONL Export 🆕"]
    end

    subgraph Existing["Existing Infrastructure"]
        SQLite[(SQLite)]
        EventBus["EventBus"]
        Web["FastAPI"]
        CLI["Typer CLI"]
    end

    SQLite --> FTS
    SQLite --> Insights
    SQLite --> SchedulerDB
    SQLite --> UserModel
    EventBus --> TrajectoryExport
    Web --> FTS
    Web --> Insights
    Web --> SchedulerDB
    Web --> UserModel
    Web --> TrajectoryExport
    CLI --> FTS
    CLI --> Insights
    CLI --> SchedulerDB
    CLI --> UserModel
    CLI --> TrajectoryExport
```

---

## 8. Technology Stack

| Layer | Technology |
|---|---|
| **Runtime** | Python 3.10+, asyncio |
| **Web** | FastAPI, WebSocket, uvicorn |
| **CLI** | Typer, Rich, httpx |
| **Database** | SQLite + aiosqlite + SQLAlchemy 2.0 |
| **Vector Search** | ChromaDB + sentence-transformers + BM25 |
| **FTS** | SQLite FTS5 (native) |
| **Observability** | Prometheus, OpenTelemetry-style tracing |
| **Testing** | pytest, AsyncMock |
| **Lint/Format** | ruff, black, mypy |
