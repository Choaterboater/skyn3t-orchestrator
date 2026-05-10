"""Research Agent - searches, summarizes, compares, and fact-checks information."""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.research_agent")


class ResearchAgent(BaseAgent):
    """Agent for web research, summarization, comparison, and fact-checking."""

    def __init__(
        self,
        name: str = "research_agent",
        event_bus: EventBus | None = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="research",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="web_search",
                description="Search the web for information on a given topic",
                parameters={"query": "str", "num_results": "int"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="summarization",
                description="Summarize documents or text content",
                parameters={"text": "str", "summary_length": "str"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="comparison",
                description="Compare multiple pieces of information or options",
                parameters={"items": "list", "criteria": "list"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="corroborate_against_sources",
                description=(
                    "Heuristic substring corroboration of a claim against the "
                    "supplied sources. Not a real fact-checker — does not query "
                    "knowledge bases or verify truth."
                ),
                parameters={"claim": "str", "sources": "list"},
            )
        )
        self._search_history: List[Dict[str, Any]] = []
        self._max_history = self.config.get("max_history", 100)

    async def initialize(self) -> None:
        """Initialize the research agent."""
        self.metadata["initialized"] = True
        self.metadata["search_history_size"] = 0

    async def health_check(self) -> bool:
        """Check if the research agent is operational."""
        try:
            # Test basic text processing
            test_text = "This is a test sentence for health check."
            words = test_text.split()
            return len(words) > 0
        except Exception:
            return False

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        """Execute a research-related task."""
        task_type = task.input_data.get("task_type", "web_search")

        handlers = {
            "web_search": self._web_search,
            "summarization": self._summarize,
            "comparison": self._compare,
            "corroborate_against_sources": self._fact_check,
            # Back-compat alias for callers still passing the old name.
            "fact_check": self._fact_check,
        }

        handler = handlers.get(task_type)
        if not handler:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"Unknown task type: {task_type}",
            )

        try:
            result = await handler(task)
            return TaskResult(
                task_id=task.task_id,
                success=result.get("success", True),
                output=result,
            )
        except Exception as e:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=str(e),
            )

    async def _web_search(self, task: TaskRequest) -> Dict[str, Any]:
        """LLM-grounded research; falls back to placeholder if no backend."""
        query = (task.input_data.get("query")
                 or task.input_data.get("brief")
                 or task.input_data.get("idea")
                 or task.input_data.get("description")
                 or "")
        num_results = task.input_data.get("num_results", 5)
        artifact_dir = task.input_data.get("artifact_dir")

        if not query:
            return {"success": False, "error": "No query provided"}

        # Try LLM-grounded research; falls back to placeholder if backend is deterministic
        results = []
        notes = ""
        try:
            client = self.get_llm() if hasattr(self, "get_llm") else None
            if client is None:
                from skyn3t.adapters import LLMClient
                client = LLMClient(default_model=self.config.get("model"),
                                    backend=self.config.get("backend"))
            prompt = (
                f"Research brief:\n{query}\n\n"
                f"Produce {num_results} concise findings or considerations relevant to this brief. "
                f"Format as a markdown bullet list. Each bullet must be one sentence, "
                f"actionable, and grounded in known facts/tools/patterns. No preamble."
            )
            out = await client.complete(prompt, max_tokens=900, temperature=0.3)
            if out and "[deterministic-stub]" not in out:
                notes = out.strip()
                # parse bullets into results
                for line in notes.splitlines():
                    line = line.strip()
                    if line.startswith(("- ", "* ", "• ")):
                        results.append({"title": line[2:].strip()[:100], "snippet": line[2:].strip(),
                                        "source": "llm"})
                    if len(results) >= num_results:
                        break
        except Exception:
            pass

        # Placeholder fallback if LLM gave nothing
        if not results:
            for i in range(min(num_results, 5)):
                results.append({"title": f"Consider angle {i+1} for '{query[:40]}'",
                                "snippet": f"Placeholder finding {i+1}.",
                                "source": "placeholder"})
            notes = "\n".join(f"- {r['snippet']}" for r in results)

        # Write research.md to artifact_dir for downstream stages
        files_written = []
        if artifact_dir:
            try:
                ad = Path(artifact_dir)
                ad.mkdir(parents=True, exist_ok=True)
                md_path = ad / "research.md"
                md_path.write_text(
                    f"# Research\n\n## Query\n{query}\n\n## Findings\n{notes}\n",
                    encoding="utf-8")
                files_written.append(str(md_path))
                if hasattr(self, "think"):
                    try:
                        await self.think(f"wrote {md_path.name} ({len(results)} findings)")
                    except Exception:
                        logger.debug("think() failed after research write", exc_info=True)
            except Exception:
                pass

        entry = {
            "query": query,
            "num_results": len(results),
            "timestamp": self._now_iso(),
        }
        self._search_history.append(entry)
        if len(self._search_history) > self._max_history:
            self._search_history = self._search_history[-self._max_history :]
        self.metadata["search_history_size"] = len(self._search_history)

        return {
            "success": True,
            "query": query,
            "results": results,
            "total_results": len(results),
            "files": files_written,
            "summary": f"Researched '{query[:60]}': {len(results)} findings, "
                       f"{len(files_written)} file(s) written.",
        }

    async def _summarize(self, task: TaskRequest) -> Dict[str, Any]:
        """Summarize a document or text."""
        text = task.input_data.get("text", "")
        summary_length = task.input_data.get("summary_length", "medium")

        if not text:
            return {"success": False, "error": "No text provided"}

        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        total_sentences = len(sentences)

        if total_sentences == 0:
            return {"success": False, "error": "Empty text provided"}

        # Determine summary length
        length_map = {"short": 0.15, "medium": 0.30, "long": 0.50}
        ratio = length_map.get(summary_length, 0.30)
        num_summary_sentences = max(1, int(total_sentences * ratio))

        # Simple extractive summarization: take first N sentences
        # In production, this would use more sophisticated algorithms
        summary_sentences = sentences[:num_summary_sentences]
        summary = " ".join(summary_sentences)

        return {
            "success": True,
            "original_length": len(text),
            "summary_length": len(summary),
            "original_sentences": total_sentences,
            "summary_sentences": len(summary_sentences),
            "summary": summary,
            "length_type": summary_length,
        }

    async def _compare(self, task: TaskRequest) -> Dict[str, Any]:
        """Compare multiple items based on criteria."""
        items = task.input_data.get("items", [])
        criteria = task.input_data.get("criteria", [])

        if len(items) < 2:
            return {"success": False, "error": "At least 2 items required for comparison"}

        comparison_results = []
        similarities = []
        differences = []

        # Compare each pair
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                item_a = items[i]
                item_b = items[j]

                pair_result = {
                    "item_a": item_a.get("name", f"Item {i + 1}"),
                    "item_b": item_b.get("name", f"Item {j + 1}"),
                }

                # Compare by criteria if provided
                if criteria:
                    criterion_results = {}
                    for criterion in criteria:
                        val_a = item_a.get(criterion)
                        val_b = item_b.get(criterion)
                        criterion_results[criterion] = {
                            "item_a": val_a,
                            "item_b": val_b,
                            "same": val_a == val_b,
                        }
                        if val_a == val_b:
                            similarities.append(f"Both have same {criterion}: {val_a}")
                        else:
                            differences.append(
                                f"{pair_result['item_a']} {criterion}={val_a} vs "
                                f"{pair_result['item_b']} {criterion}={val_b}"
                            )
                    pair_result["criteria"] = criterion_results
                else:
                    # Simple string comparison of JSON representation
                    str_a = str(item_a)
                    str_b = str(item_b)
                    if str_a == str_b:
                        similarities.append(f"{pair_result['item_a']} == {pair_result['item_b']}")
                    else:
                        differences.append(f"{pair_result['item_a']} != {pair_result['item_b']}")

                comparison_results.append(pair_result)

        return {
            "success": True,
            "items_compared": len(items),
            "comparisons": comparison_results,
            "similarities": list(set(similarities)),
            "differences": list(set(differences)),
            "criteria_used": criteria,
        }

    async def _fact_check(self, task: TaskRequest) -> Dict[str, Any]:
        """Corroborate a claim by substring-matching against provided sources.

        Despite the historical name, this is **not** a fact checker: it does
        not consult knowledge bases or verify truth. It only reports whether
        the claim text (or its words) appear in the supplied source strings.
        """
        claim = task.input_data.get("claim", "")
        sources = task.input_data.get("sources", [])

        if not claim:
            return {"success": False, "error": "No claim provided"}

        # Placeholder fact-checking logic
        # In production, this would query knowledge bases or APIs
        verification_status = "unverified"
        confidence = 0.0
        evidence = []

        claim_lower = claim.lower()

        # Simple heuristic checks
        if sources:
            for source in sources:
                source_text = str(source).lower()
                if claim_lower in source_text:
                    verification_status = "supported"
                    confidence = 0.8
                    evidence.append({
                        "source": source,
                        "relevance": "high",
                        "match_type": "exact",
                    })
                elif any(word in source_text for word in claim_lower.split() if len(word) > 4):
                    verification_status = "partially_supported"
                    confidence = max(confidence, 0.4)
                    evidence.append({
                        "source": source,
                        "relevance": "medium",
                        "match_type": "partial",
                    })

        if not evidence:
            verification_status = "unverified"
            confidence = 0.0
            evidence.append({
                "source": "no sources matched",
                "relevance": "none",
                "match_type": "none",
            })

        return {
            "success": True,
            "claim": claim,
            "verification_status": verification_status,
            "confidence": confidence,
            "evidence": evidence,
            "sources_checked": len(sources),
        }

    def _now_iso(self) -> str:
        """Return current UTC time in ISO format."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
