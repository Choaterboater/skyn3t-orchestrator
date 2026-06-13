"""Inject golden networking corpus snippets into Studio stage prompts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, List, Optional

from skyn3t.intelligence.domain_corpus import NETWORKING_DOMAINS, NETWORKING_VENDORS

_NETWORKING_BRIEF_RE = re.compile(
    r"\b("
    + "|".join(
        re.escape(v)
        for v in (
            *NETWORKING_VENDORS,
            "central",
            "mist",
            "arubaos",
            "aos8",
            "ssid",
            "switch",
            "access point",
            "pycentral",
            "centralmcp",
        )
    )
    + r")\b",
    re.IGNORECASE,
)


def brief_is_networking(brief: str) -> bool:
    text = (brief or "").strip()
    if not text:
        return False
    return bool(_NETWORKING_BRIEF_RE.search(text))


def _load_local_corpus_snippets(data_dir: Path, *, limit: int = 4) -> List[str]:
    corpus_dir = data_dir / "golden_corpus"
    if not corpus_dir.is_dir():
        return []
    snippets: List[str] = []
    for path in sorted(corpus_dir.glob("*.json"))[:limit]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        title = str(record.get("title") or path.stem)
        patterns = record.get("reusable_patterns") or []
        commands = record.get("commands") or []
        lines = [f"- **{title}**"]
        if isinstance(patterns, list) and patterns:
            lines.append("  patterns: " + "; ".join(str(p) for p in patterns[:5]))
        if isinstance(commands, list) and commands:
            lines.append("  commands: " + "; ".join(str(c) for c in commands[:3]))
        snippets.append("\n".join(lines))
    return snippets


async def corpus_prompt_block(
    brief: str,
    *,
    rag: Any = None,
    data_dir: Optional[Path] = None,
    limit: int = 4,
) -> str:
    """Return a prompt appendix for architect/reviewer on networking briefs."""
    if not brief_is_networking(brief):
        return ""
    lines: List[str] = [
        "### Golden networking corpus (read-only patterns)",
        "",
        "Apply these approved exemplar patterns when the brief targets "
        f"{', '.join(NETWORKING_VENDORS)} / {', '.join(NETWORKING_DOMAINS)}:",
        "",
    ]
    local = _load_local_corpus_snippets(data_dir or Path("./data"), limit=limit)
    if local:
        lines.extend(local)
    elif rag is not None:
        try:
            hits = await rag.query(
                "networking golden corpus dry-run credential validation patterns",
                top_k=limit,
            )
            for hit in (hits or [])[:limit]:
                text = str(getattr(hit, "content", hit) or "").strip()
                if text:
                    lines.append(text[:800])
        except Exception:
            pass
    if len(lines) <= 4:
        lines.extend(
            [
                "- Prefer `--dry-run` / read-only API modes until operator approves live reads.",
                "- Validate credentials against `.env.example`; never hard-code secrets.",
                "- Ship offline sample CSV fixtures for demos without live Central/Mist access.",
            ]
        )
    return "\n".join(lines).strip()
