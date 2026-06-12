"""Regression tests for the three runner.py audit fixes (group "runner").

1. C2  — code worktree files are merged back into scaffold/ before the
         post-code checks and verifiers read the (otherwise empty) main
         checkout.
2. HIGH — an intentional post-code bail (UnresolvedScaffoldStubError /
         MissingPlannedFilesError) actually fires _maybe_auto_retry from the
         _run_pipeline outer except instead of dead-ending while claiming
         "Retrying with…".
3. HIGH — _save_manifest writes project.json atomically (tmp + os.replace),
         so a crash mid-write can't truncate an existing manifest.

These all failed before the fixes and pass after.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from skyn3t.studio.runner import StudioRunner, UnresolvedScaffoldStubError
from skyn3t.studio.templates import get_template
from skyn3t.worktree import ensure_worktree


class _FakeBus:
    def publish(self, *args, **kwargs):  # pragma: no cover - inert
        return None


def _make_runner(tmp_path: Path) -> StudioRunner:
    return StudioRunner(event_bus=_FakeBus(), projects_root=tmp_path / "projects")


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10, check=False)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


# ── Fix #1: worktree merge-back ────────────────────────────────────────────
@pytest.mark.skipif(not _git_available(), reason="git not available")
def test_merge_worktree_into_scaffold_lands_generated_files(tmp_path):
    runner = _make_runner(tmp_path)
    artifact_dir = runner.projects_root / "demo"
    scaffold = artifact_dir / "scaffold"
    scaffold.mkdir(parents=True, exist_ok=True)

    # Real worktree, exactly like the code-stage setup does it.
    wt = ensure_worktree(scaffold, track_id="default")

    # CodeAgent writes ONLY into the isolated worktree, not scaffold/.
    (wt.worktree_path / "src").mkdir(parents=True, exist_ok=True)
    (wt.worktree_path / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (wt.worktree_path / "README.md").write_text("# generated\n", encoding="utf-8")

    manifest = {
        "worktree": {
            "track": "default",
            "branch": wt.branch,
            "path": str(wt.worktree_path),
        }
    }

    # Before the fix the generated files never reach scaffold/.
    assert not (scaffold / "src" / "main.py").exists()

    merged = runner._merge_worktree_into_scaffold(artifact_dir, manifest)

    assert merged is True
    assert (scaffold / "src" / "main.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert (scaffold / "README.md").read_text(encoding="utf-8") == "# generated\n"
    # Worktree info is cleared from the manifest and the worktree is gone.
    assert "worktree" not in manifest
    assert not wt.worktree_path.exists()


def test_merge_worktree_no_worktree_is_noop(tmp_path):
    runner = _make_runner(tmp_path)
    artifact_dir = runner.projects_root / "demo2"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    # No "worktree" key → must be a no-op returning False, not a crash.
    assert runner._merge_worktree_into_scaffold(artifact_dir, {}) is False


def test_merge_worktree_copy_fallback_skips_git_metadata(tmp_path):
    """When the scaffold isn't a usable git repo the helper copies the file
    tree and skips .git metadata."""
    runner = _make_runner(tmp_path)
    artifact_dir = runner.projects_root / "demo3"
    scaffold = artifact_dir / "scaffold"
    scaffold.mkdir(parents=True, exist_ok=True)

    wt_path = artifact_dir / ".worktrees" / "default"
    (wt_path / "app").mkdir(parents=True, exist_ok=True)
    (wt_path / "app" / "index.js").write_text("export default 1;\n", encoding="utf-8")
    (wt_path / ".git").mkdir(parents=True, exist_ok=True)
    (wt_path / ".git" / "HEAD").write_text("ref: x\n", encoding="utf-8")

    manifest = {"worktree": {"track": "default", "branch": "", "path": str(wt_path)}}

    merged = runner._merge_worktree_into_scaffold(artifact_dir, manifest)

    assert merged is True
    assert (scaffold / "app" / "index.js").read_text(encoding="utf-8") == "export default 1;\n"
    # .git metadata must not be copied into the delivered scaffold.
    assert not (scaffold / ".git").exists()
    assert "worktree" not in manifest


# ── Fix #3: atomic manifest write ──────────────────────────────────────────
def test_save_manifest_is_atomic(tmp_path):
    runner = _make_runner(tmp_path)
    artifact_dir = runner.projects_root / "atomic"

    runner._save_manifest(artifact_dir, {"slug": "atomic", "status": "running"})

    written = artifact_dir / "project.json"
    assert written.exists()
    data = json.loads(written.read_text(encoding="utf-8"))
    assert data["slug"] == "atomic"
    # No temp file left behind after a successful write.
    assert not (artifact_dir / "project.json.tmp").exists()


def test_save_manifest_writes_via_tmp_then_replace(tmp_path, monkeypatch):
    """The atomic path must render to project.json.tmp and never write the
    final project.json directly.

    We make ``Path.write_text`` blow up for the *final* manifest path only.
    The old non-atomic code wrote straight to ``project.json`` and would
    raise (and on a real crash, truncate); the atomic code writes to
    ``project.json.tmp`` first, so the final-path write is never attempted
    and the call succeeds.
    """
    runner = _make_runner(tmp_path)
    artifact_dir = runner.projects_root / "atomic2"

    orig_write_text = Path.write_text

    def _guarded_write_text(self, *args, **kwargs):
        # Trip only on a direct write to the final manifest — the symptom of
        # the non-atomic write path.
        if self.name == "project.json":
            raise RuntimeError("direct write to project.json is not atomic")
        return orig_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _guarded_write_text)

    # With the fix this succeeds (write hits project.json.tmp, then replace).
    runner._save_manifest(artifact_dir, {"slug": "atomic2", "status": "good"})

    target = artifact_dir / "project.json"
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "good"
    # tmp is renamed away by os.replace, never left behind.
    assert not (artifact_dir / "project.json.tmp").exists()


# ── Fix #2: intentional bail fires auto-retry (in _run_pipeline) ───────────
def _bail_pipeline_kwargs(runner: StudioRunner, slug: str):
    manifest = runner.reserve_project("auto", "build a thing", slug=slug)
    return dict(
        template=get_template("auto"),
        template_key="auto",
        brief="build a thing",
        slug=slug,
        artifact_dir=runner.projects_root / slug,
        manifest=manifest,
        extra={},
    )


@pytest.mark.asyncio
async def test_intentional_bail_triggers_auto_retry(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path)
    kwargs = _bail_pipeline_kwargs(runner, "bailproj")

    # Raise an intentional bail early inside _run_pipeline's outer try.
    def _raise_bail(_manifest):
        raise UnresolvedScaffoldStubError("two stubs remain")

    monkeypatch.setattr(runner, "_init_benchmark", _raise_bail)

    retried: list[str] = []

    async def _spy_retry(manifest, brief, slug):
        retried.append(slug)

    monkeypatch.setattr(runner, "_maybe_auto_retry", _spy_retry)
    monkeypatch.delenv("SKYN3T_AUTO_RETRY", raising=False)

    with pytest.raises(UnresolvedScaffoldStubError):
        await runner._run_pipeline(**kwargs)

    # Before the fix the outer except re-raised without calling
    # _maybe_auto_retry, so the "Retrying with…" next_action was a dead end.
    assert retried == ["bailproj"]


@pytest.mark.asyncio
async def test_intentional_bail_respects_auto_retry_disabled(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path)
    kwargs = _bail_pipeline_kwargs(runner, "bailproj2")

    def _raise_bail(_manifest):
        raise UnresolvedScaffoldStubError("two stubs remain")

    monkeypatch.setattr(runner, "_init_benchmark", _raise_bail)
    monkeypatch.setenv("SKYN3T_AUTO_RETRY", "0")

    retried: list[str] = []

    async def _spy_retry(manifest, brief, slug):
        retried.append(slug)

    monkeypatch.setattr(runner, "_maybe_auto_retry", _spy_retry)

    with pytest.raises(UnresolvedScaffoldStubError):
        await runner._run_pipeline(**kwargs)

    # SKYN3T_AUTO_RETRY=0 must suppress the retry, matching the success path.
    assert retried == []


# ─── Phase 3 critical security regressions (runner path traversal) ────────

def test_validate_slug_rejects_traversal(tmp_path):
    runner = _make_runner(tmp_path)
    for bad in ("../evil", "foo/../bar", "/absolute", "..", ".hidden"):
        with pytest.raises(ValueError):
            runner._validate_slug(bad)


def test_validate_slug_accepts_single_name(tmp_path):
    runner = _make_runner(tmp_path)
    path = runner._validate_slug("my-project_123")
    assert path == runner.projects_root / "my-project_123"


def test_get_project_rejects_traversal_slug(tmp_path):
    runner = _make_runner(tmp_path)
    with pytest.raises(ValueError, match="invalid project slug"):
        runner.get_project("../evil")


def test_export_zip_rejects_traversal_slug(tmp_path):
    runner = _make_runner(tmp_path)
    with pytest.raises(ValueError, match="invalid project slug"):
        runner.export_zip("../evil")
