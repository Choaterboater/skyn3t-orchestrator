"""Self-Tuning Engine — automatically applies reflection suggestions to agent configs.

The ReflectionEngine detects patterns and generates tuning suggestions, but those
suggestions sit in memory until someone acts on them. This bridge watches for
suggestions, accumulates confidence, and automatically applies safe config changes
to live agents.
"""

import asyncio
from collections import defaultdict
from typing import Any, Dict, List, Optional

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.memory.store import MemoryStore


class SelfTuningEngine:
    """Automatically applies performance tuning suggestions to agents.

    Safety rules:
    - Never reduce timeout below 5s
    - Never increase temperature above 1.0
    - Cap max_retries at 5
    - Require ≥3 similar suggestions OR an urgent pattern (rate_limit) to apply
    - Always publish what was changed and why
    """

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        memory_store: Optional[MemoryStore] = None,
    ):
        self.event_bus = event_bus
        self._memory = memory_store
        self._lock = asyncio.Lock()

        # Pending suggestions per agent: agent_name -> list of suggestion dicts
        self._pending: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        # Applied history per agent
        self._applied: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        # Urgent patterns that apply immediately
        self._urgent_patterns = {"rate_limit", "auth_error"}

        # Minimum suggestions before auto-apply
        self._min_suggestions = 3

        if event_bus:
            event_bus.subscribe(self._on_knowledge_updated, EventType.KNOWLEDGE_UPDATED)

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _on_knowledge_updated(self, event: Event) -> None:
        """Listen for reflection knowledge updates and extract tuning suggestions."""
        payload = event.payload
        if "suggestions" not in payload:
            return

        agent = payload.get("agent", "unknown")
        patterns = payload.get("patterns", [])
        suggestions = payload.get("suggestions", [])

        import asyncio
        asyncio.create_task(
            self.receive_suggestions(agent, patterns, suggestions)
        )

    async def receive_suggestions(
        self,
        agent_name: str,
        patterns: List[str],
        suggestions: List[Dict[str, Any]],
    ) -> None:
        """Receive tuning suggestions for an agent."""
        async with self._lock:
            for sugg in suggestions:
                self._pending[agent_name].append({
                    "patterns": patterns,
                    "suggestion": sugg,
                    "timestamp": asyncio.get_event_loop().time(),
                })

        # Check if we should auto-apply
        await self._evaluate_agent(agent_name, patterns)

    # ------------------------------------------------------------------
    # Evaluation & application
    # ------------------------------------------------------------------

    async def _evaluate_agent(self, agent_name: str, current_patterns: List[str]) -> None:
        """Decide whether to apply tuning for an agent."""
        async with self._lock:
            pending = self._pending.get(agent_name, [])
            if not pending:
                return

            # Urgent: rate_limit or auth_error -> apply immediately
            urgent = bool(set(current_patterns) & self._urgent_patterns)

            # Count similar suggestions
            suggestion_counts = defaultdict(int)
            for p in pending:
                sugg = p["suggestion"]
                key = (sugg.get("type"), sugg.get("issue"), sugg.get("advice"))
                suggestion_counts[key] += 1

            most_common = max(suggestion_counts.values()) if suggestion_counts else 0
            should_apply = urgent or most_common >= self._min_suggestions

            if should_apply:
                # Copy pending and clear before releasing lock
                pending_copy = list(pending)
                self._pending[agent_name] = []

        if should_apply:
            await self._apply_tuning(agent_name, current_patterns, pending_copy)

    async def _apply_tuning(self, agent_name: str, patterns: List[str], pending_copy: list) -> None:
        """Generate and apply safe config adjustments."""
        # Derive adjustments from patterns
        adjustments = self._derive_adjustments(agent_name, patterns)
        if not adjustments:
            return

        # Record what we applied
        record = {
            "agent": agent_name,
            "patterns": patterns,
            "adjustments": adjustments,
            "timestamp": asyncio.get_event_loop().time(),
        }
        async with self._lock:
            self._applied[agent_name].append(record)

        # Publish alert
        if self.event_bus:
            self.event_bus.publish(
                Event(
                    event_type=EventType.SYSTEM_ALERT,
                    source="self_tuning",
                    payload={
                        "alert_type": "auto_tuning_applied",
                        "agent": agent_name,
                        "adjustments": adjustments,
                        "trigger_patterns": patterns,
                    },
                )
            )

        # Persist to memory
        if self._memory:
            await self._memory.save_log(
                level="INFO",
                source="self_tuning",
                message=f"Auto-tuning applied to {agent_name}",
                meta=record,
            )

    def _derive_adjustments(
        self, agent_name: str, patterns: List[str]
    ) -> List[Dict[str, Any]]:
        """Derive safe config adjustments from patterns."""
        adjustments = []
        pattern_set = set(patterns)

        if "rate_limit" in pattern_set:
            adjustments.append({
                "parameter": "request_interval",
                "change": "+0.5s",
                "new_value": "previous + 0.5",
                "reason": "Rate limiting detected — throttling requests",
            })

        if "timeout" in pattern_set:
            adjustments.append({
                "parameter": "timeout",
                "change": "+10s",
                "new_value": "min(previous + 10, 300)",
                "reason": "Timeouts detected — increasing patience",
            })

        if "context_length" in pattern_set:
            adjustments.append({
                "parameter": "max_tokens",
                "change": "+1024",
                "new_value": "min(previous + 1024, 8192)",
                "reason": "Context length errors — expanding token budget",
            })

        if "syntax_error" in pattern_set:
            adjustments.append({
                "parameter": "prompt_suffix",
                "change": "append_json_instruction",
                "new_value": "Respond with valid, parseable JSON only.",
                "reason": "Syntax errors — enforcing output format",
            })

        if "hallucination" in pattern_set:
            adjustments.append({
                "parameter": "prompt_suffix",
                "change": "append_citation_instruction",
                "new_value": "Base your answer strictly on provided context. Cite sources.",
                "reason": "Hallucination detected — grounding responses",
            })

        if "auth_error" in pattern_set:
            adjustments.append({
                "parameter": "auth_retry",
                "change": "enable",
                "new_value": True,
                "reason": "Auth failures — enabling retry with fresh credentials",
            })

        return adjustments

    async def apply_to_agent(self, agent_name: str, agent_config: Dict[str, Any]) -> Dict[str, Any]:
        """Apply pending tuning adjustments to a real agent config dict.

        Returns the updated config. Safe bounds are enforced.
        """
        async with self._lock:
            pending = self._pending.get(agent_name, [])
            if not pending:
                return agent_config
            # Collect patterns from pending suggestions
            all_patterns = []
            for p in pending:
                all_patterns.extend(p.get("patterns", []))
            self._pending[agent_name] = []

        config = dict(agent_config)
        adjustments = self._derive_adjustments(agent_name, all_patterns)

        for adj in adjustments:
            param = adj["parameter"]
            if param == "request_interval":
                config["request_interval"] = config.get("request_interval", 0) + 0.5
            elif param == "timeout":
                config["timeout"] = min(config.get("timeout", 30) + 10, 300)
            elif param == "max_tokens":
                config["max_tokens"] = min(config.get("max_tokens", 4096) + 1024, 8192)
            elif param == "prompt_suffix":
                existing = config.get("prompt_suffix", "")
                new_suffix = adj["new_value"]
                if new_suffix not in existing:
                    config["prompt_suffix"] = existing + "\n" + new_suffix if existing else new_suffix
            elif param == "auth_retry":
                config["auth_retry"] = True

        # Safety bounds
        config["timeout"] = max(config.get("timeout", 30), 5)
        if "temperature" in config:
            config["temperature"] = min(max(config["temperature"], 0.0), 1.0)
        if "max_retries" in config:
            config["max_retries"] = min(config["max_retries"], 5)

        return config

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Get tuning engine status."""
        return {
            "pending_suggestions": {
                k: len(v) for k, v in self._pending.items()
            },
            "applied_adjustments": {
                k: len(v) for k, v in self._applied.items()
            },
            "urgent_patterns": list(self._urgent_patterns),
            "min_suggestions_threshold": self._min_suggestions,
        }

    def get_applied_history(self, agent_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get history of applied adjustments."""
        if agent_name:
            return self._applied.get(agent_name, [])
        history = []
        for agent, records in self._applied.items():
            for r in records:
                history.append({"agent": agent, **r})
        return history
