"""Regression test for the architecture-drift role-routing bug.

Bug: ``_check_architecture_drift`` derived the package.json target role
from ``arch_path`` (``<artifact_dir>/architecture.md``), which never
contains the substring "server" — so the role was ALWAYS "frontend".
Backend/db deps (fastify, pg, prisma, drizzle, node-cron…) were therefore
routed to the ROOT package.json. The re-check passed (deps are unioned
across all package.json files) but ``server/`` still couldn't boot — a
false-pass on full-stack apps.

Fix: each ARCHITECTURE_TECH_MAP entry now carries an owning role, and the
drift check routes the missing dep to ``_package_json_for_role(role)``.
"""

from __future__ import annotations

from pathlib import Path

from skyn3t.agents.contract_engine import check_contract


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_architecture_drift_routes_backend_dep_to_server_package(
    tmp_path: Path,
) -> None:
    """A missing backend dep (fastify) must target server/package.json.

    Before the fix this finding targeted the ROOT package.json because the
    role was always "frontend"; adding the dep there left server/ unable
    to boot while the re-check falsely passed.
    """
    artifact = tmp_path / "artifacts"
    scaffold = tmp_path / "scaffold"

    # Architecture promises a Fastify backend.
    _write(artifact / "architecture.md", "The API is built on Fastify.")

    # Full-stack layout: a root manifest (frontend) and a server manifest
    # (backend) that does NOT yet declare fastify.
    _write(
        scaffold / "package.json",
        '{"name": "app", "dependencies": {"react": "^18.0.0"}}',
    )
    _write(
        scaffold / "server" / "package.json",
        '{"name": "server", "dependencies": {"express": "^4.18.0"}}',
    )

    report = check_contract(scaffold, brief="", artifact_dir=artifact)

    drift = [f for f in report.findings if f.category == "architecture_drift"]
    fastify = [f for f in drift if "fastify" in f.message.lower()]
    assert fastify, "expected an architecture_drift blocker for the missing fastify dep"

    # The core regression assertion: the missing backend dep must be routed
    # to server/package.json, not the root manifest.
    assert fastify[0].file == "server/package.json", (
        f"backend dep routed to {fastify[0].file!r}; "
        "should target server/package.json"
    )
    assert fastify[0].fix_hint.get("role") == "backend"


def test_architecture_drift_routes_frontend_dep_to_root_package(
    tmp_path: Path,
) -> None:
    """A missing frontend dep (Next.js) still targets the root package.json."""
    artifact = tmp_path / "artifacts"
    scaffold = tmp_path / "scaffold"

    _write(artifact / "architecture.md", "Built with Next.js using the App Router.")
    _write(
        scaffold / "package.json",
        '{"name": "app", "dependencies": {"react": "^18.0.0"}}',
    )
    _write(
        scaffold / "server" / "package.json",
        '{"name": "server", "dependencies": {"express": "^4.18.0"}}',
    )

    report = check_contract(scaffold, brief="", artifact_dir=artifact)

    drift = [f for f in report.findings if f.category == "architecture_drift"]
    nextjs = [f for f in drift if "next" in f.message.lower()]
    assert nextjs, "expected an architecture_drift blocker for the missing next dep"
    assert nextjs[0].file == "package.json", (
        f"frontend dep routed to {nextjs[0].file!r}; should target root package.json"
    )
    assert nextjs[0].fix_hint.get("role") == "frontend"
