from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from skyn3t.studio.penpot_handoff import (
    build_penpot_manifest,
    build_penpot_package,
    handoff_files,
    materialize_penpot_handoff,
)


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_manifest_includes_penpot_workflow_fields(tmp_path: Path):
    _write(
        tmp_path,
        "palette.json",
        json.dumps(
            {
                "primary": "#112233",
                "secondary": "#223344",
                "accent": "#334455",
                "bg": "#0B1020",
                "text": "#F8FAFC",
            }
        ),
    )
    _write(
        tmp_path,
        "tokens.json",
        json.dumps(
            {
                "font": {
                    "heading": {"value": "Inter", "type": "fontFamily"},
                    "body": {"value": "Inter", "type": "fontFamily"},
                    "mono": {"value": "JetBrains Mono", "type": "fontFamily"},
                }
            }
        ),
    )
    _write(tmp_path, "logo.svg", "<svg />")
    _write(tmp_path, "brand.md", "# Brand\n")
    _write(tmp_path, "components.md", "# Components\n")

    manifest = build_penpot_manifest(tmp_path)

    assert manifest["schema_version"] == 1
    assert manifest["tool_target"] == "penpot"
    assert manifest["handoff_kind"] == "design_tokens_package"
    assert manifest["project_name"] == tmp_path.name.replace("-", " ").replace("_", " ").title()
    assert manifest["colors"][0] == {"name": "primary", "value": "#112233"}
    assert manifest["typography"][0] == {"role": "heading", "font_family": "Inter"}
    assert any(asset["path"] == "tokens.json" for asset in manifest["assets"])
    assert manifest["import_workflow"]
    assert manifest["compatibility_notes"]


def test_package_only_includes_design_allowlist(tmp_path: Path):
    _write(tmp_path, "tokens.json", json.dumps({"font": {}}))
    _write(tmp_path, "logo.svg", "<svg />")
    _write(tmp_path, "brand.md", "# Brand\n")
    _write(tmp_path, "project.json", '{"status":"done"}')
    _write(tmp_path, "scaffold/src/App.jsx", "export default function App() { return null; }\n")

    package_bytes = build_penpot_package(tmp_path)

    with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
        names = sorted(archive.namelist())

    assert "penpot_manifest.json" in names
    assert "penpot_import.md" in names
    assert "tokens.json" in names
    assert "logo.svg" in names
    assert "brand.md" in names
    assert "project.json" not in names
    assert "scaffold/src/App.jsx" not in names


def test_handoff_files_skip_missing_optionals(tmp_path: Path):
    _write(tmp_path, "brand.md", "# Brand\n")

    files = handoff_files(tmp_path)
    paths = [entry["path"] for entry in files]

    assert paths == ["brand.md"]


def test_materialize_penpot_handoff_writes_manifest_notes_and_zip(tmp_path: Path):
    _write(tmp_path, "brand.md", "# Brand\n")
    _write(tmp_path, "tokens.json", json.dumps({"font": {}}))

    written = materialize_penpot_handoff(tmp_path)

    assert written == ["penpot_manifest.json", "penpot_import.md", "penpot-handoff.zip"]
    assert (tmp_path / "penpot_manifest.json").is_file()
    assert (tmp_path / "penpot_import.md").is_file()
    with zipfile.ZipFile(tmp_path / "penpot-handoff.zip") as archive:
        names = archive.namelist()
    assert "penpot_manifest.json" in names
    assert "brand.md" in names


def test_materialize_penpot_handoff_noops_without_design_assets(tmp_path: Path):
    _write(tmp_path, "project.json", '{"status":"done"}')

    written = materialize_penpot_handoff(tmp_path)

    assert written == []
    assert not (tmp_path / "penpot_manifest.json").exists()
    assert not (tmp_path / "penpot_import.md").exists()
    assert not (tmp_path / "penpot-handoff.zip").exists()
