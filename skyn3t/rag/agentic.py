from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol

if TYPE_CHECKING:
    from skyn3t.adapters.llm_client import LLMClient

logger = logging.getLogger("skyn3t.rag.agentic")

# event types referenced (defined in skyn3t.core.events by another agent):
#   RAG_QUERY_STARTED, RAG_RETRIEVED, RAG_CRITIQUED, RAG_REQUERY
# import lazily inside methods to avoid hard dependency on whether the enum
# values exist yet — fall back to string names otherwise.

@dataclass
class RetrievalStep:
    query: str
    results: List[Dict[str, Any]]
    confidence: float = 0.0
    critique: Optional[str] = None

@dataclass
class AgenticRAGResult:
    final_query: str
    steps: List[RetrievalStep] = field(default_factory=list)
    answer_context: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def hop_count(self) -> int: return len(self.steps)


class _RAGLike(Protocol):
    async def query(self, query: str, top_k: int = 5, **kw) -> List[Dict[str, Any]]: ...


class AgenticRAG:
    def __init__(self, rag: _RAGLike, event_bus=None, *, max_hops: int = 3, min_confidence: float = 0.55,
                 llm: Optional["LLMClient"] = None):
        self.rag = rag
        self.event_bus = event_bus
        self.max_hops = max_hops
        self.min_confidence = min_confidence
        self.llm = llm

    async def query(self, question: str, top_k: int = 5) -> AgenticRAGResult:
        result = AgenticRAGResult(final_query=question)
        current_q = await self._plan_query(question)

        for hop in range(self.max_hops):
            self._publish("RAG_QUERY_STARTED", {"query": current_q, "hop": hop})
            hits = await self.rag.query(current_q, top_k=top_k)
            confidence = self._score(hits, question)
            self._publish("RAG_RETRIEVED", {"query": current_q, "hop": hop, "n": len(hits), "confidence": confidence})

            critique = await self._critique(hits, question, confidence)
            self._publish("RAG_CRITIQUED", {"hop": hop, "critique": critique, "confidence": confidence})

            step = RetrievalStep(query=current_q, results=hits, confidence=confidence, critique=critique)
            result.steps.append(step)
            result.answer_context.extend(hits)

            if confidence >= self.min_confidence or hop == self.max_hops - 1:
                break

            current_q = await self._reformulate(question, current_q, hits, critique)
            self._publish("RAG_REQUERY", {"hop": hop + 1, "new_query": current_q})

        # dedupe answer_context
        seen = set()
        deduped = []
        for h in result.answer_context:
            key = h.get("id") or h.get("content", "")[:120]
            if key in seen:
                continue
            seen.add(key)
            deduped.append(h)
        result.answer_context = deduped
        return result

    # --- helpers (deterministic, no LLM dependency) ---
    async def _plan_query(self, question: str) -> str:
        # naive: strip filler words; cap length
        stop = {"please", "could", "would", "can", "you", "tell", "me", "about", "the"}
        toks = [t for t in question.split() if t.lower() not in stop]
        return " ".join(toks)[:300] or question

    def _score(self, hits: List[Dict[str, Any]], question: str) -> float:
        if not hits:
            return 0.0
        # if hits already carry "score" use the top one's score; else use saturation by count
        top = hits[0].get("score")
        if isinstance(top, (int, float)):
            return max(0.0, min(1.0, float(top)))
        return min(1.0, len(hits) / 5.0)

    async def _critique(self, hits, question, confidence) -> str:
        if self.llm is not None:
            try:
                top = "\n".join(f"- {h.get('content','')[:200]}" for h in hits[:5])
                prompt = (f"Question: {question}\nTop results:\n{top}\n"
                          "In one sentence, judge whether these answer the question. "
                          "Reply 'sufficient' or 'insufficient: <why>'.")
                return (await self.llm.complete(prompt, max_tokens=120, temperature=0.0)).strip()
            except Exception:
                logger.exception("agentic_rag llm critique failed")
        if not hits:
            return "no results — broaden query"
        if confidence < self.min_confidence:
            return "low confidence — try alternative phrasing"
        return "sufficient coverage"

    async def _reformulate(self, original: str, last: str, hits, critique: str) -> str:
        if self.llm is not None:
            try:
                ctx = "\n".join(f"- {h.get('content','')[:160]}" for h in hits[:3])
                prompt = (f"Original question: {original}\nLast query: {last}\n"
                          f"Critique: {critique}\nResults so far:\n{ctx}\n"
                          "Write a single improved search query (no quotes, no preamble).")
                q = (await self.llm.complete(prompt, max_tokens=60, temperature=0.2)).strip().splitlines()
                q0 = q[0].strip() if q else ""
                if q0:
                    return q0[:300]
            except Exception:
                logger.exception("agentic_rag llm reformulate failed")
        # use top hit's keywords to focus next query if available
        if hits:
            seed = hits[0].get("metadata", {}).get("title") or hits[0].get("content", "")[:80]
            return f"{original} {seed}".strip()
        # otherwise widen
        return original

    def _publish(self, event_name: str, payload: Dict[str, Any]) -> None:
        if not self.event_bus:
            return
        try:
            from skyn3t.core.events import Event, EventType
            et = getattr(EventType, event_name, None)
            if et is None:
                return
            self.event_bus.publish(Event(event_type=et, source="agentic_rag", payload=payload))
        except Exception:
            logger.exception("agentic_rag publish failed")
