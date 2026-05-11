from pathlib import Path
from types import SimpleNamespace

import pytest

import skyn3t.studio.repo_target as repo_target


def test_resolve_repo_target_materializes_github_url_into_managed_checkout(
    tmp_path, monkeypatch
):
    cache_root = tmp_path / "repo-targets"
    clone_calls = []

    def fake_git_root(path: Path):
        path = Path(path)
        return path.resolve() if (path / ".git").exists() else None

    def fake_clone(*, clone_url: str, owner: str, repo: str, destination: Path):
        clone_calls.append((clone_url, owner, repo, destination))
        destination.mkdir(parents=True, exist_ok=True)
        (destination / ".git").mkdir()
        (destination / "README.md").write_text("# demo\n", encoding="utf-8")

    monkeypatch.setattr(repo_target, "MANAGED_REPO_TARGETS_ROOT", cache_root)
    monkeypatch.setattr(repo_target, "_git_root", fake_git_root)
    monkeypatch.setattr(repo_target, "_clone_managed_checkout", fake_clone)
    monkeypatch.setattr(repo_target, "_refresh_managed_checkout", lambda path: None)

    result = repo_target.resolve_repo_target(
        {
            "local_path": "https://github.com/secure-ssid/skyn3t-orchestrator",
            "focus_file": "README.md",
        }
    )

    expected = (cache_root / "github" / "secure-ssid" / "skyn3t-orchestrator").resolve()
    assert result == {
        "local_path": expected.as_posix(),
        "focus_file": "README.md",
    }
    assert clone_calls == [
        (
            "https://github.com/secure-ssid/skyn3t-orchestrator.git",
            "secure-ssid",
            "skyn3t-orchestrator",
            expected,
        )
    ]


def test_resolve_repo_target_reclones_when_managed_checkout_refresh_fails(
    tmp_path, monkeypatch
):
    cache_root = tmp_path / "repo-targets"
    checkout = cache_root / "github" / "secure-ssid" / "skyn3t-orchestrator"
    checkout.mkdir(parents=True)
    (checkout / ".git").mkdir()
    (checkout / "stale.txt").write_text("stale\n", encoding="utf-8")
    clone_calls = []

    def fake_git_root(path: Path):
        path = Path(path)
        return path.resolve() if (path / ".git").exists() else None

    def fake_clone(*, clone_url: str, owner: str, repo: str, destination: Path):
        clone_calls.append(destination)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / ".git").mkdir()
        (destination / "README.md").write_text("# refreshed\n", encoding="utf-8")

    monkeypatch.setattr(repo_target, "MANAGED_REPO_TARGETS_ROOT", cache_root)
    monkeypatch.setattr(repo_target, "_git_root", fake_git_root)
    monkeypatch.setattr(repo_target, "_refresh_managed_checkout", lambda path: "fetch failed")
    monkeypatch.setattr(repo_target, "_clone_managed_checkout", fake_clone)

    result = repo_target.resolve_repo_target(
        {"local_path": "https://github.com/secure-ssid/skyn3t-orchestrator"}
    )

    assert result["local_path"] == checkout.resolve().as_posix()
    assert clone_calls == [checkout.resolve()]
    assert (checkout / "README.md").exists()
    assert not (checkout / "stale.txt").exists()


def test_clone_managed_checkout_falls_back_to_gh_for_private_repo(
    tmp_path, monkeypatch
):
    destination = tmp_path / "repo-targets" / "github" / "secure-ssid" / "skyn3t-orchestrator"
    commands = []

    def fake_run_command(args, *, timeout: int):
        commands.append((args, timeout))
        if args[:4] == ["git", "clone", "--depth", "1"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="authentication failed")
        if args[:4] == ["gh", "repo", "clone", "secure-ssid/skyn3t-orchestrator"]:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / ".git").mkdir()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(repo_target, "_run_command", fake_run_command)
    monkeypatch.setattr(repo_target.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    repo_target._clone_managed_checkout(
        clone_url="https://github.com/secure-ssid/skyn3t-orchestrator.git",
        owner="secure-ssid",
        repo="skyn3t-orchestrator",
        destination=destination,
    )

    assert destination.exists()
    assert [command[0][0] for command in commands] == ["git", "gh"]
    assert commands[1][0] == [
        "gh",
        "repo",
        "clone",
        "secure-ssid/skyn3t-orchestrator",
        str(destination),
        "--",
        "--depth",
        "1",
    ]


def test_resolve_repo_target_rejects_non_repo_github_urls():
    with pytest.raises(
        ValueError,
        match="repo path must point to an existing directory or a supported GitHub repo URL",
    ):
        repo_target.resolve_repo_target(
            {"local_path": "https://github.com/secure-ssid/skyn3t-orchestrator/pull/1"}
        )
