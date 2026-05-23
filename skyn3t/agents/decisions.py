"""Architect → downstream decisions contract.

The architect commits to a small, machine-readable set of choices in
``decisions.json`` so downstream agents (CodeAgent, PackagingAgent,
DesignerAgent, ConsistencyReviewerAgent) read one source of truth
instead of each re-deriving ports/language/framework from the scaffold.

Shape::

    {
      "frontend_bundle": str,      # e.g. "react-vite-tailwind"
      "backend_bundle": str,       # e.g. "express"
      "frontend_port": int | null,
      "backend_port": int | null,
      "framework": str,           # the architect's bundle backend, e.g. "express"
      "backend_language": str,    # "node" | "python" | "none"
      "family": str,              # "web" | "server" | "fullstack" | "unknown"
    }

Adding fields is safe — readers use ``dict.get`` and ignore unknowns.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

DECISIONS_FILENAME = "decisions.json"


# Backend bundle → port. Mirrors packaging_agent._DEFAULT_PORT_BY_STACK
# but keyed on the bundle name the architect actually picks, so the two
# sides stay aligned without one importing the other.
_BACKEND_PORT: Dict[str, Optional[int]] = {
    "express": 3000,
    "hono-node": 3000,
    "next": 3000,
    "none": None,
}

# Bundles where the frontend is its own dev server (vite default = 5173;
# next uses 3000 same as backend, which is intentional for monolithic
# next bundles).
_FRONTEND_PORT: Dict[str, Optional[int]] = {
    "react-vite": 5173,
    "react-vite-tailwind": 5173,
    "vue-vite": 5173,
    "vanilla-vite": 5173,
    "next": 3000,
    "none": None,
}

_BACKEND_LANGUAGE: Dict[str, str] = {
    "express": "node",
    "hono-node": "node",
    "next": "node",
    "none": "none",
}


def _derive_family(frontend: str, backend: str) -> str:
    if frontend != "none" and backend != "none":
        return "fullstack"
    if frontend != "none":
        return "web"
    if backend != "none":
        return "server"
    return "unknown"


def derive_decisions(stack: Dict[str, Any]) -> Dict[str, Any]:
    """Build the decisions dict from the architect's chosen stack bundle.

    Unknown bundle values fall through to ``None`` / ``"unknown"`` so the
    artifact still pins something — downstream agents can detect the
    missing decision and skip the anchor rather than crash.
    """
    backend = str(stack.get("backend", "")).strip() or "none"
    frontend = str(stack.get("frontend", "")).strip() or "none"
    return {
        "frontend_bundle": frontend,
        "backend_bundle": backend,
        "frontend_port": _FRONTEND_PORT.get(frontend),
        "backend_port": _BACKEND_PORT.get(backend),
        "framework": backend,
        "backend_language": _BACKEND_LANGUAGE.get(backend, "unknown"),
        "family": _derive_family(frontend, backend),
    }


def load_decisions(artifact_dir: Path | str) -> Optional[Dict[str, Any]]:
    """Read ``decisions.json`` from an artifact dir. Return None if absent."""
    path = Path(artifact_dir) / DECISIONS_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_decisions(artifact_dir: Path, stack: Dict[str, Any]) -> Path:
    """Write ``decisions.json`` derived from ``stack``. Returns the path."""
    path = Path(artifact_dir) / DECISIONS_FILENAME
    decisions = derive_decisions(stack)
    path.write_text(json.dumps(decisions, indent=2), encoding="utf-8")
    return path
