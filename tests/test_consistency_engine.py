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
