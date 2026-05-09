"""FastAPI web application for SkyN3t."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from skyn3t.config.settings import get_settings
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.core.models import init_db
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.integrations import github_webhook_router
from skyn3t.observability.metrics import generate_metrics
from skyn3t.observability.health import get_health_registry
from skyn3t.observability.tracing import get_tracer


# Global orchestrator instance
orchestrator: Optional[Orchestrator] = None
event_bus: Optional[EventBus] = None


class ConnectionManager:
    """Manage WebSocket connections."""

    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)

        async with self._lock:
            for conn in disconnected:
                if conn in self.active_connections:
                    self.active_connections.remove(conn)


manager = ConnectionManager()
swarm_manager = ConnectionManager()
_broadcast_tasks: set = set()

# Ring buffer of recent compact swarm events (for /api/swarm/snapshot)
_recent_swarm_events: "deque[Dict[str, Any]]" = deque(maxlen=200)


def _safe_event_type(name: str) -> Optional[EventType]:
    """Return EventType.<name> if it exists; tolerate concurrent additions."""
    return getattr(EventType, name, None)


# Build mapping of EventType -> swarm "kind". Use _safe_event_type so that
# missing/optional event types (added by other agents) are skipped silently.
def _build_swarm_kind_map() -> Dict[EventType, str]:
    pairs: List[tuple] = [
        # thoughts
        ("AGENT_THOUGHT", "thought"),
        # A2A messages
        ("AGENT_MESSAGE_SENT", "message"),
        # learning events
        ("AGENT_LEARNING", "learning"),
        # RAG lifecycle
        ("RAG_QUERY_STARTED", "rag"),
        ("RAG_RETRIEVED", "rag"),
        ("RAG_CRITIQUED", "rag"),
        ("RAG_REQUERY", "rag"),
        # Task lifecycle
        ("TASK_ROUTED", "task"),
        ("TASK_ENRICHED", "task"),
        ("TASK_QUEUED", "task"),
        ("TASK_EXECUTION_STARTED", "task"),
        ("TASK_COMPLETED", "task"),
        ("TASK_FAILED", "task"),
        ("TASK_FAILED_FINAL", "task"),
        # Pipeline / stage
        ("PIPELINE_STARTED", "stage"),
        ("PIPELINE_COMPLETED", "stage"),
        ("PIPELINE_STAGE_COMPLETED", "stage"),
        ("PIPELINE_STAGE_FAILED", "stage"),
        # Ingest
        ("INGEST_STARTED", "ingest"),
        ("INGEST_PROGRESS", "ingest"),
        ("INGEST_COMPLETE", "ingest"),
    ]
    out: Dict[EventType, str] = {}
    for name, kind in pairs:
        et = _safe_event_type(name)
        if et is not None:
            out[et] = kind
    return out


_SWARM_KIND_MAP: Dict[EventType, str] = _build_swarm_kind_map()


def _project_swarm_event(event: Event) -> Optional[Dict[str, Any]]:
    """Project a full Event to the compact swarm payload, or None if it
    isn't a kind we surface to the swarm UI."""
    kind = _SWARM_KIND_MAP.get(event.event_type)
    if kind is None:
        return None

    payload = event.payload or {}

    # Derive a friendly label per kind
    if kind == "thought":
        label = (payload.get("line") or payload.get("thought") or payload.get("content")
                 or payload.get("summary") or "thought")
    elif kind == "message":
        label = (payload.get("content") or payload.get("message")
                 or payload.get("text") or payload.get("kind") or "message")
    elif kind == "rag":
        label = (
            payload.get("query")
            or payload.get("verdict")
            or payload.get("critique")
            or event.event_type.name.lower()
        )
    elif kind == "learning":
        label = payload.get("lesson") or payload.get("insight") or payload.get("title") or "learning"
    elif kind == "task":
        label = (
            payload.get("title")
            or payload.get("task_title")
            or payload.get("task_id", "")[:8]
            or event.event_type.name.lower()
        )
    elif kind == "stage":
        label = payload.get("stage") or payload.get("name") or event.event_type.name.lower()
    elif kind == "ingest":
        label = payload.get("source") or payload.get("title") or event.event_type.name.lower()
    else:
        label = event.event_type.name.lower()

    # Truncate label so the UI stays compact
    if isinstance(label, str) and len(label) > 200:
        label = label[:197] + "..."

    # Determine "to" target
    to_field = event.target or payload.get("to") or payload.get("to_agent") or payload.get("agent")

    compact = {
        "kind": kind,
        "ts": event.timestamp.isoformat() if isinstance(event.timestamp, datetime) else str(event.timestamp),
        "from": event.source,
        "to": to_field,
        "label": str(label) if label is not None else "",
        "event_type": event.event_type.name,
        "meta": {
            "task_id": payload.get("task_id"),
            "session_id": payload.get("session_id"),
            "correlation_id": event.correlation_id,
            "payload": payload,
        },
    }
    return compact


def broadcast_event(event: Event) -> None:
    """Broadcast an event to all WebSocket clients."""
    task = asyncio.create_task(manager.broadcast({
        "type": "event",
        "data": event.to_dict(),
    }))
    _broadcast_tasks.add(task)
    task.add_done_callback(_broadcast_tasks.discard)

    # Also project to swarm clients if this event is one we surface
    compact = _project_swarm_event(event)
    if compact is not None:
        _recent_swarm_events.append(compact)
        swarm_task = asyncio.create_task(swarm_manager.broadcast({
            "type": "swarm",
            "data": compact,
        }))
        _broadcast_tasks.add(swarm_task)
        swarm_task.add_done_callback(_broadcast_tasks.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global orchestrator, event_bus

    settings = get_settings()

    # Initialize database
    await init_db()

    # Create event bus and orchestrator
    event_bus = EventBus()
    event_bus.subscribe(broadcast_event)

    orchestrator = Orchestrator(event_bus)
    orchestrator.enable_memory()
    orchestrator.enable_consciousness()
    orchestrator.enable_experience_ingestion()
    orchestrator.enable_self_tuning()
    orchestrator.enable_meta_agent()
    await orchestrator.start()

    # Initialize integration agents if configured
    await _init_integrations(orchestrator, event_bus, settings)

    # Singleton RAG engine
    try:
        from skyn3t.rag.rag_engine import RAGEngine
        rag_engine = RAGEngine()
        await rag_engine.initialize()
        app.state.rag_engine = rag_engine
    except Exception as e:
        print(f"RAGEngine initialization warning: {e}")
        app.state.rag_engine = None

    print(f"🚀 SkyN3t Orchestrator started on {settings.web_host}:{settings.web_port}")

    yield

    # Shutdown
    if orchestrator:
        await orchestrator.stop()
    print("👋 SkyN3t Orchestrator stopped")


async def _init_integrations(
    orchestrator: Orchestrator,
    event_bus: EventBus,
    settings,
) -> None:
    """Initialize external service integrations (optional)."""
    # Integrations are loaded lazily — no auto-start to keep startup fast
    pass


app = FastAPI(
    title="SkyN3t Orchestrator",
    description="Multi-agent orchestrator with self-healing, RAG, and autonomous execution",
    version="0.1.0",
    lifespan=lifespan,
)

# Include webhook routers
app.include_router(github_webhook_router)

# CORS
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main dashboard."""
    return DASHBOARD_HTML


@app.get("/api/status")
async def get_status():
    """Get system status."""
    if not orchestrator:
        return {"status": "not_initialized"}
    return orchestrator.get_system_status()


@app.get("/metrics", response_class=PlainTextResponse)
async def get_metrics():
    """Prometheus metrics endpoint."""
    return PlainTextResponse(
        content=generate_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/health")
async def get_health():
    """Detailed health check endpoint."""
    registry = get_health_registry()
    result = await registry.run_all()
    status_code = 200
    if result["status"] == "unhealthy":
        status_code = 503
    elif result["status"] == "degraded":
        status_code = 200
    from fastapi.responses import JSONResponse
    return JSONResponse(content=result, status_code=status_code)


@app.get("/traces")
async def get_traces(limit: int = 50):
    """Return recent finished traces."""
    tracer = get_tracer()
    spans = tracer.get_recent_spans(limit=limit)
    return {"traces": [span.to_dict() for span in spans]}


@app.post("/api/fallback")
async def register_fallback(data: Dict[str, Any]):
    """Register a fallback chain. Example: {'capability': 'code_generation', 'agents': ['claude', 'copilot', 'kimi']}"""
    if not orchestrator:
        return {"error": "Orchestrator not initialized"}
    capability = data.get("capability")
    agents = data.get("agents", [])
    strategy = data.get("strategy", "priority")
    orchestrator.register_fallback_chain(capability, agents, strategy)
    return {"status": "registered", "capability": capability, "agents": agents}


@app.get("/api/fallback")
async def get_fallback_status():
    """Get fallback manager status."""
    if not orchestrator:
        return {"error": "Orchestrator not initialized"}
    return orchestrator.get_fallback_status()


@app.get("/api/agents")
async def list_agents():
    """List all registered agents."""
    if not orchestrator:
        return {"agents": []}
    return {"agents": [agent.get_stats() for agent in orchestrator.agents.values()]}


@app.post("/api/agents")
async def register_new_agent(data: Dict[str, Any]):
    """Register a new agent dynamically."""
    if not orchestrator:
        return {"error": "Orchestrator not initialized"}

    name = data.get("name")
    provider = data.get("provider", "openai")
    model = data.get("model")
    cli_agent = data.get("cli_agent", False)

    if not name:
        return {"error": "Agent name is required"}

    if name in orchestrator.agents:
        return {"error": f"Agent '{name}' already exists"}

    try:
        if cli_agent or provider in ("claude", "kimi", "copilot"):
            from skyn3t.adapters.claude_cli import ClaudeCLIAgent
            from skyn3t.adapters.copilot_cli import CopilotCLIAgent
            from skyn3t.adapters.kimi_cli import KimiCLIAgent
            if provider == "claude":
                agent = ClaudeCLIAgent(
                    name=name,
                    event_bus=event_bus,
                    config={"model": model} if model else {},
                )
            elif provider == "kimi":
                agent = KimiCLIAgent(
                    name=name,
                    event_bus=event_bus,
                    config={"model": model} if model else {},
                )
            elif provider == "copilot":
                agent = CopilotCLIAgent(
                    name=name,
                    event_bus=event_bus,
                )
            else:
                return {"error": f"Unsupported CLI provider: {provider}"}
        elif provider in ("anthropic", "claude"):
            from skyn3t.adapters.anthropic_adapter import ClaudeAgent
            agent = ClaudeAgent(
                name=name,
                event_bus=event_bus,
                model=model or "claude-3-opus-20240229",
            )
        elif provider == "github":
            from skyn3t.agents.github_explorer import GitHubExplorerAgent
            agent = GitHubExplorerAgent(
                name=name,
                event_bus=event_bus,
            )
        elif provider == "kimi":
            from skyn3t.adapters.kimi_adapter import KimiAgent
            agent = KimiAgent(
                name=name,
                event_bus=event_bus,
                model=model or "kimi-latest",
            )
        elif provider == "copilot":
            from skyn3t.adapters.copilot_adapter import CopilotAgent
            agent = CopilotAgent(
                name=name,
                event_bus=event_bus,
            )
        else:
            return {"error": f"Unsupported provider: {provider}"}

        await agent.initialize()
        await agent.start()
        orchestrator.register_agent(agent)

        return {"status": "registered", "agent": agent.get_stats()}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/agents/{name}/config")
async def agent_config_get(name: str):
    """Read live config view for one agent."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)
    a = orchestrator.agents.get(name)
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        return a.get_config_view()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.patch("/api/agents/{name}/config")
async def agent_config_patch(name: str, payload: Dict[str, Any]):
    """Apply a patch live and persist to the override store."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)
    a = orchestrator.agents.get(name)
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    res = a.apply_override(payload or {})
    try:
        from skyn3t.config.agent_overrides import get_override_store
        get_override_store().set(name, payload or {})
    except Exception:
        pass
    return res


@app.post("/api/agents/{name}/enable")
async def agent_enable(name: str):
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)
    a = orchestrator.agents.get(name)
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    a.apply_override({"enabled": True})
    try:
        from skyn3t.config.agent_overrides import get_override_store
        get_override_store().set(name, {"enabled": True})
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/agents/{name}/disable")
async def agent_disable(name: str):
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)
    a = orchestrator.agents.get(name)
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    a.apply_override({"enabled": False})
    try:
        from skyn3t.config.agent_overrides import get_override_store
        get_override_store().set(name, {"enabled": False})
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/agents/types")
async def agent_types():
    """List base types available for creating custom agents."""
    types = ["blank", "ResearchAgent", "ArchitectAgent", "WriterAgent", "DesignerAgent",
             "MarketerAgent", "ReviewerAgent", "BusinessAnalystAgent", "CodeAgent",
             "FileOpsAgent", "GitHubExplorerAgent", "GitHubIngestorAgent",
             "ExplorerAgent", "CodeImproverAgent", "SchedulerAgent"]
    return {"types": types}


@app.post("/api/agents/create")
async def agent_create(payload: Dict[str, Any]):
    """Create a new custom agent, persist its spec, and register it live."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)
    name = ((payload or {}).get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    if name in orchestrator.agents:
        return JSONResponse({"error": "agent name already exists"}, status_code=409)
    base_type = (payload or {}).get("base_type") or "blank"
    # persist first so the orchestrator boot logic can re-create on restart
    try:
        from skyn3t.config.custom_agents import get_custom_store
        get_custom_store().upsert({**(payload or {}), "name": name})
    except Exception as e:
        return JSONResponse({"error": f"persist failed: {e}"}, status_code=500)
    # also register live (avoid restart)
    try:
        import importlib
        import inspect as _ins
        mod = importlib.import_module("skyn3t.agents")
        cls = getattr(mod, base_type, None)
        if cls is None and base_type == "blank":
            from skyn3t.agents.research_agent import ResearchAgent as _Blank
            cls = _Blank
        if cls is None:
            return JSONResponse({"error": f"unknown base_type {base_type}"}, status_code=400)
        sig = _ins.signature(cls)
        kwargs = {}
        if "event_bus" in sig.parameters:
            kwargs["event_bus"] = orchestrator.event_bus
        if "rag" in sig.parameters:
            kwargs["rag"] = getattr(orchestrator, "_rag", None)
        if "name" in sig.parameters:
            kwargs["name"] = name
        agent = cls(**kwargs)
        if hasattr(agent, "initialize"):
            init = agent.initialize()
            if _ins.iscoroutine(init):
                await init
        if hasattr(agent, "apply_override"):
            agent.apply_override(payload or {})
        orchestrator.register_agent(agent)
    except Exception as e:
        return JSONResponse({"error": f"register failed: {e}"}, status_code=500)
    return {"ok": True, "name": name}


@app.delete("/api/agents/{name}")
async def agent_delete(name: str):
    """Delete a registered agent and remove its persisted custom spec/overrides."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)
    if name not in orchestrator.agents:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        orchestrator.agents.pop(name, None)
        orchestrator.agent_registry.pop(name, None)
    except Exception:
        pass
    try:
        from skyn3t.config.custom_agents import get_custom_store
        get_custom_store().delete(name)
    except Exception:
        pass
    try:
        from skyn3t.config.agent_overrides import get_override_store
        get_override_store().delete(name)
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/llm/backends")
async def llm_backends():
    return {"backends": [
        "auto", "claude_cli", "kimi_cli", "copilot_cli", "openai_cli",
        "anthropic", "openrouter", "deterministic",
    ]}


@app.get("/api/llm/models")
async def llm_models(backend: str = "auto"):
    from skyn3t.adapters.model_catalog import list_models
    return {"backend": backend, "models": await list_models(backend)}


@app.post("/api/agents/{agent_name}/task")
async def submit_task(agent_name: str, task_data: Dict[str, Any]):
    """Submit a task to an agent."""
    from skyn3t.core.agent import TaskRequest

    if not orchestrator:
        return {"error": "Orchestrator not initialized"}

    task = TaskRequest(
        title=task_data.get("title", "Untitled"),
        description=task_data.get("description", ""),
        input_data=task_data.get("input", {}),
        priority=task_data.get("priority", 0),
    )

    task_id = await orchestrator.submit_task(task, agent_name=agent_name)
    return {"task_id": task_id, "status": "submitted"}


@app.post("/api/agents/{agent_name}/exec")
async def exec_agent(agent_name: str, data: Dict[str, Any]):
    """Quick one-off execution on an agent."""
    from skyn3t.core.agent import TaskRequest

    if not orchestrator:
        return {"error": "Orchestrator not initialized"}

    agent = orchestrator.get_agent(agent_name)
    if not agent:
        return {"error": f"Agent '{agent_name}' not found"}

    prompt = data.get("prompt", "") or data.get("message", "")
    stdin = data.get("stdin", "")

    # Ensure agent is initialized
    if not agent.metadata.get("initialized"):
        try:
            await agent.initialize()
        except Exception as e:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=500,
                content={"error": f"Agent initialization failed: {e}"},
            )

    task = TaskRequest(
        title="Quick exec",
        description=prompt,
        input_data={"message": prompt, "stdin": stdin},
    )

    result = await agent.execute(task)
    return {
        "task_id": task.task_id,
        "success": result.success,
        "output": result.output.get("response", str(result.output)) if result.success else None,
        "error": result.error,
        "execution_time_ms": result.execution_time_ms,
    }


@app.get("/api/tasks/{task_id}/result")
async def get_task_result(task_id: str):
    """Get task result."""
    if not orchestrator:
        return {"error": "Orchestrator not initialized"}

    result = orchestrator.get_task_result(task_id)
    if result:
        return {
            "task_id": task_id,
            "success": result.success,
            "output": result.output,
            "error": result.error,
            "execution_time_ms": result.execution_time_ms,
        }
    return {"task_id": task_id, "status": "pending"}


@app.post("/api/pipeline")
async def create_pipeline(data: Dict[str, Any]):
    """Create a pipeline. Optionally run it immediately."""
    if not orchestrator:
        return {"error": "Orchestrator not initialized"}

    name = data.get("name", "pipeline")
    agents = data.get("agents", [])
    prompts = data.get("prompts", [])
    run_now = data.get("run", False)

    if len(agents) != len(prompts):
        return {"error": "Number of agents and prompts must match"}

    try:
        pipeline_id = await orchestrator.create_and_run_pipeline(
            name=name,
            agent_names=agents,
            prompts=prompts,
            collaborative=False,
        )
        return {
            "pipeline_id": pipeline_id,
            "name": name,
            "status": "running" if run_now else "created",
            "stages": len(agents),
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/pipeline/{pipeline_id}/run")
async def run_pipeline(pipeline_id: str):
    """Run a pipeline."""
    if not orchestrator:
        return {"error": "Orchestrator not initialized"}

    pipeline = orchestrator._pipelines.get(pipeline_id)
    if not pipeline:
        return {"error": f"Pipeline '{pipeline_id}' not found"}

    if not pipeline.is_completed:
        asyncio.create_task(orchestrator._run_pipeline(pipeline, [s.name for s in pipeline.stages]))
    return {"pipeline_id": pipeline_id, "status": "running"}


@app.get("/api/pipeline/{pipeline_id}")
async def get_pipeline(pipeline_id: str):
    """Get pipeline status."""
    if not orchestrator:
        return {"error": "Orchestrator not initialized"}

    pipeline_data = orchestrator.get_pipeline(pipeline_id)
    if not pipeline_data:
        return {"error": f"Pipeline '{pipeline_id}' not found"}
    return pipeline_data


@app.post("/api/conversation")
async def run_conversation(data: Dict[str, Any]):
    """Run a multi-agent conversation."""
    if not orchestrator:
        return {"error": "Orchestrator not initialized"}

    initiator = data.get("initiator", "")
    participants = data.get("participants", [])
    topic = data.get("topic", "")
    rounds = data.get("rounds", 3)

    conversation = await orchestrator.run_conversation(
        initiator=initiator,
        participants=participants,
        topic=topic,
        rounds=rounds,
    )

    return {"conversation": conversation}


async def _get_rag_engine(request: Request):
    engine = getattr(request.app.state, "rag_engine", None)
    if engine is None:
        from skyn3t.rag.rag_engine import RAGEngine
        engine = RAGEngine()
        await engine.initialize()
        request.app.state.rag_engine = engine
    return engine


@app.post("/api/rag/query")
async def rag_query(request: Request, data: Dict[str, Any]):
    """Query the RAG system."""
    engine = await _get_rag_engine(request)

    query = data.get("query", "")
    n_results = data.get("n_results", 5)

    result = await engine.answer(query, n_results=n_results)
    return result


@app.post("/api/rag/add")
async def rag_add(request: Request, data: Dict[str, Any]):
    """Add knowledge to RAG."""
    engine = await _get_rag_engine(request)

    content = data.get("content", "")
    title = data.get("title", "Untitled")
    source = data.get("source", "")
    doc_type = data.get("doc_type", "text")

    ids = await engine.add_knowledge(
        content=content,
        title=title,
        source=source,
        doc_type=doc_type,
    )

    return {"ids": ids, "status": "added"}


# ------------------------------------------------------------------
# Brain / Memory API
# ------------------------------------------------------------------

@app.get("/api/memory/stats")
async def memory_stats():
    """Get persistent memory statistics."""
    if not orchestrator or not orchestrator._memory:
        return {"enabled": False}
    stats = await orchestrator._memory.get_stats()
    return {"enabled": True, **stats}


@app.get("/api/memory/sessions")
async def list_sessions():
    """List active collective consciousness sessions."""
    if not orchestrator or not orchestrator._consciousness:
        return {"sessions": []}
    sessions = await orchestrator._consciousness.list_sessions()
    return {"sessions": sessions}


@app.get("/api/memory/sessions/{session_id}")
async def get_session(session_id: str):
    """Get a session's context and history."""
    if not orchestrator or not orchestrator._consciousness:
        return {"error": "consciousness not enabled"}
    sess = await orchestrator._consciousness.get_session(session_id)
    if not sess:
        return {"error": "session not found"}
    recent = await orchestrator._consciousness.get_recent_context(session_id, limit=10)
    return {"session_id": session_id, "context": sess, "recent_activity": recent}


@app.get("/api/memory/insights")
async def get_insights(agent: Optional[str] = None, capability: Optional[str] = None, limit: int = 20):
    """Get recent agent insights from collective consciousness."""
    if not orchestrator or not orchestrator._consciousness:
        return {"insights": []}
    insights = await orchestrator._consciousness.get_insights(
        agent_name=agent, capability=capability, limit=limit
    )
    return {"insights": insights}


@app.get("/api/memory/experiences")
async def query_experiences(request: Request, query: str = "", limit: int = 10):
    """Semantic search over past experiences via RAG."""
    engine = await _get_rag_engine(request)
    result = await engine.query(query, n_results=limit, filter_dict={"doc_type": "experience"})
    return result


@app.get("/api/memory/tuning")
async def get_tuning_status():
    """Get self-tuning engine status."""
    if not orchestrator or not orchestrator._tuner:
        return {"enabled": False}
    return {"enabled": True, **orchestrator._tuner.get_status()}


@app.get("/api/meta/status")
async def meta_agent_status():
    """Get meta-agent status and recent actions."""
    if not orchestrator or not orchestrator._meta_agent:
        return {"enabled": False}
    return {"enabled": True, **orchestrator._meta_agent.get_status()}


@app.post("/api/meta/pause")
async def pause_meta_agent():
    """Pause the autonomous meta-agent."""
    if not orchestrator or not orchestrator._meta_agent:
        return {"error": "meta-agent not enabled"}
    orchestrator._meta_agent.pause()
    return {"status": "paused"}


@app.post("/api/meta/resume")
async def resume_meta_agent():
    """Resume the autonomous meta-agent."""
    if not orchestrator or not orchestrator._meta_agent:
        return {"error": "meta-agent not enabled"}
    orchestrator._meta_agent.resume()
    return {"status": "resumed"}


@app.post("/api/orchestrator/reorder")
async def trigger_reorder():
    """Trigger manual task reordering."""
    if not orchestrator:
        return {"error": "orchestrator not initialized"}
    result = await orchestrator.reorder_tasks()
    return result


@app.get("/api/consciousness/status")
async def consciousness_status():
    """Get collective consciousness status."""
    if not orchestrator or not orchestrator._consciousness:
        return {"enabled": False}
    status = await orchestrator._consciousness.get_status()
    return {"enabled": True, **status}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates."""
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            # Handle client messages if needed
            await websocket.send_json({"type": "ack", "data": data})
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception:
        await manager.disconnect(websocket)


@app.websocket("/ws/swarm")
async def swarm_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for the Swarm Live UI.

    Receives the same events as /ws but projected to the compact swarm
    schema: {"kind", "ts", "from", "to", "label", "meta"}.
    """
    await swarm_manager.connect(websocket)
    try:
        # On connect, replay the most recent ring-buffer entries so the
        # client's stream isn't blank until the next event arrives.
        for compact in list(_recent_swarm_events)[-50:]:
            try:
                await websocket.send_json({"type": "swarm", "data": compact})
            except Exception:
                break
        while True:
            try:
                data = await websocket.receive_json()
                await websocket.send_json({"type": "ack", "data": data})
            except WebSocketDisconnect:
                break
            except Exception:
                break
    finally:
        await swarm_manager.disconnect(websocket)


@app.get("/api/swarm/snapshot")
async def swarm_snapshot():
    """Compact snapshot of swarm state for initial render."""
    if not orchestrator:
        return {
            "agents": [],
            "running_tasks": [],
            "recent_messages": list(_recent_swarm_events),
        }

    agents_out: List[Dict[str, Any]] = []
    for name, agent in orchestrator.agents.items():
        try:
            stats = agent.get_stats()
        except Exception:
            stats = {}
        agents_out.append({
            "name": name,
            "state": stats.get("status", "idle"),
            "queue_depth": stats.get("queue_size", 0),
            "capabilities": stats.get("capabilities", []),
            "provider": stats.get("provider"),
            "current_task": stats.get("current_task"),
        })

    running_tasks_out: List[Dict[str, Any]] = []
    for task_id, task in list(orchestrator.running_tasks.items()):
        # Best-effort agent attribution: scan each agent for matching current_task
        owning_agent = None
        for a in orchestrator.agents.values():
            try:
                if getattr(a, "_current_task", None) and a._current_task.task_id == task_id:
                    owning_agent = a.name
                    break
            except Exception:
                continue
        running_tasks_out.append({
            "task_id": task_id,
            "agent": owning_agent,
            "started_at": getattr(task, "started_at", None),
            "title": getattr(task, "title", "") or "Untitled",
            "session_id": getattr(task, "session_id", None),
        })

    return {
        "agents": agents_out,
        "running_tasks": running_tasks_out,
        "recent_messages": list(_recent_swarm_events),
    }


# ─── Proposals (Cortex) ───
@app.get("/api/proposals")
async def proposals_list(status: str | None = None):
    from skyn3t.cortex import get_store
    return {"proposals": [p.to_public() for p in get_store().list(status=status)]}


@app.get("/api/proposals/{pid}")
async def proposals_get(pid: str):
    from skyn3t.cortex import get_store
    p = get_store().get(pid)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return p.to_public()


@app.post("/api/proposals/{pid}/approve")
async def proposals_approve(pid: str):
    from skyn3t.cortex import get_store
    return await get_store().approve(pid)


@app.post("/api/proposals/{pid}/reject")
async def proposals_reject(pid: str, payload: dict | None = None):
    from skyn3t.cortex import get_store
    reason = (payload or {}).get("reason", "")
    return get_store().reject(pid, reason=reason)


@app.post("/api/proposals/feature")
async def proposals_feature(payload: dict):
    idea = (payload or {}).get("idea", "").strip()
    if not idea:
        return JSONResponse({"error": "idea required"}, status_code=400)
    try:
        from skyn3t.cortex.feature_suggester import FeatureSuggester
        # use the orchestrator's instance if present, else a transient one
        suggester = getattr(orchestrator, "_feature_suggester", None) or FeatureSuggester(event_bus)
        pid = suggester.file_user_idea(idea, source="user_dashboard")
        return {"ok": bool(pid), "proposal_id": pid}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.websocket("/ws/proposals")
async def ws_proposals(websocket: WebSocket):
    await websocket.accept()
    from skyn3t.cortex import get_store
    q = get_store().subscribe()
    try:
        # send a snapshot
        await websocket.send_json({
            "type": "snapshot",
            "proposals": [p.to_public() for p in get_store().list(status="pending")],
        })
        while True:
            evt = await q.get()
            await websocket.send_json(evt)
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ─── Project Studio ───
def _get_studio_runner(app):
    runner = getattr(app.state, "studio_runner", None)
    if runner is None:
        from skyn3t.studio import StudioRunner
        runner = StudioRunner(event_bus=event_bus, rag=getattr(app.state, "rag_engine", None))
        app.state.studio_runner = runner
    return runner


@app.get("/api/studio/templates")
async def studio_templates():
    from skyn3t.studio import list_templates
    return {"templates": list_templates()}


@app.post("/api/studio/start")
async def studio_start(payload: dict):
    from skyn3t.studio import StudioRunner  # noqa: F401  ensure package importable
    template_key = payload.get("template")
    brief = (payload.get("brief") or "").strip()
    if not template_key:
        return JSONResponse({"error": "missing template"}, status_code=400)
    runner = _get_studio_runner(app)
    # don't await; run in background so the HTTP request returns fast
    task = asyncio.create_task(runner.start(template_key, brief, extra=payload.get("extra") or {}))
    # store reference so it isn't GC'd
    app.state.studio_tasks = getattr(app.state, "studio_tasks", set())
    app.state.studio_tasks.add(task)
    task.add_done_callback(app.state.studio_tasks.discard)
    return {"accepted": True, "template": template_key}


@app.get("/api/studio/projects")
async def studio_projects():
    runner = _get_studio_runner(app)
    return {"projects": runner.list_projects()}


@app.get("/api/studio/projects/{slug}")
async def studio_project(slug: str):
    runner = _get_studio_runner(app)
    proj = runner.get_project(slug)
    if proj is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return proj


@app.get("/api/studio/projects/{slug}/file")
async def studio_project_file(slug: str, path: str):
    # safe read of an artifact; reject path traversal
    base = (Path("projects") / slug).resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        return JSONResponse({"error": "invalid path"}, status_code=400)
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return JSONResponse({"error": "binary file not supported"}, status_code=415)
    return PlainTextResponse(text)


@app.get("/api/studio/projects/{slug}/zip")
async def studio_project_zip(slug: str):
    runner = _get_studio_runner(app)
    try:
        zip_path = runner.export_zip(slug)
    except FileNotFoundError:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(zip_path), media_type="application/zip", filename=f"{slug}.zip")


# Dashboard HTML
DASHBOARD_HTML = open(Path(__file__).parent / "dashboard.html").read()

# Fallback inline dashboard (kept for reference, replaced above)
OLD_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SkyN3t Orchestrator</title>
    <style>
        :root {
            --bg-primary: #0a0e1a;
            --bg-secondary: #121827;
            --bg-card: #1a2332;
            --border-color: #2a3441;
            --text-primary: #e2e8f0;
            --text-secondary: #94a3b8;
            --accent: #3b82f6;
            --accent-glow: rgba(59, 130, 246, 0.3);
            --success: #10b981;
            --warning: #f59e0b;
            --error: #ef4444;
            --idle: #6b7280;
            --cli-badge: #f97316;
            --api-badge: #8b5cf6;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
        }

        .header {
            background: var(--bg-secondary);
            border-bottom: 1px solid var(--border-color);
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .header h1 {
            font-size: 1.5rem;
            background: linear-gradient(135deg, var(--accent), #8b5cf6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        .status-badge {
            padding: 0.375rem 1rem;
            border-radius: 9999px;
            font-size: 0.875rem;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .status-badge.online {
            background: rgba(16, 185, 129, 0.1);
            color: var(--success);
        }

        .status-badge::before {
            content: '';
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: currentColor;
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 2rem;
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2rem;
        }

        .card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 0.75rem;
            padding: 1.5rem;
        }

        .card h2 {
            font-size: 0.875rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-secondary);
            margin-bottom: 1rem;
        }

        .stat-value {
            font-size: 2.5rem;
            font-weight: 700;
            color: var(--accent);
        }

        .stat-label {
            font-size: 0.875rem;
            color: var(--text-secondary);
            margin-top: 0.25rem;
        }

        .agents-list {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }

        .agent-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0.875rem 1rem;
            background: var(--bg-secondary);
            border-radius: 0.5rem;
            border: 1px solid var(--border-color);
            transition: all 0.2s;
        }

        .agent-item:hover {
            border-color: var(--accent);
            box-shadow: 0 0 0 3px var(--accent-glow);
        }

        .agent-info {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .agent-avatar {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            background: linear-gradient(135deg, var(--accent), #8b5cf6);
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 600;
            font-size: 0.875rem;
        }

        .agent-details h3 {
            font-size: 0.9375rem;
            font-weight: 600;
        }

        .agent-details p {
            font-size: 0.8125rem;
            color: var(--text-secondary);
        }

        .agent-status {
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 500;
            text-transform: uppercase;
        }

        .agent-status.idle { background: rgba(16, 185, 129, 0.1); color: var(--success); }
        .agent-status.busy { background: rgba(59, 130, 246, 0.1); color: var(--accent); }
        .agent-status.error { background: rgba(239, 68, 68, 0.1); color: var(--error); }
        .agent-status.offline { background: rgba(107, 114, 128, 0.1); color: var(--idle); }

        .agent-badges {
            display: flex;
            gap: 0.375rem;
        }

        .badge {
            padding: 0.125rem 0.5rem;
            border-radius: 9999px;
            font-size: 0.6875rem;
            font-weight: 600;
            text-transform: uppercase;
        }

        .badge.cli {
            background: rgba(249, 115, 22, 0.15);
            color: var(--cli-badge);
        }

        .badge.api {
            background: rgba(139, 92, 246, 0.15);
            color: var(--api-badge);
        }

        .section-title {
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .event-log {
            max-height: 400px;
            overflow-y: auto;
            font-family: 'Courier New', monospace;
            font-size: 0.8125rem;
        }

        .event-item {
            padding: 0.5rem;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            gap: 0.75rem;
        }

        .event-time {
            color: var(--text-secondary);
            white-space: nowrap;
        }

        .event-type {
            color: var(--accent);
            white-space: nowrap;
        }

        .event-source {
            color: var(--warning);
            white-space: nowrap;
        }

        .event-pipeline {
            color: var(--cli-badge);
            white-space: nowrap;
        }

        .controls {
            display: flex;
            gap: 0.75rem;
            margin-bottom: 1.5rem;
            flex-wrap: wrap;
        }

        .btn {
            padding: 0.625rem 1.25rem;
            border-radius: 0.5rem;
            border: 1px solid var(--border-color);
            background: var(--bg-card);
            color: var(--text-primary);
            font-size: 0.875rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }

        .btn:hover {
            border-color: var(--accent);
            background: var(--bg-secondary);
        }

        .btn-primary {
            background: var(--accent);
            border-color: var(--accent);
            color: white;
        }

        .btn-primary:hover {
            background: #2563eb;
        }

        .pipelines-list {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            margin-bottom: 1.5rem;
        }

        .pipeline-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0.75rem 1rem;
            background: var(--bg-secondary);
            border-radius: 0.5rem;
            border: 1px solid var(--border-color);
        }

        .pipeline-info {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .pipeline-status {
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 500;
            text-transform: uppercase;
        }

        .pipeline-status.completed { background: rgba(16, 185, 129, 0.1); color: var(--success); }
        .pipeline-status.failed { background: rgba(239, 68, 68, 0.1); color: var(--error); }
        .pipeline-status.running { background: rgba(59, 130, 246, 0.1); color: var(--accent); }
        .pipeline-status.pending { background: rgba(107, 114, 128, 0.1); color: var(--idle); }

        @media (max-width: 768px) {
            .container { padding: 1rem; }
            .header { padding: 1rem; flex-direction: column; gap: 1rem; }
            .grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <header class="header">
        <h1>🤖 SkyN3t Orchestrator</h1>
        <div class="status-badge online" id="systemStatus">Online</div>
    </header>

    <div class="container">
        <div class="grid">
            <div class="card">
                <h2>Total Agents</h2>
                <div class="stat-value" id="totalAgents">0</div>
                <div class="stat-label">Active agents in swarm</div>
            </div>
            <div class="card">
                <h2>Running Tasks</h2>
                <div class="stat-value" id="runningTasks">0</div>
                <div class="stat-label">Currently executing</div>
            </div>
            <div class="card">
                <h2>Completed Tasks</h2>
                <div class="stat-value" id="completedTasks">0</div>
                <div class="stat-label">Since startup</div>
            </div>
            <div class="card">
                <h2>Pipelines</h2>
                <div class="stat-value" id="totalPipelines">0</div>
                <div class="stat-label">Active pipelines</div>
            </div>
        </div>

        <div class="controls">
            <button class="btn btn-primary" onclick="refreshStatus()">🔄 Refresh</button>
            <button class="btn" onclick="runConversation()">💬 Conversation</button>
            <button class="btn" onclick="queryRAG()">📚 RAG Query</button>
            <button class="btn" onclick="exploreGitHub()">🐙 Explore GitHub</button>
            <button class="btn" onclick="createPipeline()">🔄 Pipeline</button>
        </div>

        <div class="card" style="margin-bottom: 1.5rem;">
            <h2 class="section-title">👥 Agent Swarm</h2>
            <div class="agents-list" id="agentsList">
                <p style="color: var(--text-secondary);">Loading agents...</p>
            </div>
        </div>

        <div class="card" style="margin-bottom: 1.5rem;">
            <h2 class="section-title">🔄 Pipelines</h2>
            <div class="pipelines-list" id="pipelinesList">
                <p style="color: var(--text-secondary);">No pipelines yet.</p>
            </div>
        </div>

        <div class="card">
            <h2 class="section-title">📡 Event Stream</h2>
            <div class="event-log" id="eventLog">
                <div class="event-item">
                    <span class="event-time">--:--:--</span>
                    <span class="event-type">SYSTEM</span>
                    <span class="event-source">orchestrator</span>
                    <span>Waiting for events...</span>
                </div>
            </div>
        </div>
    </div>

    <script>
        let ws = null;
        let startTime = Date.now();
        let pipelines = [];

        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.type === 'event') {
                    addEvent(data.data);
                }
            };

            ws.onclose = () => {
                setTimeout(connectWebSocket, 3000);
            };
        }

        function addEvent(event) {
            const log = document.getElementById('eventLog');
            const time = new Date(event.timestamp).toLocaleTimeString();
            const item = document.createElement('div');
            item.className = 'event-item';

            let typeClass = 'event-type';
            if (event.event_type && event.event_type.startsWith('PIPELINE')) {
                typeClass = 'event-pipeline';
            }

            item.innerHTML = `
                <span class="event-time">${time}</span>
                <span class="${typeClass}">${event.event_type}</span>
                <span class="event-source">${event.source}</span>
                <span>${JSON.stringify(event.payload).slice(0, 100)}...</span>
            `;
            log.insertBefore(item, log.firstChild);
            while (log.children.length > 50) {
                log.removeChild(log.lastChild);
            }

            // Refresh status on pipeline events
            if (event.event_type && event.event_type.startsWith('PIPELINE')) {
                refreshStatus();
            }
        }

        async function refreshStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();

                document.getElementById('totalAgents').textContent = data.total_agents || 0;
                document.getElementById('runningTasks').textContent = data.running_tasks || 0;
                document.getElementById('completedTasks').textContent = data.completed_tasks || 0;
                document.getElementById('totalPipelines').textContent = data.pipelines || 0;

                const agentsList = document.getElementById('agentsList');
                agentsList.innerHTML = '';

                for (const [name, stats] of Object.entries(data.agents || {})) {
                    const item = document.createElement('div');
                    item.className = 'agent-item';
                    const modeBadge = stats.cli_agent
                        ? '<span class="badge cli">CLI</span>'
                        : '<span class="badge api">API</span>';
                    item.innerHTML = `
                        <div class="agent-info">
                            <div class="agent-avatar">${name[0].toUpperCase()}</div>
                            <div class="agent-details">
                                <h3>${name}</h3>
                                <p>${stats.type} • ${stats.provider} • Queue: ${stats.queue_size}</p>
                            </div>
                        </div>
                        <div class="agent-badges">
                            ${modeBadge}
                            <span class="agent-status ${stats.status}">${stats.status}</span>
                        </div>
                    `;
                    agentsList.appendChild(item);
                }
            } catch (e) {
                console.error('Failed to refresh:', e);
            }
        }

        function updateUptime() {
            const elapsed = Math.floor((Date.now() - startTime) / 1000);
            const hours = Math.floor(elapsed / 3600).toString().padStart(2, '0');
            const minutes = Math.floor((elapsed % 3600) / 60).toString().padStart(2, '0');
            const seconds = (elapsed % 60).toString().padStart(2, '0');
            document.getElementById('uptime').textContent = `${hours}:${minutes}:${seconds}`;
        }

        async function runConversation() {
            const topic = prompt('Enter conversation topic:', 'How can we improve our codebase?');
            if (!topic) return;

            try {
                const res = await fetch('/api/conversation', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        initiator: 'user',
                        participants: Object.keys((await (await fetch('/api/status')).json()).agents || {}),
                        topic,
                        rounds: 2
                    })
                });
                const data = await res.json();
                alert('Conversation completed! Check console for results.');
                console.log(data);
            } catch (e) {
                alert('Error: ' + e.message);
            }
        }

        async function queryRAG() {
            const query = prompt('Enter RAG query:', 'What is the system architecture?');
            if (!query) return;

            try {
                const res = await fetch('/api/rag/query', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({query, n_results: 3})
                });
                const data = await res.json();
                alert('Answer: ' + data.answer.slice(0, 200) + '...');
            } catch (e) {
                alert('Error: ' + e.message);
            }
        }

        async function exploreGitHub() {
            const repo = prompt('Enter GitHub repo (owner/repo):', 'torvalds/linux');
            if (!repo) return;
            const [owner, name] = repo.split('/');

            try {
                const res = await fetch(`/api/agents/github_explorer/task`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        title: 'Analyze repo',
                        input: {
                            task_type: 'repo_analysis',
                            owner,
                            repo: name
                        }
                    })
                });
                const data = await res.json();
                alert('Task submitted: ' + data.task_id);
            } catch (e) {
                alert('Error: ' + e.message);
            }
        }

        async function createPipeline() {
            const agentsInput = prompt('Enter agents (comma-separated):', 'claude,kimi');
            if (!agentsInput) return;
            const promptsInput = prompt('Enter prompts (comma-separated, quoted):', "'write a function','review it'");
            if (!promptsInput) return;

            try {
                const res = await fetch('/api/pipeline', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        name: 'Web Pipeline',
                        agents: agentsInput.split(',').map(s => s.trim()),
                        prompts: promptsInput.split(',').map(s => s.trim().replace(/^['"]|['"]$/g, '')),
                        run: true
                    })
                });
                const data = await res.json();
                alert('Pipeline created: ' + data.pipeline_id);
                refreshStatus();
            } catch (e) {
                alert('Error: ' + e.message);
            }
        }

        connectWebSocket();
        refreshStatus();
        setInterval(refreshStatus, 5000);
        setInterval(updateUptime, 1000);
    </script>
</body>
</html>
"""
