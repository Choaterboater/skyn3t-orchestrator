"""Learnings Store — a distilled, callable knowledge layer over what SkyN3t has
learned, meant to replace noisy chunk-RAG for "how should I build this?".

It compiles the *curated* signal the system already produces — top-graded skills,
winning build-pattern shapes, confirmed lessons — into a compact playbook, and
exposes ``ask()`` which grounds a FREE local model (Ollama, e.g. gemma3:4b) in
the relevant slice. The local model is the "micro-LLM" the owner wanted: it calls
from what the system has learned, at $0 and fully private.

Storage is path-configurable via ``SKYN3T_LEARNINGS_DIR`` so the corpus (and,
later, fine-tune datasets / model weights) can live on a NAS as it grows. The
same compiled corpus is the training set for a real fine-tuned micro-LLM later.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skyn3t.intelligence.learnings_store")

DEFAULT_DIR = "data/learnings"
DEFAULT_OLLAMA_MODEL = "gemma3:4b"
OLLAMA_URL = "http://localhost:11434/api/generate"
PLAYBOOK_SKILL_MIN_SCORE = -0.5
_UNSAFE_PLAYBOOK_TAGS = {
    "malicious_skill",
    "mcp_mismatched_skill",
    "mcp_overprivileged_skill",
    "mcp_poisoned_tool",
    "mcp_underdeclared_skill",
    "sdi1_mismatch",
    "sdi2_inappropriate",
    "sdi3_scope_creep",
    "sdi4_divergence",
    "sqp1_vague_triggers",
    "sqp2_missing_warnings",
    "sqp3_locale_forcing",
    "ssd1_semantic_injection",
    "ssd2_novel_phrasing",
    "ssd3_nl_exfiltration",
    "ssd4_narrative_deception",
}


def learnings_dir() -> Path:
    """Where the compiled corpus lives. Point at a NAS via SKYN3T_LEARNINGS_DIR."""
    raw = os.environ.get("SKYN3T_LEARNINGS_DIR", "").strip()
    if raw:
        return Path(raw)
    try:
        from skyn3t.config.settings import get_settings

        return Path(getattr(get_settings(), "learnings_dir", DEFAULT_DIR))
    except Exception:
        return Path(DEFAULT_DIR)


class LearningsStore:
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else learnings_dir()
        self.json_path = self.root / "playbook.json"
        self.md_path = self.root / "playbook.md"

    # ── compile ──────────────────────────────────────────────────────────
    def compile(
        self,
        *,
        library: Any = None,
        prefs_path: Optional[Path] = None,
        min_skill_score: float = -0.5,
        data_dir: str = "data",
    ) -> int:
        """Gather curated learnings into the store. Returns the entry count.

        Pulls from EVERY signal the system already produces so the corpus grows
        as the system runs: skills, winning build shapes, model-tournament
        winners per task, and per-stack build success rates.
        """
        d = Path(data_dir)
        entries: List[Dict[str, Any]] = []
        entries.extend(self._skill_entries(library, min_skill_score))
        entries.extend(self._build_pattern_entries(prefs_path))
        entries.extend(self._model_tournament_entries(d / "model_tournament.json"))
        entries.extend(self._build_success_entries(d / "build_success_rate.json"))
        self._write(entries)
        return len(entries)

    def _model_tournament_entries(self, path: Path) -> List[Dict[str, Any]]:
        """Best PASSING model per task-domain from the tournament (Claude/CLI excl)."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return []
        trials = data.get("trials") if isinstance(data, dict) else None
        if not isinstance(trials, list):
            return []
        best: Dict[str, Any] = {}
        for t in trials:
            if not isinstance(t, dict) or not t.get("passed"):
                continue
            mid = str(t.get("model_id") or "")
            low = mid.lower()
            if not mid or "claude" in low or low in {"sonnet", "opus", "haiku", "fable"}:
                continue
            score = float(t.get("score") or 0.0)
            for tag in (t.get("domain_tags") or ["general"]):
                cur = best.get(tag)
                if cur is None or score > cur[1]:
                    best[tag] = (mid, score, float(t.get("quality_per_dollar") or 0.0))
        out = []
        for tag, (mid, score, qpd) in best.items():
            out.append({
                "kind": "model_winner", "key": f"model:{tag}",
                "title": f"Best model for {tag} tasks",
                "content": (
                    f"For {tag} tasks, {mid} performed best (score {score:.0f}, "
                    f"quality/$ {qpd:.0f}). Prefer it for {tag} work."
                ),
                "score": min(1.0, score / 100.0), "tags": [tag, "model", "routing"],
            })
        return out

    def _build_success_entries(self, path: Path) -> List[Dict[str, Any]]:
        """Per-stack build reliability from build_success_rate.json."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return []
        stacks = data.get("stacks") if isinstance(data, dict) else None
        if not isinstance(stacks, dict):
            return []
        out = []
        for stack, s in stacks.items():
            if not isinstance(s, dict):
                continue
            succ, fail = int(s.get("success") or 0), int(s.get("failure") or 0)
            total = succ + fail
            if total < 3:
                continue
            rate = succ / total
            out.append({
                "kind": "build_success", "key": f"success:{stack}",
                "title": f"{stack} build reliability",
                "content": (
                    f"Builds for stack '{stack}': {succ}/{total} pass ({rate:.0%}) — "
                    f"{'reliable' if rate > 0.6 else 'fragile, needs extra verification'}."
                ),
                "score": rate, "tags": [stack, "build_success"],
            })
        return out

    def _skill_entries(self, library: Any, min_score: float) -> List[Dict[str, Any]]:
        try:
            if library is None:
                from skyn3t.intelligence.skill_library import get_default_library

                library = get_default_library()
            out = []
            for s in library.find(min_score=min_score, limit=100):
                out.append({
                    "kind": "skill",
                    "key": getattr(s, "slug", s.name),
                    "title": s.name,
                    "content": (getattr(s, "body", "") or getattr(s, "description", "")).strip(),
                    "score": float(getattr(s, "score", 0.0)),
                    "tags": list(getattr(s, "tags", [])),
                })
            return out
        except Exception:
            logger.debug("skill entries compile failed", exc_info=True)
            return []

    def _build_pattern_entries(self, prefs_path: Optional[Path]) -> List[Dict[str, Any]]:
        path = prefs_path or Path("data/build_pattern_preferences.json")
        try:
            prefs = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return []
        out = []
        for stack, p in (prefs.items() if isinstance(prefs, dict) else []):
            if not isinstance(p, dict):
                continue
            wsr = float(p.get("winner_success_rate") or 0.0)
            lsr = float(p.get("loser_success_rate") or 0.0)
            shape = p.get("shape") or []
            distinguishing = p.get("distinguishing_files") or []
            content = (
                f"For {stack} builds, the winning scaffold shape includes "
                f"{', '.join(shape)}. The files that most distinguish a passing "
                f"build from a failing one: {', '.join(distinguishing) or 'n/a'}. "
                f"Winner success {wsr:.0%} vs loser {lsr:.0%} — always include the "
                f"distinguishing files."
            )
            out.append({
                "kind": "build_pattern",
                "key": stack,
                "title": f"{stack} winning shape",
                "content": content,
                "score": wsr,
                "tags": [stack, "build_pattern"],
            })
        return out

    def _write(self, entries: List[Dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.json_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        lines = ["# SkyN3t Learnings Playbook", "",
                 f"_{len(entries)} curated learnings._", ""]
        for e in sorted(entries, key=lambda x: x.get("score", 0.0), reverse=True):
            lines.append(f"## [{e['kind']}] {e['title']}  (score {e.get('score', 0.0):+.2f})")
            lines.append(e.get("content", "").strip())
            lines.append("")
        self.md_path.write_text("\n".join(lines), encoding="utf-8")

    # ── query ────────────────────────────────────────────────────────────
    def _load(self) -> List[Dict[str, Any]]:
        try:
            return json.loads(self.json_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def guidance_for(
        self, query: str, *, stack: Optional[str] = None,
        tags: Optional[List[str]] = None, limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Most relevant distilled learnings for a query (the retrieval slice)."""
        entries = self._load()
        q = (query or "").lower()
        terms = [w for w in q.split() if len(w) > 2]

        def rel(e: Dict[str, Any]) -> float:
            s = float(e.get("score") or 0.0)
            hay = f"{e.get('title', '')} {e.get('content', '')} {' '.join(e.get('tags', []))}".lower()
            if stack and stack.lower() in hay:
                s += 1.0
            for t in (tags or []):
                if t and t.lower() in hay:
                    s += 0.5
            s += sum(0.3 for w in terms if w in hay)
            return s

        return sorted(entries, key=rel, reverse=True)[: max(0, int(limit))]

    def ask(
        self, query: str, *, stack: Optional[str] = None,
        tags: Optional[List[str]] = None, limit: int = 5,
        use_model: bool = True, model: Optional[str] = None,
    ) -> str:
        """Guidance grounded in learnings. Synthesizes via the local model when
        available; falls back to the raw distilled context."""
        items = self.guidance_for(query, stack=stack, tags=tags, limit=limit)
        context = "\n\n".join(
            f"[{e['kind']}] {e['title']}: {e['content']}" for e in items
        )
        if not context:
            return ""
        if not use_model:
            return context
        prompt = (
            "You are SkyN3t's learnings oracle. Using ONLY the learned facts "
            "below, give concise, actionable guidance. If the facts don't cover "
            f"it, say so briefly.\n\nQUESTION: {query}\n\n"
            f"LEARNED FACTS:\n{context}\n\nGUIDANCE:"
        )
        out = _ollama_generate(model or os.environ.get(
            "SKYN3T_LEARNINGS_MODEL", DEFAULT_OLLAMA_MODEL), prompt)
        return out or context


def _ollama_generate(model: str, prompt: str, *, timeout: float = 30.0) -> Optional[str]:
    """Best-effort call to a local Ollama model. None if unavailable."""
    try:
        body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
        req = urllib.request.Request(
            OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return (data.get("response") or "").strip() or None
    except Exception:
        logger.debug("ollama generate unavailable", exc_info=True)
        return None


def _score_to_counts(score: float) -> tuple[int, int]:
    score = max(-1.0, min(1.0, float(score)))
    if abs(score) < 0.001:
        return 0, 0
    total = 6 if score < 0 else 10
    success = round(((score + 1.0) / 2.0) * total)
    success = max(0, min(total, success))
    return success, total - success


def _playbook_skill_safe(entry: Dict[str, Any], *, min_score: float) -> bool:
    return playbook_entry_safe_for_prompt(entry, min_score=min_score)


def playbook_entry_safe_for_prompt(entry: Dict[str, Any], *, min_score: float = -1.0) -> bool:
    try:
        score = float(entry.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if score < min_score:
        return False
    tags = {str(tag).strip().lower() for tag in (entry.get("tags") or []) if str(tag).strip()}
    if tags & _UNSAFE_PLAYBOOK_TAGS:
        return False
    content = str(entry.get("content") or "").strip()
    if not str(entry.get("title") or "").strip() or not content:
        return False
    try:
        from skyn3t.intelligence.skill_library import scan_skill_markdown

        if scan_skill_markdown(content):
            return False
    except Exception:
        logger.debug("playbook safety scan failed", exc_info=True)
        return False
    return True


def sync_playbook_skills_to_library(
    *,
    store: Optional[LearningsStore] = None,
    library: Any = None,
    min_score: float = PLAYBOOK_SKILL_MIN_SCORE,
) -> Dict[str, Any]:
    """Import safe playbook ``kind=skill`` entries into SkillLibrary.

    The playbook is the curated NAS corpus. Some useful patterns have negative
    historical scores because they were measured before the current prompt path;
    keep mildly negative entries available while filtering known unsafe tags and
    dangerous markdown.
    """

    if store is None:
        store = get_default_store()
    if library is None:
        from skyn3t.intelligence.skill_library import get_default_library

        library = get_default_library()
    from skyn3t.intelligence.skill_library import Skill, scan_skill_markdown

    imported: List[str] = []
    skipped: List[str] = []
    flagged: List[str] = []
    for entry in store._load():
        if not isinstance(entry, dict) or entry.get("kind") != "skill":
            continue
        title = str(entry.get("title") or "").strip()
        if not _playbook_skill_safe(entry, min_score=min_score):
            skipped.append(title or "(untitled)")
            continue
        content = str(entry.get("content") or "").strip()
        findings = scan_skill_markdown(content)
        if findings:
            flagged.append(f"{title}: {', '.join(findings)}")
            skipped.append(title)
            continue
        try:
            score = float(entry.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        success_count, failure_count = _score_to_counts(score)
        tags = sorted(
            {
                "learnings-playbook",
                "playbook",
                *[str(tag).strip() for tag in (entry.get("tags") or []) if str(tag).strip()],
            }
        )
        skill = Skill(
            name=title,
            body=content,
            description=content.splitlines()[0][:240] if content else "",
            tags=tags,
            triggers=tags,
            success_count=success_count,
            failure_count=failure_count,
            source="learnings_playbook",
        )
        path = library.upsert(skill, count_mode="set")
        imported.append(Path(path).stem)
    return {"imported": imported, "skipped": skipped, "flagged": flagged}


_default_store: Optional[LearningsStore] = None


def get_default_store() -> LearningsStore:
    global _default_store
    if _default_store is None:
        _default_store = LearningsStore()
    return _default_store
