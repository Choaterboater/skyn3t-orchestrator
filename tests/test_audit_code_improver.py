"""Regression tests for the self-modifying ``git apply`` path in
:class:`skyn3t.agents.code_improver.CodeImproverAgent`.

The original ``_do_apply`` ran ``git checkout -b`` + ``git apply`` against the
target working tree with:
  * no clean-tree guard / stash  -> a dirty tree could be clobbered and the
    user's uncommitted work lost on rollback;
  * no apply lock                -> concurrent applies corrupt each other;
  * 1s-resolution branch names   -> ``skyn3t/auto/<int(time.time())>`` collides
    when two applies land in the same second.

These tests drive real (tiny, fast) git repos under ``tmp_path`` so the
contract is exercised end-to-end. The temp repos carry no Python/Node/Go/Rust
markers and no ``tests/`` dir, so ``_run_repo_checks`` short-circuits to
"no validation command detected" (ok) and never shells out to pytest.
"""

from __future__ import annotations

import asyncio
import subprocess
import time

from skyn3t.agents import code_improver
from skyn3t.agents.code_improver import CodeImproverAgent


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True,
        capture_output=True, text=True,
    )


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "f.txt").write_text("alpha\nbeta\ngamma\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _patch_changing_beta(target="f.txt"):
    # Minimal unified diff: change "beta" -> "BETA".
    return (
        f"--- a/{target}\n"
        f"+++ b/{target}\n"
        "@@ -1,3 +1,3 @@\n"
        " alpha\n"
        "-beta\n"
        "+BETA\n"
        " gamma\n"
    )


def test_apply_lock_is_module_level():
    """A serialization lock must exist so concurrent applies can't corrupt
    the shared working tree."""
    assert isinstance(code_improver._APPLY_LOCK, asyncio.Lock)


def test_branch_names_are_unique_within_same_second(tmp_path):
    """Two applies that land in the same wall-clock second must produce
    distinct branch names (uuid/pid suffix), not collide on
    ``skyn3t/auto/<int(time.time())>``."""
    agent = CodeImproverAgent()
    repo1 = _make_repo(tmp_path / "a")
    repo2 = _make_repo(tmp_path / "b")

    async def run():
        # Pin time so both applies share the same int(time.time()).
        t = int(time.time())
        orig_time = code_improver.time.time
        code_improver.time.time = lambda: t  # type: ignore[assignment]
        try:
            r1 = await agent._do_apply(
                {"target_file": "f.txt", "patch": _patch_changing_beta(),
                 "repo_root": str(repo1), "rationale": "x"}
            )
            r2 = await agent._do_apply(
                {"target_file": "f.txt", "patch": _patch_changing_beta(),
                 "repo_root": str(repo2), "rationale": "x"}
            )
        finally:
            code_improver.time.time = orig_time  # type: ignore[assignment]
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert r1.get("ok") and r1.get("applied"), r1
    assert r2.get("ok") and r2.get("applied"), r2
    assert r1["branch"] != r2["branch"], (r1["branch"], r2["branch"])


def test_dirty_tree_is_stashed_and_restored(tmp_path):
    """When the tree has uncommitted work, the apply must not clobber it:
    the patch still applies, and the user's uncommitted change is restored
    on the original branch afterwards."""
    agent = CodeImproverAgent()
    repo = tmp_path / "a"
    repo = _make_repo(repo)

    # Introduce uncommitted user work in a *different* file.
    (repo / "user.txt").write_text("user-wip\n")

    async def run():
        return await agent._do_apply(
            {"target_file": "f.txt", "patch": _patch_changing_beta(),
             "repo_root": str(repo), "rationale": "x"}
        )

    res = asyncio.run(run())
    assert res.get("ok") and res.get("applied"), res

    # We should be back on main with the user's uncommitted file intact.
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo),
        capture_output=True, text=True,
    ).stdout.strip()
    assert head == "main", head
    assert (repo / "user.txt").read_text() == "user-wip\n"

    # The patched change lives on the auto branch, not lost.
    branch_content = subprocess.run(
        ["git", "show", f"{res['branch']}:f.txt"], cwd=str(repo),
        capture_output=True, text=True,
    ).stdout
    assert "BETA" in branch_content, branch_content


def test_clean_tree_happy_path_unchanged(tmp_path):
    """The clean-tree happy path is unchanged: apply succeeds, the agent
    stays on the new auto branch, and the change is present in the tree."""
    agent = CodeImproverAgent()
    repo = _make_repo(tmp_path / "a")

    async def run():
        return await agent._do_apply(
            {"target_file": "f.txt", "patch": _patch_changing_beta(),
             "repo_root": str(repo), "rationale": "x"}
        )

    res = asyncio.run(run())
    assert res.get("ok") and res.get("applied"), res
    assert res["branch"].startswith("skyn3t/auto/")
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo),
        capture_output=True, text=True,
    ).stdout.strip()
    assert head == res["branch"], (head, res["branch"])
    assert "BETA" in (repo / "f.txt").read_text()
