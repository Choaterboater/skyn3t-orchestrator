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

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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
    description: str = ""
    author: str = ""
    tags: List[str] = field(default_factory=list)
    triggers: List[str] = field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0
    last_used_at: float = field(default_factory=time.time)
    source: str = ""
    created_at: float = field(default_factory=time.time)
    memory_doc_id: str = ""

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
        triggers_str = "[" + ", ".join(sorted(set(self.triggers))) + "]"
        front_lines = [
            "---",
            f"name: {self.name}",
        ]
        if self.author:
            front_lines.append(f"author: {self.author}")
        if self.description:
            front_lines.append(f"description: {self.description}")
        if self.memory_doc_id:
            front_lines.append(f"memory_doc_id: {self.memory_doc_id}")
        front_lines.append(f"tags: {tags_str}")
        if self.triggers:
            front_lines.append(f"triggers: {triggers_str}")
        front_lines.extend(
            [
                f"success_count: {self.success_count}",
                f"failure_count: {self.failure_count}",
                f"last_used_at: {self.last_used_at:.1f}",
                f"source: {self.source}",
                f"created_at: {self.created_at:.1f}",
                "---",
                "",
            ]
        )
        front = "\n".join(front_lines)
        return front + (self.body or "").rstrip() + "\n"

    @classmethod
    def from_markdown(cls, text: str) -> "Skill":
        """Parse a skill-file. Robust to missing fields; never raises on a
        partially-malformed file (returns best-effort defaults instead)."""
        front, body = _split_frontmatter(text)
        return cls(
            name=str(front.get("name") or "untitled"),
            author=str(front.get("author") or ""),
            description=str(front.get("description") or ""),
            tags=_parse_list(front.get("tags")),
            triggers=_parse_list(front.get("triggers")),
            success_count=int(front.get("success_count") or 0),
            failure_count=int(front.get("failure_count") or 0),
            last_used_at=float(front.get("last_used_at") or time.time()),
            source=str(front.get("source") or ""),
            created_at=float(front.get("created_at") or time.time()),
            memory_doc_id=str(front.get("memory_doc_id") or ""),
            body=body.strip(),
        )

    @classmethod
    def from_agent_skill_markdown(
        cls,
        text: str,
        *,
        source: str = "agent_skills_import",
        fallback_name: str = "untitled",
        extra_tags: Optional[Iterable[str]] = None,
    ) -> "Skill":
        """Parse an Agent Skills / SKILL.md file into the local Skill shape."""
        front, body = _split_frontmatter(text)
        description = str(front.get("description") or "").strip()
        tags = sorted(set(_parse_list(front.get("tags"))) | set(extra_tags or []))
        triggers = _parse_list(front.get("triggers"))
        if not triggers and description:
            triggers = _extract_triggers_from_description(description)
        return cls(
            name=str(front.get("name") or fallback_name or "untitled"),
            author=str(front.get("author") or ""),
            description=description,
            tags=tags,
            triggers=triggers,
            source=source,
            body=body.strip(),
        )

    def relevance(self, query: str) -> float:
        """Cheap metadata/body match score for trigger-aware retrieval."""
        tokens = _query_tokens(query)
        if not tokens:
            return self.score
        score = self.score
        name_lc = self.name.lower()
        desc_lc = self.description.lower()
        body_lc = self.body.lower()
        tags_lc = {t.lower() for t in self.tags}
        triggers_lc = [t.lower() for t in self.triggers]
        for token in tokens:
            if token in tags_lc:
                score += 3.0
            if token in name_lc:
                score += 2.0
            if token in desc_lc:
                score += 1.5
            if any(token in trig for trig in triggers_lc):
                score += 2.5
            if token in body_lc:
                score += 0.5
        return score


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


def _query_tokens(text: str) -> List[str]:
    return [tok for tok in re.findall(r"[a-z0-9_./+-]{3,}", (text or "").lower())]


def _extract_triggers_from_description(description: str) -> List[str]:
    """Best-effort extraction of trigger phrases from Agent Skills descriptions."""
    if not description:
        return []
    text = description.strip()
    candidates: List[str] = []
    patterns = [
        r"whenever the user mentions (.+?)(?:\.| also trigger|$)",
        r"also trigger when (.+?)(?:\.|$)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            chunk = m.group(1).strip()
            chunk = re.sub(r"\bor\b", ",", chunk, flags=re.IGNORECASE)
            for piece in chunk.split(","):
                cleaned = piece.strip(" `\"'.()")
                cleaned = re.sub(
                    r"^(?:the user mentions|user mentions|code imports|imports|references)\s+",
                    "",
                    cleaned,
                    flags=re.IGNORECASE,
                )
                if cleaned:
                    candidates.append(cleaned)
    seen: set[str] = set()
    out: List[str] = []
    for cand in candidates:
        key = cand.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out


_UNSAFE_SKILL_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bcurl\b[^\n|]{0,200}\|\s*(?:bash|sh)\b", "shell-pipe-download"),
    (r"\bwget\b[^\n|]{0,200}\|\s*(?:bash|sh)\b", "shell-pipe-download"),
    (r"\brm\s+-rf\s+/(?:\s|$)", "destructive-rm-root"),
    (r"\bsudo\s+", "privileged-command"),
    (r"\beval\s+\$", "dynamic-shell-eval"),
    (r"\bos\.system\(", "python-os-system"),
    (r"\bsubprocess\.(?:Popen|run)\([^)]*shell\s*=\s*True", "python-shell-true"),
)


def scan_skill_markdown(text: str) -> List[str]:
    """Return simple rule ids for dangerous patterns in a skill file."""
    hits: List[str] = []
    for pattern, rule_id in _UNSAFE_SKILL_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
            hits.append(rule_id)
    return hits


class SkillLibrary:
    """Persistent on-disk skill collection. Tag-indexed reads, atomic writes.

    Files live under ``root/`` (default ``data/skills/``). Each ``.md`` file
    is one Skill. The library re-scans the directory on every public read
    so external edits (a human curating a skill file directly) are picked
    up without a restart.
    """

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else Path("data/skills")
        self.drafts_root = self.root / "drafts"
        self._lock = threading.Lock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.drafts_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("skill library root creation failed: %s", self.root)

    # ------------------------------------------------------------------
    # Scanning + caching
    # ------------------------------------------------------------------

    def _path_for(self, skill: Skill) -> Path:
        return self.root / f"{skill.slug}.md"

    def _draft_path_for(self, skill: Skill) -> Path:
        return self.drafts_root / f"{skill.slug}.md"

    def _scan_dir(self, root: Path) -> List[Skill]:
        """Load every .md file in the root, best-effort."""
        out: List[Skill] = []
        try:
            entries = sorted(root.glob("*.md"))
        except Exception:
            return out
        # Documentation files in the skills dir aren't skills; they
        # have no frontmatter and would otherwise land as untitled
        # records that clutter the registry and confuse the curator.
        _skip_names = {"README.md", "readme.md", "INDEX.md", "index.md"}
        for p in entries:
            if p.name in _skip_names:
                continue
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

    def _scan(self) -> List[Skill]:
        return self._scan_dir(self.root)

    def _write_skill(self, path: Path, skill: Skill) -> Path:
        existing: Optional[Skill] = None
        if path.exists():
            try:
                existing = Skill.from_markdown(path.read_text(encoding="utf-8"))
            except Exception:
                existing = None
        if existing is not None:
            skill.created_at = existing.created_at
            skill.success_count = max(skill.success_count, existing.success_count)
            skill.failure_count = max(skill.failure_count, existing.failure_count)
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def all(self) -> List[Skill]:
        """Snapshot every skill currently on disk."""
        with self._lock:
            return self._scan()

    def all_drafts(self) -> List[Skill]:
        """Snapshot every pending skill draft."""
        with self._lock:
            drafts = self._scan_dir(self.drafts_root)
        drafts.sort(key=lambda s: s.created_at, reverse=True)
        return drafts

    def get_draft(self, name: str) -> Optional[Skill]:
        """Return one skill draft by name or slug."""
        slug = _slugify(name)
        path = self.drafts_root / f"{slug}.md"
        with self._lock:
            if not path.exists():
                return None
            try:
                return Skill.from_markdown(path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("draft skill parse failed: %s", path)
                return None

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

    def find_relevant(
        self,
        query: str,
        *,
        min_score: float = -1.0,
        limit: int = 5,
    ) -> List[Skill]:
        """Return skills ranked by metadata/body relevance to ``query``."""
        with self._lock:
            candidates = self._scan()
        candidates = [s for s in candidates if s.score >= min_score]
        candidates.sort(key=lambda s: (s.relevance(query), s.last_used_at), reverse=True)
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
            return self._write_skill(path, skill)

    def upsert_draft(self, skill: Skill) -> Path:
        """Write a pending skill draft without making it live."""
        path = self._draft_path_for(skill)
        with self._lock:
            return self._write_skill(path, skill)

    def approve_draft(self, name: str) -> Optional[Path]:
        """Promote a draft into the live library."""
        slug = _slugify(name)
        draft_path = self.drafts_root / f"{slug}.md"
        with self._lock:
            if not draft_path.exists():
                return None
            try:
                skill = Skill.from_markdown(draft_path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("draft skill parse failed: %s", draft_path)
                return None
            path = self._write_skill(self.root / f"{slug}.md", skill)
            try:
                draft_path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                logger.exception("draft skill cleanup failed: %s", draft_path)
            return path

    def reject_draft(self, name: str) -> bool:
        """Delete a pending skill draft."""
        slug = _slugify(name)
        path = self.drafts_root / f"{slug}.md"
        with self._lock:
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False
            except Exception:
                logger.exception("draft skill delete failed: %s", path)
                return False

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

    def import_agent_skill(
        self,
        skill_dir: Path | str,
        *,
        source: str = "agent_skills_import",
        reject_unsafe: bool = True,
    ) -> tuple[Optional[Path], List[str]]:
        """Import one Agent Skills standard directory containing ``SKILL.md``."""
        skill_dir = Path(skill_dir)
        skill_file = skill_dir / "SKILL.md"
        text = skill_file.read_text(encoding="utf-8")
        findings = scan_skill_markdown(text)
        if reject_unsafe and findings:
            return None, findings
        extra_tags = {"agent-skill", skill_dir.name}
        skill = Skill.from_agent_skill_markdown(
            text,
            source=source,
            fallback_name=skill_dir.name,
            extra_tags=extra_tags,
        )
        return self.upsert(skill), findings

    def import_agent_skills(
        self,
        root: Path | str,
        *,
        source: str = "agent_skills_import",
        reject_unsafe: bool = True,
    ) -> Dict[str, List[str]]:
        """Bulk-import a tree of Agent Skills directories."""
        root = Path(root)
        imported: List[str] = []
        skipped: List[str] = []
        flagged: List[str] = []
        for skill_file in sorted(root.rglob("SKILL.md")):
            try:
                path, findings = self.import_agent_skill(
                    skill_file.parent,
                    source=source,
                    reject_unsafe=reject_unsafe,
                )
            except Exception:
                logger.exception("agent skill import failed: %s", skill_file)
                skipped.append(str(skill_file.parent))
                continue
            if findings:
                flagged.append(f"{skill_file.parent.name}: {', '.join(findings)}")
            if path is None:
                skipped.append(str(skill_file.parent))
            else:
                imported.append(path.stem)
        return {"imported": imported, "skipped": skipped, "flagged": flagged}

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


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if "," in text:
        return [piece.strip() for piece in text.split(",") if piece.strip()]
    return [text]


def _memory_guidance(doc_type: str, content: str) -> str:
    stripped_lines = [line.strip() for line in (content or "").splitlines() if line.strip()]
    if not stripped_lines:
        return "Review the source memory before using this skill."
    if doc_type == "insight":
        for line in stripped_lines:
            if line.startswith("Insight:"):
                return line.partition(":")[2].strip()
    if doc_type == "pattern":
        description = ""
        fix = ""
        eval_ideas: List[str] = []
        collecting_eval = False
        for line in stripped_lines:
            if line.startswith("Description:"):
                description = line.partition(":")[2].strip()
            elif line.startswith("Suggested Fix:"):
                fix = line.partition(":")[2].strip()
            elif line == "Evaluation Ideas:":
                collecting_eval = True
                continue
            elif collecting_eval and line.startswith("-"):
                eval_ideas.append(line[1:].strip())
            elif collecting_eval and not line.startswith("-"):
                collecting_eval = False
        bits = [bit for bit in [description, fix] if bit]
        if eval_ideas:
            bits.append("Evaluation ideas:\n" + "\n".join(f"- {item}" for item in eval_ideas))
        if bits:
            return "\n\n".join(bits)
    if doc_type == "lesson":
        suggestions: List[str] = []
        collecting = False
        for line in stripped_lines:
            if line == "Suggestions:":
                collecting = True
                continue
            if collecting and line.startswith("-"):
                suggestions.append(line[1:].strip())
        if suggestions:
            return "\n".join(f"- {item}" for item in suggestions)
    return "\n".join(stripped_lines[:6])


def _external_learning_guidance(doc: Dict[str, Any], meta: Dict[str, Any]) -> str:
    repo = str(meta.get("repo") or doc.get("title") or "the external repo").strip()
    lane = str(meta.get("lane") or "fit").strip() or "fit"
    query = str(meta.get("query") or "").strip()
    language = str(meta.get("language") or "unknown").strip() or "unknown"
    reuse_risk = str(meta.get("reuse_risk") or "unknown").strip() or "unknown"
    topics = _as_list(meta.get("topics"))
    docs = _as_list(meta.get("external_doc_paths_ingested"))

    context_bits = [bit for bit in [query, ", ".join(topics[:4]), language] if bit]
    focus = " / ".join(context_bits) if context_bits else "its strongest patterns"
    guidance = (
        f"Use `{repo}` as inspiration for {lane} work around {focus}. "
        "Adapt the underlying pattern into SkyN3t's architecture instead of copying implementation details."
    )
    if docs:
        guidance += f" Start with: {', '.join(docs[:3])}."
    if reuse_risk == "high":
        guidance += " License risk is high, so treat it as reference-only and avoid close code/text reuse."
    elif reuse_risk == "medium":
        guidance += " Check provenance carefully before lifting any structure or wording."
    return guidance


def _external_learning_body(
    *,
    doc: Dict[str, Any],
    meta: Dict[str, Any],
    doc_id: str,
    title: str,
) -> str:
    repo = str(meta.get("repo") or "unknown").strip() or "unknown"
    repo_url = str(meta.get("repo_url") or "unknown").strip() or "unknown"
    lane = str(meta.get("lane") or "fit").strip() or "fit"
    query = str(meta.get("query") or "none").strip() or "none"
    language = str(meta.get("language") or "unknown").strip() or "unknown"
    license_name = str(meta.get("license") or "unknown").strip() or "unknown"
    reuse_risk = str(meta.get("reuse_risk") or "unknown").strip() or "unknown"
    selection_reason = str(meta.get("selection_reason") or lane).strip() or lane
    docs = _as_list(meta.get("external_doc_paths_ingested"))
    topics = _as_list(meta.get("topics"))
    confidence = meta.get("confidence")
    guidance = _external_learning_guidance(doc, meta)
    review_targets = "\n".join(f"- `{path}`" for path in docs) if docs else "- No approved docs were captured."
    topics_text = ", ".join(topics) if topics else "none"
    source_memory = str(doc.get("content") or "").strip()
    return (
        f"# {title}\n\n"
        "## Inspiration guidance\n\n"
        f"{guidance}\n\n"
        "## Adaptation rules\n\n"
        "- Borrow the pattern, not the code.\n"
        "- Rebuild the idea in SkyN3t's own architecture, naming, and UX.\n"
        f"- License: `{license_name}`\n"
        f"- Reuse risk: `{reuse_risk}`\n\n"
        "## Review targets\n\n"
        f"{review_targets}\n\n"
        "## Fit signals\n\n"
        f"- Repo: `{repo}`\n"
        f"- URL: {repo_url}\n"
        f"- Lane: `{lane}`\n"
        f"- Query: `{query}`\n"
        f"- Language: `{language}`\n"
        f"- Topics: {topics_text}\n"
        f"- Why selected: {selection_reason}\n\n"
        "## Provenance\n\n"
        f"- Memory document: `{doc_id or 'unknown'}`\n"
        f"- Memory type: `external_learning`\n"
        f"- Source: `{doc.get('source') or 'unknown'}`\n"
        f"- Review status: `{meta.get('review_status') or 'unknown'}`\n"
        f"- Confidence: `{confidence if confidence is not None else 'unknown'}`\n\n"
        "## Source memory\n\n"
        "```text\n"
        f"{source_memory}\n"
        "```\n"
    )


def skill_from_memory_doc(
    doc: Dict[str, Any],
    *,
    source: str = "memory_promotion",
) -> Skill:
    """Derive a pending skill draft from an approved memory document."""
    meta = dict(doc.get("meta") or {})
    doc_id = str(doc.get("id") or "").strip()
    doc_type = str(doc.get("doc_type") or "memory").strip() or "memory"
    title = str(doc.get("title") or f"{doc_type.title()} skill").strip()
    suffix = (doc_id.replace("-", "")[:8] or str(int(time.time())))
    name = f"{title} {suffix}"
    guidance = _memory_guidance(doc_type, str(doc.get("content") or ""))

    tags = {
        "memory-promoted",
        "governed-memory",
        doc_type.lower(),
    }
    for raw in (
        meta.get("memory_layer"),
        meta.get("capability"),
        meta.get("source_platform"),
        meta.get("agent"),
        meta.get("agent_name"),
    ):
        for item in _as_list(raw):
            tags.add(_slugify(item))
    for item in _as_list(meta.get("patterns")):
        tags.add(_slugify(item))
    if meta.get("external_pattern"):
        tags.update({"external-pattern", "adaptation-skill"})
    if doc_type == "external_learning":
        tags.update({"external-learning", "adaptation-skill"})
    for raw in (
        meta.get("language"),
        meta.get("lane"),
    ):
        for item in _as_list(raw):
            tags.add(_slugify(item))
    for item in _as_list(meta.get("topics")):
        tags.add(_slugify(item))

    triggers: List[str] = []
    for raw in (
        meta.get("capability"),
        meta.get("pattern_name"),
        meta.get("agent"),
        meta.get("agent_name"),
        title,
    ):
        for item in _as_list(raw):
            if item and item not in triggers:
                triggers.append(item)
    for raw in (
        meta.get("query"),
        meta.get("repo"),
        meta.get("source_repos"),
        meta.get("lane"),
        meta.get("language"),
    ):
        for item in _as_list(raw):
            if item and item not in triggers:
                triggers.append(item)
    for item in _as_list(meta.get("topics")):
        if item and item not in triggers:
            triggers.append(item)

    confidence = meta.get("confidence")
    if doc_type == "external_learning":
        body = _external_learning_body(doc=doc, meta=meta, doc_id=doc_id, title=title)
        description = "Adapt a pattern inspired by approved external repo learning."
    else:
        body = (
            f"# {title}\n\n"
            "## Guidance\n\n"
            f"{guidance}\n\n"
            "## Provenance\n\n"
            f"- Memory document: `{doc_id or 'unknown'}`\n"
            f"- Memory type: `{doc_type}`\n"
            f"- Source: `{doc.get('source') or 'unknown'}`\n"
            f"- Review status: `{meta.get('review_status') or 'unknown'}`\n"
            f"- Confidence: `{confidence if confidence is not None else 'unknown'}`\n\n"
            "## Source memory\n\n"
            "```text\n"
            f"{str(doc.get('content') or '').strip()}\n"
            "```\n"
        )
        description = f"Promoted from approved {doc_type} memory."
    return Skill(
        name=name,
        description=description,
        tags=sorted(tag for tag in tags if tag),
        triggers=triggers,
        source=source,
        body=body,
        memory_doc_id=doc_id,
    )
