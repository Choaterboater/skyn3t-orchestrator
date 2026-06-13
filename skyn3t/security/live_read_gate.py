"""Approval gate for live-read networking API credentials."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

_APPROVAL_FILE = Path("./data/live_read_approvals.json")

_LIVE_READ_ENV_KEYS = (
    "ARUBA_CENTRAL_CLIENT_ID",
    "ARUBA_CENTRAL_CLIENT_SECRET",
    "ARUBA_CENTRAL_CUSTOMER_ID",
    "ARUBA_CENTRAL_REFRESH_TOKEN",
    "MIST_API_TOKEN",
    "MIST_ORG_ID",
)


def _load_approvals() -> Dict[str, Any]:
    if not _APPROVAL_FILE.exists():
        return {}
    try:
        data = json.loads(_APPROVAL_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_approvals(data: Dict[str, Any]) -> None:
    _APPROVAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    _APPROVAL_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def live_read_credentials_present() -> bool:
    import os

    return any(os.environ.get(key) for key in _LIVE_READ_ENV_KEYS)


def is_live_read_approved(*, slug: Optional[str] = None) -> bool:
    """True when operator has approved live-read for this slug or globally."""
    data = _load_approvals()
    if data.get("global_approved"):
        return True
    if slug and str(data.get("slugs", {}).get(slug, "")).lower() == "approved":
        return True
    return False


def approve_live_read(*, slug: Optional[str] = None, operator: str = "operator") -> Dict[str, Any]:
    data = _load_approvals()
    data["global_approved"] = slug is None
    if slug:
        slugs = data.setdefault("slugs", {})
        slugs[slug] = "approved"
    data["last_approved_at"] = time.time()
    data["last_operator"] = operator
    _save_approvals(data)
    return {"ok": True, "slug": slug, "global": slug is None}


def revoke_live_read(*, slug: Optional[str] = None) -> Dict[str, Any]:
    data = _load_approvals()
    if slug is None:
        data["global_approved"] = False
        data["slugs"] = {}
    else:
        slugs = data.setdefault("slugs", {})
        slugs.pop(slug, None)
    data["revoked_at"] = time.time()
    _save_approvals(data)
    return {"ok": True, "slug": slug}


def live_read_gate_status(*, slug: Optional[str] = None) -> Dict[str, Any]:
    """Status for UI/CLI: credentials present, approval state, dry-run default."""
    return {
        "credentials_present": live_read_credentials_present(),
        "approved": is_live_read_approved(slug=slug),
        "dry_run_default": not is_live_read_approved(slug=slug),
        "env_keys": list(_LIVE_READ_ENV_KEYS),
    }


def require_live_read_approval(*, slug: Optional[str] = None) -> Optional[str]:
    """Return an error message when live reads are blocked; None when allowed."""
    if not live_read_credentials_present():
        return None
    if is_live_read_approved(slug=slug):
        return None
    return (
        "Live-read credentials are configured but not approval-gated. "
        "Use --dry-run or approve via /api/security/live-read/approve."
    )
