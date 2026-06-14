"""Domain corpus schema for golden networking projects.

Golden projects are approved examples that SkyN3t can learn from. They may
come from local folders or GitHub repositories, but original sources stay
read-only: downstream loops must work in local candidate copies/branches and
only propose verified improvements.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

NETWORKING_VENDORS = ("aruba", "juniper")
NETWORKING_DOMAINS = (
    "field_troubleshooting",
    "inventory_config",
    "automation_scripts",
)
PERMISSIVE_LICENSES = {
    "mit",
    "apache-2.0",
    "bsd-2-clause",
    "bsd-3-clause",
    "isc",
    "mpl-2.0",
}

_TAG_RE = re.compile(r"[^a-z0-9_+-]+")
_GITHUB_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)?"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)

_SECRET_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "assignment_secret",
        re.compile(
            r"(?i)\b[a-z0-9_-]*(api[_-]?key|token|secret|password|passwd|pwd|"
            r"client[_-]?secret|snmp[_-]?community|community[_-]?string|private[_-]?key)"
            r"[a-z0-9_-]*\b"
            r"\s*[:=]\s*['\"]?[^'\"\s,;]+"
        ),
    ),
    (
        "authorization_header",
        re.compile(r"(?i)\bAuthorization\s*:\s*(?:Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]+"),
    ),
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----",
            re.MULTILINE,
        ),
    ),
    (
        "ipv4_address",
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b"),
    ),
    (
        "mac_address",
        re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"),
    ),
)


@dataclass
class RedactionFinding:
    """A redaction applied to corpus text."""

    kind: str
    count: int


@dataclass
class RedactionResult:
    """Redacted text and aggregate finding counts."""

    text: str
    findings: List[RedactionFinding] = field(default_factory=list)

    @property
    def redacted(self) -> bool:
        return bool(self.findings)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "redacted": self.redacted,
            "findings": [
                {"kind": finding.kind, "count": finding.count}
                for finding in self.findings
            ],
        }


@dataclass
class CorpusSource:
    """Where a golden project came from."""

    source_type: str
    uri: str
    ref: str = ""
    clone_strategy: str = "local_copy"
    read_only_original: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_type": self.source_type,
            "uri": self.uri,
            "ref": self.ref,
            "clone_strategy": self.clone_strategy,
            "read_only_original": self.read_only_original,
        }


@dataclass
class GoldenProjectRecord:
    """Approved exemplar project metadata for domain learning."""

    corpus_id: str
    title: str
    source: CorpusSource
    vendor_tags: List[str] = field(default_factory=list)
    domain_tags: List[str] = field(default_factory=list)
    stack: str = ""
    commands: List[str] = field(default_factory=list)
    file_shape: List[str] = field(default_factory=list)
    reusable_patterns: List[str] = field(default_factory=list)
    quality_notes: str = ""
    redaction: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "corpus_id": self.corpus_id,
            "title": self.title,
            "source": self.source.to_dict(),
            "vendor_tags": list(self.vendor_tags),
            "domain_tags": list(self.domain_tags),
            "stack": self.stack,
            "commands": list(self.commands),
            "file_shape": list(self.file_shape),
            "reusable_patterns": list(self.reusable_patterns),
            "quality_notes": self.quality_notes,
            "redaction": dict(self.redaction),
            "created_at": self.created_at,
        }


@dataclass
class GithubLearningSafety:
    """Safety decision for learning from a GitHub source."""

    allowed: bool
    repo: str
    read_only_original: bool = True
    candidate_strategy: str = "local_candidate_copy"
    license_status: str = "unknown"
    redaction_required: bool = True
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "repo": self.repo,
            "read_only_original": self.read_only_original,
            "candidate_strategy": self.candidate_strategy,
            "license_status": self.license_status,
            "redaction_required": self.redaction_required,
            "reasons": list(self.reasons),
        }


def normalize_tag(value: Any) -> str:
    text = _TAG_RE.sub("_", str(value or "").strip().lower()).strip("_")
    return text[:64]


def normalize_tags(values: Iterable[Any], *, allowed: Iterable[str] = ()) -> List[str]:
    allowed_set = {normalize_tag(item) for item in allowed if normalize_tag(item)}
    out: List[str] = []
    for value in values:
        tag = normalize_tag(value)
        if not tag:
            continue
        if allowed_set and tag not in allowed_set:
            continue
        if tag not in out:
            out.append(tag)
    return out


def redact_sensitive_text(text: str) -> RedactionResult:
    redacted = text or ""
    findings: List[RedactionFinding] = []
    for kind, pattern in _SECRET_PATTERNS:
        marker = "assignment" if kind == "assignment_secret" else kind
        redacted, count = pattern.subn(f"<REDACTED:{marker}>", redacted)
        if count:
            findings.append(RedactionFinding(kind=kind, count=count))
    return RedactionResult(text=redacted, findings=findings)


def infer_networking_tags(text: str) -> Tuple[List[str], List[str]]:
    haystack = (text or "").lower()
    vendors: List[str] = []
    if any(term in haystack for term in ("aruba", "aoscx", "aos-cx", "clearpass", "airwave")):
        vendors.append("aruba")
    if any(term in haystack for term in ("juniper", "junos", "mist", "srx", "ex switch")):
        vendors.append("juniper")

    domains: List[str] = []
    if any(term in haystack for term in ("troubleshoot", "diagnostic", "field", "runbook")):
        domains.append("field_troubleshooting")
    if any(term in haystack for term in ("inventory", "config", "validate", "backup", "diff")):
        domains.append("inventory_config")
    if any(term in haystack for term in ("automation", "script", "cli", "ansible", "netmiko", "napalm")):
        domains.append("automation_scripts")
    return vendors, domains


def parse_github_repo(value: str) -> Optional[Tuple[str, str]]:
    text = str(value or "").strip()
    match = _GITHUB_RE.match(text)
    if match:
        return match.group("owner"), match.group("repo")
    parsed = urlparse(text)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    return parts[0], repo


def source_from_uri(uri: str, *, ref: str = "") -> CorpusSource:
    text = str(uri or "").strip()
    if parse_github_repo(text):
        return CorpusSource(
            source_type="github",
            uri=text,
            ref=ref,
            clone_strategy="local_clone_or_fork",
            read_only_original=True,
        )
    return CorpusSource(
        source_type="local",
        uri=str(Path(text).expanduser()) if text else "",
        ref=ref,
        clone_strategy="local_copy",
        read_only_original=True,
    )


def assess_github_learning_source(
    uri: str,
    *,
    approved: bool = False,
    public: bool = True,
    license_spdx: str = "",
) -> GithubLearningSafety:
    """Decide whether a GitHub repo may be ingested as learning material.

    This is an ingestion guardrail, not a license engine: unknown licenses can
    still be learned from only after explicit approval, and originals always
    remain read-only with work done in local candidate copies.
    """

    parsed = parse_github_repo(uri)
    repo = "/".join(parsed) if parsed else str(uri or "").strip()
    reasons: List[str] = []
    allowed = True
    if not parsed:
        allowed = False
        reasons.append("source is not a recognized GitHub repository")
    if not public and not approved:
        allowed = False
        reasons.append("private/non-public repositories require explicit approval")

    license_id = normalize_tag(license_spdx).replace("_", "-")
    if license_id:
        if license_id in PERMISSIVE_LICENSES:
            license_status = "permissive"
        elif approved:
            license_status = "approved_non_permissive"
            reasons.append(f"license {license_spdx} requires pattern-only learning")
        else:
            license_status = "needs_review"
            allowed = False
            reasons.append(f"license {license_spdx} requires review before ingestion")
    else:
        license_status = "unknown"
        if not approved:
            allowed = False
            reasons.append("license unknown; approval required before ingestion")

    if allowed:
        reasons.append("original repository remains read-only; learn into local candidate copies only")
        reasons.append("redact secrets and environment-specific data before RAG/skill storage")
    return GithubLearningSafety(
        allowed=allowed,
        repo=repo,
        license_status=license_status,
        reasons=reasons,
    )


def corpus_id_for_source(source: CorpusSource) -> str:
    digest = hashlib.sha256(
        f"{source.source_type}:{source.uri}:{source.ref}".encode("utf-8", "replace")
    ).hexdigest()[:12]
    return f"{source.source_type}-{digest}"


def build_github_search_queries(
    *,
    vendors: Iterable[str],
    domains: Iterable[str],
    extra_terms: Iterable[str] = (),
) -> List[str]:
    vendor_tags = normalize_tags(vendors, allowed=NETWORKING_VENDORS)
    domain_tags = normalize_tags(domains, allowed=NETWORKING_DOMAINS)
    extras = [str(term).strip() for term in extra_terms if str(term).strip()]
    query_parts: List[str] = []
    for vendor in vendor_tags or list(NETWORKING_VENDORS):
        vendor_term = "aos-cx aruba" if vendor == "aruba" else "junos juniper"
        for domain in domain_tags or list(NETWORKING_DOMAINS):
            if domain == "field_troubleshooting":
                topic = "troubleshooting diagnostics field tool"
            elif domain == "inventory_config":
                topic = "network inventory config validation backup"
            else:
                topic = "network automation cli script netmiko napalm"
            query = f"{vendor_term} {topic}"
            if extras:
                query += " " + " ".join(extras)
            query_parts.append(query)
    return list(dict.fromkeys(query_parts))


def make_golden_project_record(
    *,
    title: str,
    source_uri: str,
    source_ref: str = "",
    vendor_tags: Iterable[str] = (),
    domain_tags: Iterable[str] = (),
    stack: str = "",
    commands: Iterable[str] = (),
    file_shape: Iterable[str] = (),
    reusable_patterns: Iterable[str] = (),
    quality_notes: str = "",
) -> GoldenProjectRecord:
    source = source_from_uri(source_uri, ref=source_ref)
    redaction = redact_sensitive_text(quality_notes)
    inferred_vendors, inferred_domains = infer_networking_tags(
        " ".join([title, quality_notes, " ".join(reusable_patterns)])
    )
    vendors = normalize_tags(
        list(vendor_tags) + inferred_vendors,
        allowed=NETWORKING_VENDORS,
    )
    domains = normalize_tags(
        list(domain_tags) + inferred_domains,
        allowed=NETWORKING_DOMAINS,
    )
    return GoldenProjectRecord(
        corpus_id=corpus_id_for_source(source),
        title=(title or source.uri or "golden project").strip(),
        source=source,
        vendor_tags=vendors,
        domain_tags=domains,
        stack=normalize_tag(stack),
        commands=[str(command).strip() for command in commands if str(command).strip()],
        file_shape=sorted(
            {
                str(path).replace("\\", "/").strip().lstrip("/")
                for path in file_shape
                if str(path).strip()
            }
        ),
        reusable_patterns=[
            redact_sensitive_text(str(pattern)).text
            for pattern in reusable_patterns
            if str(pattern).strip()
        ],
        quality_notes=redaction.text,
        redaction=redaction.to_dict(),
    )
