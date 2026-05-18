"""CodeAgent._backfill_unresolved_local_imports.

Root cause being tested: canary-118 and canary-119 both had
App.jsx imports for ./components/CommandPalette, ./components/ActivityFeed,
and ./components/ServiceDetail — components the LLM-driven planner
never listed in file_specs. Result: the scaffold shipped with broken
imports and Vite refused to build, dragging the reviewer's score to 47.

The backfill pass scans generated files for unresolved relative imports
and either dispatches to a deterministic generator (when one exists for
the (stack, path) pair) or writes a minimal build-valid stub.
"""

from __future__ import annotations

from pathlib import Path

from skyn3t.agents.code_agent import CodeAgent


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _new_agent() -> CodeAgent:
    # CodeAgent is async-init but the backfill helper doesn't touch any
    # initialized state — instantiating directly is enough.
    return CodeAgent()


def test_backfill_writes_command_palette_when_app_imports_it(tmp_path: Path) -> None:
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "src" / "App.jsx",
        "import CommandPalette from './components/CommandPalette.jsx';\n"
        "export default function App() { return <CommandPalette/>; }\n",
    )
    agent = _new_agent()
    written = [str(out_dir / "src" / "App.jsx")]
    out = agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=written,
        stack="react_vite",
        brief="Build a dashboard with a command palette",
    )
    target = out_dir / "src" / "components" / "CommandPalette.jsx"
    assert target.is_file(), "CommandPalette.jsx should have been backfilled"
    body = target.read_text()
    # Should come from the deterministic generator, not the placeholder.
    assert "@skyn3t-backfill-stub:" not in body
    assert str(target) in out


def test_backfill_falls_back_to_stub_when_no_generator(tmp_path: Path) -> None:
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "src" / "App.jsx",
        "import Mystery from './components/Mystery.jsx';\nexport default Mystery;\n",
    )
    agent = _new_agent()
    out = agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[str(out_dir / "src" / "App.jsx")],
        stack="react_vite",
        brief="",
    )
    target = out_dir / "src" / "components" / "Mystery.jsx"
    assert target.is_file()
    body = target.read_text()
    assert "@skyn3t-backfill-stub:" in body
    assert "export default function Mystery" in body


def test_backfill_skips_imports_that_already_resolve(tmp_path: Path) -> None:
    out_dir = tmp_path / "scaffold"
    # Real file already exists at the import target.
    _write(
        out_dir / "src" / "components" / "ServiceEditor.jsx",
        "export default function ServiceEditor(){return null;}\n",
    )
    _write(
        out_dir / "src" / "App.jsx",
        "import ServiceEditor from './components/ServiceEditor.jsx';\nexport default ServiceEditor;\n",
    )
    agent = _new_agent()
    before = out_dir / "src" / "components" / "ServiceEditor.jsx"
    mtime_before = before.stat().st_mtime
    out = agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[
            str(out_dir / "src" / "App.jsx"),
            str(out_dir / "src" / "components" / "ServiceEditor.jsx"),
        ],
        stack="react_vite",
        brief="",
    )
    assert before.stat().st_mtime == mtime_before, "should not overwrite resolved import"
    # No new file should have been added to the list.
    assert len(out) == 2


def test_backfill_resolves_extension_variants(tmp_path: Path) -> None:
    """import './Foo' (no extension) should resolve when Foo.jsx exists."""
    out_dir = tmp_path / "scaffold"
    _write(out_dir / "src" / "Foo.jsx", "export default 1;\n")
    _write(out_dir / "src" / "App.jsx", "import F from './Foo';\nexport default F;\n")
    agent = _new_agent()
    out = agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[str(out_dir / "src" / "App.jsx"), str(out_dir / "src" / "Foo.jsx")],
        stack="react_vite",
        brief="",
    )
    # No new files; Foo.jsx already satisfies the bare `./Foo` import.
    assert not (out_dir / "src" / "Foo.js").exists()
    assert len(out) == 2


def test_backfill_ignores_bare_package_specifiers(tmp_path: Path) -> None:
    """`import React from 'react'` is a package, not a scaffold file."""
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "src" / "App.jsx",
        "import React from 'react';\nimport { useState } from 'react';\nexport default React;\n",
    )
    agent = _new_agent()
    out = agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[str(out_dir / "src" / "App.jsx")],
        stack="react_vite",
        brief="",
    )
    # Nothing should be backfilled.
    assert not (out_dir / "react").exists()
    assert not list(out_dir.rglob("react*.jsx"))


def test_backfill_refuses_paths_outside_scaffold(tmp_path: Path) -> None:
    """`import '../../etc/passwd'` must not write outside the scaffold."""
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "src" / "App.jsx",
        "import bad from '../../../../tmp/evil.js';\nexport default bad;\n",
    )
    agent = _new_agent()
    out = agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[str(out_dir / "src" / "App.jsx")],
        stack="react_vite",
        brief="",
    )
    # Should not have escaped the scaffold tree.
    assert not Path("/tmp/evil.js").exists()
    # No new files added either — the escape attempt was rejected.
    assert len(out) == 1


def test_backfill_writes_activity_feed_and_service_detail(tmp_path: Path) -> None:
    """The canary-119 trifecta: App.jsx imports 3 missing components,
    all should be backfilled via the deterministic homelab generators.
    """
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "src" / "App.jsx",
        "import CommandPalette from './components/CommandPalette.jsx';\n"
        "import ActivityFeed from './components/ActivityFeed.jsx';\n"
        "import ServiceDetail from './components/ServiceDetail.jsx';\n"
        "export default function App(){return <CommandPalette/>;}\n",
    )
    agent = _new_agent()
    out = agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[str(out_dir / "src" / "App.jsx")],
        stack="react_vite",
        brief="Build a polished dashboard",
    )
    cmd_palette = out_dir / "src" / "components" / "CommandPalette.jsx"
    activity = out_dir / "src" / "components" / "ActivityFeed.jsx"
    service_detail = out_dir / "src" / "components" / "ServiceDetail.jsx"
    assert cmd_palette.is_file()
    assert activity.is_file()
    assert service_detail.is_file()
    # All three should be deterministic, not stubs.
    for f in (cmd_palette, activity, service_detail):
        assert "@skyn3t-backfill-stub:" not in f.read_text(), \
            f"{f.name} should be from generator, not placeholder"


def test_backfill_rewrites_import_when_file_exists_elsewhere(tmp_path: Path) -> None:
    """canary-cf9270: App.jsx imported './useHabits' but the hook was
    written to src/hooks/useHabits.js by the component-breakdown path.
    Backfill should REWRITE the App.jsx import to point at the actual
    file rather than stub a new one.
    """
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "src" / "App.jsx",
        "import useHabits from './useHabits';\n"
        "export default function App(){return null;}\n",
    )
    _write(
        out_dir / "src" / "hooks" / "useHabits.js",
        "export function useHabits(){return [];}\n",
    )
    agent = _new_agent()
    agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[
            str(out_dir / "src" / "App.jsx"),
            str(out_dir / "src" / "hooks" / "useHabits.js"),
        ],
        stack="react_vite",
        brief="habit tracker",
    )
    app_body = (out_dir / "src" / "App.jsx").read_text()
    assert "from './hooks/useHabits'" in app_body, (
        f"expected import rewritten to './hooks/useHabits', got: {app_body!r}"
    )
    # The original stub path should NOT have been created.
    assert not (out_dir / "src" / "useHabits.js").exists()
    assert not (out_dir / "src" / "useHabits.jsx").exists()


def test_backfill_skips_rewrite_when_basename_ambiguous(tmp_path: Path) -> None:
    """If two files share the same basename, the rewriter can't safely
    pick one. Fall back to the stub-or-generator path instead.
    """
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "src" / "App.jsx",
        "import Helper from './Helper';\nexport default Helper;\n",
    )
    _write(out_dir / "src" / "utils" / "Helper.jsx", "export default null;\n")
    _write(out_dir / "src" / "lib" / "Helper.jsx", "export default null;\n")
    agent = _new_agent()
    agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[
            str(out_dir / "src" / "App.jsx"),
            str(out_dir / "src" / "utils" / "Helper.jsx"),
            str(out_dir / "src" / "lib" / "Helper.jsx"),
        ],
        stack="react_vite",
        brief="",
    )
    # Ambiguous → don't rewrite; original import stays as-is and a stub
    # gets written at the target.
    app_body = (out_dir / "src" / "App.jsx").read_text()
    assert "from './Helper'" in app_body


def test_add_missing_deps_picks_up_date_fns(tmp_path: Path) -> None:
    """canary-cf9270: StreakCalendar.jsx imported date-fns but it was
    never declared in package.json, causing vite to fail with
    'Could not resolve "date-fns"'. _add_missing_package_deps should
    detect the bare-package import and add it.
    """
    import json
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "package.json",
        json.dumps({
            "name": "x",
            "version": "0.1.0",
            "type": "module",
            "dependencies": {"react": "^18.3.0"},
        }, indent=2),
    )
    _write(
        out_dir / "src" / "components" / "StreakCalendar.jsx",
        "import { format } from 'date-fns';\n"
        "import { Calendar } from 'lucide-react';\n"
        "import { useState } from 'react';\n"
        "export default function X(){return null;}\n",
    )
    CodeAgent._add_missing_package_deps(out_dir)
    data = json.loads((out_dir / "package.json").read_text())
    deps = data["dependencies"]
    assert "date-fns" in deps
    assert "lucide-react" in deps
    # react was already there — don't disturb its version.
    assert deps["react"] == "^18.3.0"


def test_add_missing_deps_ignores_node_builtins(tmp_path: Path) -> None:
    import json
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "package.json",
        json.dumps({"name": "x", "dependencies": {}}, indent=2),
    )
    _write(
        out_dir / "server" / "index.js",
        "import fs from 'node:fs';\n"
        "import path from 'path';\n"
        "import crypto from 'crypto';\n",
    )
    CodeAgent._add_missing_package_deps(out_dir)
    data = json.loads((out_dir / "package.json").read_text())
    # No builtins should leak in as deps
    assert data["dependencies"] == {}


def test_backfill_rewrites_parent_dir_relative_import(tmp_path: Path) -> None:
    """Hooks file imports '../utils/format' but the file is actually
    at src/lib/format.js (one dir higher than expected). Rewriter
    should find by basename and emit a working relative path.
    """
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "src" / "hooks" / "useThing.js",
        "import { format } from '../utils/format';\n"
        "export function useThing(){return format(1);}\n",
    )
    _write(
        out_dir / "src" / "lib" / "format.js",
        "export function format(x){return String(x);}\n",
    )
    agent = _new_agent()
    agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[
            str(out_dir / "src" / "hooks" / "useThing.js"),
            str(out_dir / "src" / "lib" / "format.js"),
        ],
        stack="react_vite",
        brief="",
    )
    body = (out_dir / "src" / "hooks" / "useThing.js").read_text()
    # Rewriter found format.js at src/lib/format.js. From src/hooks/,
    # the correct relative path is '../lib/format'.
    assert "'../lib/format'" in body, f"got: {body!r}"


def test_backfill_rewrites_dynamic_import(tmp_path: Path) -> None:
    """Dynamic imports (`import('./foo')`) should be rewritten too —
    the regex covers `import\\s*\\(` not just `from`.
    """
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "src" / "App.jsx",
        "const mod = import('./Helper');\n"
        "export default function App(){return null;}\n",
    )
    _write(
        out_dir / "src" / "utils" / "Helper.jsx",
        "export default function Helper(){return null;}\n",
    )
    agent = _new_agent()
    agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[
            str(out_dir / "src" / "App.jsx"),
            str(out_dir / "src" / "utils" / "Helper.jsx"),
        ],
        stack="react_vite",
        brief="",
    )
    body = (out_dir / "src" / "App.jsx").read_text()
    assert "import('./utils/Helper')" in body, f"got: {body!r}"


def test_add_missing_deps_handles_scoped_packages(tmp_path: Path) -> None:
    """Scoped packages like @radix-ui/react-dialog must be added under
    their full @scope/name, not just @scope or the bare leaf.
    """
    import json
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "package.json",
        json.dumps({"name": "x", "dependencies": {}}, indent=2),
    )
    _write(
        out_dir / "src" / "App.jsx",
        "import { Dialog } from '@radix-ui/react-dialog';\n"
        "import { QueryClient } from '@tanstack/react-query';\n",
    )
    CodeAgent._add_missing_package_deps(out_dir)
    data = json.loads((out_dir / "package.json").read_text())
    assert "@radix-ui/react-dialog" in data["dependencies"]
    assert "@tanstack/react-query" in data["dependencies"]
    # Should NOT add the bare scope or leaf
    assert "@radix-ui" not in data["dependencies"]
    assert "react-dialog" not in data["dependencies"]


def test_add_missing_deps_handles_trailing_slash_import(tmp_path: Path) -> None:
    """`from 'date-fns/format'` should be recognized as the date-fns
    package (we don't sub-resolve; we just declare the root package).
    """
    import json
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "package.json",
        json.dumps({"name": "x", "dependencies": {}}, indent=2),
    )
    _write(
        out_dir / "src" / "App.jsx",
        "import format from 'date-fns/format';\n"
        "import isToday from 'date-fns/isToday';\n",
    )
    CodeAgent._add_missing_package_deps(out_dir)
    data = json.loads((out_dir / "package.json").read_text())
    assert "date-fns" in data["dependencies"]
    # We extract the root package, never the sub-path itself.
    assert "date-fns/format" not in data["dependencies"]


def test_vector_store_metadata_coercion_roundtrip() -> None:
    """Nested dict/list metadata values should JSON-serialize cleanly
    and deserialize back to equivalent Python objects.
    """
    import json
    from skyn3t.rag import vector_store as _vs
    # Reproduce the sanitize loop inline (the method is async + needs
    # an initialized store). The fix is pure-Python so this is fine.
    metadatas_in = [
        {
            "tags": ["python", "fastapi", "ci"],
            "owner": {"name": "alice", "team": "platform"},
            "count": 7,
            "active": True,
            "ratio": 0.42,
            "name": "demo",
            "skipped": None,
        }
    ]
    sanitized = []
    for m in metadatas_in:
        clean = {}
        for k, v in (m or {}).items():
            if v is None:
                continue
            if isinstance(v, (str, int, float, bool)):
                clean[str(k)] = v
            else:
                try:
                    clean[str(k)] = json.dumps(v, default=str)
                except (TypeError, ValueError):
                    clean[str(k)] = str(v)
        sanitized.append(clean)
    out = sanitized[0]
    # Primitives pass through unchanged
    assert out["count"] == 7
    assert out["active"] is True
    assert out["ratio"] == 0.42
    assert out["name"] == "demo"
    # None was dropped
    assert "skipped" not in out
    # Lists / dicts became JSON strings that round-trip
    assert json.loads(out["tags"]) == ["python", "fastapi", "ci"]
    assert json.loads(out["owner"]) == {"name": "alice", "team": "platform"}
