"""Slow end-to-end Studio test using stubbed agents.

This is not a unit test: it exercises the full StudioRunner pipeline
(start → planning → stage loop → manifest persistence) without calling
real LLMs. Mark it with `-m slow` so the fast suite stays fast.

NOTE: this is a PIPELINE-WIRING test, NOT a ship-rate gate. Because every
agent is stubbed to return success, it stays green even when the live build
loop ships ~0% of real briefs (e.g. when routing 403s on a paid model). For a
gate that catches real routing/build regressions, see
``test_studio_real_agent_e2e.py`` (opt-in, real agents, real routing).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from skyn3t.core.events import EventBus
from skyn3t.studio import runner as runner_module
from skyn3t.studio.runner import StudioRunner


class _FakeAgent:
    """Agent stand-in that returns a trivial successful result."""

    # Keep the runner's critique path from warning about a missing llm attr.
    llm = None

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def initialize(self) -> None:
        pass

    async def execute(self, task: Any) -> Any:
        return SimpleNamespace(
            task_id=getattr(task, "task_id", "fake"),
            success=True,
            output={
                "files": [],
                "summary": f"fake output for {getattr(task, 'title', 'task')}",
            },
        )


@pytest.fixture
def stub_registry(monkeypatch):
    """Replace every Studio agent with the fake agent."""
    monkeypatch.setattr(runner_module, "get_agent", lambda *a, **k: _FakeAgent())


@pytest.mark.slow
@pytest.mark.asyncio
async def test_studio_brand_kit_pipeline_completes(tmp_path, stub_registry):
    """Run the brand_kit template end-to-end with stubbed agents."""
    bus = EventBus()
    runner = StudioRunner(event_bus=bus, projects_root=tmp_path / "projects")

    # Longer than 14 words so sparse-brief expansion skips the LLM path, and
    # move_fast autonomy bypasses the human approval gates.
    manifest = await runner.start(
        template_key="brand_kit",
        brief=(
            "Create a complete brand and design kit for a fictional AI gardening "
            "assistant named Plantwise, including logo guidance, color palette, "
            "typography, voice and tone, plus a one-page brand cheat sheet."
        ),
        mission_setup={"autonomy": "move_fast"},
    )

    assert manifest["status"] == "done"
    assert manifest["template"] == "brand_kit"
    stage_names = [s["name"] for s in manifest.get("stages", [])]
    # The runner may inject verification/packaging stages around the template.
    for expected in ("brainstorm", "research", "designer", "writer", "reviewer"):
        assert expected in stage_names, f"missing stage {expected}"
    for stage in manifest.get("stages", []):
        assert stage.get("status") == "done"
