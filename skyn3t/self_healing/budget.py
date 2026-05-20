"""Smart iteration budget — beats Hermes' per-agent counter.

Per-project, per-retry-type counters with grace calls per type.
Refunds non-LLM operations (like Hermes' refund() method).
"""

from __future__ import annotations

import threading
from typing import Dict, Optional


DEFAULT_TOTAL_BUDGET = 20
DEFAULT_STUB_RETRY_MAX = 5
DEFAULT_FIX_RETRY_MAX = 2  # build_fix, boot_fix, integration_fix


class ProjectIterationBudget:
    """Thread-safe budget scoped to a single project (not just a single agent).

    Hermes has per-agent IterationBudget — we extend that with:
    - Per-retry-type counters (stub_retry vs build_fix vs boot_fix)
    - Grace call per type (Hermes only has global grace)
    - Refund for non-LLM operations
    """

    def __init__(
        self,
        project_id: str,
        max_total: int = DEFAULT_TOTAL_BUDGET,
        per_type_limits: Optional[Dict[str, int]] = None,
    ):
        self.project_id = project_id
        self.max_total = max_total
        self._total_used = 0
        self._type_counters: Dict[str, int] = {}
        self._grace_used: Dict[str, bool] = {}
        self._lock = threading.Lock()

        # Per-type limits (override defaults)
        self._per_type_limits = per_type_limits or {
            "stub_retry": DEFAULT_STUB_RETRY_MAX,
            "build_fix": DEFAULT_FIX_RETRY_MAX,
            "boot_fix": DEFAULT_FIX_RETRY_MAX,
            "integration_fix": DEFAULT_FIX_RETRY_MAX,
            "code_critique": 4,
        }

    def consume(self, retry_type: str, is_llm: bool = True) -> bool:
        """Try to consume one iteration. Returns True if allowed."""
        with self._lock:
            if self._total_used >= self.max_total:
                return False
            type_used = self._type_counters.get(retry_type, 0)
            type_limit = self._per_type_limits.get(retry_type, 2)
            if type_used >= type_limit:
                return False
            self._total_used += 1
            self._type_counters[retry_type] = type_used + 1
            return True

    def refund(self, retry_type: str) -> None:
        """Give back one iteration (e.g., for non-LLM operations)."""
        with self._lock:
            if self._total_used > 0:
                self._total_used -= 1
            type_used = self._type_counters.get(retry_type, 0)
            if type_used > 0:
                self._type_counters[retry_type] = type_used - 1

    def use_grace_call(self, retry_type: str) -> bool:
        """Allow one extra LLM call after budget exhausted (like Hermes)."""
        with self._lock:
            if self._grace_used.get(retry_type, False):
                return False  # Already used grace for this type
            self._grace_used[retry_type] = True
            return True

    def is_exhausted(self, retry_type: str = None) -> bool:
        """Check if budget is exhausted (globally or per-type)."""
        with self._lock:
            if self._total_used >= self.max_total:
                return True
            if retry_type:
                type_used = self._type_counters.get(retry_type, 0)
                type_limit = self._per_type_limits.get(retry_type, 2)
                return type_used >= type_limit
            return False

    @property
    def total_used(self) -> int:
        with self._lock:
            return self._total_used

    @property
    def total_remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._total_used)

    def type_count(self, retry_type: str) -> int:
        with self._lock:
            return self._type_counters.get(retry_type, 0)

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"ProjectIterationBudget(project={self.project_id}, "
                f"used={self._total_used}/{self.max_total}, "
                f"types={dict(self._type_counters)})"
            )
