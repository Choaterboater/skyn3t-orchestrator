"""Skills Hub — install curated + safe skills from local seed directories.

Hermes/OpenClaw parity path: ship a hub of installable skills under
``examples/skills_seed/`` and optional ``skills/``, auto-install when
no-approval mode is on, and expose CLI/REPL/API install entrypoints.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skyn3t.intelligence.skills_hub")

_SKIP_MD = {"README.md", "readme.md", "INDEX.md", "index.md"}
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_hub_path(raw_path: str | Path) -> Optional[Path]:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()

    repo_root = _REPO_ROOT.resolve()
    candidate = (repo_root / path).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError:
        logger.warning("Ignoring skills hub path that escapes repo root: %s", raw_path)
        return None
    return candidate


def hub_roots() -> List[Path]:
    """Return configured hub directories (repo-relative)."""
    raw = os.environ.get("SKYN3T_SKILLS_HUB_PATHS", "").strip()
    if raw:
        roots: List[Path] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            resolved = _resolve_hub_path(part)
            if resolved is not None:
                roots.append(resolved)
        return roots
    defaults = ["examples/skills_seed", "skills"]
    return [
        resolved
        for raw_default in defaults
        if (resolved := _resolve_hub_path(raw_default)) is not None
    ]


def list_hub_entries() -> Dict[str, Any]:
    """Summarize installable hub content without writing anything."""
    markdown: List[str] = []
    agent_dirs: List[str] = []
    for root in hub_roots():
        if not root.is_dir():
            continue
        for md in sorted(root.glob("*.md")):
            if md.name in _SKIP_MD:
                continue
            markdown.append(str(md))
        for skill_md in sorted(root.rglob("SKILL.md")):
            agent_dirs.append(str(skill_md.parent))
    return {
        "roots": [str(r) for r in hub_roots()],
        "markdown_skills": markdown,
        "agent_skill_dirs": agent_dirs,
        "total": len(markdown) + len(agent_dirs),
    }


def _existing_slugs(lib: Any) -> set[str]:
    return {s.slug for s in lib.all()}


def install_from_hub(
    *,
    only_missing: bool = True,
    reject_unsafe: bool = True,
    source: str = "skills_hub",
) -> Dict[str, Any]:
    """Install skills from all hub roots into ``data/skills/``."""
    from skyn3t.intelligence.skill_library import Skill, get_default_library, scan_skill_markdown

    lib = get_default_library()
    installed: List[str] = []
    skipped: List[str] = []
    flagged: List[str] = []
    present = _existing_slugs(lib) if only_missing else set()

    for root in hub_roots():
        if not root.is_dir():
            continue

        for md_path in sorted(root.glob("*.md")):
            if md_path.name in _SKIP_MD:
                continue
            try:
                text = md_path.read_text(encoding="utf-8")
                skill = Skill.from_markdown(text)
                skill.source = source or skill.source or "skills_hub"
                if only_missing and skill.slug in present:
                    skipped.append(skill.slug)
                    continue
                findings = scan_skill_markdown(text)
                if reject_unsafe and findings:
                    flagged.append(f"{md_path.name}: {', '.join(findings)}")
                    skipped.append(skill.slug)
                    continue
                path = lib.upsert(skill)
                if path:
                    installed.append(path.stem)
                    present.add(skill.slug)
            except Exception:
                logger.exception("hub markdown import failed: %s", md_path)
                skipped.append(md_path.stem)

        batch = lib.import_agent_skills(
            root,
            source=source,
            reject_unsafe=reject_unsafe,
        )
        for name in batch.get("imported") or []:
            if only_missing and name in present and name not in installed:
                continue
            installed.append(name)
            present.add(name)
        skipped.extend(batch.get("skipped") or [])
        flagged.extend(batch.get("flagged") or [])

    return {
        "installed": sorted(set(installed)),
        "skipped": skipped,
        "flagged": flagged,
        "hub": list_hub_entries(),
    }


def auto_approve_safe_skill_drafts() -> Dict[str, Any]:
    """Promote pending skill drafts when no-approval mode is active."""
    from skyn3t.config.settings import auto_approve_enabled, get_settings
    from skyn3t.intelligence.skill_library import get_default_library, scan_skill_markdown

    if not auto_approve_enabled(get_settings()):
        return {"approved": [], "skipped": [], "reason": "approval required"}

    lib = get_default_library()
    approved: List[str] = []
    skipped: List[str] = []
    for draft in lib.all_drafts():
        text = draft.to_markdown()
        findings = scan_skill_markdown(text)
        if findings:
            skipped.append(f"{draft.slug}: {', '.join(findings)}")
            continue
        path = lib.approve_draft(draft.slug)
        if path:
            approved.append(path.stem)
        else:
            skipped.append(draft.slug)
    return {"approved": approved, "skipped": skipped}


def auto_install_hub_if_enabled() -> Optional[Dict[str, Any]]:
    """Boot-time hook: seed hub skills + auto-approve safe drafts."""
    from skyn3t.config.settings import auto_approve_enabled, get_settings

    settings = get_settings()
    if not getattr(settings, "skills_hub_auto_install", True):
        return None
    if not auto_approve_enabled(settings):
        return None

    hub_result = install_from_hub(only_missing=True, reject_unsafe=True)
    draft_result = auto_approve_safe_skill_drafts()
    return {"hub": hub_result, "drafts": draft_result}


def distill_skill_from_build(
    *,
    slug: str,
    brief: str,
    stack: str,
    score: int,
    min_score: int = 85,
) -> Optional[Dict[str, Any]]:
    """Auto-distill a skill draft after a high-scoring successful build (M5).

    Writes to ``data/skills/drafts/`` — promotion to live library stays
    approval-gated unless no-approval mode auto-approves safe drafts.
    """
    if score < min_score:
        return None
    from skyn3t.intelligence.skill_library import Skill, _slugify, get_default_library

    lib = get_default_library()
    stack_tag = (stack or "unknown").strip().lower() or "unknown"
    name = f"{stack_tag}-winning-pattern-{slug[:40]}"
    slugged = _slugify(name)
    existing = {s.slug for s in lib.all()}
    if lib.get_draft(name) or slugged in existing:
        return {"skipped": name, "reason": "already exists"}

    body = (
        f"# Winning build pattern: {slug}\n\n"
        f"Studio build scored **{score}/100** on stack `{stack_tag}`.\n\n"
        f"**Brief (truncated):** {brief[:600]}\n\n"
        "Reuse this scaffold shape, verification gates, and packaging layout "
        "for similar briefs on the same stack."
    )
    skill = Skill(
        name=name,
        body=body,
        description=f"Auto-distilled from successful build {slug} ({score}/100).",
        tags=[stack_tag, "build-success", "auto-distilled"],
        triggers=[stack_tag, "scaffold"],
        success_count=1,
        source="build_distill",
    )
    path = lib.upsert_draft(skill)
    return {"draft": path.stem, "path": str(path)}
