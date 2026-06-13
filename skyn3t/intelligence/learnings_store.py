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


def learnings_dir() -> Path:
    """Where the compiled corpus lives. Point at a NAS via SKYN3T_LEARNINGS_DIR."""
    return Path(os.environ.get("SKYN3T_LEARNINGS_DIR", DEFAULT_DIR))


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
        min_skill_score: float = 0.2,
    ) -> int:
        """Gather curated learnings into the store. Returns the entry count."""
        entries: List[Dict[str, Any]] = []
        entries.extend(self._skill_entries(library, min_skill_score))
        entries.extend(self._build_pattern_entries(prefs_path))
        self._write(entries)
        return len(entries)

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


_default_store: Optional[LearningsStore] = None


def get_default_store() -> LearningsStore:
    global _default_store
    if _default_store is None:
        _default_store = LearningsStore()
    return _default_store
