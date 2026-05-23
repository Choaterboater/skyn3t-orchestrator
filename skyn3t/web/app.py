"""FastAPI web application for SkyN3t."""

import asyncio
import html
import ipaddress
import json
import logging
import mimetypes
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect, WebSocketException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette import status as starlette_status

from skyn3t.config.settings import get_settings
from skyn3t.core.agent import BaseAgent
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.core.models import init_db
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.integrations import github_webhook_router, telegram_webhook_router
from skyn3t.observability.health import get_health_registry
from skyn3t.observability.metrics import generate_metrics
from skyn3t.observability.tracing import get_tracer
from skyn3t.registry.catalog import get_agent_catalog_metadata
from skyn3t.studio.penpot_handoff import build_penpot_manifest, build_penpot_package

# Global orchestrator instance
orchestrator: Optional[Orchestrator] = None
event_bus: Optional[EventBus] = None
trajectory_logger: Optional[Any] = None
logger = logging.getLogger("skyn3t.web.app")


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


def _finish_broadcast(task: asyncio.Task) -> None:
    """Remove tracked broadcast tasks and log unexpected failures."""
    _broadcast_tasks.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("WebSocket broadcast failed")


def _track_studio_task(
    task: asyncio.Task,
    *,
    runner: Any,
    slug: str,
    action: str,
) -> None:
    """Keep studio tasks alive and fail the manifest if the background task crashes."""
    app.state.studio_tasks = getattr(app.state, "studio_tasks", set())
    app.state.studio_tasks.add(task)

    def _on_done(fut: asyncio.Task) -> None:
        app.state.studio_tasks.discard(fut)
        if fut.cancelled():
            error = f"CancelledError: studio task cancelled while {action}"
        else:
            try:
                exc = fut.exception()
            except asyncio.CancelledError:
                error = f"CancelledError: studio task cancelled while {action}"
            else:
                if exc is None:
                    return
                logger.error(
                    "studio %s crashed for %s",
                    action,
                    slug,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                error = f"{type(exc).__name__}: {exc}"
        try:
            runner.mark_project_failed(
                slug,
                error,
                next_action=f"Project stopped while {action}.",
            )
        except Exception:
            logger.exception("could not persist failed studio task state for %s", slug)

    task.add_done_callback(_on_done)


def _schedule_broadcast(connection_manager: ConnectionManager, message: Dict[str, Any]) -> bool:
    """Schedule a websocket broadcast when a running loop is available."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("Skipping websocket broadcast without a running event loop")
        return False

    task = loop.create_task(connection_manager.broadcast(message))
    _broadcast_tasks.add(task)
    task.add_done_callback(_finish_broadcast)
    return True


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
        # LLM prompt/response exchanges (for live conversation panel)
        ("LLM_EXCHANGE", "convo"),
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
    payload = event.payload or {}
    if event.event_type == EventType.SYSTEM_ALERT:
        payload_kind = str(payload.get("kind") or "")
        if payload_kind.startswith("PROJECT_"):
            kind = "project"
        else:
            return None
    else:
        mapped_kind = _SWARM_KIND_MAP.get(event.event_type)
        if mapped_kind is None:
            return None
        kind = mapped_kind

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
    elif kind == "convo":
        label = (str(payload.get("agent", "?")) + " · " + str(payload.get("model", "")))[:140]
    elif kind == "project":
        label = (
            payload.get("stage")
            or payload.get("summary")
            or payload.get("message")
            or payload.get("kind")
            or event.event_type.name.lower()
        )
    else:
        label = event.event_type.name.lower()

    # Truncate label so the UI stays compact
    if isinstance(label, str) and len(label) > 200:
        label = label[:197] + "..."

    # Determine "to" target
    to_field = event.target or payload.get("to") or payload.get("to_agent") or payload.get("agent")

    meta: Dict[str, Any] = {
        "task_id": payload.get("task_id"),
        "session_id": payload.get("session_id"),
        "correlation_id": event.correlation_id,
        "payload": payload,
    }
    if kind == "convo":
        meta["prompt"] = (payload.get("prompt") or "")[:2000]
        meta["response"] = (payload.get("response") or "")[:2000]
        meta["model"] = payload.get("model", "")
        meta["backend"] = payload.get("backend", "")
        meta["duration_ms"] = payload.get("duration_ms", 0)

    compact = {
        "kind": kind,
        "ts": event.timestamp.isoformat() if isinstance(event.timestamp, datetime) else str(event.timestamp),
        "from": event.source,
        "to": to_field,
        "label": str(label) if label is not None else "",
        "event_type": event.event_type.name,
        "meta": meta,
    }
    return compact


def broadcast_event(event: Event) -> None:
    """Broadcast an event to all WebSocket clients."""
    _schedule_broadcast(manager, {
        "type": "event",
        "data": event.to_dict(),
    })

    # Also project to swarm clients if this event is one we surface
    compact = _project_swarm_event(event)
    if compact is not None:
        _recent_swarm_events.append(compact)
        _schedule_broadcast(swarm_manager, {
            "type": "swarm",
            "data": compact,
        })


async def _resume_cortex_proposals() -> Dict[str, int]:
    from skyn3t.cortex import get_store

    return await get_store().resume_inflight()


async def _retriage_cortex_proposals() -> Dict[str, int]:
    from skyn3t.cortex import get_store

    return await get_store().retriage_pending()


async def _reset_runtime_services() -> Dict[str, Any]:
    from skyn3t.cortex import get_store

    if orchestrator is None:
        raise RuntimeError("Orchestrator not initialized")
    store = get_store()
    cancelled = await store.cancel_inflight()
    await orchestrator.reset_cortex()
    replay = await store.resume_inflight()
    return {
        "ok": True,
        "services": ["cortex"],
        "cancelled": cancelled,
        "replayed": replay,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global orchestrator, event_bus, trajectory_logger

    settings = get_settings()

    # Initialize database
    await init_db()

    # Create event bus and orchestrator
    event_bus = EventBus()
    event_bus.subscribe(broadcast_event)
    # Token tracker: subscribes to LLM_EXCHANGE events and keeps
    # per-agent / per-project rollups. No-op if subscribe fails.
    try:
        from skyn3t.observability.token_tracker import get_default_tracker
        get_default_tracker().subscribe(event_bus)
    except Exception:
        logger.exception("token tracker subscribe failed")

    # Trajectory logger: captures task-level execution traces for dataset export.
    try:
        from skyn3t.observability.trajectory_logger import TrajectoryLogger

        trajectory_logger = TrajectoryLogger()
        trajectory_logger.subscribe(event_bus)
    except Exception:
        logger.exception("trajectory logger subscribe failed")
        trajectory_logger = None

    orchestrator = Orchestrator(event_bus)
    orchestrator.enable_memory()
    orchestrator.enable_consciousness()
    orchestrator.enable_experience_ingestion()
    orchestrator.enable_self_tuning()
    orchestrator.enable_meta_agent()
    await orchestrator.start()
    try:
        app.state.proposal_recovery = await _resume_cortex_proposals()
    except Exception:
        logger.exception("proposal recovery boot failed")
        app.state.proposal_recovery = {"requeued": 0, "failed_no_handler": 0}
    try:
        app.state.proposal_triage = await _retriage_cortex_proposals()
    except Exception:
        logger.exception("proposal triage boot failed")
        app.state.proposal_triage = {"auto_approved": 0, "auto_rejected": 0}

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
    # Cleanup loky/joblib semaphores to suppress leak warnings
    try:
        from loky import get_reusable_executor
        get_reusable_executor().shutdown()
    except Exception:
        pass
    print("👋 SkyN3t Orchestrator stopped")


async def _init_integrations(
    orchestrator: Orchestrator,
    event_bus: EventBus,
    settings,
) -> None:
    """Initialize external service integrations (optional)."""
    # Discord bot: only start if a token is configured. The bot runs as a
    # background task so it doesn't block server startup, and any failure
    # in the Gateway connection is logged but doesn't crash the app.
    if settings.discord_token:
        try:
            from skyn3t.integrations.discord_bot import DiscordBot
            # Runner is wired lazily — _get_studio_runner attaches it to
            # app.state on first access. We use a thunk so the bot picks
            # up the runner at message-receive time, not boot time.
            class _LazyRunner:
                def _runner(self):
                    return _get_studio_runner(app)
                def reserve_project(self, *args, **kwargs):
                    return self._runner().reserve_project(*args, **kwargs)
                async def start(self, *args, **kwargs):
                    return await self._runner().start(*args, **kwargs)
                def get_project(self, *args, **kwargs):
                    return self._runner().get_project(*args, **kwargs)
                def list_projects(self):
                    return self._runner().list_projects()
                async def resume_after_approval(self, *args, **kwargs):
                    return await self._runner().resume_after_approval(*args, **kwargs)

            bot = DiscordBot(event_bus, token=settings.discord_token, studio_runner=_LazyRunner())
            app.state.discord_bot = bot
            task = asyncio.create_task(bot.start())
            app.state.discord_bot_task = task
            logger.info("Discord bot starting in background")
        except Exception:
            logger.exception("Discord bot failed to start")

    # Telegram studio control surface (long-polling — no public URL needed).
    if settings.telegram_token and settings.telegram_user_id:
        try:
            from skyn3t.integrations.telegram_bot import TelegramBot

            class _TGLazyRunner:
                def _runner(self):
                    return _get_studio_runner(app)
                def reserve_project(self, *args, **kwargs):
                    return self._runner().reserve_project(*args, **kwargs)
                async def start(self, *args, **kwargs):
                    return await self._runner().start(*args, **kwargs)
                def get_project(self, *args, **kwargs):
                    return self._runner().get_project(*args, **kwargs)
                def list_projects(self):
                    return self._runner().list_projects()
                async def resume_after_approval(self, *args, **kwargs):
                    return await self._runner().resume_after_approval(*args, **kwargs)

            tg_bot = TelegramBot(
                token=settings.telegram_token,
                allowed_user_id=settings.telegram_user_id,
                studio_runner=_TGLazyRunner(),
            )
            app.state.telegram_bot = tg_bot
            tg_task = asyncio.create_task(tg_bot.start())
            app.state.telegram_bot_task = tg_task
            logger.info("Telegram bot starting in background")
        except Exception:
            logger.exception("Telegram bot failed to start")


app = FastAPI(
    title="SkyN3t Orchestrator",
    description="Multi-agent orchestrator with self-healing, RAG, and autonomous execution",
    version="0.1.0",
    lifespan=lifespan,
)

# Include webhook routers
app.include_router(github_webhook_router)
app.include_router(telegram_webhook_router)

# CORS
settings = get_settings()
_cors_origins = [origin for origin in settings.cors_origins if origin]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

_SESSION_COOKIE_NAME = "skyn3t_session"


def _is_loopback_host(host: Optional[str]) -> bool:
    if not host:
        return False
    normalized = host.strip().lower()
    if normalized in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(normalized.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _extract_bearer_token(header_value: Optional[str]) -> Optional[str]:
    if not header_value:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _origin_matches(expected_scheme: str, expected_netloc: str, origin: Optional[str]) -> bool:
    if not origin:
        return True
    parsed = urlsplit(origin)
    return parsed.scheme == expected_scheme and parsed.netloc == expected_netloc


def _http_origin_allowed(request: Request) -> bool:
    return _origin_matches(request.url.scheme, request.url.netloc, request.headers.get("origin"))


def _websocket_origin_allowed(websocket: WebSocket) -> bool:
    expected_scheme = "https" if websocket.url.scheme == "wss" else "http"
    return _origin_matches(expected_scheme, websocket.url.netloc, websocket.headers.get("origin"))


def _dashboard_token_hint() -> str:
    current_settings = get_settings()
    if current_settings.web_token:
        return (
            "Provide ?token=<SKYN3T_WEB_TOKEN> on the dashboard URL once to establish "
            "a session cookie, or send Authorization: Bearer <token> / X-API-Key."
        )
    return "SkyN3t web access is limited to localhost unless SKYN3T_WEB_TOKEN is configured."


def _extract_http_token(request: Request) -> Optional[str]:
    if request.url.path == "/":
        query_token = request.query_params.get("token")
        if query_token:
            return query_token
    return (
        _extract_bearer_token(request.headers.get("authorization"))
        or request.headers.get("x-api-key")
        or request.cookies.get(_SESSION_COOKIE_NAME)
    )


def _extract_websocket_token(websocket: WebSocket) -> Optional[str]:
    return (
        _extract_bearer_token(websocket.headers.get("authorization"))
        or websocket.headers.get("x-api-key")
        or websocket.query_params.get("token")
        or websocket.cookies.get(_SESSION_COOKIE_NAME)
    )


def _set_session_cookie(response: RedirectResponse, token: str, *, secure: bool) -> None:
    response.set_cookie(
        _SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=60 * 60 * 24,
        path="/",
    )


def _authorize_http_request(request: Request) -> tuple[bool, str, bool]:
    current_settings = get_settings()
    expected_token = current_settings.web_token
    provided_token = _extract_http_token(request)
    if expected_token:
        if provided_token != expected_token:
            return False, _dashboard_token_hint(), False
        if not _http_origin_allowed(request):
            return False, "Cross-origin browser access denied.", False
        should_issue_cookie = request.url.path == "/" and request.query_params.get("token") == expected_token
        return True, "", should_issue_cookie
    client_host = request.client.host if request.client else None
    if not _is_loopback_host(client_host):
        return False, _dashboard_token_hint(), False
    if not _http_origin_allowed(request):
        return False, "Cross-origin browser access denied.", False
    return True, "", False


def _http_auth_response(request: Request, detail: str) -> HTMLResponse | JSONResponse:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": detail}, status_code=401)
    body = (
        "<html><body style=\"font-family:sans-serif;background:#0b1020;color:#e5e7eb;padding:2rem;\">"
        "<h1>Access denied</h1>"
        f"<p>{html.escape(detail)}</p>"
        "</body></html>"
    )
    return HTMLResponse(body, status_code=401)


def _authorize_websocket(websocket: WebSocket) -> None:
    current_settings = get_settings()
    expected_token = current_settings.web_token
    provided_token = _extract_websocket_token(websocket)
    if expected_token:
        if provided_token != expected_token:
            raise WebSocketException(
                code=starlette_status.WS_1008_POLICY_VIOLATION,
                reason="Missing or invalid auth token.",
            )
        if not _websocket_origin_allowed(websocket):
            raise WebSocketException(
                code=starlette_status.WS_1008_POLICY_VIOLATION,
                reason="Cross-origin browser access denied.",
            )
        return
    client_host = websocket.client.host if websocket.client else None
    if not _is_loopback_host(client_host):
        raise WebSocketException(
            code=starlette_status.WS_1008_POLICY_VIOLATION,
            reason="Remote access requires SKYN3T_WEB_TOKEN.",
        )
    if not _websocket_origin_allowed(websocket):
        raise WebSocketException(
            code=starlette_status.WS_1008_POLICY_VIOLATION,
            reason="Cross-origin browser access denied.",
        )


# Cap request body size at 8 MiB. Without this, /api/rag/add and similar
# JSON-accepting routes will happily buffer arbitrary payloads into memory.
MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Attach a baseline CSP and other security headers to every response."""
    response = await call_next(request)
    # CSP is tuned to the dashboard's actual third-party hosts (Font Awesome
    # CDN, Google Fonts, jsDelivr for Chart.js, plus Cytoscape on jsdelivr).
    # If you remove a CDN dep from dashboard.html, remove it here too.
    csp = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com 'unsafe-inline'; "
        "style-src 'self' https://cdnjs.cloudflare.com https://fonts.googleapis.com 'unsafe-inline'; "
        "font-src 'self' https://cdnjs.cloudflare.com https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    )
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@app.middleware("http")
async def enforce_request_size(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            size = int(cl)
        except ValueError:
            return JSONResponse({"error": "invalid Content-Length"}, status_code=400)
        if size > MAX_REQUEST_BODY_BYTES:
            return JSONResponse(
                {"error": f"request body too large (>{MAX_REQUEST_BODY_BYTES} bytes)"},
                status_code=413,
            )
        return await call_next(request)

    # Chunked / no-Content-Length uploads still need a hard cap.
    # Wrap the ASGI receive callable to count bytes as they stream in
    # and bail with 413 once the cap is exceeded. Without this, a
    # malicious chunked POST can stream unlimited bytes through the
    # middleware.
    original_receive = request.receive
    counter = {"bytes": 0, "exceeded": False}

    async def _counting_receive():
        message = await original_receive()
        if message.get("type") == "http.request":
            body = message.get("body") or b""
            counter["bytes"] += len(body)
            if counter["bytes"] > MAX_REQUEST_BODY_BYTES:
                counter["exceeded"] = True
        return message

    request._receive = _counting_receive  # type: ignore[attr-defined]
    response = await call_next(request)
    if counter["exceeded"]:
        return JSONResponse(
            {"error": f"request body too large (>{MAX_REQUEST_BODY_BYTES} bytes)"},
            status_code=413,
        )
    return response


@app.middleware("http")
async def enforce_web_access(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path.startswith("/webhooks/"):
        return await call_next(request)
    allowed, detail, should_issue_cookie = _authorize_http_request(request)
    if not allowed:
        return _http_auth_response(request, detail)
    if should_issue_cookie:
        response = RedirectResponse(url=str(request.url.replace(query="")), status_code=303)
        _set_session_cookie(response, get_settings().web_token or "", secure=request.url.scheme == "https")
        return response
    return await call_next(request)


_DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"
_SPA_DIST = Path(__file__).parent / "ui" / "dist"


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Stub favicon route. Browsers always request /favicon.ico; without an
    explicit handler the catch-all served the dashboard HTML and burned a
    full template render per page load."""
    return Response(status_code=204)


@app.get("/legacy", response_class=HTMLResponse)
async def legacy_dashboard():
    """The original single-file dashboard.html. Kept as an escape hatch
    while the Vite+React SPA finishes covering every view. Tagged
    'legacy' on purpose — new work should go in skyn3t/web/ui/."""
    return FileResponse(str(_DASHBOARD_PATH), media_type="text/html")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the new Vite+React SPA when it's been built, otherwise fall
    back to the legacy dashboard so a fresh checkout still shows
    something. Run `npm --prefix skyn3t/web/ui run build` to populate
    the dist/ directory."""
    index = _SPA_DIST / "index.html"
    if index.exists():
        return FileResponse(str(index), media_type="text/html")
    return FileResponse(str(_DASHBOARD_PATH), media_type="text/html")


# Serve SPA static assets (JS/CSS/images) when the build exists.
if _SPA_DIST.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=str(_SPA_DIST / "assets")),
        name="spa-assets",
    )


@app.get("/api/status")
async def get_status():
    """Get system status."""
    if not orchestrator:
        return {"status": "not_initialized"}
    return orchestrator.get_system_status()


@app.post("/api/search")
async def search(data: Dict[str, Any]):
    """Full-text search over messages, tasks, and logs via FTS5.

    Body: {"query": "dashboard", "limit": 20, "table": "messages|tasks|logs|all"}
    """
    if not orchestrator or not orchestrator.memory_store:
        return JSONResponse({"error": "memory not initialized"}, status_code=503)
    query = str(data.get("query", "")).strip()
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)
    limit = max(1, min(100, int(data.get("limit", 20))))
    table = data.get("table", "all")
    store = orchestrator.memory_store
    if table == "messages":
        results = await store.search_messages(query, limit)
    elif table == "tasks":
        results = await store.search_tasks(query, limit)
    elif table == "logs":
        results = await store.search_logs(query, limit)
    else:
        results = await store.search_all(query, limit)
    return {"query": query, "table": table, "count": len(results), "results": results}


@app.get("/api/insights")
async def get_system_insights(days: int = 7):
    """Aggregate telemetry: token usage, stage latency, task stats."""
    if not orchestrator:
        return JSONResponse({"error": "orchestrator not initialized"}, status_code=503)

    from skyn3t.intelligence.stage_latency import snapshot as stage_latency_snapshot
    from skyn3t.observability.token_tracker import get_default_tracker

    token_tracker = get_default_tracker()
    token_data = token_tracker.per_agent()
    totals = token_tracker.totals()
    stages = stage_latency_snapshot()

    # Task stats from memory store
    task_stats = {}
    if orchestrator.memory_store:
        task_stats = await orchestrator.memory_store.get_stats()

    # Agent health
    agent_health = []
    for name, agent in orchestrator.agents.items():
        agent_health.append({
            "name": name,
            "type": agent.agent_type,
            "status": agent.status.value if hasattr(agent.status, "value") else str(agent.status),
            "queue_size": getattr(agent, "queue_size", 0),
            "recent_errors": getattr(agent, "recent_errors", [])[-3:],
        })

    return {
        "period_days": days,
        "tokens": {
            "per_agent": token_data,
            "totals": totals,
        },
        "stages": stages,
        "tasks": {
            "success_rate": task_stats.get("success_rate", 0),
            "total_completed": task_stats.get("total_completed", 0),
            "total_failed": task_stats.get("total_failed", 0),
            "total_tasks": task_stats.get("tasks", 0),
        },
        "agents": agent_health,
    }


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


@app.post("/api/services/reset")
async def services_reset():
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)
    try:
        return await _reset_runtime_services()
    except Exception as e:
        return _safe_error_response(e)


# In-memory per-IP token buckets. Keyed by (route_label, client_ip).
# Refill: tokens regenerate at a rate of ``refill_per_sec`` up to ``capacity``.
# This is process-local — fine for a single-node deploy; in a horizontally
# scaled deploy plug a Redis-backed store in here.
_RATE_BUCKETS: Dict[str, Dict[str, float]] = {}


def _client_ip(request: Any) -> str:
    """Best-effort IP extraction. Tolerates duck-typed test stubs that may
    not implement the full Request interface."""
    headers = getattr(request, "headers", None)
    if headers is not None:
        try:
            fwd = headers.get("x-forwarded-for", "")
        except Exception:
            fwd = ""
        if fwd:
            return fwd.split(",")[0].strip() or "unknown"
    client = getattr(request, "client", None)
    if client is not None:
        host = getattr(client, "host", None)
        if host:
            return str(host)
    return "unknown"


def _rate_limit_check(
    request: Request, *, label: str, capacity: float, refill_per_sec: float,
) -> Optional[JSONResponse]:
    """Token-bucket gate. Returns a 429 response when over budget, else None."""
    import time as _time
    ip = _client_ip(request)
    key = f"{label}:{ip}"
    now = _time.monotonic()
    bucket = _RATE_BUCKETS.get(key)
    if bucket is None:
        _RATE_BUCKETS[key] = {"tokens": float(capacity) - 1.0, "ts": now}
        return None
    elapsed = now - bucket["ts"]
    bucket["tokens"] = min(capacity, bucket["tokens"] + elapsed * refill_per_sec)
    bucket["ts"] = now
    if bucket["tokens"] < 1.0:
        retry_after = max(1.0, (1.0 - bucket["tokens"]) / max(refill_per_sec, 1e-6))
        return JSONResponse(
            {"error": "rate limit exceeded"},
            status_code=429,
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    bucket["tokens"] -= 1.0
    return None


def _safe_error_response(exc: Exception, *, status_code: int = 500) -> JSONResponse:
    """Log the full exception and return a generic response.

    Routes used to leak ``str(e)`` directly, exposing internal paths and
    stack frame text to any caller. We instead log with a correlation id
    and return only the id; operators can grep logs for the id.
    """
    import uuid as _uuid
    correlation_id = _uuid.uuid4().hex[:12]
    logger.exception("api error [%s]: %s", correlation_id, type(exc).__name__)
    return JSONResponse(
        {"error": "internal error", "correlation_id": correlation_id},
        status_code=status_code,
    )


def _clamp_limit(value: int, *, default: int, hi: int = 200) -> int:
    """Clamp a user-supplied limit to a sane range.

    Endpoints accept an integer ``limit`` query param; without clamping a
    caller can request millions and force the server to materialize them.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, hi))


_EVALUATION_STATUSES = {"draft", "approved", "rejected"}


def _evaluation_record_from_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    meta = dict(doc.get("meta") or {})
    return {
        "id": str(doc.get("id") or ""),
        "title": str(doc.get("title") or ""),
        "doc_type": str(doc.get("doc_type") or ""),
        "review_status": str(meta.get("review_status") or ""),
        "lane": str(meta.get("lane") or ""),
        "language": str(meta.get("language") or ""),
        "signals": [str(item) for item in list(meta.get("patterns") or []) if str(item).strip()],
        "checks": [str(item) for item in list(meta.get("checks") or []) if str(item).strip()],
        "source_doc_ids": [str(item) for item in list(meta.get("source_doc_ids") or []) if str(item).strip()],
        "source_repos": [str(item) for item in list(meta.get("source_repos") or []) if str(item).strip()],
        "source_platform": str(meta.get("source_platform") or ""),
        "synthesis_key": str(meta.get("synthesis_key") or ""),
        "consensus_count": int(meta.get("consensus_count") or 0),
        "memory_layer": str(meta.get("memory_layer") or ""),
        "confidence": meta.get("confidence"),
        "content": str(doc.get("content") or ""),
    }


async def _append_evaluation_bundle(
    output_path: Path,
    *,
    trajectory_count: int,
    review_status: str = "approved",
    limit: int = 500,
    filters: Optional[Dict[str, Any]] = None,
) -> int:
    store = getattr(orchestrator, "memory_store", None) or getattr(orchestrator, "_memory", None)
    if not store:
        raise RuntimeError("memory not initialized")
    docs = await store.list_knowledge_drafts(
        review_status=review_status,
        doc_type="evaluation",
        limit=limit,
        preview_only=False,
    )
    with output_path.open("a", encoding="utf-8") as fh:
        for doc in docs:
            fh.write(
                json.dumps(
                    {
                        "kind": "evaluation_asset",
                        "evaluation": _evaluation_record_from_doc(doc),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        fh.write(
            json.dumps(
                {
                    "kind": "bundle_manifest",
                    "trajectory_count": trajectory_count,
                    "evaluation_count": len(docs),
                    "evaluation_review_status": review_status,
                    "filters": filters or {},
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return len(docs)


@app.get("/traces")
async def get_traces(limit: int = 50):
    """Return recent finished traces."""
    tracer = get_tracer()
    limit = _clamp_limit(limit, default=50, hi=500)
    spans = tracer.get_recent_spans(limit=limit)
    return {"traces": [span.to_dict() for span in spans]}


@app.post("/api/fallback")
async def register_fallback(data: Dict[str, Any]):
    """Register a fallback chain. Example: {'capability': 'code_generation', 'agents': ['claude', 'copilot', 'kimi']}"""
    if not orchestrator:
        return {"error": "Orchestrator not initialized"}
    capability = data.get("capability")
    if not isinstance(capability, str) or not capability:
        return {"error": "Capability is required"}

    raw_agents = data.get("agents", [])
    agents = [agent for agent in raw_agents if isinstance(agent, str) and agent]
    if not agents:
        return {"error": "At least one agent is required"}

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
    return {"agents": [_agent_list_item(agent) for agent in orchestrator.agents.values()]}


def _agent_list_item(agent: BaseAgent) -> Dict[str, Any]:
    stats = agent.get_stats()
    try:
        config_view = agent.get_config_view()
    except Exception:
        logger.exception("agent config view failed for %s", getattr(agent, "name", "?"))
        config_view = {"config": {}, "enabled": getattr(agent, "enabled", True)}
    catalog = get_agent_catalog_metadata(
        class_name=type(agent).__name__,
        runtime_name=getattr(agent, "name", ""),
    )
    return {
        **stats,
        "agent_type": stats.get("type"),
        "class_name": type(agent).__name__,
        "backend": config_view.get("effective_backend"),
        "model": config_view.get("effective_model"),
        "effective_backend": config_view.get("effective_backend"),
        "effective_model": config_view.get("effective_model"),
        "effective_source": config_view.get("effective_source"),
        "enabled": config_view.get("enabled", getattr(agent, "enabled", True)),
        "config": config_view.get("config", {}),
        **catalog,
    }


@app.post("/api/agents")
async def register_new_agent(data: Dict[str, Any]):
    """Register a new agent dynamically."""
    if not orchestrator:
        return {"error": "Orchestrator not initialized"}

    name = data.get("name")
    provider = str(data.get("provider", "claude") or "claude").lower()
    if provider == "anthropic":
        provider = "claude"
    model = data.get("model")
    cli_agent = data.get("cli_agent", False)

    if not name:
        return {"error": "Agent name is required"}

    if name in orchestrator.agents:
        return {"error": f"Agent '{name}' already exists"}

    try:
        bus = event_bus or orchestrator.event_bus
        agent: BaseAgent
        if cli_agent or provider in ("claude", "kimi", "copilot"):
            from skyn3t.adapters.claude_cli import ClaudeCLIAgent
            from skyn3t.adapters.copilot_cli import CopilotCLIAgent
            from skyn3t.adapters.kimi_cli import KimiCLIAgent
            if provider == "claude":
                agent = ClaudeCLIAgent(
                    name=name,
                    event_bus=bus,
                    config={"model": model} if model else {},
                )
            elif provider == "kimi":
                agent = KimiCLIAgent(
                    name=name,
                    event_bus=bus,
                    config={"model": model} if model else {},
                )
            elif provider == "copilot":
                agent = CopilotCLIAgent(
                    name=name,
                    event_bus=bus,
                )
            else:
                return {"error": f"Unsupported CLI provider: {provider}"}
        elif provider == "github":
            from skyn3t.agents.github_explorer import GitHubExplorerAgent
            agent = GitHubExplorerAgent(
                name=name,
                event_bus=bus,
            )
        else:
            return {
                "error": (
                    f"Unsupported provider: {provider}. "
                    "Use claude, kimi, copilot, or github."
                )
            }

        await agent.initialize()
        await agent.start()
        orchestrator.register_agent(agent)

        return {"status": "registered", "agent": _agent_list_item(agent)}
    except Exception as e:
        return _safe_error_response(e)


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
    # Persist to disk. Used to swallow errors silently — the in-memory
    # override would apply but a restart would lose it with no warning.
    # Now we log AND surface "persisted: false" so the caller (and the
    # UI / operator) can see when the disk write fails.
    persisted = True
    persist_error: Optional[str] = None
    try:
        from skyn3t.config.agent_overrides import get_override_store
        get_override_store().set(name, payload or {})
    except Exception as exc:
        persisted = False
        persist_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "agent override persistence failed for %s: %s",
            name, exc, exc_info=True,
        )
    out = dict(res) if isinstance(res, dict) else {"applied": res}
    out["persisted"] = persisted
    if persist_error:
        out["persist_error"] = persist_error
    return out


@app.post("/api/agents/{name}/config/reset")
async def agent_config_reset(name: str, payload: Optional[Dict[str, Any]] = None):
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)
    a = orchestrator.agents.get(name)
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    requested = (payload or {}).get("keys")
    keys = [str(k) for k in requested] if isinstance(requested, list) and requested else ["backend", "model"]
    res = a.clear_override(keys)
    persisted = True
    persist_error: Optional[str] = None
    try:
        from skyn3t.config.agent_overrides import get_override_store

        get_override_store().unset(name, keys)
    except Exception as exc:
        persisted = False
        persist_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "agent override reset persistence failed for %s (keys=%s): %s",
            name, keys, exc, exc_info=True,
        )
    out = dict(res) if isinstance(res, dict) else {"applied": res}
    out["persisted"] = persisted
    if persist_error:
        out["persist_error"] = persist_error
    return out


def _persist_override_or_log(name: str, patch: dict) -> tuple[bool, Optional[str]]:
    """Shared helper for the enable/disable endpoints. Returns
    (persisted, error_msg). On failure, logs at WARNING with the
    full exception."""
    try:
        from skyn3t.config.agent_overrides import get_override_store
        get_override_store().set(name, patch)
        return True, None
    except Exception as exc:
        logger.warning(
            "agent override persistence failed for %s (patch=%s): %s",
            name, patch, exc, exc_info=True,
        )
        return False, f"{type(exc).__name__}: {exc}"


def _invalidate_policy_llm_cache(stage_names: List[str]) -> None:
    if not orchestrator:
        return
    wanted = {str(stage).strip().lower() for stage in stage_names if str(stage).strip()}
    if not wanted:
        return
    for agent in orchestrator.agents.values():
        if {
            str(getattr(agent, "name", "")).strip().lower(),
            str(getattr(agent, "agent_type", "")).strip().lower(),
        } & wanted:
            try:
                if hasattr(agent, "_llm"):
                    agent._llm = None
            except Exception:
                logger.debug("failed to invalidate llm cache for %s", getattr(agent, "name", "?"))


@app.get("/api/routing/policy")
async def routing_policy_get():
    from skyn3t.core.model_router import available_tiers, list_stage_routes

    return {
        "tiers": available_tiers(),
        "routes": list_stage_routes(),
    }


@app.get("/api/routing/recommendations")
async def routing_recommendations_get():
    from skyn3t.intelligence.routing_recommendations import list_stage_recommendations

    return {
        "recommendations": list_stage_recommendations(),
    }


@app.patch("/api/routing/policy")
async def routing_policy_patch(payload: Dict[str, Any]):
    from skyn3t.config.model_routing import get_model_routing_store
    from skyn3t.core.model_router import available_tiers, list_stage_routes

    raw_candidate = payload.get("policies") if isinstance(payload.get("policies"), dict) else (payload or {})
    raw_updates: Dict[str, Any] = raw_candidate if isinstance(raw_candidate, dict) else {}
    valid_tiers = {item["name"] for item in available_tiers()}
    updates: Dict[str, Dict[str, Any]] = {}
    for stage, tier in raw_updates.items():
        stage_name = str(stage or "").strip().lower()
        tier_name = ""
        applied_via: Optional[str] = None
        if isinstance(tier, dict):
            tier_name = str(tier.get("tier") or "").strip()
            raw_applied_via = str(tier.get("applied_via") or "").strip().lower()
            if raw_applied_via:
                if raw_applied_via not in {"manual", "recommendation"}:
                    return JSONResponse(
                        {
                            "error": f"unknown applied_via '{raw_applied_via}'",
                            "valid_applied_via": ["manual", "recommendation"],
                        },
                        status_code=400,
                    )
                applied_via = raw_applied_via
        else:
            tier_name = str(tier or "").strip()
        if not stage_name:
            return JSONResponse({"error": "stage name required"}, status_code=400)
        if tier_name not in valid_tiers:
            return JSONResponse(
                {"error": f"unknown tier '{tier_name}'", "valid_tiers": sorted(valid_tiers)},
                status_code=400,
            )
        updates[stage_name] = {"tier": tier_name}
        if applied_via is not None:
            updates[stage_name]["applied_via"] = applied_via
    if not updates:
        return JSONResponse({"error": "no routing policies supplied"}, status_code=400)
    get_model_routing_store().set_entries(updates)
    _invalidate_policy_llm_cache(list(updates))
    return {
        "ok": True,
        "updated": sorted(updates),
        "tiers": available_tiers(),
        "routes": list_stage_routes(),
    }


@app.delete("/api/routing/policy/{stage_name}")
async def routing_policy_delete(stage_name: str):
    from skyn3t.config.model_routing import get_model_routing_store
    from skyn3t.core.model_router import available_tiers, list_stage_routes

    deleted = get_model_routing_store().delete(stage_name)
    _invalidate_policy_llm_cache([stage_name])
    return {
        "ok": True,
        "deleted": deleted,
        "stage": stage_name.strip().lower(),
        "tiers": available_tiers(),
        "routes": list_stage_routes(),
    }


@app.post("/api/agents/{name}/enable")
async def agent_enable(name: str):
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)
    a = orchestrator.agents.get(name)
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    a.apply_override({"enabled": True})
    persisted, err = _persist_override_or_log(name, {"enabled": True})
    out: Dict[str, Any] = {"ok": True, "persisted": persisted}
    if err:
        out["persist_error"] = err
    return out


@app.post("/api/agents/{name}/disable")
async def agent_disable(name: str):
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)
    a = orchestrator.agents.get(name)
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    a.apply_override({"enabled": False})
    persisted, err = _persist_override_or_log(name, {"enabled": False})
    out: Dict[str, Any] = {"ok": True, "persisted": persisted}
    if err:
        out["persist_error"] = err
    return out


@app.get("/api/agents/models")
async def agents_models():
    """List backends and their known models for the agent edit UI.

    Returns the static catalog (curated set with labels). The
    `list_models(backend)` async path can hit live CLIs but is too
    slow + flaky for an inline dropdown — frontends that need live
    discovery should call that explicitly.
    """
    try:
        from skyn3t.adapters.model_catalog import STATIC
        backends = []
        # Order matters for the dropdown — put the most-used first.
        preferred_order = [
            "claude_cli", "copilot_cli", "kimi_cli", "openai_cli",
            "anthropic", "openrouter", "auto", "deterministic",
        ]
        seen = set()
        for key in preferred_order:
            if key in STATIC:
                backends.append({"backend": key, "models": list(STATIC[key])})
                seen.add(key)
        # Any backend not in preferred_order tacked on at the end.
        for key, models in STATIC.items():
            if key in seen:
                continue
            backends.append({"backend": key, "models": list(models)})
        return {"backends": backends}
    except Exception as e:
        return _safe_error_response(e)


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
        kwargs: Dict[str, Any] = {}
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
    # Shut down the agent's task processor BEFORE removing it from the
    # registry — otherwise the background loop keeps running orphaned
    # against a no-longer-tracked agent. Best effort; if shutdown
    # raises we still drop the registry entry so the user isn't stuck
    # with a half-deleted record.
    agent = orchestrator.agents.get(name)
    # Three stages of cleanup, each can fail independently. We collect
    # the outcomes so the caller knows EXACTLY what landed and what
    # didn't — silent failure used to make a partially-deleted agent
    # look fully removed in the UI while its custom-spec / override
    # files remained on disk.
    cleanup: Dict[str, Any] = {
        "shutdown": "ok",
        "registry_removed": False,
        "custom_spec_deleted": "ok",
        "overrides_deleted": "ok",
    }
    errors: List[str] = []

    if agent is not None:
        try:
            shutdown = getattr(agent, "shutdown", None)
            if callable(shutdown):
                import asyncio as _asyncio
                ret = shutdown()
                if _asyncio.iscoroutine(ret):
                    await ret
        except Exception as exc:
            cleanup["shutdown"] = f"failed: {type(exc).__name__}"
            errors.append(f"shutdown: {exc}")
            logger.warning(
                "agent shutdown failed during delete name=%s: %s",
                name, exc, exc_info=True,
            )

    try:
        orchestrator.agents.pop(name, None)
        orchestrator.agent_registry.pop(name, None)
        cleanup["registry_removed"] = True
    except Exception as exc:
        errors.append(f"registry pop: {exc}")
        logger.warning(
            "registry removal failed name=%s: %s",
            name, exc, exc_info=True,
        )

    try:
        from skyn3t.config.custom_agents import get_custom_store
        get_custom_store().delete(name)
    except Exception as exc:
        cleanup["custom_spec_deleted"] = f"failed: {type(exc).__name__}"
        errors.append(f"custom spec delete: {exc}")
        logger.warning(
            "custom_agents.delete failed name=%s: %s",
            name, exc, exc_info=True,
        )

    try:
        from skyn3t.config.agent_overrides import get_override_store
        get_override_store().delete(name)
    except Exception as exc:
        cleanup["overrides_deleted"] = f"failed: {type(exc).__name__}"
        errors.append(f"overrides delete: {exc}")
        logger.warning(
            "agent_overrides.delete failed name=%s: %s",
            name, exc, exc_info=True,
        )

    # "ok" only when EVERY cleanup step succeeded. UI can colour-code
    # off this field instead of guessing from a bare {"ok": true}.
    response: Dict[str, Any] = {
        "ok": not errors,
        "cleanup": cleanup,
    }
    if errors:
        response["errors"] = errors
    return response


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


def _resolve_repl_chat_defaults(
    requested_backend: Optional[str],
    requested_model: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    settings = get_settings()
    backend = requested_backend
    model = requested_model

    if not backend:
        configured_backend = str(getattr(settings, "llm_backend", "") or "").strip().lower()
        if configured_backend and configured_backend != "auto":
            backend = configured_backend
            model = model or getattr(settings, "llm_model", None)
        elif getattr(settings, "openrouter_api_key", None):
            backend = "openrouter"

    if backend == "openrouter" and not model:
        model = getattr(settings, "llm_model", None) or "openai/gpt-4.1"

    return backend, model


@app.post("/api/llm/complete")
async def llm_complete(data: Dict[str, Any]):
    """Run a raw LLM completion for REPL-style chat."""
    prompt = str(data.get("prompt") or data.get("message") or "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt required"}, status_code=400)

    system = str(data.get("system") or "").strip() or None
    requested_backend = str(data.get("backend") or "").strip() or None
    requested_model = str(data.get("model") or "").strip() or None
    backend, model = _resolve_repl_chat_defaults(requested_backend, requested_model)
    try:
        from skyn3t.adapters import LLMClient

        client = LLMClient(
            default_model=model,
            backend=backend,
            event_bus=event_bus,
            caller_name="repl_chat",
        )
        response = await client.complete(
            prompt,
            system=system,
            max_tokens=1200,
            temperature=0.4,
            timeout=120.0,
        )
        info = client.describe()
        return {
            "response": response,
            "backend": info.get("backend"),
            "model": info.get("default_model"),
        }
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/orchestrator/submit")
async def orchestrator_submit(data: Dict[str, Any]):
    """Submit a free-form prompt through the orchestrator."""
    from skyn3t.core.agent import TaskRequest

    if not orchestrator:
        return {"error": "Orchestrator not initialized"}

    prompt = str(data.get("prompt") or data.get("message") or "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt required"}, status_code=400)

    agent_name = data.get("agent_name")
    if agent_name is not None and not isinstance(agent_name, str):
        return JSONResponse({"error": "agent_name must be a string"}, status_code=400)

    title = str(data.get("title") or prompt[:80]).strip() or "Prompt"
    task = TaskRequest(
        title=title,
        description=str(data.get("description") or prompt),
        input_data={"message": prompt},
        priority=int(data.get("priority") or 0),
        session_id=str(data.get("session_id")) if data.get("session_id") else None,
    )
    task_id = await orchestrator.submit_task(task, agent_name=agent_name or None)
    return {"task_id": task_id, "status": "submitted"}


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
async def exec_agent(
    agent_name: str,
    data: Dict[str, Any],
    request: Request = None,  # type: ignore[assignment]
):
    """Quick one-off execution on an agent."""
    from skyn3t.core.agent import TaskRequest

    # Rate-limit: 30/min/IP. LLM-amplified, so cheap to abuse. Skipped when
    # called directly (no Request) — i.e. from in-process tests.
    if request is not None:
        rl = _rate_limit_check(request, label="exec_agent", capacity=30, refill_per_sec=0.5)
        if rl is not None:
            return rl

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
    output_payload: Optional[Dict[str, Any]]
    if result.success:
        raw_output = result.output if isinstance(result.output, dict) else {}
        response_text = raw_output.get("response")
        if response_text is None:
            response_text = str(result.output) if result.output is not None else ""
        output_payload = {**raw_output, "response": response_text}
    else:
        output_payload = None
    return {
        "task_id": task.task_id,
        "success": result.success,
        "output": output_payload,
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


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """Cancel a running task."""
    if not orchestrator:
        return {"error": "Orchestrator not initialized"}

    if not orchestrator.cancel_task(task_id):
        return JSONResponse({"error": f"Task '{task_id}' not found"}, status_code=404)
    return {"task_id": task_id, "status": "cancelled"}


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
        # ValueError messages here are caller-facing validation; safe to expose.
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return _safe_error_response(e)


def _track_background_task(task: asyncio.Task, *, label: str) -> None:
    """Retain a strong ref to a fire-and-forget task so it can't be GC'd, and
    surface its exception via the logger instead of letting it disappear."""
    bag = getattr(app.state, "background_tasks", None)
    if bag is None:
        bag = set()
        app.state.background_tasks = bag
    bag.add(task)
    def _on_done(fut: asyncio.Task) -> None:
        bag.discard(fut)
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            logger.error("background task %s failed: %s", label, exc, exc_info=exc)
    task.add_done_callback(_on_done)


@app.post("/api/pipeline/{pipeline_id}/run")
async def run_pipeline(pipeline_id: str):
    """Run a pipeline."""
    if not orchestrator:
        return {"error": "Orchestrator not initialized"}

    pipeline = orchestrator._pipelines.get(pipeline_id)
    if not pipeline:
        return {"error": f"Pipeline '{pipeline_id}' not found"}

    if not pipeline.is_completed:
        task = asyncio.create_task(
            orchestrator._run_pipeline(pipeline, [s.name for s in pipeline.stages])
        )
        _track_background_task(task, label=f"run_pipeline:{pipeline_id}")
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
    llm_client = None
    try:
        from skyn3t.adapters import LLMClient

        llm_client = LLMClient(
            default_model=None,
            backend=None,
            event_bus=event_bus,
            caller_name="rag",
            rag=engine,
        )
    except Exception:
        llm_client = None

    result = await engine.answer(query, llm_provider=llm_client, n_results=n_results)
    return result


@app.get("/api/rag/stats")
async def rag_stats(request: Request):
    """Get knowledge-base statistics for the dashboard."""
    engine = await _get_rag_engine(request)
    return await engine.get_stats()


@app.get("/api/rag/recent")
async def rag_recent(request: Request, limit: int = 8):
    """List recent knowledge chunks for the dashboard."""
    engine = await _get_rag_engine(request)
    corpus = engine.vector_store.all_documents()
    rows: List[Dict[str, Any]] = []
    for doc in corpus:
        metadata = doc.get("metadata") or {}
        content = str(doc.get("content") or "")
        rows.append(
            {
                "id": doc.get("id"),
                "title": metadata.get("title") or "Untitled",
                "source": metadata.get("source") or "",
                "doc_type": metadata.get("doc_type") or "text",
                "timestamp": metadata.get("timestamp"),
                "chunk_index": metadata.get("chunk_index"),
                "total_chunks": metadata.get("total_chunks"),
                "preview": content[:180],
            }
        )
    rows.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
    safe_limit = max(1, min(int(limit), 50))
    return {"documents": rows[:safe_limit]}


@app.post("/api/rag/add")
async def rag_add(request: Request, data: Dict[str, Any]):
    """Add knowledge to RAG."""
    # Rate-limit: 60/min/IP. Embedding generation is expensive.
    rl = _rate_limit_check(request, label="rag_add", capacity=60, refill_per_sec=1.0)
    if rl is not None:
        return rl

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

    stats = await engine.get_stats()
    return {
        "ids": ids,
        "status": "added",
        "chunks_added": len(ids),
        "collection_count": stats.get("count", 0),
    }


# ------------------------------------------------------------------
# Brain / Memory API
# ------------------------------------------------------------------

@app.get("/api/memory/layers")
async def memory_layers(limit: int = 5):
    """Return a layer-oriented memory summary for CLI and REPL surfaces."""
    limit = _clamp_limit(limit, default=5, hi=50)
    if not orchestrator:
        return {
            "enabled": False,
            "layers": {
                "session": {"active_sessions": 0, "sessions": []},
                "operator": {"insight_count": 0, "recent_insights": [], "skill_summary": {}, "top_skills": []},
                "project": {
                    "tasks": 0,
                    "messages": 0,
                    "knowledge_documents": 0,
                    "success_rate": 0.0,
                    "recent_documents": [],
                },
            },
        }

    store = getattr(orchestrator, "memory_store", None) or getattr(orchestrator, "_memory", None)
    consciousness = getattr(orchestrator, "_consciousness", None)

    stats: Dict[str, Any] = {}
    recent_documents: List[Dict[str, Any]] = []
    if store:
        stats = await store.get_stats()
        recent_documents = await store.get_lessons(limit=limit)

    sessions: List[str] = []
    insights: List[Dict[str, Any]] = []
    if consciousness:
        sessions = await consciousness.list_sessions()
        insights = await consciousness.get_insights(limit=limit)

    from skyn3t.intelligence.skill_library import get_default_library

    lib = get_default_library()
    skill_summary = lib.summary()
    top_skills = lib.find(min_score=0.1, limit=limit)

    return {
        "enabled": bool(store or consciousness),
        "layers": {
            "session": {
                "active_sessions": len(sessions),
                "sessions": sessions[:limit],
            },
            "operator": {
                "insight_count": len(insights),
                "recent_insights": [
                    {
                        "agent": item.get("agent"),
                        "capability": item.get("capability"),
                        "insight": item.get("insight"),
                        "timestamp": item.get("timestamp"),
                    }
                    for item in insights[:limit]
                ],
                "skill_summary": skill_summary,
                "top_skills": [
                    {
                        "name": skill.name,
                        "score": skill.score,
                        "tags": skill.tags,
                        "source": skill.source,
                    }
                    for skill in top_skills
                ],
            },
            "project": {
                "tasks": stats.get("tasks", 0),
                "messages": stats.get("messages", 0),
                "knowledge_documents": stats.get("knowledge_documents", 0),
                "success_rate": stats.get("success_rate", 0.0),
                "recent_documents": [
                    {
                        "title": doc.get("title"),
                        "source": doc.get("source"),
                        "doc_type": doc.get("doc_type"),
                        "created_at": doc.get("created_at"),
                    }
                    for doc in recent_documents[:limit]
                ],
            },
        },
    }


@app.get("/api/memory/drafts")
async def memory_drafts(limit: int = 20, doc_type: Optional[str] = None):
    """List pending memory drafts awaiting review."""
    store = getattr(orchestrator, "memory_store", None) or getattr(orchestrator, "_memory", None)
    if not store:
        return {"drafts": []}
    limit = _clamp_limit(limit, default=20, hi=200)
    drafts = await store.list_knowledge_drafts(
        review_status="draft",
        doc_type=doc_type,
        limit=limit,
    )
    return {"drafts": drafts}


@app.get("/api/memory/evaluations")
async def memory_evaluations(status: str = "draft", limit: int = 20):
    """List governed evaluation assets by review status."""
    store = getattr(orchestrator, "memory_store", None) or getattr(orchestrator, "_memory", None)
    if not store:
        return {"evaluations": []}
    normalized_status = str(status or "draft").strip().lower() or "draft"
    if normalized_status not in _EVALUATION_STATUSES:
        return JSONResponse(
            {"error": "status must be one of: draft, approved, rejected"},
            status_code=422,
        )
    limit = _clamp_limit(limit, default=20, hi=100)
    docs = await store.list_knowledge_drafts(
        review_status=normalized_status,
        doc_type="evaluation",
        limit=limit,
    )
    return {
        "evaluations": [_evaluation_record_from_doc(doc) for doc in docs],
        "status": normalized_status,
    }


@app.get("/api/memory/evaluations/{doc_id}")
async def memory_evaluation_detail(doc_id: str):
    """Get one governed evaluation asset."""
    store = getattr(orchestrator, "memory_store", None) or getattr(orchestrator, "_memory", None)
    if not store:
        return JSONResponse({"error": "memory not initialized"}, status_code=503)
    doc = await store.get_knowledge_doc(doc_id)
    if doc is None or str(doc.get("doc_type") or "") != "evaluation":
        return JSONResponse({"error": "evaluation asset not found"}, status_code=404)
    return {"evaluation": _evaluation_record_from_doc(doc), "doc": doc}


@app.get("/api/memory/evaluations/{doc_id}/export")
async def memory_evaluation_export(doc_id: str, format: str = "json"):
    """Export an approved evaluation asset for downstream use."""
    store = getattr(orchestrator, "memory_store", None) or getattr(orchestrator, "_memory", None)
    if not store:
        return JSONResponse({"error": "memory not initialized"}, status_code=503)
    doc = await store.get_knowledge_doc(doc_id)
    if doc is None or str(doc.get("doc_type") or "") != "evaluation":
        return JSONResponse({"error": "evaluation asset not found"}, status_code=404)
    record = _evaluation_record_from_doc(doc)
    if record["review_status"] != "approved":
        return JSONResponse(
            {"error": "evaluation asset must be approved before export"},
            status_code=400,
        )
    normalized_format = str(format or "json").strip().lower() or "json"
    if normalized_format not in {"json", "jsonl"}:
        return JSONResponse({"error": "format must be one of: json, jsonl"}, status_code=422)
    payload = {
        "kind": "evaluation_asset",
        "evaluation": {
            key: value for key, value in record.items() if key != "content"
        },
    }
    if normalized_format == "jsonl":
        return PlainTextResponse(
            json.dumps(payload, separators=(",", ":")) + "\n",
            media_type="application/x-ndjson",
        )
    return payload


@app.post("/api/memory/drafts/{doc_id}/approve")
async def memory_draft_approve(doc_id: str, request: Request):
    """Promote a memory draft into trusted RAG-backed knowledge."""
    store = getattr(orchestrator, "memory_store", None) or getattr(orchestrator, "_memory", None)
    if not store:
        return JSONResponse({"error": "memory not initialized"}, status_code=503)
    draft = await store.get_knowledge_doc(doc_id)
    if draft is None:
        return JSONResponse({"error": "draft not found"}, status_code=404)
    meta = dict(draft.get("meta") or {})
    if meta.get("review_status") != "draft":
        return JSONResponse({"error": "draft is not pending review"}, status_code=400)

    engine = await _get_rag_engine(request)
    approved_meta = {
        **meta,
        "review_status": "approved",
        "provenance_status": "approved",
    }
    embedding_id = draft.get("embedding_id")
    if not embedding_id:
        embedding_id = await engine.add_knowledge_one(
            content=str(draft.get("content") or ""),
            title=str(draft.get("title") or ""),
            source=str(draft.get("source") or ""),
            doc_type=str(draft.get("doc_type") or "lesson"),
            metadata=approved_meta,
        )
    if not embedding_id:
        return JSONResponse({"error": "failed to promote draft"}, status_code=500)
    updated = await store.update_knowledge_doc_review(
        doc_id,
        review_status="approved",
        embedding_id=embedding_id,
        extra_meta={"provenance_status": "approved"},
    )
    if updated is None:
        return JSONResponse({"error": "draft not found"}, status_code=404)
    return {"ok": True, "draft": updated}


@app.post("/api/memory/drafts/{doc_id}/reject")
async def memory_draft_reject(doc_id: str, payload: dict | None = None):
    """Reject a memory draft so it stays out of trusted retrieval."""
    store = getattr(orchestrator, "memory_store", None) or getattr(orchestrator, "_memory", None)
    if not store:
        return JSONResponse({"error": "memory not initialized"}, status_code=503)
    draft = await store.get_knowledge_doc(doc_id)
    if draft is None:
        return JSONResponse({"error": "draft not found"}, status_code=404)
    meta = dict(draft.get("meta") or {})
    if meta.get("review_status") != "draft":
        return JSONResponse({"error": "draft is not pending review"}, status_code=400)
    reason = str((payload or {}).get("reason") or "")
    updated = await store.update_knowledge_doc_review(
        doc_id,
        review_status="rejected",
        reason=reason,
        extra_meta={"provenance_status": "rejected"},
    )
    if updated is None:
        return JSONResponse({"error": "draft not found"}, status_code=404)
    return {"ok": True, "draft": updated}


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
    recent = []
    if orchestrator._memory:
        recent = await orchestrator._memory.get_recent_context(session_id, limit=10)
    return {"session_id": session_id, "context": sess, "recent_activity": recent}


@app.get("/api/memory/insights")
async def get_insights(agent: Optional[str] = None, capability: Optional[str] = None, limit: int = 20):
    """Get recent agent insights from collective consciousness."""
    if not orchestrator or not orchestrator._consciousness:
        return {"insights": []}
    limit = _clamp_limit(limit, default=20, hi=200)
    insights = await orchestrator._consciousness.get_insights(
        agent_name=agent, capability=capability, limit=limit
    )
    return {"insights": insights}


@app.get("/api/memory/experiences")
async def query_experiences(request: Request, query: str = "", limit: int = 10):
    """Semantic search over past experiences via RAG."""
    engine = await _get_rag_engine(request)
    limit = _clamp_limit(limit, default=10, hi=100)
    result = await engine.query(query, n_results=limit, filter_dict={"doc_type": "experience"})
    return result


@app.get("/api/memory/tuning")
async def get_tuning_status():
    """Get self-tuning engine status."""
    if not orchestrator or not orchestrator._tuner:
        return {"enabled": False}
    return {"enabled": True, **orchestrator._tuner.get_status()}


@app.get("/api/memory/lesson_scores")
async def get_lesson_scores(limit: int = 10):
    """Outcome-attributed lesson scoreboard.

    Returns a summary of which injected lessons have been correlated with
    task success vs. failure, plus the top-N helpful and hurtful lessons.
    Empty payload when the LearningLoop / scoreboard isn't wired.
    """
    if not orchestrator:
        return {"enabled": False}
    loop = getattr(orchestrator, "_learning_loop", None)
    sb = getattr(loop, "scoreboard", None) if loop else None
    if sb is None:
        return {"enabled": False}
    limit = _clamp_limit(limit, default=10, hi=100)
    return {
        "enabled": True,
        "summary": sb.summary(),
        "top_helpful": [s.to_dict() for s in sb.top_helpful(limit=limit)],
        "top_hurtful": [s.to_dict() for s in sb.top_hurtful(limit=limit)],
    }


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@app.get("/api/skills")
async def list_skills(tag: Optional[str] = None, limit: int = 50):
    """List installed skills, optionally filtered by tag."""
    from skyn3t.intelligence.skill_library import get_default_library

    lib = get_default_library()
    limit = _clamp_limit(limit, default=50, hi=200)
    if tag:
        skills = lib.find(tag=tag, limit=limit)
    else:
        skills = lib.all()[:limit]
    return {
        "skills": [
            {
                "name": s.name,
                "slug": s.slug,
                "description": s.description,
                "tags": s.tags,
                "score": s.score,
                "success_count": s.success_count,
                "failure_count": s.failure_count,
                "source": s.source,
                "last_used_at": s.last_used_at,
                "created_at": s.created_at,
            }
            for s in skills
        ],
        "summary": lib.summary(),
    }


@app.post("/api/skills/install")
async def install_skill(request: Request):
    """Install a skill from a local path or git URL."""
    import shutil
    import tempfile
    from pathlib import Path

    from skyn3t.intelligence.skill_library import get_default_library

    body = await request.json()
    source = (body.get("source") or "").strip()
    if not source:
        return JSONResponse({"error": "source is required"}, status_code=400)

    lib = get_default_library()
    source_path = Path(source)

    if source_path.exists() and source_path.is_dir():
        path, findings = lib.import_agent_skill(source_path)
        if path:
            return {"installed": path.stem, "warnings": findings}
        return JSONResponse({"error": "install failed", "flagged": findings}, status_code=400)

    if source.startswith(("http://", "https://", "git@")):
        with tempfile.TemporaryDirectory() as tmp:
            if not shutil.which("git"):
                return JSONResponse({"error": "git is not installed"}, status_code=500)
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth", "1", source, tmp,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                return JSONResponse(
                    {"error": "git clone failed", "detail": stderr.decode()},
                    status_code=400,
                )
            skill_md = Path(tmp) / "SKILL.md"
            if not skill_md.exists():
                subdirs = [d for d in Path(tmp).iterdir() if d.is_dir()]
                if subdirs:
                    skill_md = subdirs[0] / "SKILL.md"
            if not skill_md.exists():
                return JSONResponse({"error": "No SKILL.md found"}, status_code=400)
            path, findings = lib.import_agent_skill(skill_md.parent)
            if path:
                return {"installed": path.stem, "warnings": findings}
            return JSONResponse({"error": "install failed", "flagged": findings}, status_code=400)

    return JSONResponse({"error": f"source not found: {source}"}, status_code=400)


@app.delete("/api/skills/{name}")
async def delete_skill(name: str):
    """Remove a skill by name."""
    from skyn3t.intelligence.skill_library import get_default_library

    lib = get_default_library()
    if lib.delete(name):
        return {"deleted": name}
    return JSONResponse({"error": f"skill '{name}' not found"}, status_code=404)


@app.post("/api/exec")
async def exec_code(request: Request):
    """Run code through the configured execution backend (inline or Docker).

    Body:
      {
        "code": "print('hello')",
        "language": "python",     # optional, default python
        "timeout": 30,            # optional, seconds
        "memory_mb": 256          # optional, MB
      }
    """
    from skyn3t.config.settings import get_settings
    from skyn3t.security.sandbox import get_backend

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    code = str(body.get("code") or "").strip()
    if not code:
        return JSONResponse({"error": "code is required"}, status_code=400)

    language = str(body.get("language") or "python").lower().strip()
    timeout = max(1, min(int(body.get("timeout") or 30), 300))
    memory_mb = max(16, min(int(body.get("memory_mb") or 256), 4096))

    try:
        settings = get_settings()
        backend = await get_backend(settings.execution_backend)
        result = await backend.execute(
            code,
            language=language,
            timeout=timeout,
            memory_mb=memory_mb,
        )
        return {
            "success": result.success,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": result.error,
            "truncated": result.truncated,
            "backend": backend.__class__.__name__,
        }
    except Exception as e:
        return JSONResponse(
            {"error": f"Execution failed: {e}"},
            status_code=500,
        )


@app.get("/api/usage/totals")
async def usage_totals():
    """Aggregate token usage across all agents + all projects."""
    from skyn3t.observability.token_tracker import get_default_tracker
    return get_default_tracker().totals()


@app.get("/api/usage/agents")
async def usage_per_agent():
    """Per-agent token rollup. Sorted desc by total_tokens."""
    from skyn3t.observability.token_tracker import get_default_tracker
    return {"agents": get_default_tracker().per_agent()}


@app.get("/api/usage/projects")
async def usage_per_project():
    """Per-project token rollup with stage breakdown."""
    from skyn3t.observability.token_tracker import get_default_tracker
    return {"projects": get_default_tracker().per_project()}


@app.get("/api/usage/projects/{slug}")
async def usage_for_project(slug: str):
    """Per-project rollup for a single slug."""
    from skyn3t.observability.token_tracker import get_default_tracker
    data = get_default_tracker().for_project(slug)
    if not data:
        return JSONResponse({"error": "no usage data for this slug yet"}, status_code=404)
    return data


@app.get("/api/usage/stage-latency")
async def stage_latency_snapshot():
    """Per-stage wall-clock latency across all runs.

    Returns one entry per stage (brainstorm, architect, code, ...) with
    count + avg/min/max seconds + a small recent-durations window. The
    UI uses this to surface "Architect: avg 187s (last 14 runs)" so you
    can spot the stage that's eating the budget without crawling every
    project.json.
    """
    from skyn3t.intelligence.stage_latency import snapshot
    return {"stages": snapshot()}


@app.get("/api/trajectories")
async def list_trajectories():
    """List available trajectory JSONL files with record counts."""
    if not trajectory_logger:
        return {"files": []}
    return {"files": trajectory_logger.list_files()}


@app.get("/api/trajectories/export")
async def export_trajectories(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    agent: Optional[str] = None,
    outcome: Optional[str] = None,
    include_evaluations: bool = False,
):
    """Export trajectories matching filters as a downloadable JSONL file."""
    if not trajectory_logger:
        return JSONResponse({"error": "trajectory logger not initialized"}, status_code=503)
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
        tmp_path = Path(fh.name)
    trajectory_count = trajectory_logger.export_jsonl(
        tmp_path, from_date=from_date, to_date=to_date, agent=agent, outcome=outcome
    )
    evaluation_count = 0
    if include_evaluations:
        try:
            evaluation_count = await _append_evaluation_bundle(
                tmp_path,
                trajectory_count=trajectory_count,
                review_status="approved",
                limit=500,
                filters={
                    key: value
                    for key, value in {
                        "from_date": from_date,
                        "to_date": to_date,
                        "agent": agent,
                        "outcome": outcome,
                    }.items()
                    if value
                },
            )
        except RuntimeError:
            tmp_path.unlink(missing_ok=True)
            return JSONResponse({"error": "memory not initialized"}, status_code=503)
    total_count = trajectory_count + evaluation_count
    if total_count == 0:
        tmp_path.unlink(missing_ok=True)
        return JSONResponse({"error": "no trajectories match filters"}, status_code=404)

    from fastapi.responses import FileResponse

    return FileResponse(
        path=str(tmp_path),
        media_type="application/jsonl",
        filename=(
            f"trajectories_bundle_{from_date or 'all'}_{to_date or 'all'}.jsonl"
            if include_evaluations
            else f"trajectories_{from_date or 'all'}_{to_date or 'all'}.jsonl"
        ),
    )


@app.get("/api/users/{platform}/{platform_id}")
async def get_user(platform: str, platform_id: str):
    """Get user profile by platform and platform_id."""
    if not orchestrator or not orchestrator.memory_store:
        return JSONResponse({"error": "memory not initialized"}, status_code=503)
    user = await orchestrator.memory_store.get_user_profile(platform_id, platform)
    if not user:
        return JSONResponse({"error": "user not found"}, status_code=404)
    return user


@app.patch("/api/users/{platform}/{platform_id}")
async def patch_user(platform: str, platform_id: str, data: Dict[str, Any]):
    """Update user profile."""
    if not orchestrator or not orchestrator.memory_store:
        return JSONResponse({"error": "memory not initialized"}, status_code=503)
    profile = data.get("profile")
    if not isinstance(profile, dict):
        return JSONResponse({"error": "profile must be an object"}, status_code=400)
    ok = await orchestrator.memory_store.update_user_profile(platform_id, platform, profile)
    if not ok:
        return JSONResponse({"error": "user not found"}, status_code=404)
    return {"ok": True}


@app.get("/api/schedule/jobs")
async def list_schedule_jobs():
    """List all scheduled jobs."""
    if not orchestrator or not orchestrator.memory_store:
        return JSONResponse({"error": "memory not initialized"}, status_code=503)
    jobs = await orchestrator.memory_store.list_scheduled_jobs()
    return {"jobs": jobs, "count": len(jobs)}


@app.post("/api/schedule/jobs")
async def create_schedule_job(data: Dict[str, Any]):
    """Create a new scheduled job."""
    if not orchestrator or not orchestrator.memory_store:
        return JSONResponse({"error": "memory not initialized"}, status_code=503)
    name = str(data.get("name", "")).strip()
    schedule_expr = str(data.get("schedule_expr", "")).strip()
    if not name or not schedule_expr:
        return JSONResponse({"error": "name and schedule_expr are required"}, status_code=400)
    job_id = str(uuid4())
    await orchestrator.memory_store.save_scheduled_job(
        job_id=job_id,
        name=name,
        schedule_expr=schedule_expr,
        agent_name=data.get("agent_name"),
        prompt=data.get("prompt"),
        enabled=data.get("enabled", True),
        next_run=None,
        run_count=0,
    )
    return {"job_id": job_id, "name": name, "created": True}


@app.delete("/api/schedule/jobs/{job_id}")
async def delete_schedule_job(job_id: str):
    """Delete a scheduled job."""
    if not orchestrator or not orchestrator.memory_store:
        return JSONResponse({"error": "memory not initialized"}, status_code=503)
    ok = await orchestrator.memory_store.delete_scheduled_job(job_id)
    if not ok:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return {"deleted": True}


async def _ensure_repo_scout():
    if not orchestrator:
        return None
    scout = getattr(orchestrator, "_repo_scout", None)
    if scout is not None:
        return scout
    try:
        from skyn3t.cortex.repo_scout import GitHubRepoScout

        scout = GitHubRepoScout(orchestrator=orchestrator, event_bus=orchestrator.event_bus)
        scout.start()
        setattr(orchestrator, "_repo_scout", scout)
        return scout
    except Exception:
        logger.exception("could not initialize repo scout")
        return None


def _normalize_repo_scout_config(
    data: Dict[str, Any] | None,
    *,
    default_platforms: List[str],
) -> Dict[str, Any]:
    payload = dict(data or {})
    if "platforms" not in payload or payload.get("platforms") is None:
        payload["platforms"] = list(default_platforms)
    return payload


async def _run_repo_scout(data: Dict[str, Any] | None = None, *, default_platforms: List[str]):
    if not orchestrator:
        return JSONResponse({"error": "orchestrator not initialized"}, status_code=503)
    scout = await _ensure_repo_scout()
    if scout is None:
        return JSONResponse({"error": "repo scout not available"}, status_code=503)
    result = await scout.run_once(_normalize_repo_scout_config(data, default_platforms=default_platforms))
    status_code = 200 if result.get("ok") else 400
    if status_code != 200:
        return JSONResponse(result, status_code=status_code)
    return result


async def _schedule_repo_scout(
    data: Dict[str, Any],
    *,
    default_platforms: List[str],
    default_name_prefix: str,
):
    if not orchestrator or not orchestrator.memory_store:
        return JSONResponse({"error": "memory not initialized"}, status_code=503)
    schedule_expr = str(data.get("schedule_expr", "")).strip()
    if not schedule_expr:
        return JSONResponse({"error": "schedule_expr is required"}, status_code=400)
    cadence = str(data.get("cadence") or "daily").strip() or "daily"
    name = str(data.get("name") or "").strip() or f"{default_name_prefix}-{cadence}-{uuid4().hex[:6]}"
    config = _normalize_repo_scout_config(
        {
            "cadence": cadence,
            "limit": data.get("limit", 4),
            "queries": data.get("queries") or [],
            **({"platforms": data.get("platforms")} if "platforms" in data else {}),
        },
        default_platforms=default_platforms,
    )
    job_id = str(uuid4())
    await orchestrator.memory_store.save_scheduled_job(
        job_id=job_id,
        name=name,
        schedule_expr=schedule_expr,
        agent_name="github_repo_scout",
        prompt=json.dumps(config, sort_keys=True),
        enabled=bool(data.get("enabled", True)),
        next_run=None,
        run_count=0,
    )
    return {"job_id": job_id, "name": name, "created": True, "config": config}


@app.post("/api/repo-scout/run")
async def run_repo_scout(data: Dict[str, Any] | None = None):
    """Run the multi-source repo scout immediately."""
    return await _run_repo_scout(data, default_platforms=["github", "gitlab", "bitbucket"])


@app.post("/api/repo-scout/schedule")
async def schedule_repo_scout(data: Dict[str, Any]):
    """Create a recurring multi-source repo scout job."""
    return await _schedule_repo_scout(
        data,
        default_platforms=["github", "gitlab", "bitbucket"],
        default_name_prefix="repo-scout",
    )


@app.post("/api/github/scout/run")
async def run_github_scout(data: Dict[str, Any] | None = None):
    """Run the GitHub scout immediately."""
    return await _run_repo_scout(data, default_platforms=["github"])


@app.post("/api/github/scout/schedule")
async def schedule_github_scout(data: Dict[str, Any]):
    """Create a recurring GitHub scout job."""
    return await _schedule_repo_scout(
        data,
        default_platforms=["github"],
        default_name_prefix="github-scout",
    )


@app.get("/api/memory/skills")
async def get_skills(tag: Optional[str] = None, limit: int = 20):
    """First-class skill library — durable learned-skill files in data/skills/.

    With ``tag``: returns matching skills (case-insensitive). Without: returns
    the aggregate summary plus the top-N highest-scored skills.
    """
    from skyn3t.intelligence.skill_library import get_default_library
    lib = get_default_library()
    limit = _clamp_limit(limit, default=20, hi=100)
    if tag:
        return {
            "tag": tag,
            "skills": [s.__dict__ | {"score": s.score} for s in lib.find(tag=tag, min_score=-1.0, limit=limit)],
        }
    summary = lib.summary()
    top = lib.find(min_score=0.1, limit=limit)
    return {
        "summary": summary,
        "top": [s.__dict__ | {"score": s.score} for s in top],
    }


@app.get("/api/skills/candidates")
async def get_skill_candidates(limit: int = 20, min_confidence: float = 0.7):
    """List approved governed-memory docs eligible for skill drafting."""
    store = getattr(orchestrator, "memory_store", None) or getattr(orchestrator, "_memory", None)
    if not store:
        return JSONResponse({"error": "memory not initialized"}, status_code=503)
    limit = _clamp_limit(limit, default=20, hi=100)
    candidates = await store.list_skill_candidate_docs(
        min_confidence=min_confidence,
        limit=limit,
    )
    return {"candidates": candidates}


@app.get("/api/skills/drafts")
async def list_skill_drafts():
    """List pending skill drafts derived from governed memory."""
    from skyn3t.intelligence.skill_library import get_default_library

    lib = get_default_library()
    return {
        "drafts": [
            {
                "name": skill.name,
                "slug": skill.slug,
                "description": skill.description,
                "tags": skill.tags,
                "triggers": skill.triggers,
                "source": skill.source,
                "created_at": skill.created_at,
                "memory_doc_id": skill.memory_doc_id,
            }
            for skill in lib.all_drafts()
        ]
    }


@app.post("/api/skills/drafts/from-memory/{doc_id}")
async def create_skill_draft_from_memory(doc_id: str):
    """Create a pending skill draft from an approved memory document."""
    from skyn3t.intelligence.skill_library import get_default_library, skill_from_memory_doc

    store = getattr(orchestrator, "memory_store", None) or getattr(orchestrator, "_memory", None)
    if not store:
        return JSONResponse({"error": "memory not initialized"}, status_code=503)
    doc = await store.get_knowledge_doc(doc_id)
    if doc is None:
        return JSONResponse({"error": "memory document not found"}, status_code=404)
    meta = dict(doc.get("meta") or {})
    if meta.get("review_status") != "approved":
        return JSONResponse({"error": "memory document must be approved first"}, status_code=400)
    if str(doc.get("doc_type") or "") == "evaluation":
        return JSONResponse(
            {"error": "evaluation assets cannot be promoted into skills"},
            status_code=400,
        )
    if not meta.get("reusable"):
        return JSONResponse({"error": "memory document is not reusable"}, status_code=400)
    if doc.get("doc_type") == "external_learning":
        if meta.get("external_doc_ingest_status") != "docs_ingested":
            return JSONResponse(
                {"error": "external learning document must ingest approved docs first"},
                status_code=400,
            )
        if not list(meta.get("external_doc_paths_ingested") or []):
            return JSONResponse(
                {"error": "external learning document has no ingested docs to promote"},
                status_code=400,
            )
    if meta.get("skill_promotion_status"):
        return JSONResponse(
            {
                "error": "memory document already has a skill promotion status",
                "status": meta.get("skill_promotion_status"),
            },
            status_code=400,
        )

    lib = get_default_library()
    skill = skill_from_memory_doc(doc)
    lib.upsert_draft(skill)
    updated = await store.merge_knowledge_doc_meta(
        doc_id,
        {
            "skill_promotion_status": "draft",
            "skill_draft_slug": skill.slug,
        },
    )
    return {
        "created": True,
        "draft": {
            "name": skill.name,
            "slug": skill.slug,
            "memory_doc_id": skill.memory_doc_id,
            "tags": skill.tags,
        },
        "memory_doc": updated,
    }


@app.post("/api/skills/drafts/{slug}/approve")
async def approve_skill_draft(slug: str):
    """Promote a pending skill draft into the live skill library."""
    from skyn3t.intelligence.skill_library import get_default_library

    lib = get_default_library()
    draft = lib.get_draft(slug)
    if draft is None:
        return JSONResponse({"error": "skill draft not found"}, status_code=404)
    path = lib.approve_draft(slug)
    if path is None:
        return JSONResponse({"error": "skill draft not found"}, status_code=404)

    store = getattr(orchestrator, "memory_store", None) or getattr(orchestrator, "_memory", None)
    memory_doc = None
    if draft.memory_doc_id and store:
        memory_doc = await store.merge_knowledge_doc_meta(
            draft.memory_doc_id,
            {
                "skill_promotion_status": "installed",
                "skill_slug": draft.slug,
                "skill_draft_slug": draft.slug,
            },
        )
    return {
        "installed": path.stem,
        "skill": {
            "name": draft.name,
            "slug": draft.slug,
            "memory_doc_id": draft.memory_doc_id,
        },
        "memory_doc": memory_doc,
    }


@app.post("/api/skills/drafts/{slug}/reject")
async def reject_skill_draft(slug: str, payload: dict | None = None):
    """Reject and delete a pending skill draft."""
    from skyn3t.intelligence.skill_library import get_default_library

    lib = get_default_library()
    draft = lib.get_draft(slug)
    if draft is None:
        return JSONResponse({"error": "skill draft not found"}, status_code=404)
    if not lib.reject_draft(slug):
        return JSONResponse({"error": "skill draft not found"}, status_code=404)
    reason = str((payload or {}).get("reason") or "")

    store = getattr(orchestrator, "memory_store", None) or getattr(orchestrator, "_memory", None)
    memory_doc = None
    if draft.memory_doc_id and store:
        extra_meta: Dict[str, Any] = {
            "skill_promotion_status": "rejected",
            "skill_draft_slug": draft.slug,
        }
        if reason:
            extra_meta["skill_promotion_reason"] = reason
        memory_doc = await store.merge_knowledge_doc_meta(draft.memory_doc_id, extra_meta)
    return {
        "rejected": True,
        "draft": {
            "name": draft.name,
            "slug": draft.slug,
            "memory_doc_id": draft.memory_doc_id,
        },
        "memory_doc": memory_doc,
    }


@app.get("/api/memory/build_patterns")
async def get_build_patterns(stack: Optional[str] = None):
    """Build-pattern scoreboard — (stack, shape) → success/failure counts.

    Without ``stack``: returns the aggregate summary plus per-stack
    best+worst shapes. With ``stack``: returns every recorded shape for
    that stack. Driven by BuildVerifier outcomes recorded in
    skyn3t/studio/runner.py after every scaffold.
    """
    from skyn3t.intelligence.build_patterns import get_default_scoreboard
    sb = get_default_scoreboard()
    if stack:
        return {
            "stack": stack,
            "shapes": [s.to_dict() for s in sb.all_stats_for(stack)],
        }
    # Aggregate view — for the dashboard tile.
    summary = sb.summary()
    per_stack = {}
    # Touch the private map carefully — we only read keys.
    try:
        with sb._lock:
            stacks = list(sb._stats.keys())
    except Exception:
        stacks = []
    for st in stacks:
        best = sb.best_shape(st)
        worst = sb.worst_shape(st)
        per_stack[st] = {
            "best": best.to_dict() if best else None,
            "worst": worst.to_dict() if worst else None,
        }
    return {"summary": summary, "per_stack": per_stack}


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


# Cap a single WS frame at 64 KiB. Browsers don't normally send anything
# this large to /ws (it's an event stream, not an upload channel); a sender
# pushing larger frames is either misconfigured or hostile.
MAX_WS_FRAME_BYTES = 64 * 1024


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates."""
    _authorize_websocket(websocket)
    await manager.connect(websocket)
    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            if len(raw) > MAX_WS_FRAME_BYTES:
                await websocket.send_json({
                    "type": "error",
                    "error": f"frame too large (>{MAX_WS_FRAME_BYTES} bytes)",
                })
                continue
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                await websocket.send_json({"type": "error", "error": "invalid JSON"})
                continue
            await websocket.send_json({"type": "ack", "data": data})
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket)


@app.websocket("/ws/swarm")
async def swarm_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for the Swarm Live UI.

    Receives the same events as /ws but projected to the compact swarm
    schema: {"kind", "ts", "from", "to", "label", "meta"}.
    """
    _authorize_websocket(websocket)
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
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except Exception:
                break
            if len(raw) > MAX_WS_FRAME_BYTES:
                await websocket.send_json({
                    "type": "error",
                    "error": f"frame too large (>{MAX_WS_FRAME_BYTES} bytes)",
                })
                continue
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                await websocket.send_json({"type": "error", "error": "invalid JSON"})
                continue
            await websocket.send_json({"type": "ack", "data": data})
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
@app.get("/api/cortex/status")
async def cortex_status():
    if orchestrator and hasattr(orchestrator, "get_cortex_status"):
        return orchestrator.get_cortex_status()
    from skyn3t.cortex import get_store

    store = get_store()
    return {
        "running": False,
        "booted": False,
        "components": [],
        "proposal_handlers": store.registered_handlers(),
        "proposal_counts": {},
        "recent_failures": [],
        "warnings": ["Orchestrator not initialized."],
    }


@app.get("/api/proposals")
async def proposals_list(status: str | None = None, origin: str | None = None):
    from skyn3t.cortex import get_store
    origin_filter = str(origin or "").strip().lower() or None
    return {
        "proposals": [
            p.to_public()
            for p in get_store().list(status=status, origin=origin_filter)
        ]
    }


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


@app.post("/api/proposals/feature/preview")
async def proposals_feature_preview(payload: dict, request: Request = None):  # type: ignore[assignment]
    """Dry-run what filing this idea would do.

    Used by the Cortex UI to show the user — before they file — what
    file would be patched, which capability area it touches, and what
    the planned execution would look like on approval. No proposal is
    created.
    """
    if request is not None:
        rl = _rate_limit_check(request, label="proposals_feature_preview", capacity=30, refill_per_sec=30/60)
        if rl is not None:
            return rl
    idea = (payload or {}).get("idea", "").strip()
    if not idea:
        return JSONResponse({"error": "idea required"}, status_code=400)
    try:
        from skyn3t.cortex.feature_suggester import (
            _TARGET_HINTS,
            REPO_ROOT,
            _idea_keywords,
            infer_feature_target_file,
        )

        target_file = infer_feature_target_file(idea, repo_root=REPO_ROOT)
        keywords = _idea_keywords(idea)

        # Which capability areas the idea touches (from _TARGET_HINTS).
        # Each hint pair is (keyword_set, related_paths).
        kw_set = set(keywords)
        capability_hits = []
        for hint_keywords, hint_paths in _TARGET_HINTS:
            overlap = hint_keywords.intersection(kw_set)
            if overlap:
                capability_hits.append({
                    "keywords": sorted(overlap),
                    "related_files": list(hint_paths),
                })

        # Heuristic: estimate what features could land. We can't truly
        # predict — but we can list adjacent files + the rationale the
        # code_improver would receive, which is most of the answer.
        next_action: dict
        if target_file:
            next_action = {
                "kind": "self_patch",
                "summary": (
                    f"On approval, the CodeImproverAgent will draft a patch "
                    f"starting from `{target_file}` based on the rationale you wrote."
                ),
                "target_file": target_file,
                "agent": "code_improver",
            }
        else:
            next_action = {
                "kind": "blocked",
                "summary": (
                    "No starting file could be inferred from this idea. "
                    "Try mentioning a concrete area (e.g. 'planner', 'cortex', "
                    "'studio', 'rag') or a file path."
                ),
                "agent": None,
            }

        return {
            "idea": idea,
            "target_file": target_file or "",
            "keywords": keywords,
            "capability_hits": capability_hits,
            "next_action": next_action,
            "would_create": {
                "kind": "feature",
                "origin": "user",
                "status": "pending",
                "requires_approval": True,
            },
        }
    except Exception as e:
        return _safe_error_response(e)


@app.post("/api/proposals/feature")
async def proposals_feature(payload: dict, request: Request = None):  # type: ignore[assignment]
    # Rate-limit: 10/min/IP. Free-text idea filing → spam vector.
    if request is not None:
        rl = _rate_limit_check(request, label="proposals_feature", capacity=10, refill_per_sec=10/60)
        if rl is not None:
            return rl
    idea = (payload or {}).get("idea", "").strip()
    if not idea:
        return JSONResponse({"error": "idea required"}, status_code=400)
    try:
        from skyn3t.cortex import get_store
        from skyn3t.cortex.feature_suggester import FeatureSuggester
        # use the orchestrator's instance if present, else a transient one
        suggester = getattr(orchestrator, "_feature_suggester", None) or FeatureSuggester(event_bus)
        pid = suggester.file_user_idea(idea, source="user_dashboard")
        if not pid:
            return JSONResponse({"error": "could not file idea"}, status_code=500)
        proposal = get_store().get(pid)
        response = {"ok": True, "proposal_id": pid}
        if proposal is not None:
            response["target_file"] = str((proposal.payload or {}).get("target_file") or "")
        return response
    except Exception as e:
        return _safe_error_response(e)


@app.websocket("/ws/proposals")
async def ws_proposals(websocket: WebSocket):
    _authorize_websocket(websocket)
    await websocket.accept()
    from skyn3t.cortex import get_store
    store = get_store()
    q = store.subscribe()
    origin_filter = str(websocket.query_params.get("origin") or "").strip().lower() or None
    try:
        # send a snapshot
        await websocket.send_json({
            "type": "snapshot",
            "proposals": [
                p.to_public()
                for p in store.list(status="pending", origin=origin_filter)
            ],
        })
        while True:
            evt = await q.get()
            proposal = evt.get("proposal") or {}
            proposal_origin = str(proposal.get("origin") or "system").strip().lower()
            if origin_filter and proposal_origin != origin_filter:
                continue
            await websocket.send_json(evt)
    except Exception:
        pass
    finally:
        store.unsubscribe(q)
        try:
            await websocket.close()
        except Exception:
            pass


# ─── Project Studio ───
def _get_studio_runner(app):
    runner = getattr(app.state, "studio_runner", None)
    if runner is None:
        from skyn3t.studio import StudioRunner
        settings = get_settings()
        runner = StudioRunner(
            event_bus=event_bus,
            rag=getattr(app.state, "rag_engine", None),
            projects_root=settings.projects_dir,
        )
        app.state.studio_runner = runner
    return runner


@app.get("/api/studio/templates")
async def studio_templates():
    from skyn3t.studio import list_templates
    from skyn3t.studio.mission_setup import mission_setup_options

    return {
        "templates": list_templates(),
        "mission_setup": mission_setup_options(),
    }


@app.get("/api/examples")
async def get_examples():
    return {
        "examples": [
            {
                "id": "redesign-dashboard",
                "title": "Redesign this dashboard",
                "subtitle": "Sweep across the UI to refine spacing, typography, and color",
                "icon": "fa-palette",
                "template": "frontend_redesign",
                "brief": "Redesign skyn3t/web/dashboard.html — refine spacing, typography hierarchy, and visual consistency. Polish forms, cards, and the swarm map. Keep all DOM IDs and JS handlers intact.",
            },
            {
                "id": "habit-tracker",
                "title": "Build a habit tracker app",
                "subtitle": "Full SaaS scaffold from brief to README + architecture",
                "icon": "fa-circle-check",
                "template": "auto",
                "brief": "Build a personal habit tracker as a small web app. Daily check-ins, streak tracking, simple visualization. Single-user, no auth needed. Pick a minimal stack (HTML+JS or Python+SQLite).",
            },
            {
                "id": "marketing-launch",
                "title": "Marketing campaign for a SaaS launch",
                "subtitle": "Positioning + channel plan + landing copy + checklist",
                "icon": "fa-bullhorn",
                "template": "auto",
                "brief": "Build a launch campaign for a new AI-powered code reviewer tool. Audience: senior engineers and engineering managers. Channels: Hacker News, X/Twitter, dev podcasts. Include positioning, channel plan, and a launch-day checklist.",
            },
            {
                "id": "brand-kit",
                "title": "Generate a brand kit",
                "subtitle": "Palette + typography + voice + logo concepts",
                "icon": "fa-paintbrush",
                "template": "brand_kit",
                "brief": "Create a brand kit for an open-source dev tool called 'Skyn3t' — autonomous multi-agent orchestration. Aesthetic: military HUD meets modern dev tool. Dark, technical, slightly menacing but trustworthy.",
            },
            {
                "id": "ingest-repo",
                "title": "Ingest a GitHub repo into RAG",
                "subtitle": "Pull docs from any repo so the swarm can reference it",
                "icon": "fa-database",
                "template": "auto",
                "brief": "Ingest the GitHub repo openai/openai-cookbook into our RAG. Pull README, examples/, and docs. Tag as kind=reference. Then summarize what topics were covered.",
            },
            {
                "id": "audit-codebase",
                "title": "Audit this codebase",
                "subtitle": "Surface risks, dead code, and improvement priorities",
                "icon": "fa-magnifying-glass",
                "template": "auto",
                "brief": "Audit the skyn3t/ Python package. Identify dead code, unused imports, modules that have grown too large, and files with high failure rates from the recent project history. Produce review.md with prioritized recommendations.",
            },
            {
                "id": "business-plan",
                "title": "Write a business plan",
                "subtitle": "Market scan + revenue model + 10-slide pitch outline",
                "icon": "fa-chart-line",
                "template": "business_plan",
                "brief": "A B2B AI-powered scheduling assistant for sales teams that reads CRM context and proposes meeting times. Subscription model. Target: mid-market SaaS sales leaders. Produce market scan, business model, and a 10-slide pitch.",
            },
        ]
    }


@app.post("/api/studio/start")
async def studio_start(payload: dict):
    from skyn3t.studio import StudioRunner  # noqa: F401  ensure package importable
    template_key = payload.get("template")
    brief = (payload.get("brief") or "").strip()
    mission_setup = payload.get("mission_setup")
    repo_target = payload.get("repo_target")
    if not template_key:
        return JSONResponse({"error": "missing template"}, status_code=400)
    runner = _get_studio_runner(app)
    extra = payload.get("extra") or {}
    try:
        # reserve_project performs a sync `git rev-parse` subprocess (timeout=10s)
        # via repo_target.resolve_repo_target; run it off the event loop so the
        # whole server isn't blocked while git resolves the repo root.
        manifest = await asyncio.to_thread(
            runner.reserve_project,
            template_key,
            brief,
            slug=payload.get("slug"),
            mission_setup=mission_setup,
            repo_target=repo_target,
        )
    except KeyError:
        return JSONResponse({"error": f"unknown template: {template_key}"}, status_code=400)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # don't await; run in background so the HTTP request returns fast
    task = asyncio.create_task(
        runner.start(
            template_key,
            brief,
            slug=manifest.get("slug"),
            extra=extra,
            mission_setup=mission_setup,
            repo_target=repo_target,
        )
    )
    _track_studio_task(task, runner=runner, slug=str(manifest.get("slug") or ""), action="starting")
    return {
        "accepted": True,
        "template": template_key,
        "slug": manifest.get("slug"),
        "title": manifest.get("title"),
        "status": manifest.get("status"),
        "next_action": manifest.get("next_action"),
        "workflow_summary": manifest.get("workflow_summary"),
        "mission_setup": manifest.get("mission_setup"),
        "repo_target": manifest.get("repo_target"),
    }


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


def _resolve_studio_project_artifact_path(slug: str, path: str) -> Path:
    runner = _get_studio_runner(app)
    if runner.get_project(slug) is None:
        raise LookupError("not found")
    base = (Path(runner.projects_root) / slug).resolve()
    requested = Path(path)
    if requested.is_absolute():
        raise ValueError("invalid path")
    parts = requested.parts
    if len(parts) >= 3 and parts[0] == "projects" and parts[1] == slug:
        requested = Path(*parts[2:])
    elif len(parts) >= 2 and parts[0] == slug:
        requested = Path(*parts[1:])
    current: Path = base
    for part in requested.parts:
        if part in {"", ".", ".."}:
            raise ValueError("invalid path")
        current = current / part
        if current.is_symlink():
            raise ValueError("invalid path")
    target = current.resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError("invalid path") from exc
    if not target.is_file():
        raise ValueError("invalid path")
    return target


def _studio_preview_csp() -> str:
    return (
        "default-src 'self' data: blob: https:; "
        "script-src 'self' https: 'unsafe-inline'; "
        "style-src 'self' https: 'unsafe-inline'; "
        "font-src 'self' https: data:; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' ws: wss: https:; "
        "frame-ancestors 'self'; "
        "base-uri 'self'"
    )


@app.get("/api/studio/projects/{slug}/file")
async def studio_project_file(slug: str, path: str):
    try:
        target = _resolve_studio_project_artifact_path(slug, path)
    except LookupError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except ValueError:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return JSONResponse({"error": "binary file not supported"}, status_code=415)
    return PlainTextResponse(text)


@app.get("/api/studio/projects/{slug}/preview/{artifact_path:path}")
async def studio_project_preview(slug: str, artifact_path: str):
    try:
        target = _resolve_studio_project_artifact_path(slug, artifact_path)
    except LookupError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except ValueError:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    media_type, _ = mimetypes.guess_type(str(target))
    response = (
        FileResponse(str(target), media_type=media_type)
        if media_type
        else FileResponse(str(target))
    )
    response.headers["Content-Security-Policy"] = _studio_preview_csp()
    return response


@app.get("/api/studio/projects/{slug}/zip")
async def studio_project_zip(slug: str):
    runner = _get_studio_runner(app)
    try:
        zip_path = runner.export_zip(slug)
    except FileNotFoundError:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(zip_path), media_type="application/zip", filename=f"{slug}.zip")


@app.get("/api/studio/projects/{slug}/design-handoff/penpot")
async def studio_project_penpot_manifest(slug: str):
    runner = _get_studio_runner(app)
    if runner.get_project(slug) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    artifact_dir = Path(runner.projects_root) / slug
    return build_penpot_manifest(artifact_dir)


@app.get("/api/studio/projects/{slug}/design-handoff/penpot/package")
async def studio_project_penpot_package(slug: str):
    runner = _get_studio_runner(app)
    if runner.get_project(slug) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    artifact_dir = Path(runner.projects_root) / slug
    content = build_penpot_package(artifact_dir)
    headers = {
        "Content-Disposition": f'attachment; filename="{slug}-penpot-handoff.zip"',
    }
    return Response(content=content, media_type="application/zip", headers=headers)


@app.post("/api/studio/projects/{slug}/clarify")
async def studio_project_clarify(slug: str, payload: dict):
    runner = _get_studio_runner(app)
    answers = payload.get("answers") or []
    if not isinstance(answers, list):
        return JSONResponse({"error": "answers must be a list"}, status_code=400)
    project = runner.get_project(slug)
    if project is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    status = str(project.get("status") or "").strip().lower()
    if status != "awaiting_clarification":
        return JSONResponse(
            {
                "error": "project is not waiting for clarification",
                "status": project.get("status"),
            },
            status_code=409,
        )
    clarification = project.get("clarification") or {}
    questions = clarification.get("questions") or []
    if questions:
        normalized_answers = [str(a).strip() for a in answers]
        if len(normalized_answers) < len(questions):
            normalized_answers.extend([""] * (len(questions) - len(normalized_answers)))
        if len(normalized_answers) > len(questions):
            normalized_answers = normalized_answers[: len(questions)]
        if any(not answer for answer in normalized_answers):
            return JSONResponse(
                {
                    "error": f"expected {len(questions)} non-empty answers",
                    "question_count": len(questions),
                },
                status_code=400,
            )
        answers = normalized_answers
    elif not answers:
        return JSONResponse({"error": "answers required"}, status_code=400)
    try:
        # Run async in background so the request returns fast
        task = asyncio.create_task(runner.resume(slug, [str(a) for a in answers]))
        _track_studio_task(task, runner=runner, slug=slug, action="resuming after clarification")
        return {"ok": True, "resuming": slug, "answer_count": len(answers)}
    except FileNotFoundError:
        return JSONResponse({"error": "project not found"}, status_code=404)
    except Exception as e:
        return _safe_error_response(e)


@app.post("/api/studio/projects/{slug}/resume-interrupted")
async def studio_project_resume_interrupted(slug: str):
    """Resume a pipeline that was marked interrupted when the server restarted."""
    runner = _get_studio_runner(app)
    project = runner.get_project(slug)
    if project is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    if str(project.get("status") or "").lower() != "interrupted":
        return JSONResponse(
            {
                "error": "project is not interrupted",
                "status": project.get("status"),
            },
            status_code=400,
        )
    try:
        task = asyncio.create_task(runner.resume_interrupted(slug))
        _track_studio_task(task, runner=runner, slug=slug, action="resuming after interrupt")
        return {"ok": True, "resuming": slug}
    except Exception as e:
        return _safe_error_response(e)


@app.post("/api/studio/projects/{slug}/approve")
async def studio_project_approve(slug: str):
    runner = _get_studio_runner(app)
    if runner.get_project(slug) is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    task = asyncio.create_task(runner.resume_after_approval(slug, "approve"))
    _track_studio_task(task, runner=runner, slug=slug, action="approving")
    return {"ok": True}


@app.post("/api/studio/projects/{slug}/approve-with-edits")
async def studio_project_approve_with_edits(slug: str, payload: dict):
    runner = _get_studio_runner(app)
    if runner.get_project(slug) is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    content = str(payload.get("content") or "").strip()
    if not content:
        return JSONResponse({"error": "content required"}, status_code=400)
    task = asyncio.create_task(
        runner.resume_after_approval(slug, "approve", edited_md=content)
    )
    _track_studio_task(task, runner=runner, slug=slug, action="approving with edits")
    return {"ok": True}


@app.post("/api/studio/projects/{slug}/reject")
async def studio_project_reject(slug: str, payload: dict):
    runner = _get_studio_runner(app)
    if runner.get_project(slug) is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    feedback = str(payload.get("feedback") or "").strip()
    task = asyncio.create_task(
        runner.resume_after_approval(slug, "reject", feedback=feedback)
    )
    _track_studio_task(task, runner=runner, slug=slug, action="rejecting")
    return {"ok": True}


@app.get("/api/studio/approval-config")
async def studio_approval_config_get():
    from skyn3t.studio.approval_gate import load_gate_config
    return load_gate_config()


@app.put("/api/studio/approval-config")
async def studio_approval_config_put(payload: dict):
    from skyn3t.studio.approval_gate import save_gate_config
    try:
        save_gate_config(payload or {})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Discord control surface
# ---------------------------------------------------------------------------


@app.post("/api/discord/interactions")
async def discord_interactions(request: Request):
    """Discord interaction webhook (slash commands, button presses, modals)."""
    from skyn3t.config.settings import get_settings
    from skyn3t.integrations.discord_commands import (
        handle_interaction,
        verify_signature,
    )

    settings = get_settings()
    public_key = settings.discord_public_key
    if not public_key:
        return JSONResponse({"error": "discord not configured"}, status_code=503)

    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    body = await request.body()
    if not verify_signature(public_key, signature, timestamp, body):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    runner = _get_studio_runner(app)
    result = await handle_interaction(payload, runner)

    if result.follow_up is not None:
        task = asyncio.create_task(result.follow_up())
        _track_studio_task(task, runner=runner, slug="discord", action="discord follow-up")

    return JSONResponse(result.response)


@app.post("/api/discord/register-commands")
async def discord_register_commands(request: Request):
    """Idempotently register slash commands with Discord.

    Requires ``X-Skyn3t-Admin`` header matching settings.discord_admin_secret.
    If no admin secret is configured the endpoint returns 503 — set
    SKYN3T_DISCORD_ADMIN_SECRET to enable.
    """
    from skyn3t.config.settings import get_settings
    from skyn3t.integrations.discord_commands import register_slash_commands

    settings = get_settings()
    admin_secret = settings.discord_admin_secret
    if not admin_secret:
        return JSONResponse(
            {"error": "set SKYN3T_DISCORD_ADMIN_SECRET to enable"},
            status_code=503,
        )
    if request.headers.get("X-Skyn3t-Admin", "") != admin_secret:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    app_id = settings.discord_application_id
    token = settings.discord_token
    if not app_id or not token:
        return JSONResponse(
            {"error": "SKYN3T_DISCORD_APP_ID and DISCORD_TOKEN must be set"},
            status_code=400,
        )

    try:
        result = await register_slash_commands(app_id, token)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)
    return result


@app.get("/api/discord/status")
async def discord_status():
    """Return Discord wiring status for the dashboard."""
    from skyn3t.config.settings import get_settings
    from skyn3t.integrations.discord_commands import SLASH_COMMANDS

    settings = get_settings()
    return {
        "app_id_configured": bool(settings.discord_application_id),
        "public_key_configured": bool(settings.discord_public_key),
        "bot_token_configured": bool(settings.discord_token),
        "bot_channel_configured": bool(settings.discord_bot_channel_id),
        "admin_secret_configured": bool(settings.discord_admin_secret),
        "command_count": len(SLASH_COMMANDS),
    }


# ---------------------------------------------------------------------------
# Cleanup routes
# ---------------------------------------------------------------------------


@app.get("/api/cleanup/preview")
async def cleanup_preview(
    projects: bool = True,
    proposals: bool = True,
    branches: bool = True,
    older_than_days: Optional[int] = None,
    keep_last: Optional[int] = None,
):
    from skyn3t.cli.cleanup import preview
    return preview(
        projects=projects,
        proposals=proposals,
        branches=branches,
        older_than_days=older_than_days,
        keep_last=keep_last,
    )


@app.post("/api/cleanup/execute")
async def cleanup_execute(payload: dict):
    from skyn3t.cli.cleanup import execute as exec_plan
    from skyn3t.cli.cleanup import preview
    plan = preview(
        projects=payload.get("projects", True),
        proposals=payload.get("proposals", True),
        branches=payload.get("branches", True),
        older_than_days=payload.get("older_than_days"),
        keep_last=payload.get("keep_last"),
    )
    return exec_plan(plan)


@app.delete("/api/studio/projects/{slug}")
async def studio_project_delete(slug: str):
    from skyn3t.cli.cleanup import delete_project
    return delete_project(slug)


# SPA catch-all — must be the last route registered. React Router owns
# the URL once the SPA loads, but a hard reload (Cmd-Shift-R) makes the
# browser ask the server for /activity, /studio, /cortex, etc. directly.
# Without this fallback, FastAPI returns 404 for those paths. The route
# matches any GET that isn't an API endpoint or a static asset and hands
# back index.html so the SPA can take over.
@app.get("/{full_path:path}", response_class=HTMLResponse)
async def spa_fallback(full_path: str):
    # Reserved prefixes: anything routed by FastAPI before this catch-all
    # already matched. We only get here for unknown paths, so just
    # exclude obvious server-owned namespaces to surface real 404s for
    # bogus API URLs instead of silently returning HTML.
    if (
        full_path.startswith("api/")
        or full_path.startswith("ws/")
        or full_path.startswith("assets/")
        or full_path.startswith("static/")
    ):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    index = _SPA_DIST / "index.html"
    if index.exists():
        return FileResponse(str(index), media_type="text/html")
    return FileResponse(str(_DASHBOARD_PATH), media_type="text/html")
