from pathlib import Path

from skyn3t.agents.consistency_engine import check_consistency


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_consistency_flags_unmounted_router(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "server" / "routes" / "config.js",
        """
import express from "express";
const router = express.Router();
router.get("/", (_req, res) => res.json({ ok: true }));
export default router;
""".strip(),
    )
    _write(
        scaffold / "server" / "index.js",
        """
import express from "express";
import configRouter from "./routes/config.js";
const app = express();
app.get("/api/health", (_req, res) => res.json({ ok: true }));
app.listen(3000);
""".strip(),
    )

    report = check_consistency(scaffold, brief="")

    missing_mounts = [i for i in report.issues if i.category == "missing_mount"]
    assert missing_mounts
    assert report.ok is False


def test_consistency_accepts_mounted_router(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "server" / "routes" / "config.js",
        """
import express from "express";
const router = express.Router();
router.get("/", (_req, res) => res.json({ ok: true }));
export default router;
""".strip(),
    )
    _write(
        scaffold / "server" / "index.js",
        """
import express from "express";
import configRouter from "./routes/config.js";
const app = express();
app.use("/api/config", configRouter);
app.listen(3000);
""".strip(),
    )

    report = check_consistency(scaffold, brief="")

    assert not [i for i in report.issues if i.category == "missing_mount"]
    assert report.ok is True


def test_consistency_warns_on_missing_design_quality_basics(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        """
export default function App() {
  return <button>Click</button>;
}
""".strip(),
    )
    _write(
        scaffold / "src" / "styles.css",
        """
body { margin: 0; }
""".strip(),
    )

    report = check_consistency(scaffold, brief="")

    design_issues = [i for i in report.issues if i.category == "design_quality"]
    assert design_issues
    assert any("design-token block" in i.message for i in design_issues)


def test_consistency_passes_design_quality_basics_when_present(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        """
export default function App() {
  const loading = false;
  const error = null;
  const empty = false;
  return <button className="cta">Open</button>;
}
""".strip(),
    )
    _write(
        scaffold / "src" / "styles.css",
        """
:root {
  --color-bg: #111;
  --space-md: 16px;
}
.cta:hover { opacity: .9; }
.cta:focus-visible { outline: 2px solid #fff; }
@media (max-width: 768px) {
  .cta { width: 100%; }
}
""".strip(),
    )

    report = check_consistency(scaffold, brief="")

    assert not [i for i in report.issues if i.category == "design_quality"]


def test_consistency_flags_todo_stub_files(tmp_path: Path) -> None:
    """A scaffold containing 'code generation failed' stubs must fail
    consistency. Verifiers (node --check / vite build / boot) all pass on
    these stubs because they're syntactically valid; this is the only
    layer that catches them."""
    scaffold = tmp_path / "scaffold"
    # Stub written by _placeholder_for in code_agent.py
    _write(
        scaffold / "src" / "App.jsx",
        """// TODO[skyn3t]: code generation failed for src/App.jsx — top-level component.

import { useState } from 'react';

export default function App() {
  const [ready] = useState(false);
  return <div><h1>App</h1><p>Generation failed for this component.</p></div>;
}
""",
    )
    # A real (non-stub) file should NOT be flagged
    _write(
        scaffold / "src" / "components" / "StatusPill.jsx",
        """export default function StatusPill({ tone, children }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}
""",
    )

    report = check_consistency(scaffold, brief="build a react vite app")

    todo_stubs = [i for i in report.issues if i.category == "todo_stub"]
    assert len(todo_stubs) == 1
    assert todo_stubs[0].file == "src/App.jsx"
    assert todo_stubs[0].severity == "error"
    assert not report.ok  # error-class issue must fail the report


def test_consistency_clean_scaffold_has_no_todo_stub_issue(tmp_path: Path) -> None:
    """No stub marker → no todo_stub issue."""
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        """export default function App() { return <h1>Hello</h1>; }\n""",
    )
    report = check_consistency(scaffold, brief="")
    assert not [i for i in report.issues if i.category == "todo_stub"]


def test_consistency_flags_organic_todo_stub_files(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        """// TODO: wire the real dashboard state here
export default function App() {
  return <div>stub</div>;
}
""",
    )

    report = check_consistency(scaffold, brief="build a react vite app")

    todo_stubs = [i for i in report.issues if i.category == "todo_stub"]
    assert len(todo_stubs) == 1
    assert todo_stubs[0].file == "src/App.jsx"
    assert todo_stubs[0].severity == "error"
    assert not report.ok


# ─── default-vs-named import-style mismatch ───────────────────────────


def test_named_import_of_default_export_is_flagged(tmp_path: Path) -> None:
    """Real bug from e79bc0 review: HabitList imports { HabitCard }
    by name, but HabitCard.jsx exports default. Would crash at
    runtime with HabitCard undefined."""
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "components" / "HabitCard.jsx",
        "export default function HabitCard() { return null; }\n",
    )
    _write(
        scaffold / "src" / "components" / "HabitList.jsx",
        """
import { HabitCard } from './HabitCard';
export default function HabitList() {
  return <HabitCard />;
}
""".strip(),
    )

    report = check_consistency(scaffold, brief="")

    mismatches = [
        i for i in report.issues
        if i.category == "broken_import"
        and "HabitCard" in i.message
        and "default export" in i.message
    ]
    assert mismatches, [i.message for i in report.issues]
    assert mismatches[0].severity == "error"
    # Suggestion should point at the fix.
    assert (
        "import HabitCard from" in mismatches[0].suggestion
        or "export { HabitCard }" in mismatches[0].suggestion
    )


def test_named_import_matching_named_export_is_clean(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "utils.js",
        """
export const formatDate = (d) => d.toISOString();
export const parseDate = (s) => new Date(s);
""".strip(),
    )
    _write(
        scaffold / "src" / "App.jsx",
        """
import { formatDate, parseDate } from './utils';
export default function App() {
  return formatDate(new Date());
}
""".strip(),
    )

    report = check_consistency(scaffold, brief="")

    style_issues = [
        i for i in report.issues
        if i.category == "broken_import" and "does not export" in i.message
    ]
    assert not style_issues, [i.message for i in style_issues]


def test_default_import_of_default_export_is_clean(tmp_path: Path) -> None:
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        "export default function App() { return null; }\n",
    )
    _write(
        scaffold / "src" / "main.jsx",
        """
import App from './App';
console.log(App);
""".strip(),
    )

    report = check_consistency(scaffold, brief="")

    style_issues = [
        i for i in report.issues
        if i.category == "broken_import"
        and "does not export" in i.message
    ]
    assert not style_issues


def test_mixed_default_and_named_import_validated(tmp_path: Path) -> None:
    """`import App, { utility } from './app'` — both parts checked.
    Default is fine; the named one is missing → flag only the named."""
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "app.js",
        """
export default function App() { return null; }
export const knownUtil = 1;
""".strip(),
    )
    _write(
        scaffold / "src" / "main.js",
        """
import App, { missingUtil } from './app';
console.log(App, missingUtil);
""".strip(),
    )

    report = check_consistency(scaffold, brief="")

    mismatches = [
        i for i in report.issues
        if i.category == "broken_import" and "missingUtil" in i.message
    ]
    assert mismatches, [i.message for i in report.issues]


def test_aliased_named_import_validated_against_source_name(tmp_path: Path) -> None:
    """`import { Foo as Bar }` — we validate that Foo is exported,
    not Bar (which is the local alias)."""
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "util.js",
        "export const realName = 1;\n",
    )
    _write(
        scaffold / "src" / "main.js",
        "import { realName as localAlias } from './util'; console.log(localAlias);\n",
    )

    report = check_consistency(scaffold, brief="")

    style_issues = [
        i for i in report.issues
        if i.category == "broken_import"
        and ("realName" in i.message or "localAlias" in i.message)
        and "does not export" in i.message
    ]
    assert not style_issues, [i.message for i in style_issues]


def test_module_with_no_detectable_exports_is_silently_skipped(tmp_path: Path) -> None:
    """CommonJS `module.exports = {...}` patterns aren't covered by
    our ES-export regexes. Don't false-flag those."""
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "legacy.js",
        "module.exports = { foo: 1, bar: 2 };\n",
    )
    _write(
        scaffold / "src" / "main.js",
        "import { foo } from './legacy'; console.log(foo);\n",
    )

    report = check_consistency(scaffold, brief="")

    style_issues = [
        i for i in report.issues
        if i.category == "broken_import" and "does not export" in i.message
    ]
    assert not style_issues


def test_re_export_named_block_validated(tmp_path: Path) -> None:
    """`export { Foo as Bar }` re-exports use Bar as the EXTERNALLY
    visible name. Importing { Bar } from this file is valid;
    importing { Foo } is not."""
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "thing.js",
        "function _internal() {} export { _internal as PublicAPI };\n",
    )
    _write(
        scaffold / "src" / "ok.js",
        "import { PublicAPI } from './thing'; console.log(PublicAPI);\n",
    )
    _write(
        scaffold / "src" / "broken.js",
        "import { _internal } from './thing'; console.log(_internal);\n",
    )

    report = check_consistency(scaffold, brief="")

    ok_issues = [
        i for i in report.issues
        if i.file == "src/ok.js"
        and i.category == "broken_import"
        and "does not export" in i.message
    ]
    broken_issues = [
        i for i in report.issues
        if i.file == "src/broken.js"
        and i.category == "broken_import"
        and "_internal" in i.message
    ]
    assert not ok_issues
    assert broken_issues


# ─── @skyn3t-backfill-stub marker detection ───────────────────────────


def test_consistency_flags_backfill_stub_marker(tmp_path: Path) -> None:
    """e79bc0 shipped HabitDashboard.jsx with `// @skyn3t-backfill-stub`
    returning null — undetected by the old stub scanner (which only
    knew about the TODO[skyn3t]: code generation failed marker)."""
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "components" / "HabitDashboard.jsx",
        """
// @skyn3t-backfill-stub: for missing import.
export default function HabitDashboard() {
  return null;
}
""".strip(),
    )

    report = check_consistency(scaffold, brief="")

    stubs = [i for i in report.issues if i.category == "todo_stub"]
    assert stubs, [i.message for i in report.issues]
    assert any("backfill" in i.message.lower() for i in stubs)
    assert stubs[0].severity == "error"
    backfill_issues = [
        i for i in stubs if "backfill stub" in i.message.lower()
    ]
    assert backfill_issues


def test_consistency_does_not_double_flag_backfill_stub_as_organic(tmp_path: Path) -> None:
    """A backfill stub commonly contains the literal word "TODO" in
    surrounding comments. The organic-stub scanner must NOT
    additionally flag a file that's already flagged via the explicit
    marker."""
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "Backfilled.jsx",
        """
// @skyn3t-backfill-stub: for missing import.
// TODO: this is a backfill stub
export default function Backfilled() {
  return null;
}
""".strip(),
    )

    report = check_consistency(scaffold, brief="")

    stubs = [i for i in report.issues if i.category == "todo_stub"]
    assert len(stubs) == 1
    assert "backfill" in stubs[0].message.lower()


def test_consistency_clean_file_no_stub_issue(tmp_path: Path) -> None:
    """Files without either marker stay clean."""
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        """
export default function App() {
  return <div>Hello</div>;
}
""".strip(),
    )

    report = check_consistency(scaffold, brief="")

    stubs = [i for i in report.issues if i.category == "todo_stub"]
    assert not stubs


def test_consistency_still_catches_code_generation_failed_marker(tmp_path: Path) -> None:
    """Regression guard for the existing marker — the new
    `_STUB_MARKERS` tuple must still pick up the legacy one."""
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "Failed.jsx",
        """
// TODO[skyn3t]: code generation failed
export default function Failed() {
  return null;
}
""".strip(),
    )

    report = check_consistency(scaffold, brief="")

    stubs = [i for i in report.issues if i.category == "todo_stub"]
    assert stubs
    assert any("code generation failed" in i.message.lower() for i in stubs)
