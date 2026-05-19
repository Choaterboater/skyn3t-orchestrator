"""Watch for Reviewer outputs and file studio_debug proposals."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict

from skyn3t.config.settings import get_settings
from skyn3t.cortex.review_utils import parse_review_markdown

logger = logging.getLogger("skyn3t.cortex.review_watcher")


class ReviewWatcher:
    """Listens for PROJECT_STAGE_COMPLETED on stage='reviewer' (or any review.md drop)
    and files kind='studio_debug' proposals when verdict isn't a clean go."""

    def __init__(self, event_bus):
        self.event_bus = event_bus
        self._wired = False
        self._seen_path = Path("data/review_watcher_seen.json")
        self._seen: set[str] = self._load_seen()  # slug -> don't re-file forever

    def _load_seen(self) -> set:
        try:
            if self._seen_path.exists():
                import json as _j
                return set(_j.loads(self._seen_path.read_text()))
        except Exception:
            pass
        return set()

    def _save_seen(self) -> None:
        try:
            import json as _j
            self._seen_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write — partial write of a JSON file makes the
            # next load() return an empty set, which causes EVERY
            # previously-reviewed run to be re-flagged as "new" and
            # spam studio_debug proposals.
            tmp = self._seen_path.with_suffix(self._seen_path.suffix + ".tmp")
            tmp.write_text(_j.dumps(sorted(self._seen)))
            tmp.replace(self._seen_path)
        except Exception:
            logger.warning(
                "review_watcher: failed to persist seen-set to %s — "
                "next restart will re-flag every reviewed run",
                self._seen_path, exc_info=True,
            )

    def start(self) -> None:
        if self._wired:
            return
        self._wired = True
        try:
            self.event_bus.subscribe(self._on_event)
        except Exception:
            logger.exception("ReviewWatcher subscribe failed")

    def _on_event(self, event) -> None:
        try:
            payload = getattr(event, "payload", {}) or {}
            kind = payload.get("kind", "")
            # The studio runner publishes SYSTEM_ALERT with kind=PROJECT_STAGE_COMPLETED
            # carrying {"slug","stage","agent"}; we trigger on reviewer stages.
            if kind == "PROJECT_STAGE_COMPLETED" and payload.get("stage") == "reviewer":
                slug = payload.get("slug")
                if slug and slug not in self._seen:
                    asyncio.create_task(self._inspect(slug, payload))

            # Also trigger on PROJECT_COMPLETED (covers projects without an explicit reviewer stage)
            if kind == "PROJECT_COMPLETED":
                slug = payload.get("slug")
                if slug and slug not in self._seen:
                    asyncio.create_task(self._inspect(slug, payload))
        except Exception:
            logger.exception("ReviewWatcher._on_event failed")

    async def _inspect(self, slug: str, payload: Dict[str, Any]) -> None:
        try:
            root = get_settings().projects_dir / slug
            review = root / "review.md"
            if not review.exists():
                return
            text = review.read_text(encoding="utf-8")
            verdict, risks = self._parse(text)
            # Only file when verdict is a hard "no-go" or "blocked".
            # `go-with-fixes` is the default for almost every project — flagging
            # it pops a useless modal every run, so we ignore it silently here.
            v = verdict.lower()
            needs_fix = any(k in v for k in ("no-go", "blocked"))
            if not needs_fix or not risks:
                return
            self._seen.add(slug)
            self._save_seen()
            from skyn3t.cortex import get_store
            primary_artifact = self._guess_target(root)
            get_store().create(
                kind="studio_debug",
                title=f"Reviewer flagged issues in {slug}",
                summary=verdict[:200] or "Reviewer flagged risks.",
                detail=(
                    f"_Project_: `{slug}`\n_Template_: `{payload.get('template','')}`\n\n"
                    f"## Verdict\n{verdict}\n\n## Risks\n"
                    + ("\n".join(f"- {r}" for r in risks) if risks else "(none parsed)")
                    + f"\n\n## Suggested target\n`{primary_artifact}`\n\n"
                    "Approving this will spawn CodeImproverAgent to draft a v2 of the target."
                ),
                payload={
                    "slug": slug,
                    "template": payload.get("template", ""),
                    "target_file": primary_artifact,
                    "verdict": verdict,
                    "risks": risks,
                    "review_path": str(review),
                },
                source="review_watcher",
            )
        except Exception:
            logger.exception("ReviewWatcher._inspect failed for %s", slug)

    def _parse(self, text: str) -> tuple[str, list[str]]:
        verdict, risks = parse_review_markdown(text)
        return verdict, risks[:10]

    def _guess_target(self, root: Path) -> str:
        # prefer landing_copy.md, brand.md, architecture.md, then anything .md non-review
        priority = ["landing_copy.md", "brand.md", "architecture.md", "spec.md", "readme.md", "brainstorm.md"]
        for name in priority:
            p = root / name
            if p.exists():
                return str(p.as_posix())
        for p in root.glob("*.md"):
            if p.name != "review.md":
                return str(p.as_posix())
        return str((root / "review.md").as_posix())
