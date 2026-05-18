"""Tests for skyn3t.agents.contract_engine."""

from __future__ import annotations

import json
from pathlib import Path

from skyn3t.agents.contract_engine import check_contract


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _brand_brief(extra: str = "") -> str:
    return "Build a dashboard with a strong brand palette and dark theme. " + extra


# ---------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------


def test_contract_flags_palette_schism_css_uses_non_palette_hex(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        artifact / "palette.json",
        json.dumps({"primary": "#E05C1A", "bg": "#0F0D0A", "text": "#E8DDCB"}),
    )
    _write(
        scaffold / "src" / "styles.css",
        ":root { --bg: #09111f; --accent: #60a5fa; --text: #e2e8f0; }\n",
    )

    report = check_contract(scaffold, _brand_brief(), artifact)

    schism = [f for f in report.findings if f.category == "palette_schism_css"]
    assert schism, "expected palette_schism_css finding"
    assert schism[0].severity == "blocker"
    assert schism[0].file == "src/styles.css"
    assert "#09111f" in schism[0].fix_hint.get("offending_hexes", [])
    assert "#e05c1a" in schism[0].fix_hint.get("canonical_palette", [])
    assert report.ok is False


def test_contract_accepts_css_using_only_palette_colors(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        artifact / "palette.json",
        json.dumps({"primary": "#E05C1A", "bg": "#0F0D0A", "text": "#E8DDCB"}),
    )
    _write(
        scaffold / "src" / "styles.css",
        ":root { --bg: #0F0D0A; --accent: #E05C1A; color: currentColor; }\n",
    )

    report = check_contract(scaffold, _brand_brief(), artifact)

    assert not [f for f in report.findings if f.category == "palette_schism_css"]


def test_contract_palette_uses_oklch_skips_check(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        artifact / "palette.json",
        json.dumps({"primary": "oklch(67% 0.18 32)", "bg": "hsl(30 25% 8%)"}),
    )
    _write(
        scaffold / "src" / "styles.css",
        ":root { --bg: #09111f; --accent: #60a5fa; }\n",
    )

    report = check_contract(scaffold, _brand_brief(), artifact)

    assert not [f for f in report.findings if f.category.startswith("palette_schism")]


def test_contract_palette_schism_is_warning_when_brief_has_no_brand_intent(
    tmp_path: Path,
) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(artifact / "palette.json", json.dumps({"primary": "#E05C1A"}))
    _write(scaffold / "src" / "styles.css", ":root { --bg: #09111f; }\n")

    report = check_contract(scaffold, brief="Ship a CLI utility", artifact_dir=artifact)

    schism = [f for f in report.findings if f.category == "palette_schism_css"]
    assert schism
    assert schism[0].severity == "warning"


# ---------------------------------------------------------------------
# tech_stack
# ---------------------------------------------------------------------


def test_contract_flags_tech_stack_mismatch_db(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        artifact / "tech_stack.json",
        json.dumps({"frontend": "react-vite", "backend": "express", "db": "sqlite-better-sqlite3"}),
    )
    _write(
        scaffold / "package.json",
        json.dumps({"dependencies": {"react": "^18", "vite": "^5"}}),
    )
    _write(
        scaffold / "server" / "package.json",
        json.dumps({"dependencies": {"express": "^4", "cors": "^2"}}),
    )

    report = check_contract(scaffold, brief="", artifact_dir=artifact)

    db_mismatches = [
        f for f in report.findings
        if f.category == "tech_stack_mismatch" and f.fix_hint.get("role") == "db"
    ]
    assert db_mismatches
    assert db_mismatches[0].severity == "blocker"
    assert db_mismatches[0].file == "server/package.json"
    assert "better-sqlite3" in db_mismatches[0].fix_hint.get("expected_packages", [])


def test_contract_accepts_tech_stack_match(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        artifact / "tech_stack.json",
        json.dumps({"frontend": "react-vite", "backend": "express", "db": "sqlite-better-sqlite3"}),
    )
    _write(
        scaffold / "package.json",
        json.dumps({"dependencies": {"react": "^18", "vite": "^5"}}),
    )
    _write(
        scaffold / "server" / "package.json",
        json.dumps({"dependencies": {"express": "^4", "better-sqlite3": "^9"}}),
    )

    report = check_contract(scaffold, brief="", artifact_dir=artifact)

    assert not [f for f in report.findings if f.category == "tech_stack_mismatch"]


def test_contract_unknown_tech_stack_value_is_warning_not_blocker(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        artifact / "tech_stack.json",
        json.dumps({"db": "exotic-new-thing"}),
    )
    _write(scaffold / "package.json", json.dumps({"dependencies": {}}))

    report = check_contract(scaffold, brief="", artifact_dir=artifact)

    mismatches = [f for f in report.findings if f.category == "tech_stack_mismatch"]
    assert mismatches
    assert all(f.severity == "warning" for f in mismatches)
    assert report.ok is True  # warnings don't fail the contract


# ---------------------------------------------------------------------
# Placeholder leaks
# ---------------------------------------------------------------------


def test_contract_flags_placeholder_literal_leak_in_readme(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(scaffold / "README.md", "# Auto-planned\n\nProject docs.\n")

    report = check_contract(scaffold, brief="", artifact_dir=artifact)

    leaks = [f for f in report.findings if f.category == "placeholder_leak"]
    assert leaks
    assert leaks[0].severity == "blocker"
    assert leaks[0].fix_hint.get("literal") == "Auto-planned"


def test_contract_flags_todo_skyn3t_marker_in_json(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(scaffold / "manifest.json", '{"note": "TODO[skyn3t]: wire this up"}\n')

    report = check_contract(scaffold, brief="", artifact_dir=artifact)

    leaks = [f for f in report.findings if f.category == "placeholder_leak"]
    assert leaks
    assert leaks[0].severity == "blocker"
    assert leaks[0].fix_hint.get("literal") == "TODO[skyn3t]"


def test_contract_warns_on_fixme_and_tbd(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(scaffold / "README.md", "# Title\n\nFIXME this section.\nTBD: more details.\n")

    report = check_contract(scaffold, brief="", artifact_dir=artifact)

    soft = [
        f for f in report.findings
        if f.category == "placeholder_leak" and f.severity == "warning"
    ]
    literals = {f.fix_hint.get("literal") for f in soft}
    assert "FIXME" in literals
    assert "TBD" in literals
    assert report.ok is True  # warnings only


def test_contract_skips_node_modules_for_placeholders(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(scaffold / "node_modules" / "foo" / "README.md", "TBD\nFIXME\n")
    _write(scaffold / "package.json", json.dumps({"dependencies": {}}))

    report = check_contract(scaffold, brief="", artifact_dir=artifact)

    leaks_in_node_modules = [
        f for f in report.findings
        if f.category == "placeholder_leak" and "node_modules" in f.file
    ]
    assert not leaks_in_node_modules


def test_contract_skips_lockfiles_for_placeholders(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    # package-lock.json frequently contains "TBD" inside license strings,
    # vendor changelogs, etc. The scanner must skip it.
    _write(scaffold / "package-lock.json", json.dumps({"note": "Auto-planned"}))

    report = check_contract(scaffold, brief="", artifact_dir=artifact)

    leaks = [f for f in report.findings if f.category == "placeholder_leak"]
    assert not leaks


# ---------------------------------------------------------------------
# Feature evidence
# ---------------------------------------------------------------------


def test_contract_warns_on_missing_command_palette_evidence(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(scaffold / "src" / "App.jsx", "export default function App() { return <div/>; }\n")

    report = check_contract(
        scaffold,
        brief="Build a dashboard with a command palette triggered by Cmd+K.",
        artifact_dir=artifact,
    )

    feat = [f for f in report.findings if f.category == "missing_feature_evidence"]
    assert any("command palette" in f.fix_hint.get("keyword", "") for f in feat)
    assert all(f.severity == "warning" for f in feat)


def test_contract_accepts_command_palette_via_useHotkeys(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        "import { useHotkeys } from 'react-hotkeys-hook';\n"
        "useHotkeys('Mod+K', () => setOpen(true));\n",
    )

    report = check_contract(
        scaffold,
        brief="Build a dashboard with a command palette triggered by Cmd+K.",
        artifact_dir=artifact,
    )

    feat = [
        f for f in report.findings
        if f.category == "missing_feature_evidence"
        and "command palette" in f.fix_hint.get("keyword", "")
    ]
    assert not feat


def test_contract_blocks_on_missing_glassmorphism_when_blur_absent_and_target_exists(
    tmp_path: Path,
) -> None:
    """When the brief requires glassmorphism AND a target CSS file exists,
    the missing backdrop-filter is a BLOCKER so targeted-fix can repair it.
    """
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(scaffold / "src" / "styles.css", ".card { background: rgba(0,0,0,0.5); }\n")

    report = check_contract(
        scaffold,
        brief="Build a polished dashboard with glassmorphism aesthetic.",
        artifact_dir=artifact,
    )

    feat = [
        f for f in report.findings
        if f.category == "missing_feature_evidence"
        and "glassmorphism" in f.fix_hint.get("keyword", "")
    ]
    assert feat
    assert feat[0].severity == "blocker"
    assert feat[0].fix_hint.get("fix_target") == "src/styles.css"
    assert "backdrop-filter" in feat[0].fix_hint.get("fix_instruction", "")


def test_contract_glassmorphism_stays_warning_when_no_css_file_exists(
    tmp_path: Path,
) -> None:
    """If there's no CSS file to fix, glassmorphism gap stays a warning —
    blocking a run we can't auto-repair is worse than letting it pass.
    """
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    # No styles.css / index.css / app.css exists.
    _write(scaffold / "src" / "App.jsx", "export default function App(){return <div/>}")

    report = check_contract(
        scaffold,
        brief="Build a polished dashboard with glassmorphism aesthetic.",
        artifact_dir=artifact,
    )

    feat = [
        f for f in report.findings
        if f.category == "missing_feature_evidence"
        and "glassmorphism" in f.fix_hint.get("keyword", "")
    ]
    assert feat
    assert feat[0].severity == "warning"


# ---------------------------------------------------------------------
# Architecture↔scaffold drift
# ---------------------------------------------------------------------


def test_contract_flags_architecture_drift_nextjs_promised_but_vite_shipped(
    tmp_path: Path,
) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        artifact / "architecture.md",
        "## Stack\n\nWe will build the frontend with Next.js 14 App Router and "
        "the backend with Hono on Node.js. Persistence uses better-sqlite3.\n",
    )
    _write(
        scaffold / "package.json",
        json.dumps({"dependencies": {"react": "^18", "vite": "^5"}}),
    )
    _write(
        scaffold / "server" / "package.json",
        json.dumps({"dependencies": {"express": "^4"}}),
    )

    report = check_contract(scaffold, brief="", artifact_dir=artifact)
    drift = [f for f in report.findings if f.category == "architecture_drift"]
    keywords = {f.fix_hint.get("keyword", "") for f in drift}
    assert any("next" in k for k in keywords), f"expected next drift, got {keywords}"
    assert any("hono" in k for k in keywords), f"expected hono drift, got {keywords}"
    assert any("better-sqlite3" in k for k in keywords), f"expected sqlite drift, got {keywords}"
    assert all(f.severity == "blocker" for f in drift)


def test_contract_no_drift_when_architecture_matches_scaffold(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(artifact / "architecture.md", "We use React + Vite for the UI and Express on Node.\n")
    _write(
        scaffold / "package.json",
        json.dumps({"dependencies": {"react": "^18", "vite": "^5"}}),
    )
    _write(
        scaffold / "server" / "package.json",
        json.dumps({"dependencies": {"express": "^4"}}),
    )

    report = check_contract(scaffold, brief="", artifact_dir=artifact)
    drift = [f for f in report.findings if f.category == "architecture_drift"]
    assert not drift


def test_contract_drift_blocks_when_package_declared_but_not_imported(
    tmp_path: Path,
) -> None:
    """canary-115 pattern: architecture.md promises Next.js + better-sqlite3,
    package.json declares both, but no source file imports them. The drift
    check must catch this — declared-but-unused is the same lie as missing.
    """
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        artifact / "architecture.md",
        "## Stack\n\nNext.js 14 App Router with better-sqlite3 persistence.\n",
    )
    _write(
        scaffold / "package.json",
        json.dumps({"dependencies": {
            "next": "^14",
            "react": "^18",
            "better-sqlite3": "^11",
        }}),
    )
    # Scaffold ships a Vite app instead — no Next.js routes, no sqlite usage.
    _write(scaffold / "src" / "App.jsx", "export default function App(){return <div/>}\n")
    _write(scaffold / "vite.config.js", "export default {};\n")

    report = check_contract(scaffold, brief="", artifact_dir=artifact)
    drift = [f for f in report.findings if f.category == "architecture_drift"]
    keywords = {f.fix_hint.get("keyword", "") for f in drift}
    assert "nextjs" in keywords, f"expected nextjs drift, got {keywords}"
    assert "better-sqlite3" in keywords, f"expected sqlite drift, got {keywords}"
    # The fix_hint should distinguish declared-but-unused from missing —
    # downstream regen prompts read differently for the two cases.
    next_drift = [f for f in drift if f.fix_hint.get("keyword") == "nextjs"][0]
    assert next_drift.fix_hint.get("package_declared") is True
    assert next_drift.fix_hint.get("import_present") is False
    assert "but no source file imports" in next_drift.message


def test_contract_drift_passes_when_package_declared_and_imported(
    tmp_path: Path,
) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        artifact / "architecture.md",
        "## Stack\n\nUses better-sqlite3 for persistence.\n",
    )
    _write(
        scaffold / "server" / "package.json",
        json.dumps({"dependencies": {"better-sqlite3": "^11"}}),
    )
    _write(
        scaffold / "server" / "db.js",
        "import Database from 'better-sqlite3';\nconst db = new Database('app.db');\n",
    )

    report = check_contract(scaffold, brief="", artifact_dir=artifact)
    drift = [f for f in report.findings if f.category == "architecture_drift"]
    assert not drift, f"unexpected drift findings: {[f.fix_hint for f in drift]}"


def test_contract_flags_python_lib_in_npm_package_json(tmp_path: Path) -> None:
    """canary-117 pattern: server/package.json had \"fastapi\": \"^0.1.0\".
    That's a Python framework as an npm dep — `npm install` will fail or
    pull a squatter. Always a blocker.
    """
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        scaffold / "server" / "package.json",
        json.dumps({"dependencies": {
            "express": "^4.21",
            "fastapi": "^0.1.0",       # Python lib as npm dep
            "pydantic": "^2.0",        # ditto
        }}),
    )

    report = check_contract(scaffold, brief="", artifact_dir=artifact)
    leaks = [f for f in report.findings if f.category == "language_mismatch"]
    assert leaks
    assert leaks[0].severity == "blocker"
    polluted = leaks[0].fix_hint.get("polluted_packages", [])
    assert "fastapi" in polluted
    assert "pydantic" in polluted


def test_contract_flags_python_promised_but_node_shipped(tmp_path: Path) -> None:
    """canary-116/117 pattern: tech_stack says backend=fastapi but the
    scaffold has package.json + no pyproject.toml.
    """
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(artifact / "tech_stack.json", json.dumps({"backend": "fastapi", "db": "postgres"}))
    _write(scaffold / "package.json", json.dumps({"dependencies": {"express": "^4"}}))
    _write(scaffold / "server" / "index.js", "import express from 'express';\n")

    report = check_contract(scaffold, brief="", artifact_dir=artifact)
    findings = [
        f for f in report.findings
        if f.category == "language_mismatch" and f.file == "tech_stack.json"
    ]
    assert findings
    assert findings[0].severity == "blocker"
    assert "Python (fastapi)" in findings[0].message


def test_contract_no_language_mismatch_when_python_scaffold_real(tmp_path: Path) -> None:
    """If tech_stack says Python AND scaffold has pyproject.toml, no finding."""
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(artifact / "tech_stack.json", json.dumps({"backend": "fastapi"}))
    _write(scaffold / "pyproject.toml", "[project]\nname = 'app'\n")
    _write(scaffold / "app" / "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")

    report = check_contract(scaffold, brief="", artifact_dir=artifact)
    assert not [f for f in report.findings if f.category == "language_mismatch"]


def test_contract_no_language_mismatch_when_pure_node(tmp_path: Path) -> None:
    """Pure Node scaffold with Node tech_stack — no finding."""
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(artifact / "tech_stack.json", json.dumps({"backend": "express", "db": "better-sqlite3"}))
    _write(scaffold / "server" / "package.json", json.dumps({"dependencies": {"express": "^4"}}))

    report = check_contract(scaffold, brief="", artifact_dir=artifact)
    assert not [f for f in report.findings if f.category == "language_mismatch"]


def test_contract_drift_skips_rejected_alternatives(tmp_path: Path) -> None:
    """Sentences like 'we considered Next.js but chose Vite' should not
    fire a drift blocker — the architecture doc is naming an alternative,
    not promising it.
    """
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        artifact / "architecture.md",
        "We considered Next.js but rejected it in favor of Vite. "
        "We chose Express instead of Hono for the backend.\n",
    )
    _write(
        scaffold / "package.json",
        json.dumps({"dependencies": {"react": "^18", "vite": "^5"}}),
    )
    _write(
        scaffold / "server" / "package.json",
        json.dumps({"dependencies": {"express": "^4"}}),
    )

    report = check_contract(scaffold, brief="", artifact_dir=artifact)
    drift = [f for f in report.findings if f.category == "architecture_drift"]
    assert not drift, f"unexpected drift findings on rejected alternatives: {[f.fix_hint for f in drift]}"


# ---------------------------------------------------------------------
# CLI prose leak detection
# ---------------------------------------------------------------------


def test_contract_flags_cli_prose_leak_in_readme(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(
        scaffold / "server" / "README.md",
        "I'm checking the project structure to write a fitting README.\n\n"
        "● Search (glob)\n"
        "  │ \"**/README.md\"\n"
        "  └ No matches found\n\n"
        "# Server\n\nExpress backend.\n",
    )

    report = check_contract(scaffold, brief="", artifact_dir=artifact)
    leaks = [f for f in report.findings if f.category == "cli_prose_leak"]
    assert leaks
    assert leaks[0].severity == "blocker"
    assert leaks[0].file == "server/README.md"


def test_contract_no_cli_prose_false_positive_for_legitimate_docs(tmp_path: Path) -> None:
    """A README that legitimately documents the term 'No matches found'
    deep in its body should not trip the leak detector."""
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    body = "# Server\n\nExpress backend for the homelab.\n\n"
    body += "Lorem ipsum dolor sit amet. " * 100  # push past the 1500-char head window
    body += "\n\n## Troubleshooting\n\nIf grep returns 'No matches found', check the path.\n"
    _write(scaffold / "server" / "README.md", body)

    report = check_contract(scaffold, brief="", artifact_dir=artifact)
    leaks = [f for f in report.findings if f.category == "cli_prose_leak"]
    assert not leaks


# ---------------------------------------------------------------------
# Edge cases & API
# ---------------------------------------------------------------------


def test_contract_no_findings_when_artifact_dir_lacks_palette_json(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(scaffold / "src" / "styles.css", ":root { --bg: #09111f; }\n")

    report = check_contract(scaffold, brief="Ship a tool", artifact_dir=artifact)

    assert not [f for f in report.findings if f.category.startswith("palette_schism")]


def test_contract_report_to_json_round_trip(tmp_path: Path) -> None:
    artifact = tmp_path
    scaffold = artifact / "scaffold"
    _write(artifact / "palette.json", json.dumps({"primary": "#E05C1A"}))
    _write(scaffold / "src" / "styles.css", ":root { --bg: #09111f; }\n")

    report = check_contract(scaffold, _brand_brief(), artifact)
    parsed = json.loads(report.to_json())

    assert "ok" in parsed
    assert isinstance(parsed["findings"], list)
    for item in parsed["findings"]:
        for key in ("severity", "category", "file", "message", "suggestion", "fix_hint"):
            assert key in item
