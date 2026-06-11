"""Networking-domain quality rubric for generated tools."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List

_NETWORKING_TERMS = (
    "aruba",
    "aos-cx",
    "aoscx",
    "clearpass",
    "airwave",
    "juniper",
    "junos",
    "mist",
    "switch",
    "wireless",
    "vlan",
    "interface",
    "inventory",
    "troubleshoot",
    "diagnostic",
    "netmiko",
    "napalm",
)


@dataclass
class RubricAxis:
    name: str
    points: int
    max_points: int
    passed: bool
    message: str


@dataclass
class NetworkingQualityReport:
    applicable: bool
    score: int
    max_score: int = 100
    axes: List[RubricAxis] = field(default_factory=list)

    @property
    def gaps(self) -> List[str]:
        return [axis.message for axis in self.axes if not axis.passed]

    def to_dict(self) -> Dict[str, object]:
        return {
            "applicable": self.applicable,
            "score": self.score,
            "max_score": self.max_score,
            "axes": [
                {
                    "name": axis.name,
                    "points": axis.points,
                    "max_points": axis.max_points,
                    "passed": axis.passed,
                    "message": axis.message,
                }
                for axis in self.axes
            ],
            "gaps": self.gaps,
        }


def _combined_text(brief: str, contents: Dict[str, str]) -> str:
    return "\n".join([brief or "", *(contents.values())]).lower()


def is_networking_project(brief: str, contents: Dict[str, str]) -> bool:
    haystack = _combined_text(brief, contents)
    return any(term in haystack for term in _NETWORKING_TERMS)


def _has_any(haystack: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, haystack, flags=re.IGNORECASE) for pattern in patterns)


def evaluate_networking_quality(
    *,
    brief: str,
    contents: Dict[str, str],
    artifact_dir: Path | None = None,
) -> NetworkingQualityReport:
    """Score networking-tool usefulness beyond generic app quality."""

    haystack = _combined_text(brief, contents)
    applicable = is_networking_project(brief, contents)
    if not applicable:
        return NetworkingQualityReport(applicable=False, score=100, axes=[])

    axes: List[RubricAxis] = []

    def add(name: str, max_points: int, passed: bool, message: str) -> None:
        axes.append(
            RubricAxis(
                name=name,
                points=max_points if passed else 0,
                max_points=max_points,
                passed=passed,
                message=message,
            )
        )

    add(
        "vendor_api_realism",
        20,
        _has_any(
            haystack,
            (
                r"\brequests\.",
                r"\bhttpx\.",
                r"\bfetch\(",
                r"\bnetmiko\b",
                r"\bnapalm\b",
                r"/rest/",
                r"\bjunos\b.*\brpc\b",
                r"\baos[-_]?cx\b.*\b(rest|api)\b",
            ),
        ),
        "Networking rubric: no realistic vendor/API integration path found.",
    )
    add(
        "safe_dry_run",
        15,
        _has_any(haystack, (r"--dry-run", r"\bdry[-_ ]run\b", r"\bpreview\b", r"\bread[-_ ]only\b")),
        "Networking rubric: missing dry-run/read-only safety mode.",
    )
    add(
        "config_validation",
        15,
        _has_any(haystack, (r"\bvalidate", r"\bschema\b", r"\bconfig\s+diff\b", r"\bbackup\b")),
        "Networking rubric: missing config validation, backup, or diff workflow.",
    )
    add(
        "inventory_workflow",
        15,
        _has_any(haystack, (r"\binventory\b", r"\bdevice[s]?\b", r"\bswitch(?:es)?\b", r"\bsite\b")),
        "Networking rubric: missing inventory/device workflow.",
    )
    add(
        "troubleshooting_value",
        15,
        _has_any(
            haystack,
            (
                r"\btroubleshoot",
                r"\bdiagnostic",
                r"\bhealth\s+check",
                r"\binterface\s+status\b",
                r"\blog collection\b",
            ),
        ),
        "Networking rubric: missing field troubleshooting/diagnostic workflow.",
    )
    add(
        "offline_sample_mode",
        10,
        _has_any(haystack, (r"\bsample\s+data\b", r"\bfixture", r"\boffline\b", r"\bmock\b")),
        "Networking rubric: missing offline/sample-data mode for demos and tests.",
    )
    add(
        "operator_docs",
        10,
        _has_any(haystack, (r"\.env\.example", r"\bREADME\b", r"\bcredential", r"\bSNMP\b", r"\bAPI token\b")),
        "Networking rubric: missing operator setup docs for credentials/env/config.",
    )

    score = sum(axis.points for axis in axes)
    return NetworkingQualityReport(applicable=True, score=score, axes=axes)
