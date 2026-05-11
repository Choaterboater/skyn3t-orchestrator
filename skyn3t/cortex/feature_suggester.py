"""Feature suggestion aggregator — turns operational signals into Cortex proposals.

Signals consumed:
  - MetaAgent observation events (payload kind ∈ {'pattern','observation'})
  - ReflectionEngine failure patterns (recurring TASK_FAILED on same agent/capability)
  - ExplorerAgent gap reports (new event source 'explorer' with kind='capability_gap')
  - Direct user submissions via POST /api/proposals/feature

All filed as Proposal(kind='feature') — same review path as tunings/patches.
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skyn3t.cortex.feature_suggester")
REPO_ROOT = Path(__file__).resolve().parents[2]
_IDEA_SOURCE_GLOBS = (
    "skyn3t/**/*.py",
    "skyn3t/**/*.html",
    "skyn3t/**/*.js",
    "skyn3t/**/*.ts",
    "skyn3t/**/*.tsx",
)
_SHORT_KEEP_TOKENS = {"ai", "api", "cli", "db", "llm", "qa", "rag", "ui", "ux", "ws"}
_STOPWORDS = {
    "a",
    "an",
    "and",
    "be",
    "but",
    "by",
    "do",
    "for",
    "from",
    "how",
    "i",
    "if",
    "in",
    "into",
    "it",
    "later",
    "make",
    "more",
    "my",
    "new",
    "of",
    "on",
    "or",
    "please",
    "should",
    "so",
    "something",
    "that",
    "the",
    "then",
    "this",
    "to",
    "up",
    "we",
    "with",
}
_KEYWORD_ALIASES: Dict[str, set[str]] = {
    "approval": {"approve", "proposal"},
    "approve": {"approval", "proposal"},
    "cortex": {"proposal", "feature", "suggest"},
    "dashboard": {"web", "frontend", "ui"},
    "frontend": {"dashboard", "ui", "web"},
    "idea": {"feature", "proposal", "suggest"},
    "ideas": {"feature", "proposal", "suggest"},
    "ingest": {"github", "knowledge", "rag"},
    "knowledge": {"rag", "memory"},
    "learning": {"memory", "meta", "self"},
    "memory": {"learning", "meta", "rag"},
    "meta": {"learning", "memory", "self"},
    "proposal": {"approve", "cortex", "feature"},
    "self": {"learning", "memory", "meta"},
    "studio": {"brief", "project", "workflow"},
    "suggest": {"feature", "proposal"},
    "ui": {"dashboard", "frontend", "web"},
    "web": {"api", "dashboard", "frontend"},
}
_TARGET_HINTS: tuple[tuple[set[str], tuple[str, ...]], ...] = (
    (
        {"approve", "approval", "cortex", "feature", "idea", "proposal", "suggest"},
        (
            "skyn3t/cortex/handlers.py",
            "skyn3t/cortex/feature_suggester.py",
            "skyn3t/cortex/proposals.py",
            "skyn3t/web/dashboard.html",
        ),
    ),
    (
        {"api", "endpoint", "http", "server", "web"},
        (
            "skyn3t/web/app.py",
            "skyn3t/web/dashboard.html",
        ),
    ),
    (
        {"brief", "project", "repo", "studio", "workflow"},
        (
            "skyn3t/studio/planner.py",
            "skyn3t/studio/repo_target.py",
            "skyn3t/studio/runner.py",
        ),
    ),
    (
        {"learning", "memory", "meta", "self"},
        (
            "skyn3t/core/orchestrator.py",
            "skyn3t/memory/meta_agent.py",
            "skyn3t/memory/tuner.py",
        ),
    ),
    (
        {"knowledge", "rag"},
        (
            "skyn3t/rag/rag_engine.py",
            "skyn3t/web/app.py",
            "skyn3t/web/dashboard.html",
        ),
    ),
)


def _idea_keywords(idea: str) -> List[str]:
    raw_tokens = re.findall(r"[a-z0-9_]+", str(idea or "").lower())
    tokens = {
        token
        for token in raw_tokens
        if (len(token) >= 3 or token in _SHORT_KEEP_TOKENS) and token not in _STOPWORDS
    }
    expanded = set(tokens)
    for token in list(tokens):
        expanded.update(_KEYWORD_ALIASES.get(token, set()))
    if len(expanded) < 2:
        expanded.update({"cortex", "feature", "proposal"})
    return sorted(expanded)


def _candidate_source_files(repo_root: Path) -> List[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for pattern in _IDEA_SOURCE_GLOBS:
        for path in repo_root.glob(pattern):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(resolved)
    return candidates


def infer_feature_target_file(idea: str, *, repo_root: Path | None = None) -> Optional[str]:
    root = (repo_root or REPO_ROOT).resolve()
    keywords = _idea_keywords(idea)
    if not keywords:
        return None

    best_rel: Optional[str] = None
    best_score = 0
    best_matches = -1

    for path in _candidate_source_files(root):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        rel_lower = rel.lower()
        score = 0
        matches = 0
        for keyword in keywords:
            if keyword in rel_lower:
                score += 12
                matches += 1
        for hint_keywords, hint_paths in _TARGET_HINTS:
            if rel in hint_paths and hint_keywords.intersection(keywords):
                score += 14
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")[:24000].lower()
        except Exception:
            content = ""
        if content:
            for keyword in keywords:
                if keyword in content:
                    score += 2
                    matches += 1
        if score > best_score or (score == best_score and matches > best_matches):
            best_rel = rel
            best_score = score
            best_matches = matches

    return best_rel if best_score > 0 else None


class FeatureSuggester:
    def __init__(self, event_bus, *, cooldown_per_signature: float = 3600.0,
                 min_signal_count: int = 3):
        self.event_bus = event_bus
        self.cooldown = cooldown_per_signature
        self.min_signal = min_signal_count
        self._wired = False
        self._last_filed: Dict[str, float] = {}     # signature → ts
        self._failure_counter: Counter[str] = Counter()
        self._observation_buf: List[Dict[str, Any]] = []

    def start(self) -> None:
        if self._wired:
            return
        self._wired = True
        # We care about three discrete signal sources:
        #  - TASK_FAILED / TASK_FAILED_FINAL → repeated failure patterns
        #  - SYSTEM_ALERT (kind=capability_gap from explorer; kind=pattern/observation/
        #    anomaly from meta_agent)
        # Subscribing globally was a per-event tax on a busy bus.
        try:
            from skyn3t.core.events import EventType
            self.event_bus.subscribe(self._on_event, EventType.TASK_FAILED)
            self.event_bus.subscribe(self._on_event, EventType.TASK_FAILED_FINAL)
            self.event_bus.subscribe(self._on_event, EventType.SYSTEM_ALERT)
        except Exception:
            logger.exception("subscribe failed")

    def _on_event(self, event) -> None:
        try:
            etype = getattr(event, "event_type", None)
            etype_value = getattr(etype, "value", str(etype)) if etype else ""
            payload = getattr(event, "payload", {}) or {}
            kind = payload.get("kind", "")
            source = getattr(event, "source", "")

            # 1. recurring task failures → suggest behavior change
            if etype_value == "TASK_FAILED" or etype_value == "TASK_FAILED_FINAL":
                sig = f"{payload.get('agent') or source}::{payload.get('capability','')}"
                self._failure_counter[sig] += 1
                if self._failure_counter[sig] >= self.min_signal:
                    self._maybe_file(
                        signature=f"failure-{sig}",
                        title=f"Reduce repeated failures on {sig}",
                        summary=(f"Agent/capability '{sig}' has failed {self._failure_counter[sig]} times. "
                                 f"Consider tuning, fallback, or a behavior change."),
                        detail=f"Repeated failure pattern detected.\n\n- signature: `{sig}`\n"
                               f"- count: {self._failure_counter[sig]}\n\n"
                               f"_Suggested action: investigate root cause; consider increasing timeout, "
                               f"swapping backend/model, or adding a fallback agent._",
                        payload={"signature": sig, "count": self._failure_counter[sig],
                                 "action": "investigate"},
                        source="feature_suggester:failure_pattern",
                    )

            # 2. explorer capability gaps
            if source == "explorer" and kind == "capability_gap":
                cap = payload.get("capability") or "unknown"
                self._maybe_file(
                    signature=f"gap-{cap}",
                    title=f"New agent for capability '{cap}'?",
                    summary=f"Explorer flagged a capability gap: '{cap}'.",
                    detail=f"_Suggested by ExplorerAgent based on usage patterns._\n\n"
                           f"- missing capability: `{cap}`\n"
                           f"- consider creating a new specialist agent or extending an existing one.",
                    payload={"capability": cap, "action": "create_agent"},
                    source="feature_suggester:gap",
                )

            # 3. meta-agent pattern observations
            if source == "meta_agent" and kind in ("pattern", "observation", "anomaly"):
                self._observation_buf.append(payload)
                if len(self._observation_buf) >= 5:
                    # naive: file a digest
                    observations = list(self._observation_buf)
                    digest = "; ".join(
                        (observation.get("summary") or "")[:120]
                        for observation in observations
                        if observation.get("summary")
                    )[:500]
                    self._observation_buf.clear()
                    if digest:
                        self._maybe_file(
                            signature=f"meta-{hash(digest) & 0xffff}",
                            title="Meta-agent: behavior trend detected",
                            summary=digest[:140],
                            detail=f"_MetaAgent aggregated 5 observations:_\n\n{digest}",
                            payload={"observations": observations, "action": "review"},
                            source="feature_suggester:meta",
                        )
        except Exception:
            logger.exception("_on_event failed")

    def file_user_idea(self, idea: str, *, source: str = "user") -> Optional[str]:
        idea = (idea or "").strip()
        if not idea:
            return None
        try:
            from skyn3t.cortex import get_store
            target_file = infer_feature_target_file(idea)
            payload: Dict[str, Any] = {
                "idea": idea,
                "source": source,
                "action": "user_request",
            }
            detail_lines = [
                "_Submitted by user via dashboard 'Suggest improvement' button._",
                "",
                "## Requested change",
                idea,
                "",
                "## Planned execution",
            ]
            if target_file:
                payload["target_file"] = target_file
                payload["repo_root"] = str(REPO_ROOT.resolve())
                detail_lines.extend(
                    [
                        f"- Starting file: `{target_file}`",
                        "- On approval: SkyN3t will draft and auto-apply a repo patch starting from this file.",
                        "- Scope: this self-update flow starts from an existing repo file, not a brand-new file path.",
                    ]
                )
            else:
                detail_lines.extend(
                    [
                        "- Starting file: _not inferred yet_",
                        "- On approval: SkyN3t will need a clearer repo target before it can patch the codebase.",
                    ]
                )
            p = get_store().create(
                kind="feature",
                title=f"User idea: {idea[:80]}",
                summary=idea[:200],
                detail="\n".join(detail_lines),
                payload=payload,
                source=source,
                origin="user",
            )
            return p.id
        except Exception:
            logger.exception("file_user_idea failed")
            return None

    def _maybe_file(self, *, signature: str, title: str, summary: str, detail: str,
                     payload: Dict[str, Any], source: str) -> None:
        now = time.time()
        last = self._last_filed.get(signature, 0)
        if now - last < self.cooldown:
            return
        self._last_filed[signature] = now
        try:
            from skyn3t.cortex import get_store
            get_store().create(kind="feature", title=title, summary=summary,
                               detail=detail, payload=payload, source=source)
        except Exception:
            logger.exception("file failed")
