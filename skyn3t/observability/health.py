"""Advanced health monitoring for SkyN3t."""

import asyncio
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional

from skyn3t.config.settings import get_settings

try:
    import psutil
except ModuleNotFoundError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
class HealthStatus(str, Enum):
    """Health check status."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class HealthCheck:
    """Individual health check result."""

    name: str
    status: HealthStatus
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    response_time_ms: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "details": self.details,
            "timestamp": self.timestamp.isoformat(),
            "response_time_ms": round(self.response_time_ms, 3),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
HealthCheckFn = Callable[[], Awaitable[HealthCheck]]
_CLI_TOOL_CANDIDATES: Dict[str, tuple[str, ...]] = {
    "python": ("python3", "python"),
    "git": ("git",),
    "docker": ("docker",),
}


class HealthRegistry:
    """Manages all health checks and produces aggregated status."""

    def __init__(self):
        self._checks: Dict[str, HealthCheckFn] = {}
        self._last_results: Dict[str, HealthCheck] = {}
        self._built_in_registered = False

    def register(
        self, name: str, check_fn: HealthCheckFn, *, built_in: bool = False
    ) -> None:
        """Register a health check function."""
        self._checks[name] = check_fn

    def unregister(self, name: str) -> None:
        """Remove a health check."""
        self._checks.pop(name, None)
        self._last_results.pop(name, None)

    async def run_check(self, name: str) -> Optional[HealthCheck]:
        """Run a single check by name."""
        fn = self._checks.get(name)
        if not fn:
            return None
        start = datetime.now(timezone.utc)
        try:
            result = await fn()
        except Exception as exc:
            result = HealthCheck(
                name=name,
                status=HealthStatus.UNHEALTHY,
                error=str(exc),
            )
        result.response_time_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        self._last_results[name] = result
        return result

    async def run_all(self) -> Dict[str, Any]:
        """Run all registered checks concurrently and return aggregate data."""
        results: Dict[str, HealthCheck] = {}
        if self._checks:
            checks = await asyncio.gather(
                *[self.run_check(name) for name in self._checks.keys()],
                return_exceptions=True,
            )
            for name, result in zip(self._checks.keys(), checks):
                if isinstance(result, Exception):
                    results[name] = HealthCheck(
                        name=name,
                        status=HealthStatus.UNHEALTHY,
                        error=str(result),
                    )
                elif isinstance(result, HealthCheck):
                    results[name] = result

        self._last_results.update(results)
        return self._aggregate(results)

    def _aggregate(self, results: Dict[str, HealthCheck]) -> Dict[str, Any]:
        """Aggregate individual check results into a single health report."""
        if not results:
            return {
                "status": HealthStatus.UNKNOWN.value,
                "checks": {},
                "summary": {"total": 0, "healthy": 0, "degraded": 0, "unhealthy": 0},
            }

        status_order = [HealthStatus.HEALTHY, HealthStatus.DEGRADED, HealthStatus.UNKNOWN, HealthStatus.UNHEALTHY]
        worst = HealthStatus.HEALTHY
        summary = {"total": len(results), "healthy": 0, "degraded": 0, "unhealthy": 0}

        for check in results.values():
            if check.status == HealthStatus.HEALTHY:
                summary["healthy"] += 1
            elif check.status == HealthStatus.DEGRADED:
                summary["degraded"] += 1
                if status_order.index(HealthStatus.DEGRADED) > status_order.index(worst):
                    worst = HealthStatus.DEGRADED
            elif check.status in (HealthStatus.UNHEALTHY, HealthStatus.UNKNOWN):
                summary["unhealthy"] += 1
                if status_order.index(check.status) > status_order.index(worst):
                    worst = check.status

        return {
            "status": worst.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": {name: check.to_dict() for name, check in results.items()},
            "summary": summary,
        }

    def get_last_results(self) -> Dict[str, HealthCheck]:
        """Return the most recent check results without re-running."""
        return self._last_results.copy()

    def register_built_in_checks(self) -> None:
        """Register the built-in SkyN3t health checks."""
        if self._built_in_registered:
            return
        self._built_in_registered = True

        self.register("disk", _check_disk, built_in=True)
        self.register("memory", _check_memory, built_in=True)
        self.register("cpu", _check_cpu, built_in=True)
        self.register("cli_tools", _check_cli_tools, built_in=True)


# ---------------------------------------------------------------------------
# Built-in check implementations
# ---------------------------------------------------------------------------
async def _check_disk() -> HealthCheck:
    """Check available disk space."""
    settings = get_settings()
    path = str(settings.data_dir.resolve())
    usage = shutil.disk_usage(path)
    total_gb = usage.total / (1024**3)
    free_gb = usage.free / (1024**3)
    percent_used = (usage.used / usage.total) * 100

    status = HealthStatus.HEALTHY
    if percent_used > 95:
        status = HealthStatus.UNHEALTHY
    elif percent_used > 85:
        status = HealthStatus.DEGRADED

    return HealthCheck(
        name="disk",
        status=status,
        details={
            "path": path,
            "total_gb": round(total_gb, 2),
            "free_gb": round(free_gb, 2),
            "used_percent": round(percent_used, 2),
        },
    )


async def _check_memory() -> HealthCheck:
    """Check system memory usage."""
    if psutil is None:
        return HealthCheck(
            name="memory",
            status=HealthStatus.UNKNOWN,
            details={"reason": "psutil not installed"},
            error="psutil not installed",
        )
    mem = psutil.virtual_memory()
    status = HealthStatus.HEALTHY
    if mem.percent > 95:
        status = HealthStatus.UNHEALTHY
    elif mem.percent > 85:
        status = HealthStatus.DEGRADED

    return HealthCheck(
        name="memory",
        status=status,
        details={
            "total_mb": round(mem.total / (1024**2), 2),
            "available_mb": round(mem.available / (1024**2), 2),
            "used_percent": mem.percent,
        },
    )


async def _check_cpu() -> HealthCheck:
    """Check CPU usage."""
    if psutil is None:
        return HealthCheck(
            name="cpu",
            status=HealthStatus.UNKNOWN,
            details={"reason": "psutil not installed"},
            error="psutil not installed",
        )
    # psutil.cpu_percent(interval=0.1) blocks briefly; run in thread
    cpu_percent = await asyncio.to_thread(psutil.cpu_percent, interval=0.5)
    status = HealthStatus.HEALTHY
    if cpu_percent > 95:
        status = HealthStatus.UNHEALTHY
    elif cpu_percent > 85:
        status = HealthStatus.DEGRADED

    return HealthCheck(
        name="cpu",
        status=status,
        details={
            "usage_percent": round(cpu_percent, 2),
            "cpu_count": psutil.cpu_count(),
        },
    )


async def _check_cli_tools() -> HealthCheck:
    """Check availability of essential CLI tools."""
    available = []
    missing = []
    resolved: Dict[str, Dict[str, str]] = {}

    for tool, candidates in _CLI_TOOL_CANDIDATES.items():
        found_command = None
        found_path = None
        for command in candidates:
            if path := shutil.which(command):
                found_command = command
                found_path = path
                break
        if found_command and found_path:
            available.append(found_command)
            resolved[tool] = {"command": found_command, "path": found_path}
        else:
            missing.append(tool)

    status = HealthStatus.HEALTHY if not missing else HealthStatus.DEGRADED

    return HealthCheck(
        name="cli_tools",
        status=status,
        details={
            "available": available,
            "missing": missing,
            "resolved": resolved,
            "accepted_commands": {
                tool: list(candidates) for tool, candidates in _CLI_TOOL_CANDIDATES.items()
            },
        },
        error=f"Missing tools: {missing}" if missing else None,
    )


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
_registry: Optional[HealthRegistry] = None


def get_health_registry() -> HealthRegistry:
    """Return the global health registry, creating it if necessary."""
    global _registry
    if _registry is None:
        _registry = HealthRegistry()
        _registry.register_built_in_checks()
    return _registry


def reset_health_registry() -> None:
    """Reset the global health registry (useful in tests)."""
    global _registry
    _registry = None
