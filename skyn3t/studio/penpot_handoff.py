"""Penpot-oriented handoff packaging for Studio design artifacts.

This module does NOT try to generate a native `.penpot` project file.
Instead, it builds a small design-tokens package from the artifacts that
SkyN3t already produces (`tokens.json`, `logo.svg`, `brand.md`, etc.) so
operators can move those assets into Penpot with minimal manual work.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

_SCHEMA_VERSION = 1

_FILE_SPECS: List[Dict[str, str]] = [
    {
        "path": "tokens.json",
        "role": "design_tokens",
        "penpot_import_method": "tokens_studio_plugin",
        "description": "Structured design tokens for Penpot token plugins.",
    },
    {
        "path": "logo.svg",
        "role": "logo_asset",
        "penpot_import_method": "direct_import",
        "description": "Vector logo asset ready for import into a Penpot file or shared library.",
    },
    {
        "path": "palette.json",
        "role": "palette_reference",
        "penpot_import_method": "manual_reference",
        "description": "Flat color roles for quickly rebuilding Penpot color styles.",
    },
    {
        "path": "brand.md",
        "role": "brand_direction",
        "penpot_import_method": "reference_doc",
        "description": "Narrative brand guidance for mood, tone, and visual intent.",
    },
    {
        "path": "components.md",
        "role": "component_notes",
        "penpot_import_method": "reference_doc",
        "description": "Component-level guidance for turning the brand kit into reusable UI pieces.",
    },
    {
        "path": "tokens.css",
        "role": "web_reference",
        "penpot_import_method": "reference_doc",
        "description": "Matching CSS variables for engineers implementing the Penpot design.",
    },
    {
        "path": "README.md",
        "role": "usage_guide",
        "penpot_import_method": "reference_doc",
        "description": "Generated usage guide for the brand kit.",
    },
    {
        "path": "brand_voice_guide.md",
        "role": "copy_reference",
        "penpot_import_method": "reference_doc",
        "description": "Optional long-form voice guide for UI copy decisions.",
    },
    {
        "path": "review.md",
        "role": "quality_notes",
        "penpot_import_method": "reference_doc",
        "description": "Optional reviewer notes for the generated kit.",
    },
]


def _artifact_dir(value: Path | str) -> Path:
    return Path(value).expanduser().resolve()


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_fonts(tokens: Dict[str, Any]) -> List[Dict[str, str]]:
    fonts = tokens.get("font")
    if not isinstance(fonts, dict):
        return []
    out: List[Dict[str, str]] = []
    for role in ("heading", "body", "mono"):
        token = fonts.get(role)
        if not isinstance(token, dict):
            continue
        value = str(token.get("value") or "").strip()
        if value:
            out.append({"role": role, "font_family": value})
    return out


def _load_colors(palette: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for role in ("primary", "secondary", "accent", "bg", "text"):
        value = str(palette.get(role) or "").strip()
        if value:
            out.append({"name": role, "value": value})
    return out


def _project_name(artifact_dir: Path) -> str:
    name = artifact_dir.name.replace("-", " ").replace("_", " ").strip()
    return name.title() if name else "SkyN3t Design Kit"


def handoff_files(artifact_dir: Path | str) -> List[Dict[str, str]]:
    base = _artifact_dir(artifact_dir)
    files: List[Dict[str, str]] = []
    for spec in _FILE_SPECS:
        path = base / spec["path"]
        if path.is_file():
            files.append(dict(spec))
    return files


def build_penpot_manifest(artifact_dir: Path | str) -> Dict[str, Any]:
    base = _artifact_dir(artifact_dir)
    palette = _load_json(base / "palette.json")
    tokens = _load_json(base / "tokens.json")
    files = handoff_files(base)
    return {
        "schema_version": _SCHEMA_VERSION,
        "tool_target": "penpot",
        "handoff_kind": "design_tokens_package",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_name": _project_name(base),
        "colors": _load_colors(palette),
        "typography": _load_fonts(tokens),
        "assets": files,
        "import_workflow": [
            "Import logo.svg directly into Penpot if you want a starting logo asset.",
            "Load tokens.json through a Penpot token plugin such as Tokens Studio for Penpot.",
            "Recreate color styles and text styles from palette.json and the typography list when a plugin is unavailable.",
            "Use brand.md and components.md as reference while turning the kit into Penpot components and variants.",
        ],
        "compatibility_notes": [
            "This is a Penpot-oriented handoff package, not a native .penpot project export.",
            "tokens.json is shaped for design-token tooling; plugin support may vary by Penpot plugin version.",
            "Only design files are packaged here; project.json, scaffold/, and other Studio internals are intentionally excluded.",
        ],
        "references": [
            {"name": "Penpot", "url": "https://github.com/penpot/penpot"},
            {"name": "Penpot Files", "url": "https://github.com/penpot/penpot-files"},
        ],
    }


def render_penpot_import_notes(manifest: Dict[str, Any]) -> str:
    out = [
        "# Penpot handoff notes",
        "",
        f"Project: **{manifest.get('project_name') or 'SkyN3t Design Kit'}**",
        "",
        "## Workflow",
        "",
    ]
    for index, step in enumerate(manifest.get("import_workflow") or [], start=1):
        out.append(f"{index}. {step}")
    out.extend(
        [
            "",
            "## Included assets",
            "",
        ]
    )
    for asset in manifest.get("assets") or []:
        out.append(
            f"- `{asset.get('path')}` — {asset.get('description')} "
            f"(method: `{asset.get('penpot_import_method')}`)"
        )
    notes = manifest.get("compatibility_notes") or []
    if notes:
        out.extend(["", "## Compatibility notes", ""])
        out.extend(f"- {note}" for note in notes)
    return "\n".join(out).rstrip() + "\n"


def build_penpot_package(artifact_dir: Path | str) -> bytes:
    base = _artifact_dir(artifact_dir)
    manifest = build_penpot_manifest(base)
    notes = render_penpot_import_notes(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("penpot_manifest.json", json.dumps(manifest, indent=2))
        archive.writestr("penpot_import.md", notes)
        for asset in manifest.get("assets") or []:
            rel_path = str(asset.get("path") or "").strip()
            if not rel_path:
                continue
            source = base / rel_path
            if source.is_file():
                archive.write(source, arcname=rel_path)
    return buffer.getvalue()


def materialize_penpot_handoff(artifact_dir: Path | str) -> List[str]:
    """Write the Penpot handoff files into the project artifact dir.

    Returns the relative paths written. When the project does not yet have
    any design-facing handoff files, this is a no-op so normal non-design
    runs do not grow extra artifacts.
    """
    base = _artifact_dir(artifact_dir)
    if not handoff_files(base):
        return []

    manifest = build_penpot_manifest(base)
    notes = render_penpot_import_notes(manifest)
    package_bytes = build_penpot_package(base)

    manifest_path = base / "penpot_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    notes_path = base / "penpot_import.md"
    notes_path.write_text(notes, encoding="utf-8")

    package_path = base / "penpot-handoff.zip"
    package_path.write_bytes(package_bytes)

    return [
        manifest_path.name,
        notes_path.name,
        package_path.name,
    ]
