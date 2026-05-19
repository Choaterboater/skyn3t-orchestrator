"""Tests for the human approval gate (approval_gate.py + runner wiring)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.core.agent import TaskRequest, TaskResult
from skyn3t.studio import approval_gate


def _reset_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        approval_gate, "_CONFIG_PATH", tmp_path / "approval_gates.json"
    )
    monkeypatch.setattr(
        approval_gate, "_SKILL_PATH", tmp_path / "approval_skill.json"
    )


def test_load_gate_config_creates_default(tmp_path, monkeypatch):
    _reset_paths(tmp_path, monkeypatch)
    cfg = approval_gate.load_gate_config()
    assert cfg["gates"] == ["ArchitectAgent"]
    assert cfg["disabled"] is False
    assert cfg["graduate_after"] == 5
    assert (tmp_path / "approval_gates.json").exists()


def test_should_gate_true_when_agent_listed(tmp_path, monkeypatch):
    _reset_paths(tmp_path, monkeypatch)
    approval_gate.save_gate_config(
        {"gates": ["ArchitectAgent"], "disabled": False, "graduate_after": 5}
    )
    assert approval_gate.should_gate("ArchitectAgent", "Build a dashboard") is True


def test_should_gate_false_when_disabled(tmp_path, monkeypatch):
    _reset_paths(tmp_path, monkeypatch)
    approval_gate.save_gate_config(
        {"gates": ["ArchitectAgent"], "disabled": True, "graduate_after": 5}
    )
    assert approval_gate.should_gate("ArchitectAgent", "Build a dashboard") is False


def test_should_gate_false_when_agent_not_listed(tmp_path, monkeypatch):
    _reset_paths(tmp_path, monkeypatch)
    approval_gate.save_gate_config(
        {"gates": ["DesignerAgent"], "disabled": False, "graduate_after": 5}
    )
    assert approval_gate.should_gate("ArchitectAgent", "Anything") is False


def test_brief_shape_normalizes_whitespace_and_case(tmp_path, monkeypatch):
    _reset_paths(tmp_path, monkeypatch)
    a = approval_gate.brief_shape("Build A Dashboard")
    b = approval_gate.brief_shape("  build a dashboard\n")
    c = approval_gate.brief_shape("BUILD-A-DASHBOARD!")
    assert a == b == c


def test_record_decision_increments_clean_approves(tmp_path, monkeypatch):
    _reset_paths(tmp_path, monkeypatch)
    brief = "Build a homelab dashboard"
    for _ in range(3):
        approval_gate.record_decision(brief, "ArchitectAgent", "approve", edited=False)
    skill = json.loads((tmp_path / "approval_skill.json").read_text())
    entry = next(iter(skill.values()))["ArchitectAgent"]
    assert entry["approved_unchanged"] == 3


def test_record_decision_resets_on_edit(tmp_path, monkeypatch):
    _reset_paths(tmp_path, monkeypatch)
    brief = "Build a homelab dashboard"
    approval_gate.record_decision(brief, "ArchitectAgent", "approve", edited=False)
    approval_gate.record_decision(brief, "ArchitectAgent", "approve", edited=False)
    approval_gate.record_decision(brief, "ArchitectAgent", "approve", edited=True)
    skill = json.loads((tmp_path / "approval_skill.json").read_text())
    entry = next(iter(skill.values()))["ArchitectAgent"]
    assert entry["approved_unchanged"] == 0


def test_record_decision_resets_on_reject(tmp_path, monkeypatch):
    _reset_paths(tmp_path, monkeypatch)
    brief = "Build a homelab dashboard"
    approval_gate.record_decision(brief, "ArchitectAgent", "approve", edited=False)
    approval_gate.record_decision(brief, "ArchitectAgent", "reject", edited=False)
    skill = json.loads((tmp_path / "approval_skill.json").read_text())
    entry = next(iter(skill.values()))["ArchitectAgent"]
    assert entry["approved_unchanged"] == 0


def test_should_gate_false_after_graduation(tmp_path, monkeypatch):
    _reset_paths(tmp_path, monkeypatch)
    approval_gate.save_gate_config(
        {"gates": ["ArchitectAgent"], "disabled": False, "graduate_after": 5}
    )
    brief = "Build a homelab dashboard"
    for _ in range(5):
        approval_gate.record_decision(brief, "ArchitectAgent", "approve", edited=False)
    assert approval_gate.should_gate("ArchitectAgent", brief) is False
    # Different brief shape should still gate.
    assert (
        approval_gate.should_gate("ArchitectAgent", "Completely different brief")
        is True
    )


@pytest.mark.asyncio
async def test_normalize_manifest_handles_pre_feature_projects(
    tmp_path, monkeypatch, event_bus
):
    from skyn3t.studio.runner import StudioRunner

    runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
    # A legacy manifest dict without the new keys
    legacy = {"slug": "legacy", "status": "done", "template": "demo"}
    normalized = runner._normalize_manifest(legacy)
    assert normalized["approval_history"] == []
    assert normalized["awaiting_approval_for"] is None


class _StubAgent:
    """Tracks how many times execute() is called and writes a fake output file."""

    def __init__(
        self,
        content: str = "# Original architecture\n",
        filename: str = "architecture.md",
    ):
        self.calls = 0
        self.content = content
        self.filename = filename

    async def initialize(self):
        return None

    async def execute(self, task: TaskRequest) -> TaskResult:
        self.calls += 1
        artifact_dir = Path(task.input_data["artifact_dir"])
        (artifact_dir / self.filename).write_text(self.content, encoding="utf-8")
        return TaskResult(task_id=task.task_id, success=True, output={"files": [self.filename]})


def _two_stage_template():
    return SimpleNamespace(
        title="Approval gate fixture",
        description="Architect then designer",
        stages=[
            SimpleNamespace(
                name="architect",
                agent="ArchitectAgent",
                capability="architecture",
                handoff_to="DesignerAgent",
                input_extra={},
            ),
            SimpleNamespace(
                name="designer",
                agent="DesignerAgent",
                capability="design",
                handoff_to=None,
                input_extra={},
            ),
        ],
    )


@pytest.mark.asyncio
async def test_full_gate_lifecycle_in_runner(tmp_path, monkeypatch, event_bus):
    _reset_paths(tmp_path / "gate_data", monkeypatch)
    approval_gate.save_gate_config(
        {"gates": ["ArchitectAgent"], "disabled": False, "graduate_after": 5}
    )

    from skyn3t.studio.runner import StudioRunner

    architect = _StubAgent("# Original\n")
    designer = _StubAgent("# Designer ran\n", filename="design.md")

    def fake_get_agent(name, *args, **kwargs):
        if name == "ArchitectAgent":
            return architect
        if name == "DesignerAgent":
            return designer
        raise AssertionError(f"unexpected agent: {name}")

    monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
    template = _two_stage_template()
    monkeypatch.setattr("skyn3t.studio.runner.get_template", lambda _key: template)

    runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
    # Explicitly pass autonomy="balanced" — the default mission_setup
    # is move_fast which now bypasses gates by design (see
    # `test_move_fast_runs_through_without_pausing_for_approval`).
    manifest = await runner.start(
        "demo",
        "Build a homelab dashboard",
        slug="gate-1",
        mission_setup={"autonomy": "balanced"},
    )

    assert manifest["status"] == "awaiting_approval"
    assert manifest["awaiting_approval_for"]["agent"] == "ArchitectAgent"
    assert architect.calls == 1
    assert designer.calls == 0  # designer must not run yet

    resumed = await runner.resume_after_approval("gate-1", "approve")
    assert resumed["status"] in {"running", "done", "needs_fixes"}
    # After resume, designer should have run exactly once.
    assert designer.calls == 1
    # Architect should NOT have re-run.
    assert architect.calls == 1


@pytest.mark.asyncio
async def test_resume_with_edits_overwrites_architecture_md(
    tmp_path, monkeypatch, event_bus
):
    _reset_paths(tmp_path / "gate_data", monkeypatch)
    approval_gate.save_gate_config(
        {"gates": ["ArchitectAgent"], "disabled": False, "graduate_after": 5}
    )

    from skyn3t.studio.runner import StudioRunner

    architect = _StubAgent("# Original architecture\n")
    designer = _StubAgent("# Designer\n", filename="design.md")

    def fake_get_agent(name, *args, **kwargs):
        return architect if name == "ArchitectAgent" else designer

    monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
    template = _two_stage_template()
    monkeypatch.setattr("skyn3t.studio.runner.get_template", lambda _key: template)

    runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
    # See note in test_full_gate_lifecycle_in_runner — gates only
    # fire when autonomy isn't move_fast.
    await runner.start(
        "demo",
        "Build a homelab dashboard",
        slug="gate-edits",
        mission_setup={"autonomy": "balanced"},
    )

    edited_md = "# Edited architecture — auth added\n"
    await runner.resume_after_approval(
        "gate-edits", "approve", edited_md=edited_md
    )

    on_disk = (tmp_path / "gate-edits" / "architecture.md").read_text()
    assert on_disk == edited_md

    # Edit should reset the graduation counter to 0.
    skill = json.loads((tmp_path / "gate_data" / "approval_skill.json").read_text())
    entry = next(iter(skill.values()))["ArchitectAgent"]
    assert entry["approved_unchanged"] == 0


@pytest.mark.asyncio
async def test_resume_after_rejection_reruns_architect_with_feedback(
    tmp_path, monkeypatch, event_bus
):
    _reset_paths(tmp_path / "gate_data", monkeypatch)
    approval_gate.save_gate_config(
        {"gates": ["ArchitectAgent"], "disabled": False, "graduate_after": 5}
    )

    from skyn3t.studio.runner import StudioRunner

    architect = _StubAgent("# Original\n")
    designer = _StubAgent("# Designer\n", filename="design.md")

    def fake_get_agent(name, *args, **kwargs):
        return architect if name == "ArchitectAgent" else designer

    monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
    template = _two_stage_template()
    monkeypatch.setattr("skyn3t.studio.runner.get_template", lambda _key: template)

    runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
    # See note in test_full_gate_lifecycle_in_runner — gates only
    # fire when autonomy isn't move_fast.
    await runner.start(
        "demo",
        "Build a homelab dashboard",
        slug="gate-reject",
        mission_setup={"autonomy": "balanced"},
    )

    assert architect.calls == 1
    await runner.resume_after_approval(
        "gate-reject", "reject", feedback="Architecture missed the auth requirement."
    )
    # Architect should have run again; designer should still have run zero
    # times (the second architect run gates again).
    assert architect.calls == 2
    assert designer.calls == 0

    manifest = json.loads(
        (tmp_path / "gate-reject" / "project.json").read_text()
    )
    assert "Architecture missed the auth requirement." in manifest["brief"]


@pytest.mark.asyncio
async def test_disabled_config_lets_pipeline_run_through(
    tmp_path, monkeypatch, event_bus
):
    _reset_paths(tmp_path / "gate_data", monkeypatch)
    approval_gate.save_gate_config(
        {"gates": ["ArchitectAgent"], "disabled": True, "graduate_after": 5}
    )

    from skyn3t.studio.runner import StudioRunner

    architect = _StubAgent()
    designer = _StubAgent()

    def fake_get_agent(name, *args, **kwargs):
        return architect if name == "ArchitectAgent" else designer

    monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
    template = _two_stage_template()
    monkeypatch.setattr("skyn3t.studio.runner.get_template", lambda _key: template)

    runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
    # Pass balanced explicitly so this test verifies the disabled-config
    # path specifically, not the autonomy-bypass path (which would
    # produce the same outcome but for a different reason).
    manifest = await runner.start(
        "demo",
        "Build a homelab dashboard",
        slug="gate-off",
        mission_setup={"autonomy": "balanced"},
    )

    assert manifest["status"] != "awaiting_approval"
    assert architect.calls == 1
    assert designer.calls == 1


# Mission-setup autonomy bypass — move_fast must skip approval gates.
# Per mission_setup.py move_fast is documented as "Do not pause for
# kickoff clarification questions… only stop if the work is truly
# blocked." Approval gates contradict that, so move_fast skips them.

def test_should_gate_false_under_move_fast_autonomy(tmp_path, monkeypatch):
    _reset_paths(tmp_path, monkeypatch)
    approval_gate.save_gate_config(
        {"gates": ["ArchitectAgent"], "disabled": False, "graduate_after": 5}
    )
    assert approval_gate.should_gate(
        "ArchitectAgent", "Build a dashboard", autonomy="move_fast"
    ) is False


def test_should_gate_true_under_balanced_autonomy(tmp_path, monkeypatch):
    _reset_paths(tmp_path, monkeypatch)
    approval_gate.save_gate_config(
        {"gates": ["ArchitectAgent"], "disabled": False, "graduate_after": 5}
    )
    # Non-move_fast autonomy (balanced, confirm_first, missing) all
    # respect the global gate config.
    assert approval_gate.should_gate(
        "ArchitectAgent", "Build a dashboard", autonomy="balanced"
    ) is True
    assert approval_gate.should_gate(
        "ArchitectAgent", "Build a dashboard", autonomy="confirm_first"
    ) is True
    assert approval_gate.should_gate(
        "ArchitectAgent", "Build a dashboard", autonomy=None
    ) is True


def test_should_gate_move_fast_is_case_insensitive(tmp_path, monkeypatch):
    _reset_paths(tmp_path, monkeypatch)
    approval_gate.save_gate_config(
        {"gates": ["ArchitectAgent"], "disabled": False, "graduate_after": 5}
    )
    # Whitespace + casing tolerance so a stray "Move_Fast" or
    # " move_fast " from the API still bypasses.
    assert approval_gate.should_gate(
        "ArchitectAgent", "Build a dashboard", autonomy=" Move_Fast "
    ) is False


@pytest.mark.asyncio
async def test_move_fast_runs_through_without_pausing_for_approval(
    tmp_path, monkeypatch, event_bus
):
    """End-to-end: a project started with mission_setup.autonomy=move_fast
    must NOT halt at PROJECT_AWAITING_APPROVAL after the architect, even
    when the global config has ArchitectAgent on the gates list and the
    brief hasn't graduated yet."""
    _reset_paths(tmp_path / "gate_data", monkeypatch)
    approval_gate.save_gate_config(
        {"gates": ["ArchitectAgent"], "disabled": False, "graduate_after": 5}
    )

    from skyn3t.studio.runner import StudioRunner

    architect = _StubAgent()
    designer = _StubAgent()

    def fake_get_agent(name, *args, **kwargs):
        return architect if name == "ArchitectAgent" else designer

    monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
    template = _two_stage_template()
    monkeypatch.setattr("skyn3t.studio.runner.get_template", lambda _key: template)

    runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
    manifest = await runner.start(
        "demo",
        "Build a homelab dashboard",
        slug="gate-movefast",
        mission_setup={"autonomy": "move_fast"},
    )

    assert manifest["status"] != "awaiting_approval"
    # Both stages must have run; the gate that normally stops the
    # pipeline after architect was bypassed.
    assert architect.calls == 1
    assert designer.calls == 1


@pytest.mark.asyncio
async def test_balanced_autonomy_still_pauses_for_approval(
    tmp_path, monkeypatch, event_bus
):
    """Regression guard for the other direction — move_fast bypass must
    NOT accidentally affect balanced/confirm_first runs."""
    _reset_paths(tmp_path / "gate_data", monkeypatch)
    approval_gate.save_gate_config(
        {"gates": ["ArchitectAgent"], "disabled": False, "graduate_after": 5}
    )

    from skyn3t.studio.runner import StudioRunner

    architect = _StubAgent()
    designer = _StubAgent()

    def fake_get_agent(name, *args, **kwargs):
        return architect if name == "ArchitectAgent" else designer

    monkeypatch.setattr("skyn3t.studio.runner.get_agent", fake_get_agent)
    template = _two_stage_template()
    monkeypatch.setattr("skyn3t.studio.runner.get_template", lambda _key: template)

    runner = StudioRunner(event_bus=event_bus, projects_root=tmp_path)
    manifest = await runner.start(
        "demo",
        "Build a homelab dashboard",
        slug="gate-balanced",
        mission_setup={"autonomy": "balanced"},
    )

    assert manifest["status"] == "awaiting_approval"
    assert architect.calls == 1
    assert designer.calls == 0
