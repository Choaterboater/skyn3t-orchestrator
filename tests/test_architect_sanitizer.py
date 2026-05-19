"""ArchitectAgent._sanitize_architecture_md — deterministic post-LLM cleanup.

Claude Opus ignores explicit "NEVER mention Python/FastAPI" prompt rules
when writing homelab-dashboard architectures. canary-117 through canary-122
all shipped architecture.md describing FastAPI + Postgres + Alembic despite
tech_stack.json saying Express + better-sqlite3. The sanitizer rewrites
the file in place so downstream agents and the reviewer LLM never see the
drift.
"""

from __future__ import annotations

from skyn3t.agents.architect import ArchitectAgent

NODE_STACK = {
    "frontend": "react-vite-tailwind",
    "backend": "express",
    "db": "better-sqlite3",
    "infra": "docker-compose",
    "ci": "github-actions",
}


def test_drops_fastapi_sentence() -> None:
    body = (
        "A React SPA talks to a FastAPI proxy that holds credentials. "
        "Health is exposed at /api/health."
    )
    out = ArchitectAgent._sanitize_architecture_md(body, NODE_STACK)
    assert "FastAPI" not in out
    # The unrelated sentence about health stays.
    assert "/api/health" in out


def test_drops_postgres_sentence() -> None:
    body = (
        "Configuration persists to PostgreSQL from first boot. "
        "The store survives restarts."
    )
    out = ArchitectAgent._sanitize_architecture_md(body, NODE_STACK)
    assert "PostgreSQL" not in out
    assert "Postgres" not in out
    # The survival sentence stays.
    assert "survives restarts" in out


def test_drops_alembic_sentence_entirely() -> None:
    body = (
        "## Migrations\n"
        "Schema changes are managed via Alembic with autogenerate. "
        "Migrations run on boot.\n"
    )
    out = ArchitectAgent._sanitize_architecture_md(body, NODE_STACK)
    assert "Alembic" not in out
    # The next sentence stays — only the Alembic one drops.
    assert "Migrations run on boot" in out


def test_drops_python_version_sentence() -> None:
    body = (
        "API: FastAPI on Python 3.11 with async handlers. "
        "Routes are versioned under /api/v1."
    )
    out = ArchitectAgent._sanitize_architecture_md(body, NODE_STACK)
    assert "Python" not in out
    assert "FastAPI" not in out
    # Unrelated sentence stays.
    assert "/api/v1" in out


def test_drops_franken_prose_components() -> None:
    """canary-123 regression: substitutions created 'ASGI app served by
    node', 'eslint + eslint', etc. The sentence-drop approach must NOT
    produce those — it drops the whole sentence instead.
    """
    body = (
        "ASGI app served by uvicorn behind nginx. "
        "Express is the actual runtime."
    )
    out = ArchitectAgent._sanitize_architecture_md(body, NODE_STACK)
    assert "ASGI" not in out.lower()
    assert "uvicorn" not in out
    # The clean sentence about Express stays.
    assert "Express is the actual runtime" in out


def test_passes_through_when_stack_is_not_node() -> None:
    """A Python stack (when CodeAgent finally has templates) should leave
    architecture.md alone — the sanitizer is Node-specific."""
    python_stack = {"backend": "fastapi", "db": "postgres"}
    body = "API: FastAPI on Python 3.11. Persistence via PostgreSQL + Alembic."
    out = ArchitectAgent._sanitize_architecture_md(body, python_stack)
    assert out == body


def test_handles_empty_body() -> None:
    assert ArchitectAgent._sanitize_architecture_md("", NODE_STACK) == ""


def test_handles_missing_stack() -> None:
    body = "FastAPI proxy with PostgreSQL"
    assert ArchitectAgent._sanitize_architecture_md(body, {}) == body
    assert ArchitectAgent._sanitize_architecture_md(body, None) == body  # type: ignore[arg-type]


def test_case_insensitive_match() -> None:
    body = "Backend: fastapi. DB: postgres. Migrations: alembic."
    out = ArchitectAgent._sanitize_architecture_md(body, NODE_STACK)
    assert "fastapi" not in out.lower()
    assert "postgres" not in out.lower()
    assert "alembic" not in out.lower()


def test_canary_122_real_world_case() -> None:
    """The exact pattern that scored 49/100 on carnary-122 — overview
    paragraph promises FastAPI proxy + PostgreSQL persistence + REST API.
    After sanitization the offending sentences should be gone."""
    body = (
        "## Overview\n\n"
        "A self-hosted homelab status dashboard. "
        "A React SPA talks exclusively to a FastAPI proxy that holds "
        "all integration credentials server-side. "
        "Configuration persists to PostgreSQL from first boot. "
        "Schema migrations are managed via Alembic.\n"
    )
    out = ArchitectAgent._sanitize_architecture_md(body, NODE_STACK)
    assert "FastAPI" not in out
    assert "PostgreSQL" not in out
    assert "Alembic" not in out
    # The opening intro stays.
    assert "self-hosted homelab status dashboard" in out


def test_drops_cloudflare_deploy_mention() -> None:
    body = (
        "## Deployment\n\n"
        "Hosted on Fly.io with Cloudflare in front for TLS and caching."
    )
    out = ArchitectAgent._sanitize_architecture_md(body, NODE_STACK)
    assert "Cloudflare" not in out
    assert "Fly.io" not in out


def test_collapses_excess_blank_lines_after_sentence_drops() -> None:
    body = "Paragraph 1.\n\nAlembic-only sentence.\n\nParagraph 2."
    out = ArchitectAgent._sanitize_architecture_md(body, NODE_STACK)
    # The Alembic sentence dropped, but we shouldn't have 3+ blank lines now.
    assert "\n\n\n" not in out


def test_drops_empty_sections() -> None:
    """canary-124 regression: sanitizer left ## APIs and ## Deployment
    headers with empty bodies after all their sentences were dropped.
    """
    body = (
        "## Overview\n\n"
        "A homelab dashboard.\n\n"
        "## APIs\n\n"
        "Routes implemented in FastAPI on Python 3.11.\n\n"
        "## Deployment\n\n"
        "Hosted on Fly.io with Cloudflare in front.\n"
    )
    out = ArchitectAgent._sanitize_architecture_md(body, NODE_STACK)
    assert "## APIs" not in out, "empty APIs section should be dropped"
    assert "## Deployment" not in out, "empty Deployment section should be dropped"
    # Overview survives because it has clean content.
    assert "## Overview" in out
    assert "A homelab dashboard" in out


def test_keeps_section_with_partial_body() -> None:
    body = (
        "## Components\n\n"
        "Web: React + Vite.\n"
        "API: FastAPI on Python 3.11.\n"
        "DB: better-sqlite3 stored in data/app.db.\n"
    )
    out = ArchitectAgent._sanitize_architecture_md(body, NODE_STACK)
    assert "## Components" in out
    assert "FastAPI" not in out
    assert "Python" not in out
    assert "Web: React" in out
    assert "DB: better-sqlite3" in out


def test_renumbers_list_after_drops() -> None:
    """canary-124 regression: risks were "1, 2, 5" because items 3 and 4
    mentioned Alembic/Postgres and got dropped, leaving gaps that the
    reviewer LLM flagged as "looks like content was cut and not
    renumbered." Renumber sequentially.
    """
    body = (
        "## Risks\n\n"
        "1. First risk.\n"
        "2. Second risk.\n"
        "3. Alembic migration race condition.\n"
        "4. PostgreSQL connection pool exhaustion.\n"
        "5. Fifth risk.\n"
    )
    out = ArchitectAgent._sanitize_architecture_md(body, NODE_STACK)
    assert "1. First risk" in out
    assert "2. Second risk" in out
    # The original item 5 should now be renumbered to 3.
    assert "3. Fifth risk" in out
    assert "5." not in out
