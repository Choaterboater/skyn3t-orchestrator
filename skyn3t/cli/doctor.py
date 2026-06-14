"""Local and remote health checks for the SkyN3t CLI."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import httpx

from skyn3t.config.settings import get_settings
from skyn3t.intelligence.skill_library import get_default_library


@dataclass
class DoctorCheck:
    name: str
    status: str
    summary: str
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def icon(self) -> str:
        return {"ok": "OK", "warn": "WARN", "fail": "FAIL"}.get(self.status, "INFO")


@dataclass
class DoctorReport:
    checks: List[DoctorCheck]

    @property
    def failed(self) -> int:
        return sum(1 for check in self.checks if check.status == "fail")

    @property
    def warned(self) -> int:
        return sum(1 for check in self.checks if check.status == "warn")


def run_doctor(api_base: str) -> DoctorReport:
    """Run local filesystem/env checks plus remote API health when available."""
    settings = get_settings()
    library = get_default_library()
    checks: List[DoctorCheck] = []

    paths = {
        "data": settings.data_dir,
        "logs": settings.logs_dir,
        "projects": settings.projects_dir,
        "vectors": Path(settings.vector_db_path),
        "skills": library.root,
    }
    missing = sorted(name for name, path in paths.items() if not path.exists())
    checks.append(
        DoctorCheck(
            name="storage",
            status="fail" if missing else "ok",
            summary="All required directories are ready." if not missing else "Missing required directories.",
            details={name: str(path) for name, path in paths.items()} | {"missing": missing},
        )
    )

    providers = {
        "openai": bool(settings.openai_api_key),
        "anthropic": bool(settings.anthropic_api_key),
        "openrouter": bool(settings.openrouter_api_key),
        "kimi": bool(settings.kimi_api_key),
    }
    enabled = sorted(name for name, present in providers.items() if present)
    checks.append(
        DoctorCheck(
            name="providers",
            status="ok" if enabled else "warn",
            summary="Provider keys configured." if enabled else "No provider API keys configured yet.",
            details={"configured": enabled or [], "all": providers},
        )
    )

    binaries = {name: shutil.which(name) for name in ("python3", "git", "gh", "docker")}
    missing_required = [name for name in ("python3", "git") if not binaries.get(name)]
    missing_optional = [name for name in ("gh", "docker") if not binaries.get(name)]
    binary_status = "fail" if missing_required else "warn" if missing_optional else "ok"
    binary_summary = (
        "Required CLI tools are available."
        if binary_status == "ok"
        else "Optional CLI tools are missing."
        if binary_status == "warn"
        else "Required CLI tools are missing."
    )
    checks.append(
        DoctorCheck(
            name="cli-tools",
            status=binary_status,
            summary=binary_summary,
            details={
                "available": {name: path for name, path in binaries.items() if path},
                "missing_required": missing_required,
                "missing_optional": missing_optional,
            },
        )
    )

    skill_summary = library.summary()
    checks.append(
        DoctorCheck(
            name="skills",
            status="ok",
            summary=f"Skill library ready ({skill_summary['total']} skill files).",
            details=skill_summary,
        )
    )

    try:
        with httpx.Client(base_url=api_base, timeout=5.0) as client:
            health_resp = client.get("/health")
            health_resp.raise_for_status()
            health = health_resp.json()
            health_status = str(health.get("status") or "unknown")
            checks.append(
                DoctorCheck(
                    name="api-health",
                    status="ok"
                    if health_status == "healthy"
                    else "warn"
                    if health_status == "degraded"
                    else "fail",
                    summary=f"Server health is {health_status}.",
                    details=health.get("summary") or {},
                )
            )

            agents_resp = client.get("/api/agents")
            agents_resp.raise_for_status()
            agents_payload = agents_resp.json() or {}
            agents = agents_payload.get("agents") or []
            agent_count = len(agents) if isinstance(agents, list) else len(agents.keys())
            checks.append(
                DoctorCheck(
                    name="agents",
                    status="ok" if agent_count else "warn",
                    summary=f"{agent_count} agent(s) registered."
                    if agent_count
                    else "Server is up, but no agents are registered.",
                    details={"count": agent_count},
                )
            )

            backends_resp = client.get("/api/llm/backends")
            backends_resp.raise_for_status()
            backends = (backends_resp.json() or {}).get("backends") or []
            backend_names = [
                item if isinstance(item, str) else str(item.get("name") or item.get("id") or "")
                for item in backends
            ]
            backend_names = [name for name in backend_names if name]
            checks.append(
                DoctorCheck(
                    name="llm-backends",
                    status="ok" if backend_names else "warn",
                    summary=f"{len(backend_names)} backend(s) reported by the API."
                    if backend_names
                    else "Server is up, but it reported no LLM backends.",
                    details={"backends": backend_names},
                )
            )

            readiness_resp = client.get("/api/llm/readiness")
            readiness_resp.raise_for_status()
            readiness = readiness_resp.json() or {}
            readiness_status = str(readiness.get("status") or "unknown")
            checks.append(
                DoctorCheck(
                    name="llm-readiness",
                    status="ok"
                    if readiness_status == "ready"
                    else "warn"
                    if readiness_status == "degraded"
                    else "fail",
                    summary=(
                        "LLM readiness passed for real project builds."
                        if readiness.get("real_project_ready")
                        else "LLM readiness has blockers for real project builds."
                    ),
                    details={
                        "status": readiness_status,
                        "real_available_backends": readiness.get("real_available_backends") or [],
                        "blockers": readiness.get("blockers") or [],
                        "warnings": readiness.get("warnings") or [],
                        "learnings": readiness.get("learnings") or {},
                    },
                )
            )
    except httpx.ConnectError:
        checks.append(
            DoctorCheck(
                name="api-health",
                status="fail",
                summary="SkyN3t API is unreachable.",
                details={"api_base": api_base},
            )
        )
    except httpx.HTTPError as exc:
        checks.append(
            DoctorCheck(
                name="api-health",
                status="fail",
                summary=f"SkyN3t API health check failed: {exc}",
                details={"api_base": api_base},
            )
        )

    return DoctorReport(checks=checks)
