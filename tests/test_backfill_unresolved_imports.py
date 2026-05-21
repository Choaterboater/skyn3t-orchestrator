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

import pytest

from skyn3t.agents.code_agent import CodeAgent


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _new_agent() -> CodeAgent:
    # CodeAgent is async-init but the backfill helper doesn't touch any
    # initialized state — instantiating directly is enough.
    return CodeAgent()


@pytest.mark.asyncio
async def test_backfill_writes_command_palette_when_app_imports_it(tmp_path: Path) -> None:
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "src" / "App.jsx",
        "import CommandPalette from './components/CommandPalette.jsx';\n"
        "export default function App() { return <CommandPalette/>; }\n",
    )
    agent = _new_agent()
    written = [str(out_dir / "src" / "App.jsx")]
    out = await agent._backfill_unresolved_local_imports(
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


@pytest.mark.asyncio
async def test_backfill_falls_back_to_stub_when_no_generator(tmp_path: Path) -> None:
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "src" / "App.jsx",
        "import Mystery from './components/Mystery.jsx';\nexport default Mystery;\n",
    )
    agent = _new_agent()
    await agent._backfill_unresolved_local_imports(
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


@pytest.mark.asyncio
async def test_backfill_skips_imports_that_already_resolve(tmp_path: Path) -> None:
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
    out = await agent._backfill_unresolved_local_imports(
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


@pytest.mark.asyncio
async def test_backfill_resolves_extension_variants(tmp_path: Path) -> None:
    """import './Foo' (no extension) should resolve when Foo.jsx exists."""
    out_dir = tmp_path / "scaffold"
    _write(out_dir / "src" / "Foo.jsx", "export default 1;\n")
    _write(out_dir / "src" / "App.jsx", "import F from './Foo';\nexport default F;\n")
    agent = _new_agent()
    out = await agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[str(out_dir / "src" / "App.jsx"), str(out_dir / "src" / "Foo.jsx")],
        stack="react_vite",
        brief="",
    )
    # No new files; Foo.jsx already satisfies the bare `./Foo` import.
    assert not (out_dir / "src" / "Foo.js").exists()
    assert len(out) == 2


@pytest.mark.asyncio
async def test_backfill_ignores_bare_package_specifiers(tmp_path: Path) -> None:
    """`import React from 'react'` is a package, not a scaffold file."""
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "src" / "App.jsx",
        "import React from 'react';\nimport { useState } from 'react';\nexport default React;\n",
    )
    agent = _new_agent()
    await agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[str(out_dir / "src" / "App.jsx")],
        stack="react_vite",
        brief="",
    )
    # Nothing should be backfilled.
    assert not (out_dir / "react").exists()
    assert not list(out_dir.rglob("react*.jsx"))


@pytest.mark.asyncio
async def test_backfill_refuses_paths_outside_scaffold(tmp_path: Path) -> None:
    """`import '../../etc/passwd'` must not write outside the scaffold."""
    out_dir = tmp_path / "scaffold"
    _write(
        out_dir / "src" / "App.jsx",
        "import bad from '../../../../tmp/evil.js';\nexport default bad;\n",
    )
    agent = _new_agent()
    out = await agent._backfill_unresolved_local_imports(
        out_dir=out_dir,
        files_written=[str(out_dir / "src" / "App.jsx")],
        stack="react_vite",
        brief="",
    )
    # Should not have escaped the scaffold tree.
    assert not Path("/tmp/evil.js").exists()
    # No new files added either — the escape attempt was rejected.
    assert len(out) == 1


@pytest.mark.asyncio
async def test_backfill_writes_activity_feed_and_service_detail(tmp_path: Path) -> None:
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
    await agent._backfill_unresolved_local_imports(
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
