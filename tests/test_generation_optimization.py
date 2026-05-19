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
    assert runner._infer_execution_profile("build a todo app", None) == "fast"
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

    async def should_not_run_targeted_fix(*args, **kwargs):
        raise AssertionError("targeted fix should be skipped for unresolved stubs")

    monkeypatch.setattr(
        "skyn3t.agents.targeted_fix.apply_targeted_fix",
        should_not_run_targeted_fix,
    )

    with pytest.raises(UnresolvedScaffoldStubError):
        await runner._run_post_code_checks(
            manifest=manifest,
            artifact_dir=artifact_dir,
            brief="Build a dashboard",
            stage_name="code",
            stage_output={"files": [str(scaffold_dir / "src" / "App.jsx")]},
        )


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
