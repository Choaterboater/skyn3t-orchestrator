"""Structured logging setup for SkyN3t."""

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from skyn3t.config.settings import get_settings

try:
    import structlog
    from structlog.processors import JSONRenderer, TimeStamper
    from structlog.stdlib import BoundLogger, LoggerFactory
except ModuleNotFoundError:  # pragma: no cover
    structlog = None  # type: ignore[assignment]
    JSONRenderer = None  # type: ignore[misc,assignment]
    TimeStamper = None  # type: ignore[misc,assignment]
    BoundLogger = None  # type: ignore[misc,assignment]
    LoggerFactory = None  # type: ignore[misc,assignment]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def configure_logging(
    level: str = "INFO",
    json_format: bool = True,
    log_file: Optional[Path] = None,
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
    console: bool = True,
) -> None:
    """Configure structured logging for the entire SkyN3t system.

    Parameters
    ----------
    level: logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    json_format: emit JSON when True, human-readable plain text when False.
    log_file: path to the log file. If None, uses settings.logs_dir / "skyn3t.log".
    max_bytes: maximum size of a single log file before rotation.
    backup_count: number of rotated log files to keep.
    console: also emit logs to stdout.
    """
    settings = get_settings()
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Ensure log directory exists
    logs_dir = settings.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)

    if log_file is None:
        log_file = logs_dir / "skyn3t.log"
    else:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Standard library logging handlers
    # ------------------------------------------------------------------
    handlers: list[logging.Handler] = []

    if console:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(log_level)
        handlers.append(stream_handler)

    # Rotating file handler by size
    rotating_handler = logging.handlers.RotatingFileHandler(
        filename=log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    rotating_handler.setLevel(log_level)
    handlers.append(rotating_handler)

    # Timed rotating file handler by date (midnight, keep 7 days)
    timed_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(log_file.with_suffix(".daily.log")),
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    timed_handler.setLevel(log_level)
    handlers.append(timed_handler)

    # ------------------------------------------------------------------
    # Standard library root logger
    # ------------------------------------------------------------------
    logging.basicConfig(
        format=DEFAULT_LOG_FORMAT,
        level=log_level,
        handlers=handlers,
        force=True,
    )

    # ------------------------------------------------------------------
    # structlog configuration (if available)
    # ------------------------------------------------------------------
    if structlog is not None:
        shared_processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.ExtraAdder(),
            TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
        ]

        if json_format:
            shared_processors.append(JSONRenderer())
        else:
            shared_processors.append(
                structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
            )

        structlog.configure(
            processors=shared_processors,
            context_class=dict,
            logger_factory=LoggerFactory(),
            wrapper_class=BoundLogger,
            cache_logger_on_first_use=True,
        )

    # Log the startup event
    log = get_logger("skyn3t.observability")
    startup_kwargs = {
        "level": level,
        "json_format": json_format,
        "log_file": str(log_file),
        "max_bytes": max_bytes,
        "backup_count": backup_count,
    }
    if structlog is not None:
        log.info("logging_configured", **startup_kwargs)
    else:
        # Fallback: include kwargs in the message so structured fields aren't dropped
        log.info(f"logging_configured {startup_kwargs}")


# ---------------------------------------------------------------------------
# Logger accessor
# ---------------------------------------------------------------------------
_loggers: Dict[str, Any] = {}


def get_logger(name: str) -> Any:
    """Return a structured logger with the given name.

    All returned loggers are structlog ``BoundLogger`` instances that support
    keyword argument binding for rich context::

        log = get_logger("skyn3t.core.agent")
        log.info("task_started", task_id="abc", agent="claude")
    """
    if name not in _loggers:
        if structlog is not None:
            _loggers[name] = structlog.get_logger(name)
        else:
            _loggers[name] = logging.getLogger(name)
    return _loggers[name]


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------
def bind_task_context(
    task_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    agent_type: Optional[str] = None,
    pipeline_id: Optional[str] = None,
    **extra: Any,
) -> None:
    """Bind task-level context variables to every subsequent log line.

    Usage::

        bind_task_context(task_id="abc", agent_name="claude")
        log.info("executing")   # automatically includes task_id & agent_name
    """
    if structlog is None:
        return
    context: Dict[str, Any] = {}
    if task_id:
        context["task_id"] = task_id
    if agent_name:
        context["agent_name"] = agent_name
    if agent_type:
        context["agent_type"] = agent_type
    if pipeline_id:
        context["pipeline_id"] = pipeline_id
    context.update(extra)
    structlog.contextvars.bind_contextvars(**context)


def clear_context() -> None:
    """Clear all bound context variables."""
    if structlog is None:
        return
    structlog.contextvars.clear_contextvars()


def unbind_context(*keys: str) -> None:
    """Unbind specific context variables."""
    if structlog is None:
        return
    structlog.contextvars.unbind_contextvars(*keys)


# ---------------------------------------------------------------------------
# Agent / CLI event log helpers
# ---------------------------------------------------------------------------
def log_agent_event(
    event_type: str,
    agent_name: str,
    agent_type: str,
    details: Optional[Dict[str, Any]] = None,
    level: str = "info",
) -> None:
    """Log a structured agent event."""
    log = get_logger("skyn3t.agent")
    kwargs = {
        "event_type": event_type,
        "agent_name": agent_name,
        "agent_type": agent_type,
        "details": details or {},
    }
    getattr(log, level.lower())(event_type, **kwargs)


def log_task_event(
    event_type: str,
    task_id: str,
    agent_name: str,
    duration_ms: Optional[float] = None,
    success: Optional[bool] = None,
    error: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Log a structured task lifecycle event."""
    log = get_logger("skyn3t.task")
    payload: Dict[str, Any] = {
        "event_type": event_type,
        "task_id": task_id,
        "agent_name": agent_name,
    }
    if duration_ms is not None:
        payload["duration_ms"] = round(duration_ms, 3)
    if success is not None:
        payload["success"] = success
    if error:
        payload["error"] = error
    if details:
        payload.update(details)

    if error:
        log.error(event_type, **payload)
    else:
        log.info(event_type, **payload)


def log_cli_execution(
    tool_name: str,
    command: str,
    duration_ms: Optional[float] = None,
    success: Optional[bool] = None,
    exit_code: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    """Log a CLI tool execution event."""
    log = get_logger("skyn3t.cli")
    payload: Dict[str, Any] = {
        "tool_name": tool_name,
        "command": command[:500],  # truncate long commands
    }
    if duration_ms is not None:
        payload["duration_ms"] = round(duration_ms, 3)
    if success is not None:
        payload["success"] = success
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if error:
        payload["error"] = error

    if error or success is False:
        log.warning("cli_execution", **payload)
    else:
        log.info("cli_execution", **payload)
