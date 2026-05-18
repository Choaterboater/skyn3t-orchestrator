"""Fallback and circuit breaker system for agent resilience."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import Event, EventBus, EventType


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = auto()      # Normal operation
    OPEN = auto()        # Failing, reject requests
    HALF_OPEN = auto()   # Testing if recovered


@dataclass
class CircuitBreaker:
    """Circuit breaker for an agent."""

    agent_name: str
    failure_threshold: int = 3
    recovery_timeout_seconds: int = 60
    half_open_max_calls: int = 1

    _failures: int = 0
    _last_failure_time: Optional[datetime] = None
    _state: CircuitState = CircuitState.CLOSED
    _half_open_calls: int = 0
    _lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def record_success(self) -> None:
        async with self._get_lock():
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_calls += 1
                if self._half_open_calls >= self.half_open_max_calls:
                    self._state = CircuitState.CLOSED
                    self._failures = 0
                    self._half_open_calls = 0
            else:
                self._failures = 0

    async def record_failure(self) -> None:
        async with self._get_lock():
            self._failures += 1
            self._last_failure_time = datetime.now(timezone.utc)

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._half_open_calls = 0
            elif self._failures >= self.failure_threshold:
                self._state = CircuitState.OPEN

    async def can_execute(self) -> bool:
        async with self._get_lock():
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                if self._last_failure_time:
                    elapsed = (datetime.now(timezone.utc) - self._last_failure_time).total_seconds()
                    if elapsed >= self.recovery_timeout_seconds:
                        self._state = CircuitState.HALF_OPEN
                        self._half_open_calls = 0
                        return True
                return False

            if self._state == CircuitState.HALF_OPEN:
                return self._half_open_calls < self.half_open_max_calls

            return True

    def get_status(self) -> Dict[str, Any]:
        return {
            "agent": self.agent_name,
            "state": self._state.name,
            "failures": self._failures,
            "last_failure": self._last_failure_time.isoformat() if self._last_failure_time else None,
        }


@dataclass
class FallbackChain:
    """Ordered chain of agents for fallback routing."""

    capability: str
    agent_names: List[str]
    strategy: str = "round_robin"  # round_robin, priority, least_loaded

    _current_index: int = 0

    def get_next(self, available_agents: Dict[str, BaseAgent]) -> Optional[str]:
        """Get the next available agent in the chain."""
        # Filter to available agents in the chain
        candidates = [name for name in self.agent_names if name in available_agents]
        if not candidates:
            return None

        if self.strategy == "priority":
            # Return first available
            return candidates[0]

        if self.strategy == "least_loaded":
            # Return agent with smallest queue
            return min(
                candidates,
                key=lambda n: available_agents[n]._task_queue.qsize(),
            )

        # Round robin
        idx = self._current_index % len(candidates)
        self._current_index = (self._current_index + 1) % len(candidates)
        return candidates[idx]


class FallbackManager:
    """Manages circuit breakers and fallback chains for resilient agent execution."""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.circuits: Dict[str, CircuitBreaker] = {}
        self.chains: Dict[str, FallbackChain] = {}
        self._fallback_history: List[Dict[str, Any]] = []
        self._max_history = 100

        # Subscribe to failure events
        self.event_bus.subscribe(self._on_task_failed, EventType.TASK_FAILED)
        self.event_bus.subscribe(self._on_task_completed, EventType.TASK_COMPLETED)

    def register_circuit(self, agent_name: str, **kwargs) -> CircuitBreaker:
        """Register a circuit breaker for an agent."""
        cb = CircuitBreaker(agent_name=agent_name, **kwargs)
        self.circuits[agent_name] = cb
        return cb

    def register_chain(self, capability: str, agent_names: List[str], strategy: str = "round_robin") -> FallbackChain:
        """Register a fallback chain for a capability."""
        chain = FallbackChain(capability=capability, agent_names=agent_names, strategy=strategy)
        self.chains[capability] = chain
        return chain

    async def execute_with_fallback(
        self,
        task: TaskRequest,
        agents: Dict[str, BaseAgent],
        preferred_agent: Optional[str] = None,
        capability: Optional[str] = None,
    ) -> TaskResult:
        """Execute a task with automatic fallback on failure."""
        attempted: List[str] = []
        errors: List[str] = []

        # Determine agent order
        agent_order: List[str] = []

        if preferred_agent and preferred_agent in agents:
            agent_order.append(preferred_agent)

        if capability and capability in self.chains:
            chain = self.chains[capability]
            chain_agent = chain.get_next(agents)
            if chain_agent and chain_agent not in agent_order:
                agent_order.append(chain_agent)

        # Add remaining agents as ultimate fallback
        for name in agents:
            if name not in agent_order:
                agent_order.append(name)

        # Try each agent
        for agent_name in agent_order:
            agent = agents[agent_name]
            circuit = self.circuits.get(agent_name)

            # Check circuit breaker
            if circuit and not await circuit.can_execute():
                errors.append(f"{agent_name}: Circuit breaker OPEN")
                continue

            attempted.append(agent_name)

            try:
                result = await agent.execute(task)

                if result.success:
                    if circuit:
                        await circuit.record_success()
                    return result
                else:
                    # Task failed - record and try next
                    error_msg = result.error or "Unknown error"
                    errors.append(f"{agent_name}: {error_msg}")

                    if circuit:
                        await circuit.record_failure()

                    # Publish fallback event
                    self.event_bus.publish(
                        Event(
                            event_type=EventType.AGENT_COLLABORATION,
                            source="fallback_manager",
                            payload={
                                "action": "fallback",
                                "task_id": task.task_id,
                                "failed_agent": agent_name,
                                "error": error_msg,
                                "next_agents": [a for a in agent_order if a not in attempted],
                            },
                            correlation_id=task.task_id,
                        )
                    )

            except Exception as e:
                errors.append(f"{agent_name}: Exception - {str(e)}")
                if circuit:
                    await circuit.record_failure()

        # All agents failed
        self._record_fallback(task.task_id, attempted, errors)

        return TaskResult(
            task_id=task.task_id,
            success=False,
            error=f"All agents failed. Attempted: {', '.join(attempted)}. Errors: {'; '.join(errors[:3])}",
            output={"attempted_agents": attempted, "errors": errors},
        )

    def _on_task_failed(self, event: Event) -> None:
        """Handle task failure events."""
        agent_name = event.source
        if agent_name in self.circuits:
            asyncio.create_task(self.circuits[agent_name].record_failure())

    def _on_task_completed(self, event: Event) -> None:
        """Handle task completion events."""
        agent_name = event.source
        if agent_name in self.circuits:
            asyncio.create_task(self.circuits[agent_name].record_success())

    def _record_fallback(self, task_id: str, attempted: List[str], errors: List[str]) -> None:
        """Record fallback history."""
        self._fallback_history.append(
            {
                "task_id": task_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "attempted": attempted,
                "errors": errors,
            }
        )
        if len(self._fallback_history) > self._max_history:
            self._fallback_history = self._fallback_history[-self._max_history :]

    def get_status(self) -> Dict[str, Any]:
        """Get fallback manager status."""
        return {
            "circuits": {name: cb.get_status() for name, cb in self.circuits.items()},
            "chains": {
                cap: {"agents": chain.agent_names, "strategy": chain.strategy}
                for cap, chain in self.chains.items()
            },
            "recent_fallbacks": self._fallback_history[-10:],
        }
