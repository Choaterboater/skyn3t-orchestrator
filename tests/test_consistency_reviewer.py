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
