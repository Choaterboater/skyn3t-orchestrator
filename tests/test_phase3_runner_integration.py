"""Phase-3 runner-integration tests (owner: runner_integration).

These pin the runner-side wiring of the Phase-3 leaf contracts WITHOUT running
the orchestrator or touching data/. Everything here exercises pure/synchronous
helpers in StudioRunner against tmp dirs only:

  * _derive_package_targets  — brief → packaging targets (change #6)
  * _consume_code_stage_stub_signal — sidecar + manifest signal (change #2)
  * _maybe_auto_answer_clarification — auto-answer vs confirm_first (change #1)
  * _surface_verifier_subgates — visual/test/functional promotion (change #3)
  * _consistency_fix_action / _unresolved_todo_stub_files — stub_entrypoint
    wired into the existing fix loop (change #2 / #4)
  * _write_failed_md / _write_completeness_scoreboard — non-destructive
    terminal disposition (change #5)

All new gates degrade safely: missing tools/fields → neutral defaults, never
a crash. The tests assert that explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from skyn3t.studio.runner import StudioRunner


class _FakeBus:
    def publish(self, *args, **kwargs):  # pragma: no cover - inert
        return None


def _make_runner(tmp_path: Path) -> StudioRunner:
    return StudioRunner(event_bus=_FakeBus(), projects_root=tmp_path / "projects")


# ── change #6: _derive_package_targets ─────────────────────────────────────
def test_derive_package_targets_spa_gets_pwa():
    targets = StudioRunner._derive_package_targets(
        "Build a single-page habit tracker web app with offline support."
    )
    assert "pwa" in targets
    assert "capacitor" not in targets
    assert "desktop" not in targets


def test_derive_package_targets_mobile_gets_capacitor_not_pwa():
    targets = StudioRunner._derive_package_targets(
        "Make a native iOS app for tracking workouts (also Android)."
    )
    assert "capacitor" in targets
    # FULL-NATIVE owner decision: mobile → capacitor, not a PWA shim.
    assert "pwa" not in targets


def test_derive_package_targets_desktop_brief():
    targets = StudioRunner._derive_package_targets(
        "Build a macOS desktop app with a menu bar icon."
    )
    assert "desktop" in targets


def test_derive_package_targets_server_gets_docker():
    targets = StudioRunner._derive_package_targets(
        "A fullstack app with an Express backend and Postgres database."
    )
    assert "docker" in targets


def test_derive_package_targets_docker_from_decisions_family():
    targets = StudioRunner._derive_package_targets(
        "Some web app",
        decisions={"family": "fullstack"},
    )
    assert "docker" in targets


def test_derive_package_targets_empty_brief_is_empty():
    # Uncertainty → [] (legacy "no extra targets" behavior).
    assert StudioRunner._derive_package_targets("") == []
    assert StudioRunner._derive_package_targets("   ") == []


def test_derive_package_targets_plain_docs_brief_is_empty():
    # No web/mobile/desktop/server signal → nothing.
    assert StudioRunner._derive_package_targets("Write a marketing strategy plan.") == []


# ── change #2: _consume_code_stage_stub_signal ─────────────────────────────
def test_consume_stub_signal_writes_sidecar_and_manifest(tmp_path):
    runner = _make_runner(tmp_path)
    artifact_dir = runner.projects_root / "demo"
    scaffold = artifact_dir / "scaffold"
    scaffold.mkdir(parents=True, exist_ok=True)
    manifest = {"slug": "demo"}
    output = {
        "planned_imports": ["src/components/Foo.jsx", "src/components/Bar.jsx"],
        "stub_markers": [{"file": "src/App.jsx", "kind": "entrypoint-stub", "marker": "TODO"}],
        "entrypoint_files": ["src/App.jsx"],
        "entrypoint_is_stub": False,
    }
    runner._consume_code_stage_stub_signal(
        manifest=manifest, artifact_dir=artifact_dir, output=output
    )
    sidecar = scaffold / ".skyn3t_planned_imports.json"
    assert sidecar.is_file()
    data = json.loads(sidecar.read_text())
    assert data["planned_imports"] == [
        "src/components/Foo.jsx",
        "src/components/Bar.jsx",
    ]
    sig = manifest["code_stub_signal"]
    assert sig["planned_imports_count"] == 2
    assert sig["entrypoint_is_stub"] is False
    assert manifest.get("_entrypoint_stub_gate") is None


def test_consume_stub_signal_sets_gate_on_entrypoint_stub(tmp_path):
    runner = _make_runner(tmp_path)
    artifact_dir = runner.projects_root / "demo2"
    (artifact_dir / "scaffold").mkdir(parents=True, exist_ok=True)
    manifest = {"slug": "demo2"}
    output = {
        "planned_imports": [],
        "stub_markers": [{"file": "src/App.jsx", "kind": "entrypoint-stub", "marker": "x"}],
        "entrypoint_files": ["src/App.jsx"],
        "entrypoint_is_stub": True,
    }
    runner._consume_code_stage_stub_signal(
        manifest=manifest, artifact_dir=artifact_dir, output=output
    )
    assert manifest["_entrypoint_stub_gate"] is True
    assert manifest["code_stub_signal"]["entrypoint_is_stub"] is True


def test_consume_stub_signal_respects_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_STUB_HARD_GATE", "0")
    runner = _make_runner(tmp_path)
    artifact_dir = runner.projects_root / "demo3"
    (artifact_dir / "scaffold").mkdir(parents=True, exist_ok=True)
    manifest = {"slug": "demo3"}
    output = {"entrypoint_is_stub": True, "entrypoint_files": ["src/App.jsx"]}
    runner._consume_code_stage_stub_signal(
        manifest=manifest, artifact_dir=artifact_dir, output=output
    )
    # Flag off → gate not raised, but signal still surfaced (degrade-safe).
    assert manifest.get("_entrypoint_stub_gate") is None
    assert manifest["code_stub_signal"]["entrypoint_is_stub"] is True


def test_consume_stub_signal_handles_none_output(tmp_path):
    runner = _make_runner(tmp_path)
    artifact_dir = runner.projects_root / "demo4"
    (artifact_dir / "scaffold").mkdir(parents=True, exist_ok=True)
    manifest = {"slug": "demo4"}
    # Must not raise on absent/None output (degrade contract).
    runner._consume_code_stage_stub_signal(
        manifest=manifest, artifact_dir=artifact_dir, output=None
    )
    assert "code_stub_signal" not in manifest


# ── change #1: _maybe_auto_answer_clarification ────────────────────────────
def _clarify_output():
    return {
        "needs_clarification": True,
        "questions": ["What is the primary outcome?"],
        "question_options": [
            {
                "id": "outcome",
                "question": "What is the primary outcome?",
                "options": [
                    {"id": "working_app", "label": "A working app"},
                    {"id": "plan", "label": "A plan"},
                ],
            }
        ],
    }


def test_auto_answer_balanced_continues(tmp_path):
    runner = _make_runner(tmp_path)
    artifact_dir = runner.projects_root / "ac1"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "slug": "ac1",
        "mission_setup": {"autonomy": "balanced"},
        "brief": "Build a todo app",
        "history": [],
    }
    stage = SimpleNamespace(name="brainstorm", agent="BrainstormAgent")
    answered = runner._maybe_auto_answer_clarification(
        manifest=manifest,
        artifact_dir=artifact_dir,
        output=_clarify_output(),
        stage=stage,
        slug="ac1",
    )
    assert answered is True
    assert manifest.get("clarification") is None
    assert manifest.get("_auto_clarify_brief_block") is not None
    assert manifest.get("clarification_history")
    assert manifest["clarification_history"][-1]["auto_answered"] is True


def test_auto_answer_confirm_first_keeps_halt(tmp_path):
    runner = _make_runner(tmp_path)
    artifact_dir = runner.projects_root / "ac2"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "slug": "ac2",
        "mission_setup": {"autonomy": "confirm_first"},
        "brief": "Build a todo app",
        "history": [],
    }
    stage = SimpleNamespace(name="brainstorm", agent="BrainstormAgent")
    answered = runner._maybe_auto_answer_clarification(
        manifest=manifest,
        artifact_dir=artifact_dir,
        output=_clarify_output(),
        stage=stage,
        slug="ac2",
    )
    assert answered is False


def test_auto_answer_flag_off_keeps_halt(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_AUTO_CLARIFY", "0")
    runner = _make_runner(tmp_path)
    artifact_dir = runner.projects_root / "ac3"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "slug": "ac3",
        "mission_setup": {"autonomy": "balanced"},
        "brief": "Build a todo app",
        "history": [],
    }
    stage = SimpleNamespace(name="brainstorm", agent="BrainstormAgent")
    assert (
        runner._maybe_auto_answer_clarification(
            manifest=manifest,
            artifact_dir=artifact_dir,
            output=_clarify_output(),
            stage=stage,
            slug="ac3",
        )
        is False
    )


# ── change #3: _surface_verifier_subgates ──────────────────────────────────
def test_surface_subgates_promotes_and_routes_hint():
    manifest = {}
    build_result = {
        "verdict": "no",
        "failure_hint": "Unstyled app: no distinct colors.",
        "visual_verification": {"ran": True, "verdict": "no", "score": 12},
        "test_run": {"ran": True, "verdict": "skipped"},
    }
    StudioRunner._surface_verifier_subgates(manifest, "build_verification", build_result)
    sub = manifest["verifier_subgates"]["build_verification"]
    assert sub["visual_verification"]["verdict"] == "no"
    assert sub["visual_verification"]["score"] == 12
    assert sub["test_run"]["verdict"] == "skipped"
    assert manifest["_retry_hint"] == "Unstyled app: no distinct colors."


def test_surface_subgates_no_hint_when_verdict_yes():
    manifest = {}
    boot_result = {
        "verdict": "yes",
        "failure_hint": "ignored",
        "functional_smoke": {"ran": True, "verdict": "yes"},
    }
    StudioRunner._surface_verifier_subgates(manifest, "boot_verification", boot_result)
    assert "_retry_hint" not in manifest
    assert (
        manifest["verifier_subgates"]["boot_verification"]["functional_smoke"]["verdict"]
        == "yes"
    )


def test_surface_subgates_handles_non_dict():
    manifest = {}
    # Degrade: None result must not raise or mutate.
    StudioRunner._surface_verifier_subgates(manifest, "build_verification", None)
    assert manifest == {}


# ── change #2/#4: stub_entrypoint wired into the fix loop ───────────────────
def test_stub_entrypoint_is_regenerate_action():
    assert StudioRunner._consistency_fix_action("stub_entrypoint") == "regenerate"
    assert StudioRunner._consistency_fix_action("todo_stub") == "regenerate"
    assert StudioRunner._consistency_fix_action("orphan_export") == "create_placeholder"


def test_unresolved_includes_stub_entrypoint():
    issues = [
        SimpleNamespace(category="stub_entrypoint", severity="error", file="src/App.jsx"),
        SimpleNamespace(category="todo_stub", severity="error", file="src/Foo.jsx"),
        SimpleNamespace(category="orphan_export", severity="error", file="src/Bar.jsx"),
        SimpleNamespace(category="stub_entrypoint", severity="warning", file="src/Baz.jsx"),
    ]
    files = StudioRunner._unresolved_todo_stub_files(issues)
    assert "src/App.jsx" in files  # error stub_entrypoint blocks
    assert "src/Foo.jsx" in files  # error todo_stub blocks
    assert "src/Bar.jsx" not in files  # orphan_export is not a stub blocker
    assert "src/Baz.jsx" not in files  # warning severity does not block


# ── change #5: non-destructive terminal disposition ────────────────────────
def test_write_failed_md_creates_file(tmp_path):
    artifact_dir = tmp_path / "proj"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "slug": "proj",
        "status": "failed",
        "error": "Build verifier rejected the scaffold.",
        "next_action": "Retrying with the build failure as a hint.",
        "stack": "react_vite",
        "build_verification": {"verdict": "no", "summary": "vite build failed"},
    }
    StudioRunner._write_failed_md(artifact_dir, manifest)
    md = artifact_dir / "FAILED.md"
    assert md.is_file()
    text = md.read_text()
    assert "Build failed: proj" in text
    assert "vite build failed" in text


def test_write_failed_md_does_not_clobber_scaffold(tmp_path):
    artifact_dir = tmp_path / "proj2"
    scaffold = artifact_dir / "scaffold"
    scaffold.mkdir(parents=True, exist_ok=True)
    keep = scaffold / "src" / "App.jsx"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("export default function App(){}\n", encoding="utf-8")
    StudioRunner._write_failed_md(artifact_dir, {"slug": "proj2", "error": "x"})
    # Existing work untouched; only FAILED.md added.
    assert keep.read_text().startswith("export default")
    assert (artifact_dir / "FAILED.md").is_file()


def test_write_completeness_scoreboard_empty_scaffold(tmp_path):
    artifact_dir = tmp_path / "proj3"
    scaffold = artifact_dir / "scaffold"
    scaffold.mkdir(parents=True, exist_ok=True)
    StudioRunner._write_completeness_scoreboard(artifact_dir, scaffold, "a brief")
    sb_path = artifact_dir / "completeness_scoreboard.json"
    assert sb_path.is_file()
    sb = json.loads(sb_path.read_text())
    assert sb["slug"] == "proj3"
    assert set(
        ("stub_count", "orphan_count", "derived_route_count", "issues_by_category")
    ) <= set(sb.keys())


def test_write_completeness_scoreboard_counts_stub_entrypoint(tmp_path):
    artifact_dir = tmp_path / "proj4"
    scaffold = artifact_dir / "scaffold"
    src = scaffold / "src"
    src.mkdir(parents=True, exist_ok=True)
    # An entrypoint that is a generation-failure stub the consistency engine
    # flags as stub_entrypoint → must land in stub_count.
    (src / "App.jsx").write_text(
        "// TODO[skyn3t]: code generation failed\nexport default null\n",
        encoding="utf-8",
    )
    (scaffold / "package.json").write_text(
        json.dumps({"name": "proj4", "dependencies": {"react": "^18"}}), encoding="utf-8"
    )
    StudioRunner._write_completeness_scoreboard(artifact_dir, scaffold, "build an app")
    sb = json.loads((artifact_dir / "completeness_scoreboard.json").read_text())
    # The exact count depends on the consistency engine, but a stub entrypoint
    # must register at least one stub-class issue.
    assert sb["stub_count"] >= 1


def test_write_failure_disposition_scoreboard_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYN3T_COMPLETENESS_SCOREBOARD", "0")
    runner = _make_runner(tmp_path)
    artifact_dir = runner.projects_root / "proj5"
    scaffold = artifact_dir / "scaffold"
    scaffold.mkdir(parents=True, exist_ok=True)
    runner._write_failure_disposition(
        artifact_dir=artifact_dir,
        manifest={"slug": "proj5", "error": "boom"},
        scaffold_dir=scaffold,
        brief="x",
    )
    # FAILED.md always written; scoreboard gated off.
    assert (artifact_dir / "FAILED.md").is_file()
    assert not (artifact_dir / "completeness_scoreboard.json").exists()


# ── flag helpers default-on ────────────────────────────────────────────────
def test_flags_default_on(monkeypatch):
    for name in (
        "SKYN3T_STUB_HARD_GATE",
        "SKYN3T_AUTO_CLARIFY",
        "SKYN3T_COMPLETENESS_SCOREBOARD",
    ):
        monkeypatch.delenv(name, raising=False)
    assert StudioRunner._stub_hard_gate_enabled() is True
    assert StudioRunner._auto_clarify_enabled() is True
    assert StudioRunner._completeness_scoreboard_enabled() is True
