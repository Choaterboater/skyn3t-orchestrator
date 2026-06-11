"""Post-build scaffold proof runs — Python equivalent of ``scripts/studio_smoke.sh``.

Used by the autonomous loop to fail-closed when a completed project scaffold
does not compile or ``npm run build`` after the Studio pipeline finishes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("skyn3t.studio.proof_run")


def resolve_scaffold_dir(slug: str, *, projects_dir: Optional[Path] = None) -> Path:
    """Return ``<projects_dir>/<slug>/scaffold``."""
    from skyn3t.config.settings import get_settings

    base = Path(projects_dir) if projects_dir else get_settings().projects_dir
    return (base / slug / "scaffold").expanduser().resolve()


async def run_scaffold_proof(
    scaffold_dir: Path,
    *,
    execution_profile: str = "balanced",
    strict: bool = True,
) -> Dict[str, Any]:
    """Run npm/py_compile proof checks on a scaffold directory.

    Returns a dict with ``ok``, ``verdict``, ``stack``, ``summary``, and
    captured stdout/stderr tails. When ``strict`` is True (default for
    autonomous builds), only ``verdict == "yes"`` counts as success.
    """
    from skyn3t.agents.build_verifier import BuildVerifierAgent
    from skyn3t.core.agent import TaskRequest

    scaffold_dir = Path(scaffold_dir).expanduser().resolve()
    if not scaffold_dir.is_dir():
        return {
            "ok": False,
            "verdict": "no",
            "stack": "unknown",
            "summary": f"scaffold not found: {scaffold_dir}",
            "scaffold_dir": str(scaffold_dir),
            "stdout": "",
            "stderr": "",
            "command": None,
        }

    agent = BuildVerifierAgent(name="proof_run")
    await agent.initialize()
    result = await agent.execute(
        TaskRequest(
            task_id="proof-run",
            description="post-build proof run",
            input_data={
                "scaffold_dir": str(scaffold_dir),
                "execution_profile": execution_profile,
            },
        )
    )
    output = dict(result.output or {})
    verdict = str(output.get("verdict") or "no")
    if strict:
        ok = verdict == "yes"
    else:
        ok = verdict in {"yes", "skipped"}
    return {
        "ok": ok,
        "verdict": verdict,
        "stack": output.get("stack") or "unknown",
        "summary": output.get("summary") or result.error or "proof run finished",
        "scaffold_dir": str(scaffold_dir),
        "stdout": str(output.get("stdout") or "")[-2000:],
        "stderr": str(output.get("stderr") or "")[-2000:],
        "command": output.get("command"),
        "failure_hint": output.get("failure_hint"),
    }


async def run_proof_for_slug(
    slug: str,
    *,
    execution_profile: str = "balanced",
    strict: bool = True,
    projects_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Proof-run a Studio project by slug."""
    scaffold = resolve_scaffold_dir(slug, projects_dir=projects_dir)
    proof = await run_scaffold_proof(
        scaffold,
        execution_profile=execution_profile,
        strict=strict,
    )
    proof["slug"] = slug
    return proof
