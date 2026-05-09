"""Smart agent selection with scoring, fallback chains, A/B testing, and cost awareness."""

import asyncio
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from skyn3t.core.agent import BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import Event, EventBus, EventType


@dataclass
class AgentPerformanceRecord:
    """Performance metrics for a single agent."""

    agent_name: str
    total_tasks: int = 0
    successful_tasks: int = 0
    failed_tasks: int = 0
    total_latency_ms: float = 0.0
    task_latencies_ms: List[float] = field(default_factory=list)
    last_used: Optional[datetime] = None
    error_patterns: Dict[str, int] = field(default_factory=dict)
    ab_test_group: Optional[str] = None
    cost_per_task: float = 0.0
    capability_scores: Dict[str, float] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        if self.total_tasks == 0:
            return 0.5  # Neutral prior
        return self.successful_tasks / self.total_tasks

    @property
    def average_latency_ms(self) -> float:
        if not self.task_latencies_ms:
            return 0.0
        return statistics.mean(self.task_latencies_ms[-50:])

    @property
    def p95_latency_ms(self) -> float:
        if not self.task_latencies_ms:
            return float("inf")
        sorted_lat = sorted(self.task_latencies_ms[-100:])
        idx = int(len(sorted_lat) * 0.95)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    def record_task(self, success: bool, latency_ms: float, capability: str = "") -> None:
        self.total_tasks += 1
        self.last_used = datetime.utcnow()
        self.total_latency_ms += latency_ms
        self.task_latencies_ms.append(latency_ms)
        if len(self.task_latencies_ms) > 500:
            self.task_latencies_ms = self.task_latencies_ms[-500:]
        if success:
            self.successful_tasks += 1
        else:
            self.failed_tasks += 1
        if capability:
            # Exponential moving average for capability score
            alpha = 0.3
            current = self.capability_scores.get(capability, 0.5)
            self.capability_scores[capability] = current + alpha * (float(success) - current)

    def record_error_pattern(self, error: str) -> None:
        self.error_patterns[error] = self.error_patterns.get(error, 0) + 1


class AgentPerformanceRegistry:
    """Registry of agent performance metrics."""

    def __init__(self):
        self._records: Dict[str, AgentPerformanceRecord] = {}
        self._capability_index: Dict[str, List[str]] = {}  # capability -> agent names
        self._lock = asyncio.Lock()

    async def get_or_create(self, agent_name: str) -> AgentPerformanceRecord:
        async with self._lock:
            if agent_name not in self._records:
                self._records[agent_name] = AgentPerformanceRecord(agent_name=agent_name)
            return self._records[agent_name]

    async def record_result(
        self,
        agent_name: str,
        success: bool,
        latency_ms: float,
        capability: str = "",
        error: Optional[str] = None,
    ) -> None:
        record = await self.get_or_create(agent_name)
        record.record_task(success, latency_ms, capability)
        if error:
            record.record_error_pattern(error[:200])

    async def update_capability_index(self, agent_name: str, capabilities: List[str]) -> None:
        async with self._lock:
            for cap in capabilities:
                if cap not in self._capability_index:
                    self._capability_index[cap] = []
                if agent_name not in self._capability_index[cap]:
                    self._capability_index[cap].append(agent_name)

    async def get_agents_for_capability(self, capability: str) -> List[str]:
        async with self._lock:
            return self._capability_index.get(capability, []).copy()

    def get_record(self, agent_name: str) -> Optional[AgentPerformanceRecord]:
        return self._records.get(agent_name)

    def get_all_records(self) -> Dict[str, AgentPerformanceRecord]:
        return self._records.copy()

    def get_top_agents(self, capability: str, n: int = 5) -> List[Tuple[str, float]]:
        """Return top N agents by capability score."""
        agents = self._capability_index.get(capability, [])
        scored = []
        for name in agents:
            rec = self._records.get(name)
            if rec:
                score = rec.capability_scores.get(capability, rec.success_rate)
                scored.append((name, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:n]

    def export_stats(self) -> Dict[str, Any]:
        return {
            name: {
                "success_rate": rec.success_rate,
                "avg_latency_ms": rec.average_latency_ms,
                "p95_latency_ms": rec.p95_latency_ms,
                "total_tasks": rec.total_tasks,
                "capability_scores": rec.capability_scores,
                "cost_per_task": rec.cost_per_task,
            }
            for name, rec in self._records.items()
        }


@dataclass
class FallbackChain:
    """A chain of agents to try in order if previous ones fail."""

    chain_id: str = field(default_factory=lambda: str(uuid4()))
    agent_names: List[str] = field(default_factory=list)
    max_attempts_per_agent: int = 2
    current_index: int = 0
    attempts_on_current: int = 0

    def next_agent(self) -> Optional[str]:
        if self.current_index >= len(self.agent_names):
            return None
        name = self.agent_names[self.current_index]
        self.attempts_on_current += 1
        if self.attempts_on_current >= self.max_attempts_per_agent:
            self.current_index += 1
            self.attempts_on_current = 0
        return name

    def reset(self) -> None:
        self.current_index = 0
        self.attempts_on_current = 0


class ABTestManager:
    """Manages A/B tests between agents or configurations."""

    @dataclass
    class Experiment:
        experiment_id: str
        name: str
        variants: List[str]  # agent names or config ids
        traffic_split: List[float]  # must sum to 1.0
        results: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
        active: bool = True

    def __init__(self):
        self._experiments: Dict[str, ABTestManager.Experiment] = {}
        self._variant_map: Dict[str, str] = {}  # task_id -> experiment_id

    def create_experiment(
        self,
        name: str,
        variants: List[str],
        traffic_split: Optional[List[float]] = None,
    ) -> str:
        if traffic_split is None:
            traffic_split = [1.0 / len(variants)] * len(variants)
        total = sum(traffic_split)
        traffic_split = [s / total for s in traffic_split]
        exp = self.Experiment(
            experiment_id=str(uuid4()),
            name=name,
            variants=variants,
            traffic_split=traffic_split,
        )
        for v in variants:
            exp.results[v] = []
        self._experiments[exp.experiment_id] = exp
        return exp.experiment_id

    def assign_variant(self, experiment_id: str, task_id: str) -> Optional[str]:
        exp = self._experiments.get(experiment_id)
        if not exp or not exp.active:
            return None
        self._variant_map[task_id] = experiment_id
        return random.choices(exp.variants, weights=exp.traffic_split)[0]

    def record_result(
        self, experiment_id: str, variant: str, success: bool, latency_ms: float
    ) -> None:
        exp = self._experiments.get(experiment_id)
        if exp:
            exp.results[variant].append(
                {"success": success, "latency_ms": latency_ms, "timestamp": datetime.utcnow()}
            )

    def get_winner(self, experiment_id: str) -> Optional[str]:
        """Return the winning variant based on success rate."""
        exp = self._experiments.get(experiment_id)
        if not exp:
            return None
        best_variant = None
        best_score = -1.0
        for variant, results in exp.results.items():
            if not results:
                continue
            successes = sum(1 for r in results if r["success"])
            score = successes / len(results)
            if score > best_score:
                best_score = score
                best_variant = variant
        return best_variant

    def get_experiment_report(self, experiment_id: str) -> Dict[str, Any]:
        exp = self._experiments.get(experiment_id)
        if not exp:
            return {}
        report = {"name": exp.name, "variants": {}}
        for variant, results in exp.results.items():
            if not results:
                report["variants"][variant] = {"tasks": 0, "success_rate": 0, "avg_latency_ms": 0}
                continue
            successes = sum(1 for r in results if r["success"])
            avg_lat = statistics.mean(r["latency_ms"] for r in results)
            report["variants"][variant] = {
                "tasks": len(results),
                "success_rate": successes / len(results),
                "avg_latency_ms": avg_lat,
            }
        report["winner"] = self.get_winner(experiment_id)
        return report


class AgentSelector:
    """Selects the best agent for a task using multi-factor scoring."""

    DEFAULT_WEIGHTS = {
        "capability": 0.30,
        "success_rate": 0.25,
        "latency": 0.20,
        "load": 0.15,
        "cost": 0.10,
    }

    def __init__(
        self,
        registry: Optional[AgentPerformanceRegistry] = None,
        ab_test_manager: Optional[ABTestManager] = None,
        weights: Optional[Dict[str, float]] = None,
        cost_provider: Optional[Callable[[BaseAgent], float]] = None,
    ):
        self.registry = registry or AgentPerformanceRegistry()
        self.ab_tests = ab_test_manager or ABTestManager()
        self.weights = {**self.DEFAULT_WEIGHTS, **(weights or {})}
        self._cost_provider = cost_provider or self._default_cost_provider
        self._fallback_chains: Dict[str, FallbackChain] = {}
        self._event_bus: Optional[EventBus] = None

    def attach_event_bus(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        event_bus.subscribe(self._on_task_completed, EventType.TASK_COMPLETED)
        event_bus.subscribe(self._on_task_failed, EventType.TASK_FAILED)

    def _default_cost_provider(self, agent: BaseAgent) -> float:
        """Default cost heuristic based on provider name."""
        cost_map = {
            "openai": 1.0,
            "anthropic": 1.2,
            "local": 0.1,
            "ollama": 0.05,
            "huggingface": 0.3,
        }
        return cost_map.get(agent.provider.lower(), 0.5)

    def _normalize(self, values: List[float], invert: bool = False) -> List[float]:
        if not values:
            return []
        min_v, max_v = min(values), max(values)
        if max_v == min_v:
            return [0.5] * len(values)
        normalized = [(v - min_v) / (max_v - min_v) for v in values]
        if invert:
            normalized = [1.0 - n for n in normalized]
        return normalized

    def score_agents(
        self,
        agents: List[BaseAgent],
        capability: Optional[str] = None,
        cost_budget: Optional[float] = None,
    ) -> List[Tuple[BaseAgent, float]]:
        """Score agents and return sorted list of (agent, score) tuples."""
        if not agents:
            return []

        records = [self.registry.get_record(a.name) for a in agents]

        # Capability scores
        cap_scores = []
        for agent, rec in zip(agents, records):
            if not capability:
                cap_scores.append(1.0)
            elif rec and capability in rec.capability_scores:
                cap_scores.append(rec.capability_scores[capability])
            else:
                cap_scores.append(
                    1.0 if any(c.name == capability for c in agent.capabilities) else 0.0
                )
        cap_norm = self._normalize(cap_scores)

        # Success rates
        sr_scores = [r.success_rate if r else 0.5 for r in records]
        sr_norm = self._normalize(sr_scores)

        # Latency (invert: lower is better)
        lat_scores = [r.p95_latency_ms if r else 5000.0 for r in records]
        lat_norm = self._normalize(lat_scores, invert=True)

        # Load (invert: less busy is better)
        load_scores = []
        for a in agents:
            q = getattr(a, "_task_queue", None)
            load_scores.append(q.qsize() if q else 0)
        load_norm = self._normalize(load_scores, invert=True)

        # Cost (invert: cheaper is better)
        cost_scores = [self._cost_provider(a) for a in agents]
        cost_norm = self._normalize(cost_scores, invert=True)

        scored = []
        for i, agent in enumerate(agents):
            if cost_budget is not None and cost_scores[i] > cost_budget:
                continue
            score = (
                self.weights["capability"] * cap_norm[i]
                + self.weights["success_rate"] * sr_norm[i]
                + self.weights["latency"] * lat_norm[i]
                + self.weights["load"] * load_norm[i]
                + self.weights["cost"] * cost_norm[i]
            )
            scored.append((agent, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    async def select(
        self,
        agents: List[BaseAgent],
        task: TaskRequest,
        capability: Optional[str] = None,
        cost_budget: Optional[float] = None,
        experiment_id: Optional[str] = None,
        min_score_threshold: float = 0.0,
    ) -> Optional[BaseAgent]:
        """Select the best agent for a task."""
        # A/B test assignment
        if experiment_id:
            variant = self.ab_tests.assign_variant(experiment_id, task.task_id)
            if variant:
                agent = next((a for a in agents if a.name == variant), None)
                if agent:
                    return agent

        capable = [
            a for a in agents
            if a.status in ("idle", "busy")
            and (not capability or any(c.name == capability for c in a.capabilities))
        ]

        scored = self.score_agents(capable, capability, cost_budget)
        if not scored:
            return None

        best_agent, best_score = scored[0]
        if best_score < min_score_threshold and len(scored) > 1:
            return scored[1][0]
        return best_agent

    def create_fallback_chain(
        self,
        chain_id: str,
        agent_names: List[str],
        max_attempts_per_agent: int = 2,
    ) -> FallbackChain:
        chain = FallbackChain(
            chain_id=chain_id,
            agent_names=agent_names,
            max_attempts_per_agent=max_attempts_per_agent,
        )
        self._fallback_chains[chain_id] = chain
        return chain

    async def select_with_fallback(
        self,
        agents: Dict[str, BaseAgent],
        task: TaskRequest,
        capability: Optional[str] = None,
        chain_id: Optional[str] = None,
    ) -> Tuple[Optional[BaseAgent], Optional[FallbackChain]]:
        """Select agent with optional fallback chain."""
        agent_list = list(agents.values())
        selected = await self.select(agent_list, task, capability)

        if chain_id and selected:
            chain = self._fallback_chains.get(chain_id)
            if chain:
                # Reorder chain to start with selected agent
                if selected.name in chain.agent_names:
                    idx = chain.agent_names.index(selected.name)
                    chain.agent_names = chain.agent_names[idx:] + chain.agent_names[:idx]
                chain.reset()
                return selected, chain
            else:
                # Auto-create fallback chain from scored agents
                scored = self.score_agents(agent_list, capability)
                names = [a.name for a, _ in scored[:3]]
                chain = self.create_fallback_chain(chain_id, names)
                return selected, chain

        return selected, None

    def _on_task_completed(self, event: Event) -> None:
        payload = event.payload
        agent_name = event.source
        task_id = payload.get("task_id")
        latency = payload.get("execution_time_ms", 0.0)
        asyncio.create_task(
            self.registry.record_result(agent_name, True, latency)
        )
        # Check if part of an A/B test
        for exp_id, exp in self.ab_tests._experiments.items():
            if task_id in self.ab_tests._variant_map:
                variant = next(
                    (v for v in exp.variants if v == agent_name), None
                )
                if variant:
                    self.ab_tests.record_result(exp_id, variant, True, latency)

    def _on_task_failed(self, event: Event) -> None:
        payload = event.payload
        agent_name = event.source
        task_id = payload.get("task_id")
        latency = payload.get("execution_time_ms", 0.0)
        error = payload.get("error", "unknown")
        asyncio.create_task(
            self.registry.record_result(agent_name, False, latency, error=error)
        )
        for exp_id, exp in self.ab_tests._experiments.items():
            if task_id in self.ab_tests._variant_map:
                variant = next(
                    (v for v in exp.variants if v == agent_name), None
                )
                if variant:
                    self.ab_tests.record_result(exp_id, variant, False, latency)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "registry": self.registry.export_stats(),
            "experiments": {
                eid: self.ab_tests.get_experiment_report(eid)
                for eid in self.ab_tests._experiments
            },
            "weights": self.weights,
        }
