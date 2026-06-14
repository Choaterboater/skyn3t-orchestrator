"""LLM readiness assessment for real Project Studio builds."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_PLACEHOLDER_VALUES = {"", "...", "sk-...", "sk-ant-...", "ghp_..."}
_CLI_BINARIES = {
    "claude_cli": "claude",
    "kimi_cli": "kimi",
    "copilot_cli": "copilot",
    "openai_cli": "openai",
}
_REAL_BACKENDS = {
    "claude_cli",
    "kimi_cli",
    "copilot_cli",
    "openai_cli",
    "anthropic",
    "openrouter",
}


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _env_falsy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _FALSE_VALUES


def _has_real_secret(value: Optional[str]) -> bool:
    return str(value or "").strip() not in _PLACEHOLDER_VALUES


def deterministic_real_projects_allowed() -> bool:
    """Whether real project builds may continue on the deterministic stub."""

    return _env_truthy("SKYN3T_ALLOW_DETERMINISTIC_REAL_PROJECTS")


def _backend_availability(settings: Any) -> Dict[str, Dict[str, Any]]:
    openrouter_key = _has_real_secret(getattr(settings, "openrouter_api_key", None))
    anthropic_key = _has_real_secret(getattr(settings, "anthropic_api_key", None))
    openai_key = _has_real_secret(getattr(settings, "openai_api_key", None))

    backends: Dict[str, Dict[str, Any]] = {
        "openrouter": {
            "available": openrouter_key,
            "real": True,
            "reason": "OPENROUTER_API_KEY configured" if openrouter_key else "OPENROUTER_API_KEY not set",
            "requires": ["OPENROUTER_API_KEY"],
        },
        "anthropic": {
            "available": anthropic_key,
            "real": True,
            "reason": "ANTHROPIC_API_KEY configured" if anthropic_key else "ANTHROPIC_API_KEY not set",
            "requires": ["ANTHROPIC_API_KEY"],
        },
        "deterministic": {
            "available": True,
            "real": False,
            "reason": "offline deterministic stub",
            "requires": [],
        },
    }

    for backend, binary in _CLI_BINARIES.items():
        path = shutil.which(binary)
        available = bool(path)
        if backend == "openai_cli":
            available = available and openai_key
        requires = [binary]
        if backend == "openai_cli":
            requires.append("OPENAI_API_KEY")
        backends[backend] = {
            "available": available,
            "real": True,
            "path": path,
            "reason": f"{binary} found on PATH" if available else f"{binary} not ready",
            "requires": requires,
        }

    no_claude = _env_truthy("SKYN3T_NO_CLAUDE")
    if no_claude:
        for disabled in ("anthropic", "claude_cli", "kimi_cli", "copilot_cli", "openai_cli"):
            if disabled in backends:
                backends[disabled] = {
                    **backends[disabled],
                    "available": False,
                    "disabled_by_policy": "SKYN3T_NO_CLAUDE",
                    "reason": f"{disabled} is disabled by SKYN3T_NO_CLAUDE; route via OpenRouter",
                }
    if no_claude:
        auto_available = openrouter_key
        auto_reason = (
            "SKYN3T_NO_CLAUDE routes auto to OpenRouter"
            if auto_available
            else "SKYN3T_NO_CLAUDE routes auto to OpenRouter, but OPENROUTER_API_KEY is not set"
        )
    else:
        auto_available = any(
            item["available"]
            for name, item in backends.items()
            if name in _REAL_BACKENDS
        )
        auto_reason = "at least one real backend is available" if auto_available else "no real backend is available"

    backends["auto"] = {
        "available": auto_available,
        "real": True,
        "reason": auto_reason,
        "requires": ["one real backend"],
    }
    return backends


def _policy_snapshot() -> Dict[str, Any]:
    return {
        "budget_mode": os.environ.get("SKYN3T_LLM_BUDGET_MODE", "adaptive_budget").strip()
        or "adaptive_budget",
        "free_only": _env_truthy("SKYN3T_FREE_ONLY"),
        "no_claude": _env_truthy("SKYN3T_NO_CLAUDE"),
        "force_claude_cli": _env_truthy("SKYN3T_LLM_FORCE_CLAUDE_CLI"),
        "auto_route": _env_truthy("SKYN3T_AUTO_ROUTE"),
        "cheap_smart": not _env_falsy("SKYN3T_CHEAP_SMART"),
        "code_tier": os.environ.get("SKYN3T_CODE_TIER", "").strip() or None,
        "deterministic_real_projects_allowed": deterministic_real_projects_allowed(),
        "claude_policy": "operator_toggle",
    }


def _catalog_snapshot() -> Dict[str, Any]:
    from skyn3t.core.model_evolution import evolution_status, load_overrides
    from skyn3t.core.openrouter_catalog import (
        catalog_ttl_seconds,
        is_sync_enabled,
        load_catalog,
        validate_tier_models,
    )

    snap = load_catalog()
    overrides = load_overrides(max_age=0.0)
    return {
        "source": snap.source,
        "count": len(snap.models),
        "synced_at": snap.synced_at,
        "ttl_seconds": catalog_ttl_seconds(),
        "stale": snap.stale,
        "sync_enabled": is_sync_enabled(),
        "tier_validation": validate_tier_models(),
        "evolution": evolution_status(),
        "tier_overrides": overrides.get("tiers") if isinstance(overrides.get("tiers"), dict) else {},
    }


def _learnings_snapshot() -> Dict[str, Any]:
    from skyn3t.intelligence.learnings_store import learnings_dir

    root = learnings_dir()
    json_path = root / "playbook.json"
    md_path = root / "playbook.md"
    count = 0
    if json_path.exists():
        try:
            rows = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(rows, list):
                count = len(rows)
        except Exception:
            count = 0
    source_hint = None
    try:
        if Path(root).as_posix().startswith("/Volumes/Projects/skynetllm"):
            source_hint = "smb://ugnas/Projects/skynetllm/"
    except Exception:
        source_hint = None
    return {
        "dir": str(root),
        "json_path": str(json_path),
        "md_path": str(md_path),
        "json_exists": json_path.exists(),
        "md_exists": md_path.exists(),
        "entry_count": count,
        "source_hint": source_hint,
    }


def _route_issues(
    routes: List[Dict[str, Any]],
    availability: Dict[str, Dict[str, Any]],
    *,
    any_real_backend_available: bool,
    deterministic_allowed: bool,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    blockers: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    for route in routes:
        stage = route.get("stage")
        backend = str(route.get("backend") or "")
        model = route.get("model")
        if backend == "deterministic":
            issue = {
                "code": "deterministic_route",
                "stage": stage,
                "backend": backend,
                "model": model,
                "message": "Stage resolves to deterministic fallback.",
            }
            if deterministic_allowed:
                warnings.append(issue)
            else:
                blockers.append(issue)
            continue

        backend_state = availability.get(backend)
        if backend_state and backend_state.get("available"):
            continue

        issue = {
            "code": "backend_unavailable",
            "stage": stage,
            "backend": backend,
            "model": model,
            "message": backend_state.get("reason") if backend_state else "Unknown backend.",
        }
        if any_real_backend_available:
            warnings.append(issue)
        else:
            blockers.append(issue)

    return blockers, warnings


def _usable_real_backends(
    availability: Dict[str, Dict[str, Any]],
    configured_backend: str,
    *,
    no_claude: bool,
) -> List[str]:
    """Backends that the current runtime policy can actually reach."""

    if no_claude:
        candidates = ["openrouter"]
    elif configured_backend and configured_backend != "auto":
        candidates = [configured_backend]
    else:
        # Mirrors LLMClient auto mode: claude_cli first, then API backends.
        # Kimi/Copilot/OpenAI CLIs can be explicit backends, but auto does not
        # attempt them, so they must not make real-build readiness pass.
        candidates = ["claude_cli", "anthropic", "openrouter"]
    return [
        name
        for name in candidates
        if name in _REAL_BACKENDS and availability.get(name, {}).get("available")
    ]


def assess_llm_readiness() -> Dict[str, Any]:
    """Return a non-secret readiness snapshot for real LLM-backed builds."""

    from skyn3t.config.settings import get_settings
    from skyn3t.core.model_router import available_tiers, list_stage_routes

    settings = get_settings()
    policy = _policy_snapshot()
    availability = _backend_availability(settings)
    routes = list_stage_routes()
    tiers = available_tiers()
    catalog = _catalog_snapshot()
    openrouter_status: Dict[str, Any] = {}
    try:
        from skyn3t.adapters.openrouter import openrouter_runtime_status

        openrouter_status = openrouter_runtime_status()
    except Exception:
        openrouter_status = {"error": "openrouter runtime status unavailable"}
    learnings = _learnings_snapshot()

    configured_backend = str(getattr(settings, "llm_backend", "") or "auto").strip().lower()
    real_available = _usable_real_backends(
        availability,
        configured_backend,
        no_claude=bool(policy["no_claude"]),
    )
    deterministic_allowed = bool(policy["deterministic_real_projects_allowed"])
    blockers: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    if configured_backend == "deterministic" and not deterministic_allowed:
        blockers.append(
            {
                "code": "configured_deterministic",
                "backend": configured_backend,
                "message": "SKYN3T_LLM_BACKEND=deterministic cannot produce real projects.",
            }
        )
    elif configured_backend in availability and not availability[configured_backend].get("available"):
        issue = {
            "code": "configured_backend_unavailable",
            "backend": configured_backend,
            "message": availability[configured_backend].get("reason"),
        }
        if real_available:
            warnings.append(issue)
        else:
            blockers.append(issue)

    if not real_available and not deterministic_allowed:
        blockers.append(
            {
                "code": "no_real_backend",
                "message": "No real LLM backend is available; only deterministic fallback can run.",
            }
        )

    route_blockers, route_warnings = _route_issues(
        routes,
        availability,
        any_real_backend_available=bool(real_available),
        deterministic_allowed=deterministic_allowed,
    )
    blockers.extend(route_blockers)
    warnings.extend(route_warnings)

    if catalog["source"] == "empty":
        warnings.append(
            {
                "code": "openrouter_catalog_empty",
                "message": "OpenRouter catalog cache is empty; sync the catalog to validate tier models.",
            }
        )
    elif catalog["stale"]:
        warnings.append(
            {
                "code": "openrouter_catalog_stale",
                "message": "OpenRouter catalog cache is stale.",
            }
        )

    if not learnings["json_exists"] or not learnings["md_exists"]:
        warnings.append(
            {
                "code": "learnings_playbook_missing",
                "message": "Curated learnings playbook is not fully available.",
                "dir": learnings["dir"],
            }
        )

    missing_tier_models = [
        row
        for row in catalog["tier_validation"]
        if row.get("exists") is False and catalog["count"] > 0
    ]
    for row in missing_tier_models:
        warnings.append(
            {
                "code": "tier_model_missing",
                "tier": row.get("tier"),
                "model": row.get("model"),
                "fallback": row.get("fallback"),
                "message": "Configured OpenRouter tier model is missing from the catalog.",
            }
        )

    if blockers:
        status = "not_ready"
    elif warnings:
        status = "degraded"
    else:
        status = "ready"

    return {
        "status": status,
        "real_project_ready": not blockers,
        "fallback_policy": {
            "deterministic": "allowed_for_real_projects"
            if deterministic_allowed
            else "blocked_for_real_projects",
            "tests_offline_development": "allowed",
        },
        "configured": {
            "backend": configured_backend,
            "model": getattr(settings, "llm_model", None),
        },
        "policy": policy,
        "availability": availability,
        "real_available_backends": real_available,
        "routes": routes,
        "tiers": tiers,
        "catalog": catalog,
        "learnings": learnings,
        "openrouter": openrouter_status,
        "blockers": blockers,
        "warnings": warnings,
        "handoff": {
            "scope": [
                "original_user_request",
                "stage_prompts",
                "generation_prompts",
                "repair_prompts",
                "review_prompts",
                "github_rag_context",
            ],
            "required_path": "LLMClient.complete",
        },
    }
