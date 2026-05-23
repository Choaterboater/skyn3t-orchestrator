from __future__ import annotations

from skyn3t.agents.architect import _normalize_stack_for_brief


def test_simple_react_brief_is_normalized_to_local_first() -> None:
    stack = {
        "frontend": "react-vite-tailwind",
        "backend": "express",
        "db": "better-sqlite3",
        "infra": "docker-compose",
        "ci": "github-actions",
    }

    normalized = _normalize_stack_for_brief("Build a habit tracker with streaks", stack)

    assert normalized["frontend"] == "react-vite-tailwind"
    assert normalized["backend"] == "none"
    assert normalized["db"] == "none"
    assert normalized["infra"] == "local-node"
    assert stack["backend"] == "express"


def test_backend_signals_keep_server_bundle() -> None:
    stack = {
        "frontend": "react-vite-tailwind",
        "backend": "express",
        "db": "better-sqlite3",
        "infra": "docker-compose",
        "ci": "github-actions",
    }

    normalized = _normalize_stack_for_brief(
        "Build a React dashboard with server-side CRUD, API keys, and health endpoints.",
        stack,
    )

    assert normalized == stack
