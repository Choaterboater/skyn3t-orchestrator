"""Mission setup helpers for Studio project launches."""

from __future__ import annotations

from typing import Any, Dict

MISSION_AUDIENCE_LABELS: Dict[str, str] = {
    "": "",
    "general": "General users",
    "builders": "Builders / developers",
    "team": "Internal team",
    "leaders": "Decision-makers",
    "investors": "Investors / partners",
}

MISSION_AUTONOMY: Dict[str, Dict[str, str]] = {
    "balanced": {
        "label": "Balanced",
        "brief_instruction": (
            "Ask follow-up questions only when a critical ambiguity would materially weaken "
            "the output."
        ),
    },
    "confirm_first": {
        "label": "Confirm first",
        "brief_instruction": (
            "Pause early and ask a short set of confirmation questions before the swarm "
            "commits to major assumptions or final deliverables."
        ),
    },
    "move_fast": {
        "label": "Move fast",
        "brief_instruction": (
            "Do not pause for kickoff clarification questions. Make reasonable assumptions, "
            "keep momentum, and only stop if the work is truly blocked."
        ),
    },
}

DEFAULT_MISSION_SETUP: Dict[str, str] = {
    "audience": "",
    "autonomy": "move_fast",
}


def mission_setup_options() -> Dict[str, Any]:
    """Return a JSON-friendly definition of the supported mission setup fields."""
    return {
        "defaults": dict(DEFAULT_MISSION_SETUP),
        "audience": [
            {"value": value, "label": label}
            for value, label in MISSION_AUDIENCE_LABELS.items()
            if value
        ],
        "autonomy": [
            {
                "value": value,
                "label": meta["label"],
                "description": meta["brief_instruction"],
            }
            for value, meta in MISSION_AUTONOMY.items()
        ],
    }


def normalize_mission_setup(value: Any) -> Dict[str, str]:
    """Coerce mission setup to the small supported schema."""
    data = value if isinstance(value, dict) else {}
    audience = str(data.get("audience") or "").strip().lower()
    autonomy = str(data.get("autonomy") or DEFAULT_MISSION_SETUP["autonomy"]).strip().lower()

    if audience not in MISSION_AUDIENCE_LABELS:
        audience = DEFAULT_MISSION_SETUP["audience"]
    if autonomy not in MISSION_AUTONOMY:
        autonomy = DEFAULT_MISSION_SETUP["autonomy"]

    return {
        "audience": audience,
        "autonomy": autonomy,
    }


def mission_setup_labels(value: Any) -> Dict[str, str]:
    """Return user-facing labels for a mission setup dict."""
    setup = normalize_mission_setup(value)
    return {
        "audience": MISSION_AUDIENCE_LABELS.get(setup["audience"], ""),
        "autonomy": MISSION_AUTONOMY[setup["autonomy"]]["label"],
    }


def augment_brief_with_mission_setup(brief: str, value: Any) -> str:
    """Append a compact mission-setup block when it adds real signal."""
    setup = normalize_mission_setup(value)
    labels = mission_setup_labels(setup)
    lines = []

    if labels["audience"]:
        lines.append(f"- Primary audience: {labels['audience']}")
    if setup["autonomy"] != DEFAULT_MISSION_SETUP["autonomy"]:
        lines.append(
            f"- Operating mode: {MISSION_AUTONOMY[setup['autonomy']]['brief_instruction']}"
        )

    if not lines:
        return brief

    clean_brief = str(brief or "").rstrip()
    if clean_brief:
        return clean_brief + "\n\n## Mission setup\n" + "\n".join(lines)
    return "## Mission setup\n" + "\n".join(lines)


def mission_setup_stage_hints(value: Any) -> Dict[str, Any]:
    """Translate mission setup into concrete stage input hints."""
    setup = normalize_mission_setup(value)
    labels = mission_setup_labels(setup)
    hints: Dict[str, Any] = {"mission_setup": setup}

    if labels["audience"]:
        hints["audience"] = labels["audience"]

    if setup["autonomy"] == "confirm_first":
        hints["require_clarification"] = True
    elif setup["autonomy"] == "move_fast":
        hints["clarifications"] = True

    return hints
