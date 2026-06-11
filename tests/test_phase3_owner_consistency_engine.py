"""Tests for the Phase 3 consistency-engine scanners (owner_consistency_engine).

Covers the five new scanners wired into check_consistency:
  - _scan_stub_entrypoint            -> category "stub_entrypoint"   (error)
  - _scan_design_token_contract      -> category "design_token_contract" (error)
  - _scan_css_coverage_orphan_classes-> category "orphan_classname"  (error|warning)
  - _scan_truncated_stylesheet       -> category "truncated_stylesheet" (error)
  - _scan_planned_component_unused   -> category "planned_component_unused" (warning)

All fixtures use tmp dirs only. New findings are additive + gated, so these
tests assert ONLY on the new categories (mirroring the existing test style).
"""

import json
from pathlib import Path

from skyn3t.agents import consistency_engine as ce
from skyn3t.agents.consistency_engine import check_consistency


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _tokens_css() -> str:
    return (
        ":root {\n"
        "  --brand-primary: #5B8DEF;\n"
        "  --brand-bg: #0B0E14;\n"
        "  --brand-surface: rgba(255,255,255,0.04);\n"
        "  --brand-text: #E2E8F0;\n"
        "  --brand-radius-md: 12px;\n"
        "}\n"
    )


# ── _scan_stub_entrypoint ────────────────────────────────────────────────


def test_stub_entrypoint_export_default_null_flagged(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(scaffold / "src" / "App.jsx", "export default null;\n")
    report = check_consistency(scaffold, brief="build a react app")
    stubs = [i for i in report.issues if i.category == "stub_entrypoint"]
    assert len(stubs) == 1
    assert stubs[0].file == "src/App.jsx"
    assert stubs[0].severity == "error"
    assert not report.ok


def test_stub_entrypoint_generation_failed_placeholder_flagged(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        "export default function App() {\n"
        "  return <div>Generation failed</div>;\n"
        "}\n",
    )
    report = check_consistency(scaffold, brief="build a react app")
    stubs = [i for i in report.issues if i.category == "stub_entrypoint"]
    assert len(stubs) == 1
    assert stubs[0].severity == "error"


def test_stub_entrypoint_real_app_not_flagged(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        "import Header from './Header.jsx';\n"
        "export default function App() {\n"
        "  return (\n"
        "    <main className=\"shell\">\n"
        "      <Header />\n"
        "      <section><h1>Dashboard</h1><p>Welcome</p></section>\n"
        "      <footer>built with skyn3t</footer>\n"
        "    </main>\n"
        "  );\n"
        "}\n",
    )
    report = check_consistency(scaffold, brief="build a react app")
    assert not [i for i in report.issues if i.category == "stub_entrypoint"]


def test_stub_entrypoint_app_with_real_failure_state_not_flagged(tmp_path: Path) -> None:
    """An app that legitimately renders a 'generation failed'-ish error
    state (large body, many tags) must NOT be mistaken for a stub."""
    scaffold = tmp_path / "scaffold"
    body = (
        "import { useState } from 'react';\n"
        "export default function App() {\n"
        "  const [err, setErr] = useState(null);\n"
        "  return (\n"
        "    <div className=\"app\">\n"
        "      <header><h1>Reports</h1></header>\n"
        "      <nav><a href=\"#a\">A</a><a href=\"#b\">B</a></nav>\n"
        "      <main>\n"
        "        <table><thead><tr><th>Name</th></tr></thead>\n"
        "        <tbody><tr><td>Row generation failed gracefully</td></tr></tbody></table>\n"
        "      </main>\n"
        "      <aside><ul><li>one</li><li>two</li><li>three</li></ul></aside>\n"
        "      <footer><span>v1</span></footer>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )
    _write(scaffold / "src" / "App.jsx", body)
    report = check_consistency(scaffold, brief="build a react app")
    assert not [i for i in report.issues if i.category == "stub_entrypoint"]


def test_stub_entrypoint_does_not_fire_on_server_index(tmp_path: Path) -> None:
    """A server/routes index.js that exports a router must not be treated
    as an SPA entrypoint."""
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "server" / "routes" / "index.js",
        "import express from 'express';\n"
        "const router = express.Router();\n"
        "export default router;\n",
    )
    report = check_consistency(scaffold, brief="")
    assert not [i for i in report.issues if i.category == "stub_entrypoint"]


# ── _scan_design_token_contract ──────────────────────────────────────────


def test_design_token_contract_noop_without_tokens_css(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    # styles.css re-defines its own :root colors but there is NO tokens.css,
    # so the contract is not in force.
    _write(
        scaffold / "src" / "styles.css",
        ":root { --bg: #111; --surface: #222; }\n.card { background: var(--bg); }\n",
    )
    _write(scaffold / "src" / "App.jsx", "export default () => <div className=\"card\">hi</div>;")
    report = check_consistency(scaffold, brief="")
    assert not [i for i in report.issues if i.category == "design_token_contract"]


def test_design_token_contract_flags_redefined_root_colors(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(scaffold / "src" / "tokens.css", _tokens_css())
    # styles.css re-invents its own palette instead of consuming var(--brand-*)
    _write(
        scaffold / "src" / "styles.css",
        ":root {\n  --bg: #0a0a0a;\n  --surface: #161616;\n}\n"
        ".card { background: var(--surface); border-radius: var(--brand-radius-md); }\n",
    )
    report = check_consistency(scaffold, brief="")
    token_issues = [i for i in report.issues if i.category == "design_token_contract"]
    redefine = [i for i in token_issues if i.file == "src/styles.css"]
    assert redefine
    assert redefine[0].severity == "error"
    assert not report.ok


def test_design_token_contract_flags_zero_brand_refs(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(scaffold / "src" / "tokens.css", _tokens_css())
    # No file references var(--brand-*) anywhere.
    _write(scaffold / "src" / "styles.css", ".card { background: #123456; }\n")
    _write(scaffold / "src" / "App.jsx", "export default () => <div className=\"card\">x</div>;")
    report = check_consistency(scaffold, brief="")
    zero = [
        i for i in report.issues
        if i.category == "design_token_contract" and "0 var(--brand-*)" in i.message
    ]
    assert zero
    assert zero[0].severity == "error"


def test_design_token_contract_clean_when_consumed(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(scaffold / "src" / "tokens.css", _tokens_css())
    _write(
        scaffold / "src" / "styles.css",
        ".card {\n"
        "  background: var(--brand-surface);\n"
        "  color: var(--brand-text);\n"
        "  border-radius: var(--brand-radius-md);\n"
        "}\n"
        ".cta { background: var(--brand-primary); }\n",
    )
    _write(scaffold / "src" / "App.jsx", "export default () => <div className=\"card\">x</div>;")
    report = check_consistency(scaffold, brief="")
    assert not [i for i in report.issues if i.category == "design_token_contract"]


# ── _scan_css_coverage_orphan_classes ────────────────────────────────────


def test_orphan_classname_severe_when_no_css_backs_any(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        "export default function App() {\n"
        "  return <div className=\"dashboard-grid\"><span className=\"kpi-tile\">x</span></div>;\n"
        "}\n",
    )
    # A stylesheet exists but defines none of the used classes.
    _write(scaffold / "src" / "styles.css", ".unused-thing { color: red; }\n")
    report = check_consistency(scaffold, brief="")
    orphans = [i for i in report.issues if i.category == "orphan_classname"]
    assert orphans
    assert orphans[0].severity == "error"


def test_orphan_classname_clean_when_backed(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        "export default () => <div className=\"dashboard-grid\">x</div>;",
    )
    _write(scaffold / "src" / "styles.css", ".dashboard-grid { display: grid; }\n")
    report = check_consistency(scaffold, brief="")
    assert not [i for i in report.issues if i.category == "orphan_classname"]


def test_orphan_classname_skips_tailwind_projects(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(scaffold / "package.json", json.dumps({"dependencies": {"tailwindcss": "^3"}}))
    _write(scaffold / "tailwind.config.js", "module.exports = {};\n")
    _write(
        scaffold / "src" / "App.jsx",
        "export default () => <div className=\"bg-slate-900 rounded-xl p-4\">x</div>;",
    )
    report = check_consistency(scaffold, brief="")
    assert not [i for i in report.issues if i.category == "orphan_classname"]


def test_orphan_classname_ignores_dynamic_class_templates(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    # Template-literal interpolation must be skipped (too noisy to resolve).
    _write(
        scaffold / "src" / "Pill.jsx",
        "export default ({tone}) => <span className={`pill pill-${tone}`}>x</span>;",
    )
    report = check_consistency(scaffold, brief="")
    assert not [i for i in report.issues if i.category == "orphan_classname"]


# ── _scan_truncated_stylesheet ───────────────────────────────────────────


def test_truncated_stylesheet_unbalanced_braces_flagged(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "styles.css",
        ".card {\n  background: #111;\n  color: #fff;\n.button {\n  padding: 8px;\n",
    )
    report = check_consistency(scaffold, brief="")
    trunc = [i for i in report.issues if i.category == "truncated_stylesheet"]
    assert trunc
    assert trunc[0].severity == "error"
    assert trunc[0].file == "src/styles.css"


def test_truncated_stylesheet_balanced_ok(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "styles.css",
        ".card { background: #111; }\n.button { padding: 8px; }\n.hero { gap: 12px; }\n",
    )
    report = check_consistency(scaffold, brief="")
    assert not [i for i in report.issues if i.category == "truncated_stylesheet"]


def test_truncated_stylesheet_tokens_css_never_flagged(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    # tokens.css is a pure :root block of meaningful size — must not be
    # flagged for "too few component rules".
    _write(scaffold / "src" / "tokens.css", _tokens_css() + ("/* pad */\n" * 40))
    report = check_consistency(scaffold, brief="")
    assert not [i for i in report.issues if i.category == "truncated_stylesheet"]


def test_truncated_stylesheet_entry_with_only_root_block_flagged(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    # A big index.css that is just :root and nothing else -> looks truncated.
    big_root = (
        ":root {\n"
        + "".join(f"  --x{i}: {i}px;\n" for i in range(40))
        + "}\n"
    )
    _write(scaffold / "src" / "index.css", big_root)
    report = check_consistency(scaffold, brief="")
    trunc = [i for i in report.issues if i.category == "truncated_stylesheet"]
    assert trunc
    assert trunc[0].severity == "error"


# ── _scan_planned_component_unused ───────────────────────────────────────


def test_planned_component_unused_via_sidecar(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        "import Header from './components/Header.jsx';\n"
        "export default () => <Header />;\n",
    )
    _write(scaffold / "src" / "components" / "Header.jsx", "export default () => <header/>;")
    # Generated then orphaned: KpiTile exists but nobody imports it.
    _write(scaffold / "src" / "components" / "KpiTile.jsx", "export default () => <div/>;")
    sidecar = scaffold / ".skyn3t_planned_imports.json"
    _write(
        sidecar,
        json.dumps({"planned_imports": ["src/components/Header.jsx", "src/components/KpiTile.jsx"]}),
    )
    report = check_consistency(scaffold, brief="")
    unused = [i for i in report.issues if i.category == "planned_component_unused"]
    assert len(unused) == 1
    assert unused[0].file == "src/components/KpiTile.jsx"
    assert unused[0].severity == "warning"


def test_planned_component_unused_noop_without_sidecar(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(scaffold / "src" / "components" / "KpiTile.jsx", "export default () => <div/>;")
    report = check_consistency(scaffold, brief="")
    assert not [i for i in report.issues if i.category == "planned_component_unused"]


def test_planned_component_unused_param_overload(tmp_path: Path) -> None:
    """The scanner accepts planned_imports directly (the code-stage signal)."""
    scaffold = tmp_path / "scaffold"
    _write(scaffold / "src" / "App.jsx", "export default () => <div/>;")
    _write(scaffold / "src" / "components" / "Orphan.jsx", "export default () => <div/>;")
    out = ce._scan_planned_component_unused(
        scaffold.resolve(), planned_imports=["src/components/Orphan.jsx"]
    )
    assert len(out) == 1
    assert out[0].category == "planned_component_unused"
    assert out[0].file == "src/components/Orphan.jsx"


def test_planned_component_used_is_clean(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        "import KpiTile from './components/KpiTile.jsx';\nexport default () => <KpiTile/>;\n",
    )
    _write(scaffold / "src" / "components" / "KpiTile.jsx", "export default () => <div/>;")
    out = ce._scan_planned_component_unused(
        scaffold.resolve(), planned_imports=["src/components/KpiTile.jsx"]
    )
    assert out == []


def test_planned_component_missing_on_disk_not_double_reported(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(scaffold / "src" / "App.jsx", "export default () => <div/>;")
    # Planned but never generated -> NOT a planned_component_unused finding
    # (broken_import covers the import side).
    out = ce._scan_planned_component_unused(
        scaffold.resolve(), planned_imports=["src/components/Ghost.jsx"]
    )
    assert out == []


# ── resilience: a single bad scanner must not crash the gate ─────────────


def test_check_consistency_survives_scanner_exception(tmp_path: Path, monkeypatch) -> None:
    scaffold = tmp_path / "scaffold"
    _write(scaffold / "src" / "App.jsx", "export default () => <div/>;")

    def boom(_scaffold_dir):  # pragma: no cover - exercised via monkeypatch
        raise RuntimeError("scanner blew up")

    monkeypatch.setattr(ce, "_scan_truncated_stylesheet", boom)
    # Must not raise; the other scanners + the core checks still run.
    report = check_consistency(scaffold, brief="")
    assert isinstance(report.issues, list)
