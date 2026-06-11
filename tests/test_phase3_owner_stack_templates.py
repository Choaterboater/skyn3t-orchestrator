"""Phase 3 owner_stack_templates contract tests.

Covers the four contracts this owner provides:
  - _data_backend_tier_files(entities, *, slug='')
  - _ui_primitive_files()
  - _needs_data_backend(brief, *, decisions=None)
  - _package_name(slug, fallback='app') + _is_valid_scaffold_filename(path)

Plus the wiring into plan_for_stack / max_files_for and the deterministic
ui/ manifest generators. Pure-Python; no external tool, no tmp dirs
needed beyond what pytest provides.
"""

import json

import pytest

from skyn3t.agents import stack_templates as st

# ── _package_name ───────────────────────────────────────────────────────


@pytest.mark.parametrize("slug,expected", [
    ("My Cool App", "my-cool-app"),
    ("  Habit Tracker  ", "habit-tracker"),
    ("SaaS!!!Portal", "saas-portal"),
    ("___weird___", "weird"),
    (".hidden", "hidden"),            # no leading dot allowed
    ("App-server", "app-server"),
    ("UPPER CASE", "upper-case"),
])
def test_package_name_is_npm_safe(slug, expected):
    assert st._package_name(slug) == expected


def test_package_name_falls_back_when_empty():
    assert st._package_name("") == "app"
    assert st._package_name("", "") == "app"
    assert st._package_name("   ", "my-fallback") == "my-fallback"
    # Fallback is itself sanitized.
    assert st._package_name("", "My Fallback!") == "my-fallback"


def test_package_name_never_starts_with_dot_or_separator():
    for raw in [".env", "...", "--x", "__y", ".a.b.c"]:
        name = st._package_name(raw)
        assert name and name[0].isalnum(), f"bad name from {raw!r}: {name!r}"


# ── _is_valid_scaffold_filename ─────────────────────────────────────────


@pytest.mark.parametrize("path,valid", [
    ("src/App.jsx", True),
    ("app/page.tsx", True),
    (".env.example", True),
    (".gitignore", True),
    (".babelrc", True),
    ("README", True),
    ("Dockerfile", True),
    ("a/b/c.test.tsx", True),
    ("foo/.bar.js", True),          # has a real stem before final dot
    # malformed / template-bleed shapes that must be rejected:
    ("src/.js", False),
    (".js", False),
    (".tsx", False),
    ("a/.css", False),
    ("", False),
    ("   ", False),
    ("src/", False),
    ("src//x.js", False),
])
def test_is_valid_scaffold_filename(path, valid):
    assert st._is_valid_scaffold_filename(path) is valid


def test_is_valid_scaffold_filename_never_raises_on_garbage():
    for bad in [None, 123, [], {}, object()]:
        assert st._is_valid_scaffold_filename(bad) is False  # type: ignore[arg-type]


def test_filter_plan_drops_malformed_and_dedupes():
    plan = [
        ("src/App.jsx", "ok"),
        ("src/.js", "bad"),
        ("", "empty"),
        (".gitignore", "keep"),
        ("src/App.jsx", "dup"),
    ]
    out = st._filter_plan(plan)
    paths = [r for r, _ in out]
    assert paths == ["src/App.jsx", ".gitignore"]


def test_plan_for_stack_never_emits_malformed_filenames():
    # Sweep every browser-first stack + a representative dashboard brief.
    brief = (
        "Build a polished homelab dashboard with sonarr, radarr, plex "
        "monitoring, a persistent backend config store, server-side CRUD, "
        "and settings that survive restart."
    )
    for stack in ("react_vite", "react_vite_tailwind", "next"):
        plan = st.plan_for_stack(stack, brief)
        assert plan is not None
        for rel, _ in plan:
            assert st._is_valid_scaffold_filename(rel), f"{stack}: {rel!r}"


# ── _needs_data_backend ─────────────────────────────────────────────────


@pytest.mark.parametrize("brief,decisions,expected", [
    ("Build a multi-user task manager saving to a sql database", None, True),
    ("Build a notes app that stores notes in postgres", None, True),
    ("Build a CRM that owns its data", None, True),
    ("Build a todo app saved to localStorage", None, False),
    ("Build a habit tracker", None, False),
    # decisions signals:
    ("Build a habit tracker", {"family": "fullstack"}, True),
    ("Build a habit tracker", {"family": "web"}, False),
    ("Build a habit tracker",
     {"framework": "express", "backend_language": "node", "backend_port": 3000}, True),
])
def test_needs_data_backend_gate(brief, decisions, expected):
    assert st._needs_data_backend(brief, decisions=decisions) is expected


def test_needs_data_backend_excludes_games_and_toys():
    # Even with a database word, a game/toy must never own server data.
    assert st._needs_data_backend(
        "Build a snake game that saves high score to a database"
    ) is False
    assert st._needs_data_backend(
        "Build a multiplayer chess game backed by a database",
        decisions={"family": "fullstack"},
    ) is False
    assert st._needs_data_backend("Build a physics toy") is False


def test_needs_data_backend_defaults_false_on_uncertainty():
    assert st._needs_data_backend("") is False
    assert st._needs_data_backend("Build something nice") is False


# ── _data_backend_tier_files ────────────────────────────────────────────


def test_data_backend_tier_has_real_crud_shape():
    plan = st._data_backend_tier_files(["task", "project"], slug="my-app")
    paths = {rel for rel, _ in plan}
    assert "server/index.js" in paths
    assert "server/db.js" in paths
    assert "server/package.json" in paths
    assert ".env.example" in paths
    assert "server/seed.js" in paths
    assert "server/routes/task.js" in paths
    assert "server/routes/project.js" in paths


def test_data_backend_db_purpose_promises_json_fallback():
    plan = dict(st._data_backend_tier_files(["task"]))
    db_purpose = plan["server/db.js"].lower()
    assert "better-sqlite3" in db_purpose
    assert "json" in db_purpose and "fall" in db_purpose


def test_data_backend_route_purpose_covers_full_crud():
    plan = dict(st._data_backend_tier_files(["task"]))
    route = plan["server/routes/task.js"]
    for verb in ("GET", "POST", "PATCH", "DELETE"):
        assert verb in route


def test_data_backend_defaults_to_item_entity_when_none():
    plan = st._data_backend_tier_files([])
    paths = {rel for rel, _ in plan}
    assert "server/routes/item.js" in paths


def test_data_backend_normalizes_and_dedupes_entities():
    plan = st._data_backend_tier_files(["Task!", "task", "  ", "Pro-ject"])
    routes = [rel for rel, _ in plan if rel.startswith("server/routes/")]
    # 'Task!' and 'task' collapse to one; blank dropped; 'Pro-ject' -> pro_ject
    assert "server/routes/task.js" in routes
    assert "server/routes/pro_ject.js" in routes
    assert len(routes) == 2


def test_data_backend_server_package_name_from_slug():
    plan = dict(st._data_backend_tier_files(["task"], slug="My Cool App"))
    purpose = plan["server/package.json"]
    assert "my-cool-app-server" in purpose


# ── _ui_primitive_files ─────────────────────────────────────────────────


def test_ui_primitive_files_full_kit():
    plan = st._ui_primitive_files()
    paths = {rel for rel, _ in plan}
    expected = {
        "src/components/ui/Button.jsx",
        "src/components/ui/Card.jsx",
        "src/components/ui/Modal.jsx",
        "src/components/ui/Input.jsx",
        "src/components/ui/StatusPill.jsx",
        "src/components/ui/KpiTile.jsx",
        "src/components/ui/Sparkline.jsx",
        "src/components/ui/Skeleton.jsx",
        "src/components/ui/Toast.jsx",
        "src/components/ui/EmptyState.jsx",
    }
    assert expected <= paths


def test_ui_primitive_purposes_mandate_state_and_tokens():
    for rel, purpose in st._ui_primitive_files():
        low = purpose.lower()
        assert "var(--brand-" in low, f"{rel} missing token mandate"
        assert "hover/focus/disabled/loading" in low, f"{rel} missing state mandate"
        # Stay a usable one-liner.
        assert len(purpose) < 200, f"{rel} purpose too long"


def test_deterministic_ui_primitives_are_token_driven():
    for stack in ("react_vite", "react_vite_tailwind", "next"):
        for prim in ("StatusPill", "Sparkline", "KpiTile"):
            body = st.manifest_for(stack, f"src/components/ui/{prim}.jsx", "a dashboard")
            assert body is not None, f"{stack}/{prim} not deterministic"
            assert "var(--brand" in body, f"{stack}/{prim} not token-driven"
            assert f"export default function {prim}" in body


# ── plan_for_stack wiring ───────────────────────────────────────────────


def test_data_owning_app_with_server_gets_data_tier_not_proxy():
    brief = (
        "Build a multi-user task and project manager that owns its data "
        "in a sql database"
    )
    plan = st.plan_for_stack("react_vite", brief, decisions={"family": "fullstack"})
    paths = {rel for rel, _ in plan}
    assert "server/db.js" in paths
    assert "server/routes/task.js" in paths
    # Not the proxy tier.
    assert not any("server/adapters/" in p for p in paths)


def test_data_owning_brief_without_server_stays_localstorage():
    brief = "Build a notes app that stores notes in postgres"
    plan = st.plan_for_stack("react_vite", brief)
    paths = {rel for rel, _ in plan}
    assert "server/db.js" not in paths
    assert "server/index.js" not in paths


def test_proxy_app_keeps_proxy_tier():
    brief = "Dashboard for sonarr and radarr with api keys"
    plan = st.plan_for_stack("react_vite", brief)
    paths = {rel for rel, _ in plan}
    assert any("server/adapters/" in p for p in paths)
    assert "server/db.js" not in paths


def test_game_with_db_word_never_gets_data_tier():
    # A game/toy is hard-excluded from the OWNS-ITS-DATA tier even when
    # it mentions a database AND the architect pinned fullstack.
    brief = "Build a snake game that saves high score to a database"
    plan = st.plan_for_stack("react_vite", brief, decisions={"family": "fullstack"})
    paths = {rel for rel, _ in plan}
    assert "server/db.js" not in paths
    assert not any(p.startswith("server/routes/") for p in paths)


def test_game_without_decisions_gets_no_backend_at_all():
    brief = "Build a snake game that saves high score to a database"
    plan = st.plan_for_stack("react_vite", brief)
    paths = {rel for rel, _ in plan}
    assert "server/db.js" not in paths
    assert "server/index.js" not in paths


def test_dashboard_brief_uses_ui_primitive_library():
    brief = "Build a homelab service status dashboard"
    plan = st.plan_for_stack("react_vite", brief)
    paths = {rel for rel, _ in plan}
    assert "src/components/ui/Button.jsx" in paths
    assert "src/components/ui/EmptyState.jsx" in paths


# ── caps stay coherent with the bigger primitive set ────────────────────


def test_max_files_cap_above_worst_case_dashboard_plan():
    brief = (
        "Build a polished homelab dashboard with sonarr, radarr, plex, "
        "jellyfin, prowlarr, pihole, unifi monitoring. Persistent backend "
        "config store, server-side CRUD, health endpoint, settings UI that "
        "saves config across restarts, edit each service api key from the "
        "settings panel, test connection."
    )
    cap = st.max_files_for(brief)
    for stack in ("react_vite", "react_vite_tailwind", "next"):
        plan = st.plan_for_stack(stack, brief)
        assert len(plan) <= cap, (
            f"{stack}: plan {len(plan)} exceeds cap {cap} — last primitives "
            "would be truncated (the v31 / line-384 bug)"
        )


def test_package_json_name_from_slug_in_manifests():
    pj = json.loads(st.manifest_for("react_vite", "package.json", "Build a Habit Tracker"))
    assert pj["name"] == "habit-tracker"
    sj = json.loads(st.manifest_for("react_vite", "server/package.json", "Build a Habit Tracker"))
    assert sj["name"] == "habit-tracker-server"
    # Server package.json stays valid JSON and ESM.
    assert sj["type"] == "module"
