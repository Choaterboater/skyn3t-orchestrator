"""REAL-agent end-to-end gate for the Studio build loop.

Unlike ``test_studio_e2e.py`` (which stubs every agent with ``_FakeAgent`` and
asserts ``status == "done"``), this test drives ``StudioRunner`` with the REAL
agent registry and REAL model routing. That distinction matters: the fake test
stayed green for months while the live build loop shipped ~0% of real briefs,
because the tier table routed cheap/reasoning stages to PAID models that 403'd
on the exhausted OpenRouter key — the build died at the *research* stage, before
any code was written. A stubbed test cannot catch a routing/auth regression like
that. This one can: it exercises the same path a user's build takes.

It is DOUBLE-GATED and OPT-IN so it never runs (or flakes) the normal suite:
  * needs ``OPENROUTER_API_KEY`` to be set, AND
  * needs ``SKYN3T_RUN_REAL_E2E=1`` to be explicitly set by the operator.
Run it deliberately:
    SKYN3T_RUN_REAL_E2E=1 .venv/bin/python -m pytest \
        tests/test_studio_real_agent_e2e.py -q -s

The assertion is intentionally a *floor*, not "ships a perfect app": on the free
tier, models are rate-limited (429) and occasionally return unusable bodies, so
demanding ``status == "done"`` would be flaky. Instead we assert the build made
REAL forward progress — it got past planning/research and into code generation
(or produced scaffold files). That floor is exactly what the 403 regression
violated, so this gate would have caught it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

_HAVE_KEY = bool(os.environ.get("OPENROUTER_API_KEY"))
_OPTED_IN = os.environ.get("SKYN3T_RUN_REAL_E2E", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (_HAVE_KEY and _OPTED_IN),
        reason=(
            "real-agent e2e is opt-in: set OPENROUTER_API_KEY and "
            "SKYN3T_RUN_REAL_E2E=1 to run it"
        ),
    ),
]

# A small but genuinely full-stack brief — a frontend that talks to a backend,
# so the build must plan + generate across both halves (the case the owner
# chose to gate on). Kept modest so it has a chance of completing on the free
# tier within the timeout.
_BRIEF = (
    "Build a small full-stack task list. Backend: a FastAPI service with "
    "GET /api/tasks, POST /api/tasks {title}, and GET /health returning "
    "{'status': 'ok'}, using in-memory storage. Frontend: a React + Vite "
    "single-page app that lists tasks and has a form to add one, fetching from "
    "the backend. Include package.json, requirements.txt, and a README."
)

_TIMEOUT_S = float(os.environ.get("SKYN3T_REAL_E2E_TIMEOUT", "1200"))


@pytest.mark.asyncio
async def test_real_agent_build_reaches_code_generation(tmp_path, monkeypatch):
    """A real-agent build must get past research into code generation.

    This is the anti-theater gate: it uses the real registry + router, on the
    free-only routing the live system uses, and proves the loop is not dead.
    """
    # Force the same policy the live runtime uses: free-only OpenRouter, no
    # Claude. This is what makes the build survive the $0 key instead of 403-ing.
    monkeypatch.setenv("SKYN3T_FREE_ONLY", "1")
    monkeypatch.setenv("SKYN3T_NO_CLAUDE", "1")
    monkeypatch.setenv("SKYN3T_LLM_BACKEND", "openrouter")
    monkeypatch.setenv("SKYN3T_LLM_FORCE_CLAUDE_CLI", "0")

    from skyn3t.core.events import EventBus
    from skyn3t.studio.runner import StudioRunner

    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")

    manifest = await asyncio.wait_for(
        runner.start(
            template_key="auto",
            brief=_BRIEF,
            slug="real-e2e-fullstack",
            mission_setup={"autonomy": "move_fast"},
        ),
        timeout=_TIMEOUT_S,
    )

    stages = manifest.get("stages", []) or []
    stage_names = [str(s.get("name", "")).lower() for s in stages]
    done_stages = [s for s in stages if s.get("status") == "done"]

    # Did the build reach code generation? Either a code-producing stage was
    # planned/attempted, or files actually landed in the scaffold.
    reached_code = any("code" in n for n in stage_names)
    scaffold_dir = tmp_path / "projects" / "real-e2e-fullstack" / "scaffold"
    scaffold_files = (
        [p for p in scaffold_dir.rglob("*") if p.is_file()]
        if scaffold_dir.exists()
        else []
    )

    # The 403 regression signature: research stage failed and nothing came
    # after it. Assert we did NOT stall there.
    research = next((s for s in stages if str(s.get("name", "")).lower() == "research"), None)
    research_failed_early = (
        research is not None
        and research.get("status") == "failed"
        and len(done_stages) <= 1
    )

    # Diagnostic output (visible with -s) so a failure is actionable.
    print(f"\n[real-e2e] status={manifest.get('status')} stages={stage_names}")
    print(f"[real-e2e] done_stages={[s.get('name') for s in done_stages]}")
    print(f"[real-e2e] reached_code={reached_code} scaffold_files={len(scaffold_files)}")

    assert not research_failed_early, (
        "build died at the research stage with no further progress — this is the "
        "paid-tier-403 regression signature (routing sent a stage to a paid/"
        f"unavailable model). manifest status={manifest.get('status')}"
    )
    assert reached_code or scaffold_files, (
        "real-agent build never reached code generation and produced no scaffold "
        f"files. stages={stage_names}, status={manifest.get('status')}. The build "
        "loop is not turning briefs into code with real agents."
    )
