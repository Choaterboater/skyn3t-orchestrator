"""Tests for skyn3t.agents.consistency_reviewer._heuristic_check.

The LLM pass (_llm_check) requires a real LLM and is exercised by
end-to-end pipeline tests. Here we cover the deterministic heuristic
checks: TypeScript-files-exist, env/compose port consistency, and
README service-mention with slug↔display alias normalization (BR-011).
"""

from __future__ import annotations

from pathlib import Path

from skyn3t.agents.consistency_reviewer import (
    ConsistencyFinding,
    ConsistencyReview,
    ConsistencyReviewerAgent,
)


def _make_agent() -> ConsistencyReviewerAgent:
    """Construct a reviewer without spinning the orchestrator."""
    return ConsistencyReviewerAgent(name="test-consistency-reviewer")


# ─── Check 1: TypeScript-requested-but-absent ──────────────────────────


def test_typescript_requested_but_no_ts_files_flags_warning(tmp_path: Path):
    agent = _make_agent()
    # Scaffold has only .jsx, brief asks for TypeScript
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text("export default function App(){}")
    findings = agent._heuristic_check(tmp_path, brief="Build a React app with TypeScript.")
    matching = [f for f in findings if "TypeScript" in f.message]
    assert matching, f"expected TypeScript finding, got {findings}"
    assert matching[0].severity == "warning"


def test_typescript_requested_with_ts_files_passes(tmp_path: Path):
    agent = _make_agent()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("export default function App(){return null}")
    findings = agent._heuristic_check(tmp_path, brief="Build a React app with TypeScript.")
    assert not [f for f in findings if "TypeScript" in f.message]


def test_typescript_not_requested_no_warning(tmp_path: Path):
    """Pure JS brief shouldn't trip the TS check even if no .ts files exist."""
    agent = _make_agent()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text("export default function App(){}")
    findings = agent._heuristic_check(tmp_path, brief="Build a React app.")
    assert not [f for f in findings if "TypeScript" in f.message]


def test_typescript_capitalize_in_brief_still_triggers_check(tmp_path: Path):
    """Brief uses 'TypeScript' (capitalized); _heuristic_check
    lowercases — so the substring 'typescript' is what gets matched.
    This is the test that catches BR-024 regressions if anyone
    re-introduces a capital-letter literal."""
    agent = _make_agent()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text("// no ts files")
    findings = agent._heuristic_check(tmp_path, brief="Use TypeScript please.")
    assert [f for f in findings if "TypeScript" in f.message]


# ─── Check 2: env/compose port mismatch ────────────────────────────────


def test_port_mismatch_between_env_and_compose_flags_warning(tmp_path: Path):
    agent = _make_agent()
    (tmp_path / ".env.example").write_text("PORT=3100\n")
    (tmp_path / "docker-compose.yml").write_text(
        'services:\n  api:\n    ports:\n      - "8080:8080"\n'
    )
    findings = agent._heuristic_check(tmp_path, brief="")
    port_findings = [f for f in findings if "Port mismatch" in f.message]
    assert port_findings
    assert "3100" in port_findings[0].message
    assert "8080" in port_findings[0].message


def test_port_match_between_env_and_compose_passes(tmp_path: Path):
    agent = _make_agent()
    (tmp_path / ".env.example").write_text("PORT=3100\n")
    (tmp_path / "docker-compose.yml").write_text(
        'services:\n  api:\n    ports:\n      - "3100:3100"\n'
    )
    findings = agent._heuristic_check(tmp_path, brief="")
    assert not [f for f in findings if "Port mismatch" in f.message]


def test_port_check_skipped_when_only_one_file_present(tmp_path: Path):
    """If env.example exists but compose doesn't (or vice versa) the
    check is silently skipped — partial scaffolds aren't a port-
    mismatch signal."""
    agent = _make_agent()
    (tmp_path / ".env.example").write_text("PORT=3100\n")
    findings = agent._heuristic_check(tmp_path, brief="")
    assert not [f for f in findings if "Port mismatch" in f.message]


# ─── Check 3: README mention with alias normalization (BR-011) ─────────


def test_readme_mentions_service_by_display_name_no_warning(tmp_path: Path):
    """Brief names 'home_assistant' (slug form); README writes
    'Home Assistant' (display form). The alias logic must accept that
    — without it, every legitimate README trips a false 'missing
    mention' warning."""
    agent = _make_agent()
    (tmp_path / "README.md").write_text(
        "# Homelab Dashboard\n\nIntegrates with Home Assistant and Pi-hole.\n"
    )
    findings = agent._heuristic_check(
        tmp_path,
        brief="Build a homelab dashboard for Home Assistant and Pi-hole.",
    )
    readme_findings = [f for f in findings if f.category == "readme_drift"]
    assert not readme_findings, f"unexpected readme_drift findings: {readme_findings}"


def test_readme_missing_service_mention_flags_warning(tmp_path: Path):
    """If README truly doesn't mention the service in any form, fire."""
    agent = _make_agent()
    (tmp_path / "README.md").write_text(
        "# Dashboard\n\nA dashboard.\n"
    )
    findings = agent._heuristic_check(
        tmp_path,
        brief="Build a homelab dashboard for Home Assistant.",
    )
    drift = [f for f in findings if f.category == "readme_drift"]
    assert drift, "expected readme_drift warning when service is unmentioned"


def test_readme_check_skipped_when_no_readme(tmp_path: Path):
    """No README → no warning. The reviewer doesn't fault scaffolds
    that didn't ship a README."""
    agent = _make_agent()
    findings = agent._heuristic_check(
        tmp_path,
        brief="Build a homelab dashboard for Home Assistant.",
    )
    assert not [f for f in findings if f.category == "readme_drift"]


def test_readme_accepts_hyphenated_variant(tmp_path: Path):
    """home_assistant slug → 'home-assistant' should also be accepted
    (alongside 'home assistant')."""
    agent = _make_agent()
    (tmp_path / "README.md").write_text(
        "# Dashboard\n\nUses home-assistant for state.\n"
    )
    findings = agent._heuristic_check(
        tmp_path,
        brief="Build a homelab dashboard for Home Assistant.",
    )
    assert not [f for f in findings if f.category == "readme_drift"]


# ─── Composite: ok=True path ───────────────────────────────────────────


def test_consistency_review_serializes_to_json():
    """The output of execute() is supposed to embed report_json. Test
    the dataclass round-trips cleanly."""
    review = ConsistencyReview(
        ok=False,
        findings=[
            ConsistencyFinding(
                severity="blocker",
                category="missing_feature",
                file="server/index.js",
                message="No /api/health route",
                suggestion="Add app.get('/api/health', ...)",
            ),
        ],
    )
    import json as _json
    parsed = _json.loads(review.to_json())
    assert parsed["ok"] is False
    assert len(parsed["findings"]) == 1
    assert parsed["findings"][0]["category"] == "missing_feature"


# ─── Check 4: backend deps without server code ────────────────────────


def test_backend_deps_without_server_dir_flags_blocker(tmp_path: Path):
    """package.json declares express + better-sqlite3 but no server/ dir
    or server.js — the "looks fullstack but ships frontend-only" pattern
    that repeatedly tanked LLM scores in production (e75f28, beea80)."""
    import json as _json
    agent = _make_agent()
    (tmp_path / "package.json").write_text(_json.dumps({
        "dependencies": {"express": "^4.0", "better-sqlite3": "^9.0", "react": "^18.0"},
    }))
    findings = agent._heuristic_check(tmp_path, brief="Build a frontend.")
    matching = [f for f in findings if "Backend dependencies" in f.message]
    assert matching, f"expected backend-deps-without-server finding, got {findings}"
    assert matching[0].severity == "blocker"
    assert "express" in matching[0].message
    assert "better-sqlite3" in matching[0].message


def test_backend_deps_with_server_dir_passes(tmp_path: Path):
    import json as _json
    agent = _make_agent()
    (tmp_path / "package.json").write_text(_json.dumps({
        "dependencies": {"express": "^4.0", "react": "^18.0"},
    }))
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "index.js").write_text("// server")
    findings = agent._heuristic_check(tmp_path, brief="Build a fullstack app.")
    assert not [f for f in findings if "Backend dependencies" in f.message]


# ─── Check 5: planned components not imported from entrypoint ─────────


def test_planned_components_not_imported_flags_blocker(tmp_path: Path):
    """Repro of beea80: component_file_plan.json declares HabitCard,
    HabitList, StreakBadge, WeekStrip — App.jsx is a localStorage demo
    importing none of them."""
    import json as _json
    artifact_root = tmp_path / "habit-tracker"
    scaffold = artifact_root / "scaffold"
    scaffold.mkdir(parents=True)
    (artifact_root / "component_file_plan.json").write_text(_json.dumps({
        "files": [
            {"path": "src/components/HabitCard.jsx"},
            {"path": "src/components/HabitList.jsx"},
            {"path": "src/components/StreakBadge.jsx"},
            {"path": "src/components/WeekStrip.jsx"},
        ],
    }))
    (scaffold / "src").mkdir()
    (scaffold / "src" / "App.jsx").write_text(
        "import { useState } from 'react'\n"
        "function App() { const [h, setH] = useState([]) }\n"
    )
    agent = _make_agent()
    findings = agent._heuristic_check(scaffold, brief="Habit tracker.")
    matching = [f for f in findings if "ignores" in f.message and "planned components" in f.message]
    assert matching, f"expected planned-components-missing finding, got {findings}"
    assert matching[0].severity == "blocker"


def test_planned_components_mostly_imported_passes(tmp_path: Path):
    import json as _json
    artifact_root = tmp_path / "habit-tracker"
    scaffold = artifact_root / "scaffold"
    scaffold.mkdir(parents=True)
    (artifact_root / "component_file_plan.json").write_text(_json.dumps({
        "files": [
            {"path": "src/components/HabitCard.jsx"},
            {"path": "src/components/HabitList.jsx"},
        ],
    }))
    (scaffold / "src").mkdir()
    (scaffold / "src" / "App.jsx").write_text(
        "import HabitCard from './components/HabitCard'\n"
        "import HabitList from './components/HabitList'\n"
        "function App() { return <HabitList /> }\n"
    )
    agent = _make_agent()
    findings = agent._heuristic_check(scaffold, brief="Habit tracker.")
    assert not [f for f in findings if "ignores" in f.message and "planned components" in f.message]


# ─── Check 6: index.html title is a template leftover ─────────────────


def test_template_title_leftover_flags_warning(tmp_path: Path):
    """e75f28, beea80, and 2d4498 all shipped <title>Homelab Dashboard</title>
    despite being habit tracker / inventory app. Brief shares no words
    with the title → almost certainly a template leftover."""
    agent = _make_agent()
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><head><title>Homelab Dashboard</title></head></html>"
    )
    findings = agent._heuristic_check(tmp_path, brief="Build a habit tracker with streaks.")
    matching = [f for f in findings if "template leftover" in f.message]
    assert matching, f"expected template-title finding, got {findings}"


def test_matching_title_passes(tmp_path: Path):
    agent = _make_agent()
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><head><title>Habit Tracker</title></head></html>"
    )
    findings = agent._heuristic_check(tmp_path, brief="Build a habit tracker with streaks.")
    assert not [f for f in findings if "template leftover" in f.message]


# ─── Check 7: tech_stack.json declared vs reality ─────────────────────


def test_tech_stack_declares_express_without_server_flags_blocker(tmp_path: Path):
    import json as _json
    artifact_root = tmp_path / "proj"
    scaffold = artifact_root / "scaffold"
    scaffold.mkdir(parents=True)
    (artifact_root / "tech_stack.json").write_text(_json.dumps({
        "backend": "express",
        "frontend": "react",
    }))
    (scaffold / "package.json").write_text(_json.dumps({"dependencies": {"react": "^18.0"}}))
    agent = _make_agent()
    findings = agent._heuristic_check(scaffold, brief="Build something.")
    matching = [f for f in findings if "tech_stack.json declares backend" in f.message]
    assert matching, f"expected hallucinated-stack finding, got {findings}"
    assert matching[0].severity == "blocker"


def test_tech_stack_declares_frontend_only_passes(tmp_path: Path):
    import json as _json
    artifact_root = tmp_path / "proj"
    scaffold = artifact_root / "scaffold"
    scaffold.mkdir(parents=True)
    (artifact_root / "tech_stack.json").write_text(_json.dumps({"backend": "none"}))
    agent = _make_agent()
    findings = agent._heuristic_check(scaffold, brief="Frontend-only.")
    assert not [f for f in findings if "tech_stack.json" in f.message]
