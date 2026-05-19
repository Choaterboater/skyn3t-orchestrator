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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.agents.decisions import load_decisions
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.consistency_reviewer")


def _parse_llm_json_array(raw: str) -> Optional[List[Any]]:
    """Extract a JSON array from an LLM response.

    Two-pass:
      1. Strict — strip ``` fences then json.loads.
      2. Salvage — find the first balanced [...] region anywhere in
         the response and parse that. Handles CLI backends that
         narrate tool calls ("Reviewing the scaffold...", "● Read
         README.md  └ 33 lines read") before / around the JSON.

    Returns the parsed list on success, or None if both passes fail.
    """
    if not raw:
        return None

    # Pass 1: strict (preserves existing behavior).
    cleaned = raw.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # Pass 2: salvage. Walk the raw string, find the first `[` that
    # opens a balanced bracket region, attempt json.loads on it.
    # Bracket-counting (not regex) so nested arrays inside the
    # findings parse correctly.
    text = raw
    n = len(text)
    for start in range(n):
        if text[start] != "[":
            continue
        depth = 0
        in_string = False
        escape = False
        for end in range(start, n):
            ch = text[end]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    candidate = text[start : end + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # try next `[`
                    if isinstance(parsed, list):
                        logger.info(
                            "LLM consistency review JSON salvaged from prose "
                            "(prefix=%r, len=%d)",
                            text[: min(80, start)],
                            len(candidate),
                        )
                        return parsed
                    break  # not a list, try next `[`
    return None


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

        # Architect's decisions.json (if present) is the single source of
        # truth for ports/framework/language. Heuristic and LLM passes
        # both use it as an anchor instead of inferring ground truth
        # from scattered scaffold files.
        artifact_dir_raw = data.get("artifact_dir") or str(scaffold_dir.parent)
        decisions = load_decisions(artifact_dir_raw) or data.get("decisions")
        if not isinstance(decisions, dict):
            decisions = None

        # Heuristic pass (fast, no LLM) catches obvious issues
        heuristic_findings = self._heuristic_check(scaffold_dir, brief, decisions)

        # LLM pass for semantic gaps
        llm_findings = await self._llm_check(
            scaffold_dir, brief, architecture_md, task, decisions=decisions
        )

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

    def _heuristic_check(
        self,
        scaffold_dir: Path,
        brief: str,
        decisions: Optional[Dict[str, Any]] = None,
    ) -> List[ConsistencyFinding]:
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
        # accept slug, space-separated, hyphen-separated, and a small
        # set of irregular aliases (pihole↔pi-hole, qbittorrent↔qBittorrent).
        _IRREGULAR_ALIASES: Dict[str, set] = {
            "pihole": {"pi-hole", "pi hole"},
            "qbittorrent": {"qbittorrent", "qbit"},
            "sabnzbd": {"sab"},
            "homeassistant": {"home assistant", "home-assistant"},
        }
        readme = scaffold_dir / "README.md"
        if readme.exists():
            readme_text = readme.read_text(encoding="utf-8").lower()
            from skyn3t.agents.stack_templates import _detect_services
            detected = set(_detect_services(brief))
            for svc in detected:
                variants = {svc, svc.replace("_", " "), svc.replace("_", "-")}
                variants.update(_IRREGULAR_ALIASES.get(svc, set()))
                if any(v in readme_text for v in variants):
                    continue
                findings.append(ConsistencyFinding(
                    severity="warning",
                    category="readme_drift",
                    file="README.md",
                    message=f"README does not mention '{svc}' which is in the brief.",
                    suggestion=f"Add a section documenting the {svc} integration.",
                ))

        # Check 4: Backend deps in package.json but no server/ directory.
        # Catches the "looks fullstack but ships frontend-only" pattern that
        # repeatedly tanked LLM scores (e75f28, beea80, 2d4498 all had this).
        # When express + better-sqlite3 / fastify / koa / etc. are declared
        # but no server source exists, packaging looks fullstack to the
        # outside world while runtime is frontend-only.
        pkg_file = scaffold_dir / "package.json"
        if pkg_file.exists():
            try:
                import json as _json_pkg
                pkg_data = _json_pkg.loads(pkg_file.read_text(encoding="utf-8"))
            except Exception:
                pkg_data = {}
            deps = {**(pkg_data.get("dependencies") or {}), **(pkg_data.get("devDependencies") or {})}
            backend_markers = {"express", "fastify", "koa", "better-sqlite3", "pg", "mysql", "mongodb"}
            backend_deps = sorted(set(deps.keys()) & backend_markers)
            if backend_deps:
                # Look for any server-side source file
                has_server = (
                    (scaffold_dir / "server").is_dir()
                    or any(scaffold_dir.glob("server.*"))
                    or any(scaffold_dir.glob("server/**/*.js"))
                    or any(scaffold_dir.glob("server/**/*.ts"))
                )
                if not has_server:
                    findings.append(ConsistencyFinding(
                        severity="blocker",
                        category="hallucination",
                        file="package.json",
                        message=(
                            f"Backend dependencies declared ({', '.join(backend_deps)}) "
                            f"but no server/ directory or server.* file exists."
                        ),
                        suggestion=(
                            "Either remove the unused backend dependencies, or add "
                            "the server code they imply (server/index.js, routes, etc.)."
                        ),
                    ))

        # Check 5: Planned components in component_file_plan.json not
        # imported from the entrypoint. The pattern that produced beea80:
        # planner declared HabitCard, HabitList, StreakBadge, etc. — but
        # App.jsx shipped a self-contained localStorage demo importing
        # none of them. To the user, the "designed app" never existed.
        # component_file_plan.json and tech_stack.json live in the
        # artifact_dir (parent of scaffold/). When scaffold_dir == artifact_dir
        # (no scaffold subdir), look there directly.
        _artifact_root = scaffold_dir.parent if scaffold_dir.name == "scaffold" else scaffold_dir
        plan_file = _artifact_root / "component_file_plan.json"
        if plan_file.exists():
            try:
                import json as _json_plan
                plan_data = _json_plan.loads(plan_file.read_text(encoding="utf-8"))
            except Exception:
                plan_data = {}
            # Plan format: {"files": [{"path": "src/components/X.jsx", ...}]}
            planned_components: List[str] = []
            for entry in (plan_data.get("files") or []):
                p = str(entry.get("path") or "")
                if "/components/" in p or p.endswith(("Card.jsx", "List.jsx", "Form.jsx")):
                    name = Path(p).stem
                    if name and name != "index":
                        planned_components.append(name)
            if planned_components:
                # Read the actual App.jsx (or main.jsx if App not present)
                app_paths = [
                    scaffold_dir / "src" / "App.jsx",
                    scaffold_dir / "src" / "App.tsx",
                    scaffold_dir / "App.jsx",
                ]
                app_text = ""
                for ap in app_paths:
                    if ap.exists():
                        try:
                            app_text = ap.read_text(encoding="utf-8")
                            break
                        except Exception:
                            continue
                if app_text:
                    missing = [
                        name for name in planned_components
                        if name not in app_text
                    ]
                    # Only fire if MOST planned components are missing — a
                    # single missing import is normal. A near-total
                    # mismatch is the pattern this check exists for.
                    if missing and len(missing) >= max(2, len(planned_components) * 2 // 3):
                        findings.append(ConsistencyFinding(
                            severity="blocker",
                            category="missing_feature",
                            file="src/App.jsx",
                            message=(
                                f"Entrypoint ignores {len(missing)}/{len(planned_components)} "
                                f"planned components ({', '.join(missing[:5])}"
                                + (f", and {len(missing) - 5} more" if len(missing) > 5 else "")
                                + ")."
                            ),
                            suggestion=(
                                "Import and render the planned components from App.jsx, "
                                "or remove them from component_file_plan.json if the "
                                "design changed."
                            ),
                        ))

        # Check 6: index.html title is the project name, not a template
        # leftover. Caught in every failing review ("Homelab Dashboard"
        # title on a habit tracker, inventory app, etc.). A regex match
        # against `<title>` keeps it simple.
        idx_html = scaffold_dir / "index.html"
        if idx_html.exists():
            try:
                idx_text = idx_html.read_text(encoding="utf-8")
            except Exception:
                idx_text = ""
            import re as _re_title
            m = _re_title.search(r"<title>([^<]*)</title>", idx_text, _re_title.IGNORECASE)
            if m:
                title = m.group(1).strip()
                # Pull a few keywords from the brief to compare
                brief_words = {
                    w.lower() for w in (brief or "").split()
                    if len(w) > 3 and w.lower() not in {"with", "that", "this", "from", "your", "build", "make"}
                }
                title_words = {w.lower() for w in title.split() if len(w) > 3}
                # If the title shares NO content-word with the brief, it's
                # almost certainly a template leftover.
                if brief_words and not (brief_words & title_words):
                    findings.append(ConsistencyFinding(
                        severity="warning",
                        category="readme_drift",
                        file="index.html",
                        message=(
                            f"<title>{title}</title> looks like a template leftover — "
                            "it shares no words with the brief."
                        ),
                        suggestion=(
                            "Set <title> to something descriptive of the actual product "
                            "(e.g. derived from the brief)."
                        ),
                    ))

        # Check 7: tech_stack.json declared stack vs files actually present.
        # If tech_stack says backend=express but no express dep + no server
        # code, that's hallucinated stack signaling.
        stack_file = _artifact_root / "tech_stack.json"
        if stack_file.exists():
            try:
                import json as _json_stack
                stack_data = _json_stack.loads(stack_file.read_text(encoding="utf-8"))
            except Exception:
                stack_data = {}
            declared_backend = str(stack_data.get("backend") or "").lower()
            if declared_backend and declared_backend not in ("", "none", "static", "frontend"):
                # Map declared backend to expected evidence
                evidence = {
                    "express": [
                        (scaffold_dir / "server").is_dir(),
                        "express" in str(pkg_file.exists() and pkg_file.read_text(encoding="utf-8") or ""),
                    ],
                    "fastapi": [
                        any(scaffold_dir.glob("**/main.py")),
                        any(scaffold_dir.glob("**/requirements.txt")),
                    ],
                    "django": [any(scaffold_dir.glob("**/manage.py"))],
                    "flask": [
                        any(scaffold_dir.glob("**/app.py")),
                        any(scaffold_dir.glob("**/requirements.txt")),
                    ],
                }
                checks = evidence.get(declared_backend, [])
                if checks and not any(checks):
                    findings.append(ConsistencyFinding(
                        severity="blocker",
                        category="hallucination",
                        file="tech_stack.json",
                        message=(
                            f"tech_stack.json declares backend='{declared_backend}' "
                            f"but no corresponding source files were found."
                        ),
                        suggestion=(
                            f"Either ship the {declared_backend} backend implementation, "
                            f"or change tech_stack.json to reflect what was actually built."
                        ),
                    ))

        # Check 8: decisions.json is the architect's port contract. Any
        # scaffold port that disagrees with it is a contradiction the
        # downstream agent introduced — flag as a blocker so the build
        # cannot quietly ship "the architect said 3000 but compose
        # exposes 8000" again.
        if decisions:
            decided_port = decisions.get("backend_port")
            if isinstance(decided_port, int):
                env_file = scaffold_dir / ".env.example"
                if env_file.is_file():
                    env_text = env_file.read_text(encoding="utf-8")
                    for line in env_text.splitlines():
                        if line.startswith("PORT="):
                            try:
                                env_port = int(line.split("=", 1)[1].strip())
                            except (ValueError, IndexError):
                                break
                            if env_port != decided_port:
                                findings.append(ConsistencyFinding(
                                    severity="blocker",
                                    category="contradiction",
                                    file=".env.example",
                                    message=(
                                        f".env.example PORT={env_port} disagrees with "
                                        f"decisions.json backend_port={decided_port}."
                                    ),
                                    suggestion=(
                                        f"Set PORT={decided_port} in .env.example to "
                                        f"match the architect's decisions contract."
                                    ),
                                ))
                            break
                compose_file = scaffold_dir / "docker-compose.yml"
                if compose_file.is_file():
                    compose_text = compose_file.read_text(encoding="utf-8")
                    compose_ports: List[int] = []
                    for line in compose_text.splitlines():
                        m = re.search(r'"(\d+):\d+"', line)
                        if m:
                            try:
                                compose_ports.append(int(m.group(1)))
                            except ValueError:
                                pass
                    if compose_ports and decided_port not in compose_ports:
                        findings.append(ConsistencyFinding(
                            severity="blocker",
                            category="contradiction",
                            file="docker-compose.yml",
                            message=(
                                f"docker-compose.yml exposes {compose_ports} but "
                                f"decisions.json backend_port={decided_port}."
                            ),
                            suggestion=(
                                f"Map host:{decided_port} to the container port in "
                                f"docker-compose.yml to match decisions.json."
                            ),
                        ))

        return findings

    async def _llm_check(
        self,
        scaffold_dir: Path,
        brief: str,
        architecture_md: str,
        task: TaskRequest,
        decisions: Optional[Dict[str, Any]] = None,
    ) -> List[ConsistencyFinding]:
        """LLM-based semantic consistency check.

        We read a curated subset of files (not the whole scaffold, to stay
        within token budget) and ask the LLM for a structured critique.
        ``decisions`` (when present) is the architect's machine-readable
        contract — we surface it as DECISIONS in the prompt so the LLM
        compares scaffold files against a single anchor instead of
        guessing the intended truth from scattered clues.
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
        if decisions:
            prompt += (
                "DECISIONS (source of truth — the architect committed to these. "
                "Any scaffold file that disagrees with DECISIONS is a contradiction):\n"
                f"{json.dumps(decisions, indent=2)}\n\n"
            )
        manifest_preview = "\n".join(file_manifest[:60])
        prompt += (
            "FILE MANIFEST:\n"
            f"{manifest_preview}\n\n"
            "KEY FILES (first 200 lines each):\n"
            f"{key_files_content[:8000]}\n\n"
            "OUTPUT FORMAT (CRITICAL):\n"
            "Your ENTIRE response must be a single JSON array — nothing else.\n"
            "  - Do NOT narrate, explain, or describe what you are doing.\n"
            "  - Do NOT include tool-call traces, file-read confirmations, or progress lines.\n"
            "  - Do NOT wrap in markdown code fences.\n"
            "  - The first character of your response must be `[`.\n"
            "  - The last character must be `]`.\n"
            "  - If there are no issues, respond with EXACTLY: []\n"
            "\n"
            "Each item in the array must have:\n"
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
            "- Do NOT hallucinate issues. Only report real problems you can see in the files.\n"
        )

        try:
            # Pin CLI backends' subprocess CWD to the real scaffold dir
            # so any Read/glob tool calls see the actual files. Without
            # this the CLI sees an empty /tmp sandbox and incorrectly
            # reports the scaffold as empty.
            raw = await llm_client.complete(
                prompt, max_tokens=4000, temperature=0.3, cwd=str(scaffold_dir),
            )
        except Exception as exc:
            logger.warning("LLM consistency review failed: %s", exc)
            return findings

        # Parse JSON. Two passes:
        #   1. Strict (current behavior): strip code fences, parse.
        #   2. Salvage: extract the first [...] balanced-bracket region
        #      from anywhere in the response. CLI backends sometimes
        #      narrate their tool calls before the actual JSON
        #      ("Reviewing the scaffold..." + tool-call traces + JSON).
        #      The salvage pass picks the JSON out of that prose.
        items = _parse_llm_json_array(raw)
        if items is None:
            logger.warning("LLM consistency review returned non-JSON: %s", raw[:500])
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
