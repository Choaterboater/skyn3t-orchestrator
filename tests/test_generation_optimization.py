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
