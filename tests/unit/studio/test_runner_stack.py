"""H22 regression: StudioRunner pins the detected stack onto the manifest so
stage-failure payloads don't report "unknown"."""

import json
from pathlib import Path

from skyn3t.studio.runner import StudioRunner


def test_pin_stack_from_scaffold_detects_react(tmp_path: Path) -> None:
    manifest: dict = {}
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / "package.json").write_text(
        json.dumps({"dependencies": {"vite": "^5.0.0"}})
    )

    stack = StudioRunner._pin_stack_from_scaffold(manifest, tmp_path)

    assert stack == "react_vite"
    assert manifest.get("stack") == "react_vite"


def test_pin_stack_from_scaffold_leaves_existing_stack(tmp_path: Path) -> None:
    manifest = {"stack": "fastapi"}
    stack = StudioRunner._pin_stack_from_scaffold(manifest, tmp_path)
    assert stack == "fastapi"
    assert manifest["stack"] == "fastapi"


def test_pin_stack_from_scaffold_returns_none_when_no_scaffold(tmp_path: Path) -> None:
    manifest: dict = {}
    assert StudioRunner._pin_stack_from_scaffold(manifest, tmp_path) is None
    assert "stack" not in manifest
