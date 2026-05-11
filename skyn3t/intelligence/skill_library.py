"""Skill library — durable, human-readable artifacts the agent learns.

The lesson scoreboard tracks string lessons; the build-pattern scoreboard
tracks (stack, shape, verdict) tuples. Both are useful but neither is
*readable* — you can't open them in your editor and see "here's what
the system learned about FastAPI."

This module gives the system first-class **skill files**: markdown
documents in ``data/skills/`` keyed by tag, with frontmatter capturing
provenance + success signal. The CodeAgent fix loop writes a skill on
every recovery; the scaffold path reads skills tagged with the current
stack and prepends them to the LLM context.

Skill file shape:

    ---
    name: fastapi-tests-test-health
    tags: [fastapi, build-success]
    success_count: 6
    failure_count: 1
    last_used_at: 1729012345.6
    source: build_pattern_scan
    created_at: 1729000000.0
    ---

    # When building a FastAPI scaffold, always include tests/test_health.py.

    Builds that included this file succeeded 86%; builds that omitted
    it succeeded 17%. The test verifies /health returns 200, which also
    catches missing-import errors at smoke-test time.

    ```python
    from fastapi.testclient import TestClient
    from src.main import app

    def test_health():
        client = TestClient(app)
        r = client.get("/health")
        assert r.status_code == 200
    ```

Design choices:
  - Filenames are slugged from the name: ``fastapi-tests-test-health.md``.
  - Tag-indexed for fast retrieval: ``skills.find(tag="fastapi")``.
  - Atomic write (tmp + os.replace) for durability.
  - Frontmatter is YAML-lite: ``key: value`` lines + list shorthand.
    We don't import yaml — we own the format so we can parse a stable
    subset without the dep.
  - All operations are thread-safe; the same scoreboard pattern as
    LessonScoreboard / BuildPatternScoreboard.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger("skyn3t.intelligence.skill_library")


_SLUG_RX = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    """Filesystem-safe slug. ASCII letters / digits / hyphens only."""
    out = _SLUG_RX.sub("-", (s or "").lower()).strip("-")
    return out[:80] or "skill"


@dataclass
class Skill:
    """A single learned skill. Reads/writes from a markdown file."""

    name: str
    body: str = ""
    tags: List[str] = field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0
    last_used_at: float = field(default_factory=time.time)
    source: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def slug(self) -> str:
        return _slugify(self.name)

    @property
    def score(self) -> float:
        """In [-1, 1]. Zero when no signal."""
        denom = self.success_count + self.failure_count
        if denom == 0:
            return 0.0
        return (self.success_count - self.failure_count) / denom

    def to_markdown(self) -> str:
        """Serialize to the documented skill-file shape."""
        tags_str = "[" + ", ".join(sorted(set(self.tags))) + "]"
        front = (
            "---\n"
            f"name: {self.name}\n"
            f"tags: {tags_str}\n"
            f"success_count: {self.success_count}\n"
            f"failure_count: {self.failure_count}\n"
            f"last_used_at: {self.last_used_at:.1f}\n"
            f"source: {self.source}\n"
            f"created_at: {self.created_at:.1f}\n"
            "---\n\n"
        )
        return front + (self.body or "").rstrip() + "\n"

    @classmethod
    def from_markdown(cls, text: str) -> "Skill":
        """Parse a skill-file. Robust to missing fields; never raises on a
        partially-malformed file (returns best-effort defaults instead)."""
        front, body = _split_frontmatter(text)
        return cls(
            name=str(front.get("name") or "untitled"),
            tags=_parse_list(front.get("tags")),
            success_count=int(front.get("success_count") or 0),
            failure_count=int(front.get("failure_count") or 0),
            last_used_at=float(front.get("last_used_at") or time.time()),
            source=str(front.get("source") or ""),
            created_at=float(front.get("created_at") or time.time()),
            body=body.strip(),
        )


def _split_frontmatter(text: str) -> tuple[Dict[str, str], str]:
    """Return ({key: value} from the leading --- block, body_after_block)."""
    if not text.startswith("---"):
        return {}, text
    try:
        _, front, body = text.split("---", 2)
    except ValueError:
        return {}, text
    parsed: Dict[str, str] = {}
    for line in front.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        parsed[k.strip()] = v.strip()
    return parsed, body


def _parse_list(raw) -> List[str]:
    """Parse `[a, b, c]` or `a, b, c` into a list of strings."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip().strip("[]")
    if not s:
        return []
    return [piece.strip() for piece in s.split(",") if piece.strip()]


class SkillLibrary:
    """Persistent on-disk skill collection. Tag-indexed reads, atomic writes.

    Files live under ``root/`` (default ``data/skills/``). Each ``.md`` file
    is one Skill. The library re-scans the directory on every public read
    so external edits (a human curating a skill file directly) are picked
    up without a restart.
    """

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else Path("data/skills")
        self._lock = threading.Lock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("skill library root creation failed: %s", self.root)

    # ------------------------------------------------------------------
    # Scanning + caching
    # ------------------------------------------------------------------

    def _path_for(self, skill: Skill) -> Path:
        return self.root / f"{skill.slug}.md"

    def _scan(self) -> List[Skill]:
        """Load every .md file in the root, best-effort."""
        out: List[Skill] = []
        try:
            entries = sorted(self.root.glob("*.md"))
        except Exception:
            return out
        for p in entries:
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            try:
                skill = Skill.from_markdown(text)
            except Exception:
                logger.exception("skill parse failed: %s", p)
                continue
            out.append(skill)
        return out

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def all(self) -> List[Skill]:
        """Snapshot every skill currently on disk."""
        with self._lock:
            return self._scan()

    def find(self, *, tag: Optional[str] = None, min_score: float = 0.0,
             limit: int = 5) -> List[Skill]:
        """Return skills matching ``tag`` (case-insensitive), filtered to
        ``score >= min_score``, sorted by score then recency, capped at
        ``limit``. Default min_score=0 means only neutral-or-better skills."""
        tag_lc = (tag or "").strip().lower()
        with self._lock:
            candidates = self._scan()
        if tag_lc:
            candidates = [
                s for s in candidates
                if any(t.lower() == tag_lc for t in s.tags)
            ]
        candidates = [s for s in candidates if s.score >= min_score]
        candidates.sort(key=lambda s: (s.score, s.last_used_at), reverse=True)
        return candidates[: max(0, int(limit))]

    def upsert(self, skill: Skill) -> Path:
        """Write a skill atomically. Returns the path written.

        If a file already exists for this slug, the success/failure counts
        are merged (taking the max of existing+new, in case the caller has
        only seen one outcome), and the `created_at` of the existing file
        is preserved so the timeline is correct.
        """
        path = self._path_for(skill)
        with self._lock:
            existing: Optional[Skill] = None
            if path.exists():
                try:
                    existing = Skill.from_markdown(path.read_text(encoding="utf-8"))
                except Exception:
                    existing = None
            if existing is not None:
                # Preserve provenance; accumulate counts.
                skill.created_at = existing.created_at
                skill.success_count = max(skill.success_count, existing.success_count)
                skill.failure_count = max(skill.failure_count, existing.failure_count)
                # Merge tag sets.
                skill.tags = sorted(set(skill.tags) | set(existing.tags))
            try:
                tmp = path.with_suffix(path.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(skill.to_markdown())
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            except Exception:
                logger.exception("skill upsert failed: %s", path)
        return path

    def record_use(self, name: str, *, success: bool) -> Optional[Skill]:
        """Tick the success/failure count for a skill on disk."""
        slug = _slugify(name)
        path = self.root / f"{slug}.md"
        with self._lock:
            if not path.exists():
                return None
            try:
                skill = Skill.from_markdown(path.read_text(encoding="utf-8"))
            except Exception:
                return None
            if success:
                skill.success_count += 1
            else:
                skill.failure_count += 1
            skill.last_used_at = time.time()
            try:
                tmp = path.with_suffix(path.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(skill.to_markdown())
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            except Exception:
                logger.exception("record_use atomic write failed: %s", path)
                return None
            return skill

    def delete(self, name: str) -> bool:
        """Remove a skill by name. Returns whether anything was deleted."""
        slug = _slugify(name)
        path = self.root / f"{slug}.md"
        with self._lock:
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False
            except Exception:
                logger.exception("skill delete failed: %s", path)
                return False

    def summary(self) -> Dict:
        """Aggregate stats for the dashboard."""
        with self._lock:
            skills = self._scan()
        return {
            "total": len(skills),
            "net_helpful": sum(1 for s in skills if s.score > 0.1),
            "demoted": sum(1 for s in skills if s.score < -0.34),
            "tags": sorted({t for s in skills for t in s.tags}),
        }

    def curate(
        self,
        *,
        max_stale_age_seconds: float = 30 * 86400,
        min_score_for_keep: float = -0.34,
        min_samples_before_demote: int = 3,
        protect_tags: Optional[Iterable[str]] = None,
    ) -> Dict[str, List[str]]:
        """Hermes-style curator pass — drop stale or hurtful skills.

        Removes a skill when EITHER:
          - It hasn't been used in ``max_stale_age_seconds`` (default 30d).
          - Its score is below ``min_score_for_keep`` AND it has at least
            ``min_samples_before_demote`` graded samples.

        Skills tagged with any name in ``protect_tags`` are NEVER removed
        — used by an operator to pin manually-curated skills. The pinned
        set is also auto-detected: any skill with the literal ``pinned``
        tag is preserved regardless of age or score.

        Returns ``{"archived": [...], "kept": [...]}`` so the meta-agent
        can publish a summary event.
        """
        now = time.time()
        protect_set = {t.lower() for t in (protect_tags or [])}
        protect_set.add("pinned")
        archived: List[str] = []
        kept: List[str] = []
        with self._lock:
            for skill in self._scan():
                tag_lc = {t.lower() for t in skill.tags}
                if tag_lc & protect_set:
                    kept.append(skill.name)
                    continue
                stale = (now - skill.last_used_at) > max_stale_age_seconds
                samples = skill.success_count + skill.failure_count
                hurtful = (
                    samples >= min_samples_before_demote
                    and skill.score < min_score_for_keep
                )
                if stale or hurtful:
                    path = self.root / f"{skill.slug}.md"
                    try:
                        path.unlink()
                        archived.append(skill.name)
                    except FileNotFoundError:
                        pass
                    except Exception:
                        logger.exception("curate could not delete %s", path)
                        kept.append(skill.name)
                else:
                    kept.append(skill.name)
        return {"archived": archived, "kept": kept}


# Module-level singleton.
_default_library: Optional[SkillLibrary] = None


def get_default_library() -> SkillLibrary:
    global _default_library
    if _default_library is None:
        _default_library = SkillLibrary()
    return _default_library
