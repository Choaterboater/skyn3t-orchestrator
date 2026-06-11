import pytest

from skyn3t.agents.product_categories import expand_sparse_brief
from skyn3t.core.events import EventBus
from skyn3t.studio.runner import StudioRunner


def test_expand_sparse_brief_for_short_prompt_is_idempotent() -> None:
    base = "build a habit tracker"
    expanded = expand_sparse_brief(base)
    assert "## Auto-expanded product baseline" in expanded
    assert expanded.startswith(base)
    assert expand_sparse_brief(expanded) == expanded


def test_expand_sparse_brief_skips_long_prompt() -> None:
    long_brief = (
        "Build a multi-tenant SaaS app with RBAC, audit logs, billing integration, "
        "structured API contracts, and end-to-end reporting workflows."
    )
    assert expand_sparse_brief(long_brief) == long_brief


def test_execution_profile_inference_and_timeout_policy(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    assert runner._infer_execution_profile("build a todo app", None) == "balanced"
    assert (
        runner._infer_execution_profile("build a todo app", {"execution_profile": "fast"})
        == "fast"
    )
    assert (
        runner._infer_execution_profile(
            "Build a dashboard with Sonarr, Radarr, Plex integrations and API sync",
            None,
        )
        == "deep"
    )
    assert (
        runner._infer_execution_profile(
            "Build a web app with auth, settings, notifications, dashboard widgets, and account preferences",
            None,
        )
        == "balanced"
    )

    balanced = runner._stage_timeout_for("code", "balanced", None)
    fast = runner._stage_timeout_for("code", "fast", None)
    deep = runner._stage_timeout_for("code", "deep", None)
    assert fast < balanced < deep
    assert runner._stage_timeout_for("code", "fast", 123.0) == 123.0
    assert runner._critique_rounds_for("code", "Build a polished dashboard UI") == 4
    assert runner._critique_rounds_for("code", "Build a background worker service") == 3
    assert (
        runner._critique_rounds_for(
            "code",
            "Build a polished dashboard UI",
            execution_profile="fast",
        )
        == 3
    )
    assert (
        runner._critique_rounds_for(
            "code",
            "Build a background worker service",
            execution_profile="fast",
        )
        == 2
    )


def test_normalize_manifest_adds_benchmark_defaults(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    manifest = runner._normalize_manifest({"slug": "demo", "template": "auto"})
    benchmark = manifest.get("benchmark") or {}
    assert benchmark.get("total_duration_ms") == 0
    assert benchmark.get("stage_failures") == 0
    assert isinstance(benchmark.get("stage_durations_ms"), dict)


def test_consistency_fix_target_uses_server_entry_for_missing_mount(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    scaffold = tmp_path / "scaffold"
    server = scaffold / "server"
    server.mkdir(parents=True)
    (server / "index.js").write_text("const app = {};\n", encoding="utf-8")

    target = runner._consistency_fix_target(
        scaffold_dir=scaffold,
        issue_file="server/routes/config.js",
        category="missing_mount",
    )

    assert target == "server/index.js"


def test_consistency_fix_target_leaves_other_categories_unchanged(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    scaffold = tmp_path / "scaffold"
    target = runner._consistency_fix_target(
        scaffold_dir=scaffold,
        issue_file="src/App.jsx",
        category="broken_import",
    )
    assert target == "src/App.jsx"


def test_normalize_scaffold_issue_path_trims_section_labels(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    scaffold = tmp_path / "scaffold"
    src = scaffold / "src"
    src.mkdir(parents=True)
    (src / "App.jsx").write_text("export default function App() { return null; }\n", encoding="utf-8")

    resolved = runner._normalize_scaffold_issue_path(scaffold, "src/App.jsx Drawer")

    assert resolved == "src/App.jsx"


def test_normalize_scaffold_issue_path_resolves_extensionless_server_entry(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    scaffold = tmp_path / "scaffold"
    server = scaffold / "server"
    server.mkdir(parents=True)
    (server / "index.js").write_text("export default {};\n", encoding="utf-8")

    resolved = runner._normalize_scaffold_issue_path(scaffold, "server/index")

    assert resolved == "server/index.js"


def test_normalize_scaffold_issue_path_returns_none_for_unknown_path(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir(parents=True)

    resolved = runner._normalize_scaffold_issue_path(scaffold, "src/Missing.jsx Drawer")

    assert resolved is None


def test_consistency_fix_action_regenerates_todo_stubs(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")

    assert runner._consistency_fix_action("todo_stub") == "regenerate"
    assert runner._consistency_fix_action("broken_import") == "regenerate"
    assert runner._consistency_fix_action("missing_mount") == "regenerate"
    assert runner._consistency_fix_action("design_quality") == "create_placeholder"


def test_todo_stub_retry_hint_names_files(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")

    hint = runner._todo_stub_retry_hint(
        ["server/index.js", "src/App.jsx", "src/hooks/useConfig.js"]
    )

    assert "server/index.js" in hint
    assert "src/App.jsx" in hint
    assert "real implementations" in hint


def test_integration_fix_targets_prefers_matching_route_module(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    scaffold = tmp_path / "scaffold"
    routes = scaffold / "server" / "routes"
    routes.mkdir(parents=True)
    (routes / "config.js").write_text("export default {};\n", encoding="utf-8")
    (scaffold / "server" / "index.js").write_text("const app = {};\n", encoding="utf-8")

    targets = runner._integration_fix_targets(
        scaffold,
        {
            "issues": [
                {
                    "issue": "missing",
                    "frontend_path": "/api/config/:*/test",
                }
            ]
        },
    )

    assert targets == ["server/routes/config.js"]


def test_integration_fix_targets_falls_back_to_server_entry(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    scaffold = tmp_path / "scaffold"
    server = scaffold / "server"
    server.mkdir(parents=True)
    (server / "index.js").write_text("const app = {};\n", encoding="utf-8")

    targets = runner._integration_fix_targets(
        scaffold,
        {
            "issues": [
                {
                    "issue": "missing",
                    "frontend_path": "/api/unknown/:*/test",
                }
            ]
        },
    )

    assert targets == ["server/index.js"]


def test_code_stage_fast_retry_signals_detect_missing_files(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    artifact_dir = tmp_path / "project"
    scaffold_dir = artifact_dir / "scaffold"
    scaffold_dir.mkdir(parents=True)
    (scaffold_dir / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")

    missing, unresolved = runner._code_stage_fast_retry_signals(
        artifact_dir=artifact_dir,
        brief="Build a dashboard",
        output={
            "files": [str(scaffold_dir / "package.json")],
            "missing_files": ["src/App.jsx"],
        },
    )

    assert missing == ["src/App.jsx"]
    assert unresolved == []


def test_code_stage_fast_retry_signals_detect_todo_stubs(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    artifact_dir = tmp_path / "project"
    scaffold_dir = artifact_dir / "scaffold"
    (scaffold_dir / "src").mkdir(parents=True)
    (scaffold_dir / "src" / "App.jsx").write_text(
        "// TODO[skyn3t]: unfinished\nexport default function App() { return null; }\n",
        encoding="utf-8",
    )

    missing, unresolved = runner._code_stage_fast_retry_signals(
        artifact_dir=artifact_dir,
        brief="Build a dashboard",
        output={"files": [str(scaffold_dir / "src" / "App.jsx")]},
    )

    assert missing == []
    assert unresolved == ["src/App.jsx"]


@pytest.mark.asyncio
async def test_run_post_code_checks_bails_on_unresolved_stubs_before_targeted_fix(
    tmp_path, monkeypatch
) -> None:
    from skyn3t.studio.runner import StudioRunner, UnresolvedScaffoldStubError

    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    artifact_dir = tmp_path / "project"
    scaffold_dir = artifact_dir / "scaffold"
    (scaffold_dir / "src").mkdir(parents=True)
    (scaffold_dir / "src" / "App.jsx").write_text(
        "// TODO[skyn3t]: unfinished\nexport default function App() { return null; }\n",
        encoding="utf-8",
    )
    manifest = {"history": [], "consistency_check": {}, "benchmark": {}, "artifacts": []}

    fix_called = {"count": 0}

    async def stub_fix_does_not_clear(*args, **kwargs):
        fix_called["count"] += 1
        from skyn3t.agents.targeted_fix import FixResult

        return FixResult(
            ok=True,
            files_changed=[],
            files_created=[],
            errors=[],
            fix_label="stub-fix",
        )

    monkeypatch.setattr(
        "skyn3t.agents.targeted_fix.apply_targeted_fix",
        stub_fix_does_not_clear,
    )

    with pytest.raises(UnresolvedScaffoldStubError):
        await runner._run_post_code_checks(
            manifest=manifest,
            artifact_dir=artifact_dir,
            brief="Build a dashboard",
            stage_name="code",
            stage_output={"files": [str(scaffold_dir / "src" / "App.jsx")]},
        )

    assert fix_called["count"] == 1


@pytest.mark.asyncio
async def test_run_post_code_checks_bails_on_missing_files_before_targeted_fix(
    tmp_path, monkeypatch
) -> None:
    from skyn3t.studio.runner import MissingPlannedFilesError, StudioRunner

    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    artifact_dir = tmp_path / "project"
    scaffold_dir = artifact_dir / "scaffold"
    scaffold_dir.mkdir(parents=True)
    (scaffold_dir / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
    manifest = {"history": [], "consistency_check": {}, "benchmark": {}, "artifacts": []}

    async def should_not_run_targeted_fix(*args, **kwargs):
        raise AssertionError("targeted fix should be skipped for missing planned files")

    monkeypatch.setattr(
        "skyn3t.agents.targeted_fix.apply_targeted_fix",
        should_not_run_targeted_fix,
    )

    with pytest.raises(MissingPlannedFilesError):
        await runner._run_post_code_checks(
            manifest=manifest,
            artifact_dir=artifact_dir,
            brief="Build a dashboard",
            stage_name="code",
            stage_output={
                "files": [str(scaffold_dir / "package.json")],
                "missing_files": ["src/App.jsx"],
            },
        )


@pytest.mark.asyncio
async def test_apply_build_fix_round_times_out_whole_scaffold_llm(tmp_path, monkeypatch) -> None:
    from skyn3t.studio.runner import StudioRunner

    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    scaffold_dir = tmp_path / "scaffold"
    src_dir = scaffold_dir / "src"
    src_dir.mkdir(parents=True)
    (scaffold_dir / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
    original_body = "export default function App() { return null; }\n"
    (src_dir / "App.jsx").write_text(original_body, encoding="utf-8")

    class SlowLLM:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def complete(self, *args, **kwargs):
            assert kwargs["timeout"] == 0.05
            assert kwargs["_allow_backend_failover"] is False
            raise TimeoutError("timed out")

    monkeypatch.setattr("skyn3t.adapters.LLMClient", SlowLLM)
    monkeypatch.setattr("skyn3t.agents.targeted_fix._parse_build_errors", lambda *_args: [])
    monkeypatch.setattr("skyn3t.studio.runner._BUILD_FIX_LLM_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(runner, "_apply_heuristic_build_fixes", lambda *_args: False)

    result = await runner._apply_build_fix_round(
        scaffold_dir=scaffold_dir,
        brief="Build a dashboard",
        build_result={"stdout": "server exited before binding port", "stderr": "", "stack": "node"},
        attempt=1,
    )

    assert result is False
    assert (src_dir / "App.jsx").read_text(encoding="utf-8") == original_body


def test_critique_timeout_for_covers_rounds_x_inner_budget(tmp_path) -> None:
    """The outer critique-window timeout must be ≥ rounds × 180s + slack.

    Regression: on fast profile + architect the old formula returned
    180 * 0.6 = 108s — shorter than even ONE round's inner 180s budget.
    Result: CRITIQUE_FAILED fired on every build mid-round. The new
    formula anchors on `rounds × _CRITIQUE_ROUND_BUDGET_SECONDS +
    overhead` and uses the old base × profile-multiplier only as a
    FLOOR.
    """
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")

    # Fast + architect: 2 rounds × 180 + 20 = 380s. Was 108s.
    fast_architect = runner._critique_timeout_for(
        stage_name="architect",
        execution_profile="fast",
        brief="Build a habit tracker",
    )
    rounds_fast_architect = runner._critique_rounds_for(
        "architect", "Build a habit tracker", execution_profile="fast"
    )
    assert fast_architect >= rounds_fast_architect * 180.0 + 20.0
    # And meaningfully bigger than the old broken value.
    assert fast_architect > 108.0

    # Fast + code with visual signals: 3 rounds × 180 + 20 = 560s. Was 252s.
    fast_code_visual = runner._critique_timeout_for(
        stage_name="code",
        execution_profile="fast",
        brief="Build a polished dashboard UI",
    )
    rounds_fast_code = runner._critique_rounds_for(
        "code", "Build a polished dashboard UI", execution_profile="fast"
    )
    assert fast_code_visual >= rounds_fast_code * 180.0 + 20.0
    assert fast_code_visual > 252.0

    # Balanced + architect: 3 rounds × 180 + 20 = 560s. Was 180s.
    balanced_architect = runner._critique_timeout_for(
        stage_name="architect",
        execution_profile="balanced",
        brief="Build a habit tracker",
    )
    assert balanced_architect >= 3 * 180.0 + 20.0

    # Deep + code with visual: 4 rounds × 180 + 20 = 740s.
    deep_code_visual = runner._critique_timeout_for(
        stage_name="code",
        execution_profile="deep",
        brief="Build a polished dashboard UI",
    )
    assert deep_code_visual >= 4 * 180.0 + 20.0

    # Balanced/deep code stages get +60s floor over the base 420s.
    balanced_code = runner._critique_timeout_for(
        stage_name="code",
        execution_profile="balanced",
        brief="Build a dashboard UI",
    )
    assert balanced_code >= 480.0
    deep_code = runner._critique_timeout_for(
        stage_name="code",
        execution_profile="deep",
        brief="Build a dashboard UI",
    )
    assert deep_code >= (420.0 * 1.3) + 60.0


def test_critique_timeout_floor_respects_unknown_stages(tmp_path) -> None:
    """Unknown / generic stage names should still get a sane floor
    (the old fast-profile floor was max(60, base * 0.6))."""
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")

    generic = runner._critique_timeout_for(
        stage_name="unknown_stage",
        execution_profile="fast",
        brief="",
    )
    # Floor for unknown stages is 120 * 0.6 = 72; rounds-budget is 2 * 180 + 20 = 380.
    # Outer must be at least the rounds-budget.
    assert generic >= 380.0


@pytest.mark.asyncio
async def test_pre_code_design_gate_flags_missing_palette(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    artifact_dir = tmp_path / "project"
    artifact_dir.mkdir()
    (artifact_dir / "brand.md").write_text("# Brand\n", encoding="utf-8")
    manifest: dict = {"history": [], "benchmark": {}, "artifacts": []}

    gate = await runner._run_pre_code_design_gate(
        manifest=manifest,
        artifact_dir=artifact_dir,
        brief="Build a polished dashboard UI",
    )

    assert gate["blockers"]
    assert any(f["file"] == "palette.json" for f in gate["blockers"])


@pytest.mark.asyncio
async def test_pre_code_design_gate_passes_valid_artifacts(tmp_path) -> None:
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    artifact_dir = tmp_path / "project"
    artifact_dir.mkdir()
    (artifact_dir / "palette.json").write_text(
        '{"bg": "#0F0D0A", "primary": "#E05C1A", "accent": "#4A90A4"}\n',
        encoding="utf-8",
    )
    (artifact_dir / "brand.md").write_text(
        "# Brand\n| Token | Hex |\n| bg | #0F0D0A |\n| primary | #E05C1A |\n",
        encoding="utf-8",
    )
    (artifact_dir / "components.md").write_text(
        "## Components\n- Dashboard shell\n",
        encoding="utf-8",
    )
    manifest: dict = {"history": [], "benchmark": {}, "artifacts": []}

    gate = await runner._run_pre_code_design_gate(
        manifest=manifest,
        artifact_dir=artifact_dir,
        brief="Build a polished dashboard UI",
    )

    assert not gate["blockers"]


@pytest.mark.asyncio
async def test_critique_code_fix_triggers_reviewer_rescore(tmp_path, monkeypatch) -> None:
    from skyn3t.core.agent import TaskRequest, TaskResult

    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    artifact_dir = tmp_path / "project"
    scaffold_dir = artifact_dir / "scaffold"
    (scaffold_dir / "src").mkdir(parents=True)
    (scaffold_dir / "src" / "App.jsx").write_text(
        "export default function App() { return null; }\n",
        encoding="utf-8",
    )
    manifest: dict = {"history": [], "execution_profile": "balanced", "benchmark": {}, "artifacts": []}

    rescore_calls: list = []

    async def fake_rescore(**kwargs):
        rescore_calls.append(kwargs)

    monkeypatch.setattr(runner, "_rerun_reviewer_scoring", fake_rescore)

    class FakeReviewer:
        llm = None

        async def initialize(self):
            return None

        async def critique(self, **kwargs):
            return {
                "has_issues": True,
                "issues": [{"file": "src/App.jsx", "problem": "empty UI"}],
                "critique_text": "Add real UI",
            }

    class FakeAgent:
        async def think(self, *_args, **_kwargs):
            return None

        async def execute(self, *_args, **_kwargs):
            return TaskResult(task_id="t1", success=True, output={})

    from skyn3t.agents.targeted_fix import FixResult

    async def fake_fix(**kwargs):
        return FixResult(
            ok=True,
            files_changed=["src/App.jsx"],
            files_created=[],
            errors=[],
            fix_label="critique-fix",
        )

    monkeypatch.setattr("skyn3t.studio.runner.get_agent", lambda *_a, **_k: FakeReviewer())
    monkeypatch.setattr("skyn3t.agents.targeted_fix.apply_targeted_fix", fake_fix)

    stage = type("Stage", (), {"name": "code", "agent": "CodeAgent"})()
    result = TaskResult(
        task_id="t1",
        success=True,
        output={"files": ["scaffold/src/App.jsx"], "summary": "code done"},
    )
    task = TaskRequest(title="code", input_data={"brief": "Build a dashboard UI"})

    await runner._critique_and_revise(
        stage=stage,
        agent=FakeAgent(),
        result=result,
        artifact_dir=artifact_dir,
        brief="Build a polished dashboard UI",
        task=task,
        manifest=manifest,
    )

    assert len(rescore_calls) >= 1
    assert rescore_calls[0]["artifact_dir"] == artifact_dir
