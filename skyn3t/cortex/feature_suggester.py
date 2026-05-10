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
import time
from collections import Counter
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skyn3t.cortex.feature_suggester")


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
            p = get_store().create(
                kind="feature",
                title=f"User idea: {idea[:80]}",
                summary=idea[:200],
                detail=f"_Submitted by user via dashboard ‘Suggest improvement’ button._\n\n{idea}",
                payload={"idea": idea, "source": source, "action": "user_request"},
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
