"""Representative Studio benchmark briefs for quality recovery."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    title: str
    template: str
    stack: str
    brief: str
    expected_gates: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def start_payload(self) -> Dict[str, Any]:
        return {
            "template": self.template,
            "brief": self.brief,
            "extra": {
                "benchmark_case": self.id,
                "benchmark_stack": self.stack,
                "quality_floor_score": 85,
            },
        }


BENCHMARK_COHORT: List[BenchmarkCase] = [
    BenchmarkCase(
        id="react-vite-dashboard",
        title="React/Vite operations dashboard",
        template="auto",
        stack="react_vite",
        tags=["react", "vite", "dashboard", "frontend"],
        expected_gates=["npm_install", "npm_build", "reviewer_go_85"],
        brief=(
            "Build a React 18 + Vite operations dashboard with a polished dark UI. "
            "Use localStorage for settings, live-looking cards with loading/error/empty "
            "states, a command palette, responsive layout, and no backend. npm run build "
            "must pass. Declare every imported package in package.json."
        ),
    ),
    BenchmarkCase(
        id="fullstack-service-dashboard",
        title="Fullstack service dashboard",
        template="auto",
        stack="fullstack",
        tags=["react", "express", "docker", "fullstack"],
        expected_gates=["npm_install", "npm_build", "compose_config", "integration_smoke", "reviewer_go_85"],
        brief=(
            "Build a fullstack service dashboard: React/Vite frontend plus Express API. "
            "The backend serves /api/health, /api/services, and /api/config, persists "
            "service settings to a JSON file, and never exposes API keys to the browser. "
            "docker-compose.yml must define every service referenced by depends_on."
        ),
    ),
    BenchmarkCase(
        id="fastapi-health-service",
        title="FastAPI health service",
        template="auto",
        stack="fastapi",
        tags=["python", "fastapi", "api"],
        expected_gates=["python_compile", "pytest", "reviewer_go_85"],
        brief=(
            "Build a FastAPI service with /health, /api/devices, and /api/validate-config. "
            "Include pydantic models, tests/test_health.py, requirements.txt, and README "
            "instructions. It must run without external credentials and pass pytest."
        ),
    ),
    BenchmarkCase(
        id="networking-domain-tool",
        title="Networking domain automation tool",
        template="auto",
        stack="networking_tool",
        tags=["networking", "aruba", "juniper", "dry-run"],
        expected_gates=["domain_benchmark", "dry_run_safety", "reviewer_go_85"],
        brief=(
            "Build a dry-run networking automation tool for Aruba Central and Juniper Mist "
            "operators. It should validate inventory/config input, show planned changes, "
            "produce troubleshooting summaries, and never make live writes by default. "
            "Include sample data, explicit credential status, and operator docs."
        ),
    ),
]


def list_benchmark_cases() -> List[Dict[str, Any]]:
    return [case.to_dict() for case in BENCHMARK_COHORT]


def get_benchmark_case(case_id: str) -> Optional[BenchmarkCase]:
    wanted = str(case_id or "").strip().lower()
    for case in BENCHMARK_COHORT:
        if case.id == wanted:
            return case
    return None


def benchmark_start_payloads() -> List[Dict[str, Any]]:
    return [case.start_payload() for case in BENCHMARK_COHORT]
