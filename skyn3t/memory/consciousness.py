"""Collective Consciousness — the swarm's shared working memory.

Think of this as the brain's prefrontal cortex: a shared blackboard where
all agents can read and write in real-time. When an agent learns something,
every other agent can see it. When a session is active, all participants
share the same context.
"""

import asyncio
import time
from typing import Any, Dict, List, Optional

from skyn3t.memory.store import MemoryStore


class CollectiveConsciousness:
    """Shared working memory for the agent swarm.

    Features:
    - working_memory: key-value store with TTL (auto-expire)
    - session_contexts: per-session accumulated state
    - agent_insights: what each agent has learned recently
    - relevant_context: fetches combined context for a task
    """

    def __init__(self, memory_store: Optional[MemoryStore] = None):
        self._memory = memory_store
        self._lock = asyncio.Lock()

        # Working memory: key -> {"value": Any, "expires_at": float}
        self._working_memory: Dict[str, Dict[str, Any]] = {}
        self._default_ttl_seconds = 3600  # 1 hour

        # Session contexts: session_id -> accumulated dict
        self._session_contexts: Dict[str, Dict[str, Any]] = {}

        # Agent insights: agent_name -> list of insight dicts
        self._agent_insights: Dict[str, List[Dict[str, Any]]] = {}
        self._max_insights_per_agent = 50

    # ------------------------------------------------------------------
    # Working memory (KV with TTL)
    # ------------------------------------------------------------------

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Store a value in working memory with TTL."""
        async with self._lock:
            expires = time.time() + (ttl or self._default_ttl_seconds)
            self._working_memory[key] = {"value": value, "expires_at": expires}

    async def get(self, key: str) -> Any:
        """Get a value from working memory. Returns None if expired/missing."""
        async with self._lock:
            entry = self._working_memory.get(key)
            if not entry:
                return None
            if time.time() > entry["expires_at"]:
                del self._working_memory[key]
                return None
            return entry["value"]

    async def delete(self, key: str) -> None:
        """Delete a key from working memory."""
        async with self._lock:
            self._working_memory.pop(key, None)

    async def list_keys(self, prefix: Optional[str] = None) -> List[str]:
        """List working memory keys, optionally filtered by prefix."""
        async with self._lock:
            now = time.time()
            keys = []
            for k, entry in list(self._working_memory.items()):
                if now > entry["expires_at"]:
                    del self._working_memory[k]
                    continue
                if prefix is None or k.startswith(prefix):
                    keys.append(k)
            return keys

    async def _cleanup_expired(self) -> None:
        """Remove expired entries."""
        async with self._lock:
            now = time.time()
            expired = [k for k, v in self._working_memory.items() if now > v["expires_at"]]
            for k in expired:
                del self._working_memory[k]

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def join_session(self, session_id: str, agent_name: str) -> None:
        """Register an agent as participating in a session."""
        async with self._lock:
            if session_id not in self._session_contexts:
                self._session_contexts[session_id] = {
                    "participants": [],
                    "history": [],
                    "metadata": {},
                }
            sess = self._session_contexts[session_id]
            if agent_name not in sess["participants"]:
                sess["participants"].append(agent_name)

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get a session's context."""
        async with self._lock:
            return self._session_contexts.get(session_id)

    async def update_session(self, session_id: str, updates: Dict[str, Any]) -> None:
        """Merge updates into a session context."""
        async with self._lock:
            if session_id not in self._session_contexts:
                self._session_contexts[session_id] = {
                    "participants": [],
                    "history": [],
                    "metadata": {},
                }
            sess = self._session_contexts[session_id]
            for key, value in updates.items():
                if key == "history" and isinstance(value, list):
                    sess["history"].extend(value)
                elif key == "metadata" and isinstance(value, dict):
                    sess["metadata"].update(value)
                else:
                    sess[key] = value

    async def add_to_session_history(self, session_id: str, entry: Dict[str, Any]) -> None:
        """Add an entry to a session's history."""
        async with self._lock:
            if session_id not in self._session_contexts:
                self._session_contexts[session_id] = {
                    "participants": [],
                    "history": [],
                    "metadata": {},
                }
            self._session_contexts[session_id]["history"].append({
                **entry,
                "timestamp": time.time(),
            })

    async def list_sessions(self) -> List[str]:
        """List active session IDs."""
        async with self._lock:
            return list(self._session_contexts.keys())

    # ------------------------------------------------------------------
    # Agent insights
    # ------------------------------------------------------------------

    async def add_insight(self, agent_name: str, insight: str,
                          capability: Optional[str] = None,
                          metadata: Optional[Dict[str, Any]] = None) -> None:
        """An agent shares what it learned."""
        async with self._lock:
            if agent_name not in self._agent_insights:
                self._agent_insights[agent_name] = []
            self._agent_insights[agent_name].append({
                "insight": insight,
                "capability": capability,
                "metadata": metadata or {},
                "timestamp": time.time(),
            })
            # Trim old insights
            if len(self._agent_insights[agent_name]) > self._max_insights_per_agent:
                self._agent_insights[agent_name] = self._agent_insights[agent_name][-self._max_insights_per_agent:]

    async def get_insights(self, agent_name: Optional[str] = None,
                           capability: Optional[str] = None,
                           limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent insights, optionally filtered."""
        async with self._lock:
            insights = []
            sources = [agent_name] if agent_name else list(self._agent_insights.keys())
            for name in sources:
                for ins in self._agent_insights.get(name, []):
                    if capability and ins.get("capability") != capability:
                        continue
                    insights.append({**ins, "agent": name})
            insights.sort(key=lambda x: x["timestamp"], reverse=True)
            return insights[:limit]

    # ------------------------------------------------------------------
    # Relevant context assembly
    # ------------------------------------------------------------------

    async def get_relevant_context(
        self,
        agent_name: str,
        task_description: str,
        capability: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Assemble relevant context for an agent about to execute a task.

        Returns a dict with:
        - session_history: recent events in this session
        - similar_experiences: past tasks from long-term memory
        - active_insights: what other agents have learned recently
        - working_memory: relevant KV entries
        """
        context: Dict[str, Any] = {
            "queried_at": time.time(),
            "for_agent": agent_name,
            "for_task": task_description,
        }

        # 1. Session history
        if session_id:
            sess = await self.get_session(session_id)
            if sess:
                context["session_participants"] = sess.get("participants", [])
                context["session_history"] = sess.get("history", [])[-10:]
            # Also query persistent memory for this session
            if self._memory:
                recent = await self._memory.get_recent_context(session_id, limit=5)
                context["persistent_session_context"] = recent

        # 2. Similar past experiences from persistent memory
        if self._memory:
            try:
                history = await self._memory.get_task_history(
                    agent_name=agent_name,
                    capability=capability,
                    status="completed",
                    limit=5,
                )
                context["similar_past_tasks"] = [
                    {
                        "title": h.get("title"),
                        "status": h.get("status"),
                        "output_summary": str(h.get("output_data", {}))[:200],
                    }
                    for h in history
                ]
            except Exception:
                context["similar_past_tasks"] = []

        # 3. Active insights from other agents
        other_insights = await self.get_insights(capability=capability, limit=10)
        other_insights = [i for i in other_insights if i.get("agent") != agent_name]
        context["active_insights_from_others"] = other_insights[:5]

        # 4. Relevant working memory
        relevant_wm = {}
        keys = await self.list_keys()
        if capability:
            cap_tokens = {t for t in capability.lower().split("_") if t}
            for k in keys:
                key_tokens = {t for t in k.lower().split("_") if t}
                if cap_tokens & key_tokens:
                    relevant_wm[k] = await self.get(k)
        if relevant_wm:
            context["working_memory"] = relevant_wm

        return context

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_status(self) -> Dict[str, Any]:
        """Get consciousness status."""
        async with self._lock:
            return {
                "working_memory_keys": len(self._working_memory),
                "active_sessions": len(self._session_contexts),
                "total_insights": sum(len(v) for v in self._agent_insights.values()),
                "agents_with_insights": list(self._agent_insights.keys()),
            }
