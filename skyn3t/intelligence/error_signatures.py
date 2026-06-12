"""Stable error-signature derivation for the experience index.

The experience_index ranks fixes by ``error_signature``. For the
ranker to be useful, the same kind of failure must always get the
same signature string — across runs, machines, and minor brief
wording changes. These helpers extract that signature from the
structured findings the contract verifier and consistency reviewer
emit.

Signature format:
    "<source>:<category>"           — when no file is identified
    "<source>:<category>:<file>"    — when the finding targets a file

Examples:
    "contract:palette_schism"
    "contract:placeholder_leak:App.jsx"
    "consistency:missing_mount:vite.config.js"
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def signature_for_finding(
    finding: Dict[str, Any],
    *,
    source: str = "contract",
) -> Optional[str]:
    """Derive a stable signature from one finding dict.

    Returns None when the finding has no usable category — better to
    record nothing than to bucket every unknown failure under
    ``unknown``.
    """
    if not isinstance(finding, dict):
        return None
    raw_category = (
        finding.get("category")
        or finding.get("rule")
        or finding.get("rule_id")
        or finding.get("kind")
        or ""
    )
    category = str(raw_category).strip().lower().replace(" ", "_")
    if not category:
        return None
    path = str(finding.get("file") or finding.get("path") or "").strip()
    basename = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if path else ""
    src = str(source or "").strip().lower() or "unknown"
    if basename:
        return f"{src}:{category}:{basename}"
    return f"{src}:{category}"


# Build-log error messages don't carry a structured ``category``. Map
# the message prefix to a stable category so the index buckets them
# the same way across runs. Patterns are ordered: most specific first.
_BUILD_ERROR_CATEGORIES: List[tuple] = [
    ("missing module:", "missing_module"),
    ("missing dependency:", "missing_dependency"),
    ("missing export", "missing_export"),
    ("syntaxerror", "syntax_error"),
    ("unexpected token", "syntax_error"),
    ("unterminated string", "syntax_error"),
    ("parsing error", "syntax_error"),
    ("type error", "type_error"),
    ("cannot find name", "type_error"),
    ("does not exist on type", "type_error"),
]


def signature_for_build_issue(
    issue: Any,
    *,
    source: str = "build",
) -> Optional[str]:
    """Derive a signature from a ``FileIssue`` produced by the build-log
    parser. Build errors don't carry a structured ``category`` field
    like contract/consistency findings do — instead we classify based
    on the error-message prefix using a small lookup table.

    ``issue`` may be a ``FileIssue`` dataclass instance or a dict with
    ``error_message`` + ``path`` fields. Returns None when no category
    matches (better silent than poisoned ``unknown`` bucket).
    """
    if issue is None:
        return None
    msg = (
        getattr(issue, "error_message", None)
        or (issue.get("error_message") if isinstance(issue, dict) else None)
        or ""
    )
    msg_lower = str(msg).strip().lower()
    if not msg_lower:
        return None
    category: Optional[str] = None
    for prefix, label in _BUILD_ERROR_CATEGORIES:
        if prefix in msg_lower:
            category = label
            break
    if category is None:
        return None
    path = (
        getattr(issue, "path", None)
        or (issue.get("path") if isinstance(issue, dict) else None)
        or ""
    )
    basename = str(path).rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if path else ""
    src = str(source or "").strip().lower() or "build"
    if basename:
        return f"{src}:{category}:{basename}"
    return f"{src}:{category}"


def signature_for_build_issues(
    issues: List[Any],
    *,
    source: str = "build",
) -> Optional[str]:
    """Pick the first issue with a classifiable category. Build logs
    usually surface one root cause (the rest are downstream noise);
    grabbing the first matched signature is the right heuristic."""
    if not issues:
        return None
    for issue in issues:
        sig = signature_for_build_issue(issue, source=source)
        if sig:
            return sig
    return None


def signatures_for_blockers(
    blockers: List[Any],
    *,
    source: str = "reviewer",
) -> List[str]:
    """Bucket a list of reviewer/contract blockers into stable signatures.

    Thin wrapper used by intelligence.reflection.build_retry_directive: where
    ``signature_for_findings`` collapses a finding list to ONE dominant
    signature, the reflective-retry directive wants the full set so the
    experience index / routing can correlate every distinct failure bucket a
    retry was reacting to.

    Returns an order-preserving, de-duplicated list (empty when nothing is
    classifiable — never poisons callers with ``unknown`` buckets).
    """
    if not blockers:
        return []
    out: List[str] = []
    seen = set()
    for blocker in blockers:
        if not isinstance(blocker, dict):
            continue
        sig = signature_for_finding(blocker, source=source)
        if sig and sig not in seen:
            seen.add(sig)
            out.append(sig)
    return out


def signature_for_findings(
    findings: List[Any],
    *,
    source: str = "contract",
) -> Optional[str]:
    """Pick a dominant signature from a list of findings.

    Prefers findings with ``severity == "blocker"`` so the recorded
    signature reflects the actual blocking issue rather than a
    cosmetic warning. Falls through to the first usable finding when
    no blockers are present.
    """
    if not findings:
        return None
    blockers = [
        f for f in findings
        if isinstance(f, dict) and f.get("severity") == "blocker"
    ]
    pool = blockers or [f for f in findings if isinstance(f, dict)]
    for f in pool:
        sig = signature_for_finding(f, source=source)
        if sig:
            return sig
    return None
