"""Prometheus metrics integration for SkyN3t."""

import asyncio
import platform
import sys
import time
from typing import Any, Callable, Dict, Optional

from skyn3t.config.settings import get_settings

try:
    from prometheus_client import (
        REGISTRY,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        Info,
        generate_latest,
    )
except ModuleNotFoundError:  # pragma: no cover
    REGISTRY = None  # type: ignore[assignment]
    CollectorRegistry = None  # type: ignore[assignment,misc]
    Counter = None  # type: ignore[assignment,misc]
    Gauge = None  # type: ignore[assignment,misc]
    Histogram = None  # type: ignore[assignment,misc]
    Info = None  # type: ignore[assignment,misc]
    generate_latest = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Default registry (can be swapped for tests)
# ---------------------------------------------------------------------------
_metrics_registry: Any = REGISTRY


def get_metrics_registry() -> Any:
    """Return the active metrics registry."""
    return _metrics_registry


def set_metrics_registry(registry: Any) -> None:
    """Swap the active registry (useful in tests)."""
    global _metrics_registry
    _metrics_registry = registry


# ---------------------------------------------------------------------------
# No-op stubs when prometheus_client is missing
# ---------------------------------------------------------------------------
class _NoOpCounter:
    def inc(self, amount: float = 1) -> None:
        pass

    def labels(self, **kwargs):
        return self


class _NoOpGauge:
    def set(self, value: float) -> None:
        pass

    def labels(self, **kwargs):
        return self


class _NoOpHistogram:
    def observe(self, value: float) -> None:
        pass

    def labels(self, **kwargs):
        return self


class _NoOpInfo:
    def info(self, data: Dict[str, str]) -> None:
        pass


# ---------------------------------------------------------------------------
# Core metrics
# ---------------------------------------------------------------------------
class AgentMetricsCollector:
    """Auto-registering collector that owns all SkyN3t Prometheus metrics."""

    def __init__(self, registry: Optional[Any] = None):
        self.registry = registry or get_metrics_registry()
        self.tasks_submitted: Any
        self.tasks_completed: Any
        self.tasks_failed: Any
        self.active_agents: Any
        self.active_tasks: Any
        self.queue_depth: Any
        self.task_execution_time: Any
        self.cli_execution_time: Any
        self.system_info: Any
        if Counter is None:
            # prometheus_client not installed — create no-op stubs
            self._noop = True
            self.tasks_submitted = _NoOpCounter()
            self.tasks_completed = _NoOpCounter()
            self.tasks_failed = _NoOpCounter()
            self.active_agents = _NoOpGauge()
            self.active_tasks = _NoOpGauge()
            self.queue_depth = _NoOpGauge()
            self.task_execution_time = _NoOpHistogram()
            self.cli_execution_time = _NoOpHistogram()
            self.system_info = _NoOpInfo()
        else:
            self._noop = False
            self._setup_metrics()

    def _setup_metrics(self) -> None:
        """Initialize all Prometheus metrics."""
        # Counters
        self.tasks_submitted = Counter(
            "skyn3t_tasks_submitted_total",
            "Total number of tasks submitted",
            ["agent_name", "agent_type"],
            registry=self.registry,
        )
        self.tasks_completed = Counter(
            "skyn3t_tasks_completed_total",
            "Total number of tasks completed successfully",
            ["agent_name", "agent_type"],
            registry=self.registry,
        )
        self.tasks_failed = Counter(
            "skyn3t_tasks_failed_total",
            "Total number of tasks that failed",
            ["agent_name", "agent_type", "reason"],
            registry=self.registry,
        )

        # Gauges
        self.active_agents = Gauge(
            "skyn3t_active_agents",
            "Number of currently registered agents",
            registry=self.registry,
        )
        self.active_tasks = Gauge(
            "skyn3t_active_tasks",
            "Number of tasks currently executing",
            ["agent_name"],
            registry=self.registry,
        )
        self.queue_depth = Gauge(
            "skyn3t_queue_depth",
            "Current depth of agent task queues",
            ["agent_name", "agent_type"],
            registry=self.registry,
        )

        # Histograms
        self.task_execution_time = Histogram(
            "skyn3t_task_execution_time_seconds",
            "Task execution time in seconds",
            ["agent_name", "agent_type"],
            buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
            registry=self.registry,
        )
        self.cli_execution_time = Histogram(
            "skyn3t_cli_execution_time_seconds",
            "CLI tool execution time in seconds",
            ["tool_name"],
            buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
            registry=self.registry,
        )

        # Info
        settings = get_settings()
        self.system_info = Info(
            "skyn3t_system",
            "SkyN3t system information",
            registry=self.registry,
        )
        self.system_info.info(
            {
                "version": settings.app_version,
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "python_implementation": platform.python_implementation(),
                "platform": platform.platform(),
            }
        )

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------
    def record_task_submitted(self, agent_name: str, agent_type: str) -> None:
        self.tasks_submitted.labels(agent_name=agent_name, agent_type=agent_type).inc()

    def record_task_completed(self, agent_name: str, agent_type: str, duration_sec: float) -> None:
        self.tasks_completed.labels(agent_name=agent_name, agent_type=agent_type).inc()
        self.task_execution_time.labels(agent_name=agent_name, agent_type=agent_type).observe(duration_sec)

    def record_task_failed(self, agent_name: str, agent_type: str, reason: str = "unknown") -> None:
        self.tasks_failed.labels(agent_name=agent_name, agent_type=agent_type, reason=reason).inc()

    def set_active_agents(self, count: int) -> None:
        self.active_agents.set(count)

    def set_active_tasks(self, agent_name: str, count: int) -> None:
        self.active_tasks.labels(agent_name=agent_name).set(count)

    def set_queue_depth(self, agent_name: str, agent_type: str, depth: int) -> None:
        self.queue_depth.labels(agent_name=agent_name, agent_type=agent_type).set(depth)

    def record_cli_execution(self, tool_name: str, duration_sec: float) -> None:
        self.cli_execution_time.labels(tool_name=tool_name).observe(duration_sec)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
_collector: Optional[AgentMetricsCollector] = None


def get_collector() -> AgentMetricsCollector:
    """Return the global metrics collector, creating it if necessary."""
    global _collector
    if _collector is None:
        _collector = AgentMetricsCollector()
    return _collector


def reset_collector() -> None:
    """Reset the global collector (useful in tests)."""
    global _collector
    _collector = None
    if CollectorRegistry is not None:
        set_metrics_registry(CollectorRegistry())
    _collector = AgentMetricsCollector()


# ---------------------------------------------------------------------------
# Prometheus exposition helpers
# ---------------------------------------------------------------------------
def generate_metrics() -> bytes:
    """Generate Prometheus exposition format output."""
    if generate_latest is None:
        return b"# prometheus_client not installed\n"
    return generate_latest(get_metrics_registry())


# ---------------------------------------------------------------------------
# Timing decorator
# ---------------------------------------------------------------------------
def timed(metric_name: str, labels: Optional[Dict[str, str]] = None):
    """Decorator / context-manager helper to time arbitrary callables.

    Usage::

        @timed("my_op", labels={"kind": "foo"})
        async def my_op(): ...
    """
    labels = labels or {}

    def decorator(func: Callable) -> Callable:
        async def async_wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                return await func(*args, **kwargs)
            finally:
                duration = time.monotonic() - start
                _observe(metric_name, duration, labels)

        def sync_wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                return func(*args, **kwargs)
            finally:
                duration = time.monotonic() - start
                _observe(metric_name, duration, labels)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator



def _observe(metric_name: str, duration: float, labels: Dict[str, str]) -> None:
    """Route observation to the correct histogram based on metric name."""
    collector = get_collector()
    if metric_name == "cli_execution" and "tool_name" in labels:
        collector.record_cli_execution(labels["tool_name"], duration)
        return
    if metric_name == "task_execution" and "agent_name" in labels and "agent_type" in labels:
        collector.task_execution_time.labels(**labels).observe(duration)
        return
    # Generic fallback: look up a registered histogram by name in the
    # global Prometheus registry and observe the duration. Silently
    # no-op if not registered or prometheus_client is unavailable.
    try:
        registry = get_metrics_registry()
        if registry is None:
            return
        collectors_attr = getattr(registry, "_names_to_collectors", None)
        histogram = None
        if isinstance(collectors_attr, dict):
            histogram = collectors_attr.get(metric_name)
        if histogram is None:
            return
        if labels:
            try:
                histogram.labels(**labels).observe(duration)
                return
            except Exception:
                pass
        observe_fn = getattr(histogram, "observe", None)
        if callable(observe_fn):
            observe_fn(duration)
    except Exception:
        # Silent no-op on any failure (e.g. metric not a histogram)
        return
