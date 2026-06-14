"""Regression test: the build-fix loop fires on a build verdict of 'no'.

DEFECT 4 in the build-grader audit was a *missing test*, not a source bug.
The mechanical build-fix loop is decoupled from the ReviewerAgent verdict so
that a build verifier returning ``verdict == "no"`` always gets at least one
``_apply_build_fix_round`` + re-verify pass — even when the reviewer already
marked the run no-go (the reviewer runs *before* the build verifier, so any
build break makes ``reviewer_failed`` True). Without this loop, broken
scaffolds (e.g. a named-vs-default import mismatch) shipped unrepaired,
scored low, and poisoned the negative-learnings miner.

This test pins that behavior: a reviewer no-go run whose build verifier
returns ``verdict == "no"`` must call ``_apply_build_fix_round`` exactly once
(the counting stub returns ``False`` so the loop breaks after the first
attempt).
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.core.agent import TaskRequest, TaskResult


@pytest.mark.asyncio
async def test_build_fix_loop_fires_on_build_no_verdict(
    event_bus, tmp_path, monkeypatch
):
    from skyn3t.studio.runner import StudioRunner

    class ReviewerStageAgent:
        async def initialize(self) -> None:
            return None

        async def execute(self, task: TaskRequest) -> TaskResult:
            artifact_dir = Path(task.input_data["artifact_dir"]).resolve()
            review_path = artifact_dir / "review.md"
            review_path.write_text("# Review\n", encoding="utf-8")
            scaffold_dir = artifact_dir / "scaffold"
            scaffold_dir.mkdir(parents=True, exist_ok=True)
            (scaffold_dir / "package.json").write_text(
                '{"name":"demo"}\n', encoding="utf-8"
            )
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output={
                    "verdict": "no-go",
                    "score": 41,
                    "summary": "Reviewer found launch-blocking gaps.",
                    "files": [str(review_path)],
                },
            )

    class PassThroughStageAgent:
        async def initialize(self) -> None:
            return None

        async def execute(self, task, stdin_data=None):
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output={"verdict": "pass", "blocker_count": 0},
            )

    def fake_get_agent(name, *args, **kwargs):
        if name == "ReviewerAgent":
            return ReviewerStageAgent()
        if name in (
            "ConsistencyReviewerAgent",
            "ContractVerifierAgent",
            "PackagingAgent",
        ):
            return PassThroughStageAgent()
        raise AssertionError(f"unexpected agent {name}")

    monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
    monkeypatch.setattr(
        "skyn3t.studio.runner.get_template",
        lambda _key: SimpleNamespace(
            title="Quality template",
            description="Review a scaffold.",
            stages=[
                SimpleNamespace(
                    name="reviewer",
                    agent="ReviewerAgent",
                    capability="review",
                    handoff_to=None,
                    input_extra={},
                )
            ],
        ),
    )

    runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)

    fix_calls: list[int] = []

    async def fake_build(scaffold_dir, brief, *, execution_profile="balanced"):
        # The build verifier reports a failing build, which must drive the
        # mechanical build-fix loop.
        return {
            "verdict": "no",
            "stack": "node",
            "summary": "build broken",
            "command": "npm run build",
            "failure_hint": "named-vs-default import mismatch",
        }

    async def fake_boot(scaffold_dir, brief):
        # Boot/integration pass so their own fix loops never enter and the
        # shared _apply_build_fix_round counter stays scoped to the build path.
        return {
            "verdict": "yes",
            "kind": "node-express",
            "summary": "boot ok",
            "command": "node index.js",
        }

    async def fake_integration(scaffold_dir, brief):
        return {
            "verdict": "yes",
            "kind": "node-express",
            "summary": "integration ok",
            "command": "node index.js",
        }

    async def counting_build_fix(scaffold_dir, brief, result, attempt):
        fix_calls.append(attempt)
        # Returning False means "no fix applied" → the loop breaks after this
        # single call without re-verifying.
        return False

    async def fake_retry(manifest, brief, slug):
        return None

    monkeypatch.setenv("SKYN3T_AUTO_RETRY", "0")
    monkeypatch.setattr(runner, "_run_build_verifier", fake_build)
    monkeypatch.setattr(runner, "_run_boot_verifier", fake_boot)
    monkeypatch.setattr(runner, "_run_integration_verifier", fake_integration)
    monkeypatch.setattr(runner, "_maybe_auto_retry", fake_retry)
    monkeypatch.setattr(runner, "_apply_build_fix_round", counting_build_fix)

    manifest = await runner.start("demo", "Build a dashboard", slug="build-fix-fires")

    assert manifest["build_verification"]["verdict"] == "no"
    assert fix_calls == [1]
