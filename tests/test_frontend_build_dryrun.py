"""Tests for the vite-build dry-run step (proposal #3).

The full subprocess path requires `npm`/`node` and a working install —
not appropriate for unit tests. Here we cover:

- _guess_failed_file: pulling the offending file path out of vite/rollup
  error tails.
- _run_frontend_build_dryrun early-return guards (no frontend, no vite,
  no npm) — these decide whether the expensive subprocess even runs.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from skyn3t.studio.runner import StudioRunner

# ─── _guess_failed_file ────────────────────────────────────────────────


def test_guess_failed_file_finds_relative_path(tmp_path: Path):
    """Vite errors usually look like 'src/foo.jsx:12:5'."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.jsx").write_text("// test")
    err = "ERROR: src/foo.jsx:12:5: Unexpected token"
    result = StudioRunner._guess_failed_file(err, tmp_path)
    assert result == "src/foo.jsx"


def test_guess_failed_file_handles_absolute_path(tmp_path: Path):
    """Some toolchains emit absolute paths; we strip the scaffold prefix."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.jsx").write_text("// test")
    abs_path = str((tmp_path / "src" / "foo.jsx").resolve())
    err = f"error during build: file: {abs_path}"
    result = StudioRunner._guess_failed_file(err, tmp_path)
    assert result == "src/foo.jsx"


def test_guess_failed_file_returns_none_for_unmatched(tmp_path: Path):
    """Random error text without a recognizable file path → None."""
    err = "[plugin:vite] some opaque error message"
    assert StudioRunner._guess_failed_file(err, tmp_path) is None


def test_guess_failed_file_returns_none_when_file_doesnt_exist(tmp_path: Path):
    """Even if the regex matches, only return paths that actually
    exist in the scaffold (defense against false matches like
    'config.js' in error prose)."""
    err = "ERROR: nonexistent.jsx:1:1"
    assert StudioRunner._guess_failed_file(err, tmp_path) is None


def test_guess_failed_file_handles_empty_input(tmp_path: Path):
    assert StudioRunner._guess_failed_file("", tmp_path) is None


def test_guess_failed_file_supports_typescript_extensions(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.tsx").write_text("// test")
    err = "src/main.tsx(8,12): TS2304: Cannot find name 'foo'"
    result = StudioRunner._guess_failed_file(err, tmp_path)
    assert result == "src/main.tsx"


# ─── _run_frontend_build_dryrun early-return guards ────────────────────
# These tests check that the function bails early (no subprocess spawned)
# when prerequisites are missing. We patch asyncio.create_subprocess_exec
# to ensure it's never called in the skip-paths.


@pytest.mark.asyncio
async def test_dryrun_skips_when_no_scaffold_dir(tmp_path: Path):
    runner = StudioRunner.__new__(StudioRunner)
    runner.event_bus = None  # not used on the skip-path
    with patch("asyncio.create_subprocess_exec") as spawn:
        await runner._run_frontend_build_dryrun(
            manifest={},
            artifact_dir=tmp_path,
            scaffold_dir=tmp_path / "does_not_exist",
            brief="x",
        )
        assert not spawn.called


@pytest.mark.asyncio
async def test_dryrun_skips_when_no_index_html(tmp_path: Path):
    """Backend-only scaffold (no index.html) → no vite build needed."""
    runner = StudioRunner.__new__(StudioRunner)
    runner.event_bus = None
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "src").mkdir()
    (scaffold / "src" / "main.jsx").write_text("// no html")
    with patch("asyncio.create_subprocess_exec") as spawn:
        await runner._run_frontend_build_dryrun(
            manifest={}, artifact_dir=tmp_path,
            scaffold_dir=scaffold, brief="x",
        )
        assert not spawn.called


@pytest.mark.asyncio
async def test_dryrun_skips_when_no_src_entry(tmp_path: Path):
    """index.html exists but src/ has no JSX/TSX/JS/TS → no build."""
    runner = StudioRunner.__new__(StudioRunner)
    runner.event_bus = None
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "index.html").write_text("<!doctype html>")
    with patch("asyncio.create_subprocess_exec") as spawn:
        await runner._run_frontend_build_dryrun(
            manifest={}, artifact_dir=tmp_path,
            scaffold_dir=scaffold, brief="x",
        )
        assert not spawn.called


@pytest.mark.asyncio
async def test_dryrun_skips_when_no_package_json(tmp_path: Path):
    """Frontend files present but no package.json → no build."""
    runner = StudioRunner.__new__(StudioRunner)
    runner.event_bus = None
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "index.html").write_text("<!doctype html>")
    (scaffold / "src").mkdir()
    (scaffold / "src" / "main.jsx").write_text("// entry")
    with patch("asyncio.create_subprocess_exec") as spawn:
        await runner._run_frontend_build_dryrun(
            manifest={}, artifact_dir=tmp_path,
            scaffold_dir=scaffold, brief="x",
        )
        assert not spawn.called


@pytest.mark.asyncio
async def test_dryrun_skips_when_vite_not_in_package_json(tmp_path: Path):
    """package.json must contain 'vite' somewhere — otherwise nothing
    to build."""
    runner = StudioRunner.__new__(StudioRunner)
    runner.event_bus = None
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "index.html").write_text("<!doctype html>")
    (scaffold / "src").mkdir()
    (scaffold / "src" / "main.jsx").write_text("// entry")
    (scaffold / "package.json").write_text('{"name": "x"}')  # no "vite"
    with patch("asyncio.create_subprocess_exec") as spawn:
        await runner._run_frontend_build_dryrun(
            manifest={}, artifact_dir=tmp_path,
            scaffold_dir=scaffold, brief="x",
        )
        assert not spawn.called


@pytest.mark.asyncio
async def test_dryrun_skips_when_npm_not_on_path(tmp_path: Path):
    """No npm binary → no build."""
    runner = StudioRunner.__new__(StudioRunner)
    runner.event_bus = None
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "index.html").write_text("<!doctype html>")
    (scaffold / "src").mkdir()
    (scaffold / "src" / "main.jsx").write_text("// entry")
    (scaffold / "package.json").write_text(
        '{"name": "x", "devDependencies": {"vite": "^4"}}'
    )
    # Patch shutil.which to return None for npm/node so the guard fires.
    with patch("shutil.which", return_value=None), \
         patch("asyncio.create_subprocess_exec") as spawn:
        await runner._run_frontend_build_dryrun(
            manifest={}, artifact_dir=tmp_path,
            scaffold_dir=scaffold, brief="x",
        )
        assert not spawn.called
