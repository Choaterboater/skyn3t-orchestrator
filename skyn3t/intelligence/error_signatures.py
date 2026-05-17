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
