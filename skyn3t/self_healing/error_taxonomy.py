"""Structured error classification — beats Hermes' API-focused ErrorClassifier.

Classifies code-generation errors (stub, missing_file, build_error, etc.)
with recovery hints tied to Skyn3t's workflow (use_deterministic_generator,
use_llm_backfill, retry, give_up).
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class ErrorClass(enum.Enum):
    """Error categories specific to Skyn3t's code-generation pipeline."""

    STUB_ERROR = "stub_error"          # UnresolvedScaffoldStubError, TODO stubs
    MISSING_FILE = "missing_file"          # Import target not on disk
    BUILD_ERROR = "build_error"          # vite/next build failures
    TIMEOUT = "timeout"                  # LLM or build timeout
    CONTEXT_OVERFLOW = "context_overflow"  # Token limit exceeded
    RATE_LIMIT = "rate_limit"              # 429, quota exceeded
    UNKNOWN = "unknown"                  # Catch-all


@dataclass
class RecoveryHint:
    """What to do after classifying an error."""

    action: str  # "retry" | "use_llm_backfill" | "use_deterministic_generator" | "give_up"
    retryable: bool = True
    should_compress: bool = False
    should_rotate: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class ErrorTaxonomy:
    """Classifies exceptions into structured (ErrorClass, RecoveryHint) pairs.

    Extends Hermes' FailoverReason with code-generation-specific
    categories and recovery hints that map to Skyn3t's actual workflow.
    """

    # Stub patterns — files that ship as TODO placeholders
    _STUB_PATTERNS = [
        "unresolvedScaffoldStubError",
        "skyn3t-backfill-stub",
        "TODO[skyn3t]",
        "code generation failed",
        "unresolved TODO stub",
    ]

    # Missing file patterns
    _MISSING_FILE_PATTERNS = [
        "no such file",
        "module not found",
        "cannot find",
        "does not exist",
        "not found",
    ]

    # Timeout patterns
    _TIMEOUT_PATTERNS = [
        "timeout",
        "timed out",
        "deadline exceeded",
        "stream idle",
    ]

    # Context overflow patterns
    _CONTEXT_OVERFLOW_PATTERNS = [
        "context length",
        "token limit",
        "too many tokens",
        "exceeds.*limit",
        "maximum context",
    ]

    @classmethod
    def classify(cls, exception: Optional[Exception] = None,
                 error_text: str = "",
                 file_path: str = None) -> tuple[ErrorClass, RecoveryHint]:
        """Classify an error into (ErrorClass, RecoveryHint)."""
        text = error_text
        if exception:
            text = text or str(exception)
            # Check for specific exception types
            exc_name = type(exception).__name__
            if exc_name == "UnresolvedScaffoldStubError":
                return cls._classify_stub_error(file_path)
            if exc_name == "MissingPlannedFilesError":
                return ErrorClass.MISSING_FILE, RecoveryHint(
                    action="use_deterministic_generator",
                    retryable=True,
                    metadata={"file_path": file_path or ""},
                )
            if exc_name == "StackShapeMismatchError":
                return ErrorClass.BUILD_ERROR, RecoveryHint(
                    action="retry",
                    retryable=True,
                )

        if not text:
            return ErrorClass.UNKNOWN, RecoveryHint(action="give_up", retryable=False)

        text_lower = text.lower()

        # Check stub patterns
        for pattern in cls._STUB_PATTERNS:
            if pattern.lower() in text_lower:
                return cls._classify_stub_error(file_path)

        # Check missing file
        for pattern in cls._MISSING_FILE_PATTERNS:
            if pattern in text_lower:
                return ErrorClass.MISSING_FILE, RecoveryHint(
                    action="use_deterministic_generator",
                    retryable=True,
                    metadata={"file_path": file_path or ""},
                )

        # Check timeout
        for pattern in cls._TIMEOUT_PATTERNS:
            if pattern in text_lower:
                return ErrorClass.TIMEOUT, RecoveryHint(
                    action="retry",
                    retryable=True,
                )

        # Check context overflow
        for pattern in cls._CONTEXT_OVERFLOW_PATTERNS:
            if re.search(pattern, text_lower):
                return ErrorClass.CONTEXT_OVERFLOW, RecoveryHint(
                    action="retry",
                    retryable=True,
                    should_compress=True,
                )

        return ErrorClass.UNKNOWN, RecoveryHint(action="retry", retryable=True)

    @classmethod
    def _classify_stub_error(cls, file_path: str = None) -> tuple[ErrorClass, RecoveryHint]:
        """Stub errors should trigger deterministic generator creation."""
        return ErrorClass.STUB_ERROR, RecoveryHint(
            action="use_deterministic_generator",
            retryable=True,
            metadata={"file_path": file_path or ""},
        )

    @classmethod
    def classify_build_error(cls, build_log: str) -> tuple[ErrorClass, RecoveryHint]:
        """Classify a build error from log text."""
        return cls.classify(error_text=build_log)
