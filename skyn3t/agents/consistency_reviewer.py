"""ConsistencyReviewerAgent — semantic cross-file reviewer.

Reads the entire scaffold directory + the brief and checks for:
- Missing features mentioned in the brief
- Services mentioned in one file but not others (Plex bleed-through)
- README drift (documented ports/env vars that don't match actual code)
- Architecture contradictions (TypeScript claimed but JS shipped, port mismatches)

Unlike ReviewerAgent (which scores prose quality and aesthetics), this agent
checks *cross-file truth*: does the code actually match itself and the brief?

Output: JSON report with blocker/warning severity. Blockers trigger targeted
fix rounds; warnings are recorded in review.md but don't block the pipeline.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.consistency_reviewer")


@dataclass
class ConsistencyFinding:
    severity: str  # "blocker" | "warning"
    category: str  # "missing_feature" | "hallucination" | "readme_drift" | "contradiction"
    file: str
    message: str
    suggestion: str = ""


@dataclass
class ConsistencyReview:
    ok: bool
    findings: List[ConsistencyFinding] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "findings": [
                    {
                        "severity": f.severity,
                        "category": f.category,
                        "file": f.file,
                        "message": f.message,
                        "suggestion": f.suggestion,
                    }
                    for f in self.findings
                ],
            },
            indent=2,
        )


class ConsistencyReviewerAgent(BaseAgent):
    """Semantic cross-file consistency reviewer.

    Runs after CodeAgent finishes but before ReviewerAgent. Feeds all scaffold
    files + brief to an LLM and asks for a structured critique focused on
    cross-file truth, not prose quality.
    """

    def __init__(
        self,
        name: str = "consistency_reviewer",
        *,
        event_bus: Optional[EventBus] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="reviewer",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(AgentCapability(
            name="consistency_review",
            description="Checks cross-file consistency between the brief and the generated scaffold.",
            parameters={"scaffold_dir": "str", "brief": "str", "architecture_md": "str (optional)"},
        ))

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        data = task.input_data or {}
        scaffold_dir_raw = (
            data.get("scaffold_dir")
            or (str(Path(data.get("artifact_dir", "")) / "scaffold")
                if data.get("artifact_dir") else None)
        )
        if not scaffold_dir_raw:
            return TaskResult(
                task_id=task.task_id, success=False,
                error="scaffold_dir required (or artifact_dir with a scaffold/ subdir)",
            )
        scaffold_dir = Path(scaffold_dir_raw).expanduser().resolve()
        brief = data.get("brief", "")
        arch_path = data.get("architecture_md_path")
        architecture_md = ""
        if arch_path:
            try:
                architecture_md = Path(arch_path).read_text(encoding="utf-8")
            except Exception:
                pass

        # Heuristic pass (fast, no LLM) catches obvious issues
        heuristic_findings = self._heuristic_check(scaffold_dir, brief)

        # LLM pass for semantic gaps
        llm_findings = await self._llm_check(scaffold_dir, brief, architecture_md, task)

        all_findings = heuristic_findings + llm_findings
        blockers = [f for f in all_findings if f.severity == "blocker"]
        review = ConsistencyReview(ok=len(blockers) == 0, findings=all_findings)

        return TaskResult(
            task_id=task.task_id, success=True,
            output={
                "verdict": "pass" if review.ok else "needs_fix",
                "blocker_count": len(blockers),
                "warning_count": len([f for f in all_findings if f.severity == "warning"]),
                "report_json": review.to_json(),
                "scaffold_dir": str(scaffold_dir),
            },
        )

    def _heuristic_check(self, scaffold_dir: Path, brief: str) -> List[ConsistencyFinding]:
        """Fast heuristic checks that don't need an LLM."""
        findings: List[ConsistencyFinding] = []
        brief_lower = brief.lower()

        # Check 1: If brief asks for TypeScript, do .tsx/.ts files exist?
        ts_requested = any(
            phrase in brief_lower
            for phrase in ("typescript", ".ts", ".tsx", "tsconfig")
        )
        if ts_requested:
            ts_files = list(scaffold_dir.rglob("*.ts")) + list(scaffold_dir.rglob("*.tsx"))
            if not ts_files:
                findings.append(ConsistencyFinding(
                    severity="warning",
                    category="contradiction",
                    file="(scaffold root)",
                    message="Brief requests TypeScript but no .ts/.tsx files found.",
                    suggestion="Convert .jsx files to .tsx or add a tsconfig.json.",
                ))

        # Check 2: Port consistency — does .env.example match docker-compose.yml?
        env_file = scaffold_dir / ".env.example"
        compose_file = scaffold_dir / "docker-compose.yml"
        if env_file.exists() and compose_file.exists():
            env_text = env_file.read_text(encoding="utf-8")
            compose_text = compose_file.read_text(encoding="utf-8")
            # Extract PORT= from .env.example
            env_port = None
            for line in env_text.splitlines():
                if line.startswith("PORT="):
                    env_port = line.split("=", 1)[1].strip()
                    break
            # Extract ports from compose
            compose_ports = []
            for line in compose_text.splitlines():
                if "ports:" in line or "- \"" in line:
                    import re
                    m = re.search(r'"(\d+):\d+"', line)
                    if m:
                        compose_ports.append(m.group(1))
            if env_port and compose_ports and env_port not in compose_ports:
                findings.append(ConsistencyFinding(
                    severity="warning",
                    category="readme_drift",
                    file="docker-compose.yml",
                    message=f"Port mismatch: .env.example says PORT={env_port} but compose exposes {compose_ports}.",
                    suggestion="Align docker-compose.yml ports with .env.example.",
                ))

        # Check 3: Does README mention services that aren't in the code?
        # _detect_services returns slug tokens ("home_assistant"), but
        # READMEs are written in display form ("Home Assistant"). We
        # accept slug, space-separated, and hyphen-separated variants.
        readme = scaffold_dir / "README.md"
        if readme.exists():
            readme_text = readme.read_text(encoding="utf-8").lower()
            from skyn3t.agents.stack_templates import _detect_services
            detected = set(_detect_services(brief))
            for svc in detected:
                variants = {svc, svc.replace("_", " "), svc.replace("_", "-")}
                if any(v in readme_text for v in variants):
                    continue
                findings.append(ConsistencyFinding(
                    severity="warning",
                    category="readme_drift",
                    file="README.md",
                    message=f"README does not mention '{svc}' which is in the brief.",
                    suggestion=f"Add a section documenting the {svc} integration.",
                ))

        return findings

    async def _llm_check(
        self,
        scaffold_dir: Path,
        brief: str,
        architecture_md: str,
        task: TaskRequest,
    ) -> List[ConsistencyFinding]:
        """LLM-based semantic consistency check.

        We read a curated subset of files (not the whole scaffold, to stay
        within token budget) and ask the LLM for a structured critique.
        """
        findings: List[ConsistencyFinding] = []
        llm_client = self._resolve_llm_client(task)
        if llm_client is None:
            logger.warning("No LLM client available for consistency review; skipping LLM pass.")
            return findings

        # Build a file manifest — list all files with sizes
        file_manifest: List[str] = []
        for path in sorted(scaffold_dir.rglob("*")):
            if path.is_file() and path.stat().st_size < 50_000:
                rel = path.relative_to(scaffold_dir).as_posix()
                file_manifest.append(f"{rel} ({path.stat().st_size} bytes)")

        # Read key files for content analysis (capped total ~15K tokens)
        key_files_content = ""
        key_file_paths = [
            "README.md",
            "src/App.jsx",
            "src/App.tsx",
            "server/index.js",
            "server/index.ts",
            "package.json",
            "server/package.json",
            ".env.example",
        ]
        for rel in key_file_paths:
            p = scaffold_dir / rel
            if p.exists():
                text = p.read_text(encoding="utf-8")
                # Cap each file at ~200 lines to stay within budget
                lines = text.splitlines()[:200]
                key_files_content += f"\n--- {rel} ---\n" + "\n".join(lines) + "\n"

        prompt = (
            "You are a senior code reviewer doing a CROSS-FILE consistency check. "
            "Your job is NOT to grade prose or aesthetics — it is to find places where "
            "the code contradicts itself, the brief, or common sense.\n\n"
            "BRIEF:\n"
            f"{brief[:4000]}\n\n"
        )
        if architecture_md:
            prompt += (
                "ARCHITECTURE DOCUMENT:\n"
                f"{architecture_md[:3000]}\n\n"
            )
        manifest_preview = "\n".join(file_manifest[:60])
        prompt += (
            "FILE MANIFEST:\n"
            f"{manifest_preview}\n\n"
            "KEY FILES (first 200 lines each):\n"
            f"{key_files_content[:8000]}\n\n"
            "INSTRUCTIONS:\n"
            "Return ONLY a JSON array. Each item must have:\n"
            '  "severity": "blocker" | "warning",\n'
            '  "category": "missing_feature" | "hallucination" | "readme_drift" | "contradiction",\n'
            '  "file": "relative/path/or (scaffold root)",\n'
            '  "message": "one-sentence description of the issue",\n'
            '  "suggestion": "one-sentence fix instruction"\n'
            "\n"
            "Rules:\n"
            "- blocker = the scaffold is objectively wrong (missing required feature, "
            "  claimed tech not used, ports/env vars don't match).\n"
            "- warning = cosmetic or minor inconsistency.\n"
            "- missing_feature: the brief explicitly asked for X but the code doesn't have it.\n"
            "- hallucination: a service/technology is mentioned in one file but not requested.\n"
            "- readme_drift: README documents something that doesn't match the code.\n"
            "- contradiction: architecture.md says TypeScript but files are .js, etc.\n"
            "- If everything is consistent, return an empty array [].\n"
            "- Do NOT hallucinate issues. Only report real problems you can see in the files.\n"
        )

        try:
            raw = await llm_client.complete(prompt, max_tokens=4000, temperature=0.3)
        except Exception as exc:
            logger.warning("LLM consistency review failed: %s", exc)
            return findings

        # Parse JSON — be tolerant of markdown fences
        cleaned = raw.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        try:
            items = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("LLM consistency review returned non-JSON: %s", raw[:500])
            return findings

        if not isinstance(items, list):
            logger.warning("LLM consistency review returned non-array: %s", type(items))
            return findings

        for item in items:
            if not isinstance(item, dict):
                continue
            findings.append(ConsistencyFinding(
                severity=item.get("severity", "warning"),
                category=item.get("category", "contradiction"),
                file=item.get("file", "(unknown)"),
                message=item.get("message", ""),
                suggestion=item.get("suggestion", ""),
            ))

        return findings

    def _resolve_llm_client(self, task: TaskRequest):
        """Get an LLM client from the task context or return None."""
        # The task may carry an llm_client in its input_data
        data = task.input_data or {}
        client = data.get("llm_client")
        if client is not None:
            return client
        # Try to construct one from config
        try:
            from skyn3t.adapters.llm_client import LLMClient
            return LLMClient()
        except Exception:
            return None
