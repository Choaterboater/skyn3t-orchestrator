"""Regression tests for two consistency-engine bugs.

Bug 1 (HIGH): ``_IMPORT_RE`` required a trailing semicolon, so it returned
∅ for the very common no-semicolon JS/TS import style that LLMs emit. With
no imports detected, the broken-import + missing-dep checks silently did
nothing and every export got mislabelled as an orphan. Fix: make the
semicolon optional (``;?``) like ``_DETAILED_IMPORT_RE`` already does.

Bug 2 (HIGH): an import resolving ABOVE the scaffold root threw an
unhandled ``ValueError`` from ``Path.relative_to`` — once in the
orphan-export scanner (``check_consistency``) and once in the named-import
scanner (``_scan_for_import_style_mismatch``). The runner callers swallow
the exception, so the ENTIRE consistency pass was silently skipped. Fix:
guard both ``relative_to`` sites with ``try/except ValueError: continue``,
mirroring ``_find_missing_router_mounts``.
"""

from __future__ import annotations

from pathlib import Path

from skyn3t.agents.consistency_engine import (
    _extract_js_imports,
    _scan_for_import_style_mismatch,
    check_consistency,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_import_re_matches_without_semicolon():
    """Bug 1: imports with no trailing semicolon must be extracted."""
    src = (
        "import App from './App'\n"
        "import { useState } from 'react'\n"
        "import './styles.css'\n"
    )
    imports = _extract_js_imports(src)
    # Before the fix none of these matched (every line lacked the `;`).
    assert "./App" in imports
    assert "react" in imports
    assert "./styles.css" in imports


def test_broken_import_detected_without_semicolon(tmp_path):
    """Bug 1: a broken no-semicolon relative import must surface as an error.

    Before the fix, ``_IMPORT_RE`` didn't match the semicolon-less import,
    so the broken_import check never fired.
    """
    scaffold = tmp_path / "scaffold"
    _write(
        scaffold / "src" / "App.jsx",
        # No trailing semicolons — the common LLM output style.
        "import Missing from './Missing'\n"
        "export default function App() { return <Missing /> }\n",
    )

    report = check_consistency(scaffold)
    broken = [
        i for i in report.issues
        if i.category == "broken_import" and "./Missing" in i.message
    ]
    assert broken, "expected a broken_import error for the missing target"


def test_import_above_scaffold_root_does_not_raise(tmp_path):
    """Bug 2: an import resolving above the scaffold root must not raise.

    ``check_consistency`` exercises the orphan-export scanner, and a sibling
    file written above the scaffold root exercises the named-import scanner.
    Before the fix either ``relative_to`` site raised ValueError, which the
    runner swallowed (skipping the whole pass).
    """
    scaffold = tmp_path / "scaffold"
    # A real file ABOVE the scaffold root so named-import resolution finds an
    # existing target that lives outside scaffold_dir.
    _write(tmp_path / "Outside.jsx", "export const Helper = 1\n")
    _write(
        scaffold / "src" / "App.jsx",
        # Default-import the above-root file (orphan-export + broken-import
        # paths) AND named-import it (import-style-mismatch path).
        "import Outside from '../../Outside'\n"
        "import { Helper } from '../../Outside'\n"
        "export default function App() { return Outside }\n",
    )

    # Must not raise ValueError from Path.relative_to.
    report = check_consistency(scaffold)
    assert report is not None

    # The named-import scanner is also reachable directly; build the file
    # index the way check_consistency does and confirm it doesn't raise.
    file_index = {}
    resolved_scaffold = scaffold.resolve()
    for p in resolved_scaffold.rglob("*"):
        if p.is_file():
            rel = p.relative_to(resolved_scaffold).as_posix()
            file_index[rel] = p
            file_index[rel.rsplit(".", 1)[0]] = p
    # Should complete without ValueError.
    _scan_for_import_style_mismatch(resolved_scaffold, file_index)
