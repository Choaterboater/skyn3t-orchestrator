"""Research Agent - searches, summarizes, compares, and fact-checks information."""

import re
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus


class ResearchAgent(BaseAgent):
    """Agent for web research, summarization, comparison, and fact-checking."""

    def __init__(
        self,
        name: str = "research_agent",
        event_bus: EventBus = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="research",
            provider="local",
            event_bus=event_bus,
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
                name="fact_check",
                description="Verify facts against known sources",
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

    async def execute(self, task: TaskRequest) -> TaskResult:
        """Execute a research-related task."""
        task_type = task.input_data.get("task_type", "web_search")

        handlers = {
            "web_search": self._web_search,
            "summarization": self._summarize,
            "comparison": self._compare,
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
        """Simulate web search with a placeholder implementation."""
        query = (task.input_data.get("query")
                 or task.input_data.get("brief")
                 or task.input_data.get("idea")
                 or task.input_data.get("description")
                 or "")
        num_results = task.input_data.get("num_results", 5)

        if not query:
            return {"success": False, "error": "No query provided"}

        # Placeholder search results
        results = []
        for i in range(min(num_results, 10)):
            results.append({
                "title": f"Result {i + 1} for '{query}'",
                "url": f"https://example.com/search/{i + 1}",
                "snippet": f"This is a simulated search result snippet for '{query}'. "
                           f"In a production environment, this would contain actual search results.",
                "source": "placeholder",
            })

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
        """Fact-check a claim against provided or known sources."""
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
