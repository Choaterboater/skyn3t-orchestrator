"""Adaptive Retry Manager — wires budget + taxonomy + Experience Index.

Consults per-signature retry counts, checks ProjectIterationBudget,
and queries the Experience Index for historically-successful fixes.
Beats Hermes' generic retry by being context-aware and learning-driven.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

from skyn3t.self_healing.budget import ProjectIterationBudget
from skyn3t.self_healing.error_taxonomy import ErrorTaxonomy, RecoveryHint
from skyn3t.self_healing.learned_generators import LearnedGeneratorManager

logger = logging.getLogger("skyn3t.self_healing.retry_manager")


class AdaptiveRetryManager:
    """Makes smart retry decisions using all available context."""

    def __init__(
        self,
        budget: ProjectIterationBudget,
        learned_manager: LearnedGeneratorManager,
        experience_store=None,  # ExperienceIndex (memory/store.py)
    ):
        self.budget = budget
        self.learned_manager = learned_manager
        self.experience_store = experience_store
        self._per_signature_counts: Dict[str, int] = {}  # error_sig → attempt count

    def should_retry(
        self,
        error_sig: str,
        retry_type: str,
        exception: Optional[Exception] = None,
        error_text: str = "",
        file_path: Optional[str] = None,
    ) -> Tuple[bool, RecoveryHint]:
        """Return (should_retry, recovery_hint) based on all context.

        1. Check ProjectIterationBudget for remaining retries
        2. Track per-signature retry counts
        3. Consult Experience Index for ranked fixes (avoid repeating failures)
        4. Classify the error for recovery hints
        """
        # Check budget first
        if not self.budget.consume(retry_type):
            logger.warning(
                "Budget exhausted for %s (used=%d/%d, type=%s=%d/%d)",
                error_sig,
                self.budget.total_used, self.budget.max_total,
                retry_type,
                self.budget.type_count(retry_type),
                self.budget._per_type_limits.get(retry_type, 2),
            )
            # Allow one grace call per type (like Hermes)
            if self.budget.use_grace_call(retry_type):
                logger.info("Grace call allowed for %s (type=%s)", error_sig, retry_type)
                return True, RecoveryHint(action="retry", retryable=True)
            return False, RecoveryHint(action="give_up", retryable=False)

        # Track per-signature attempts
        self._per_signature_counts[error_sig] = (
            self._per_signature_counts.get(error_sig, 0) + 1
        )
        sig_count = self._per_signature_counts[error_sig]

        # Check Experience Index for this signature
        if self.experience_store:
            try:
                # Get top-ranked fixes for this signature
                ranked = self.experience_store.rank_fixes_for_signature(error_sig)
                if ranked:
                    best = ranked[0]
                    if best.get("rate", 0) < 0.34 and sig_count >= 2:
                        # Historically low success rate — try learning a generator instead
                        logger.warning(
                            "Signature %s has low win rate (%.0f%%) after %d attempts — "
                            "trying learned generator",
                            error_sig, best.get("rate", 0) * 100, sig_count,
                        )
                        return True, RecoveryHint(
                            action="use_deterministic_generator",
                            retryable=True,
                            metadata={"file_path": file_path or ""},
                        )
            except Exception:
                logger.debug("Experience Index lookup failed for %s", error_sig)

        # Classify the error for recovery hints
        error_class, hint = ErrorTaxonomy.classify(
            exception=exception,
            error_text=error_text,
            file_path=file_path,
        )
        logger.info(
            "Retry decision for %s: class=%s, action=%s, attempts=%d",
            error_sig, error_class.value, hint.action, sig_count,
        )
        return True, hint

    def record_attempt(
        self,
        error_sig: str,
        fix_applied: str,
        success: bool,
    ) -> None:
        """Record an attempt to the Experience Index."""
        if self.experience_store:
            try:
                self.experience_store.record_experience_index(
                    task_id="",
                    stack="",
                    stage="",
                    error_signature=error_sig,
                    fix_applied=fix_applied,
                    fix_worked=success,
                    success=success,
                )
            except Exception:
                logger.debug("Failed to record attempt for %s", error_sig)

    def get_signature_count(self, error_sig: str) -> int:
        """Return how many times this signature has been attempted."""
        return self._per_signature_counts.get(error_sig, 0)

    def reset_signature(self, error_sig: str) -> None:
        """Reset the counter for a signature (e.g., after learning a generator)."""
        self._per_signature_counts.pop(error_sig, None)
