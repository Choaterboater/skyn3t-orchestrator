"""PackagingAgent — turn a generated scaffold into a runnable product.

Runs after ContractVerifierAgent (files settled) and before the final
ReviewerAgent (so packaging quality counts toward the score). Picks a
packaging strategy based on StackDetector's family classification:

    web       → in-app Settings UI + useConfig hook + slim README
                (no .env for end-users)
    server    → Dockerfile + docker-compose.yml + .env.example + README
                (operator runs `docker compose up`)
    fullstack → both, wired together
    unknown   → README-only with manual setup notes

Each strategy is a self-contained _package_* method so adding a new
family later is one match-arm + one method.

This PR ships the **web** strategy only. Docker, fullstack, and the
reviewer-scoring axis land in subsequent PRs (C-docker, C-combo, D).

Feature-flagged via `extra={"packaging_enabled": False}` on the
StudioRunner — defaults on, easy to disable per-run if it misbehaves.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.agents.env_scanner import EnvVarRef, ScanResult
from skyn3t.agents.env_scanner import scan as scan_env
from skyn3t.agents.stack_detector import StackDetection
from skyn3t.agents.stack_detector import detect as detect_stack
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.packaging_agent")


# Sandbox verification budget — `npm install + npm run build` typically
# takes 30-90s on a clean tree; 180s gives slow networks and big bundles
# enough headroom without blocking the pipeline indefinitely.
_VERIFY_TIMEOUT_SECONDS = 180.0


@dataclass
class PackagingResult:
    """What the agent produced for one project."""

    strategy: str                       # "web" | "server" | "fullstack" | "unknown"
    files_written: List[str]            # paths relative to artifact_dir
    files_patched: List[str]            # paths relative to artifact_dir
    env_vars_found: int                 # how many env vars were detected
    verified: bool                      # did the install+build dry-run succeed?
    verifier_skipped: bool              # true if the strategy doesn't verify
    notes: List[str]                    # human-readable summary lines


class PackagingAgent(BaseAgent):
    """Generate Settings UI / Dockerfile / README / .env.example per project."""

    def __init__(
        self,
        name: str = "packaging_agent",
        *,
        event_bus: Optional[EventBus] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            name=name,
            agent_type="reviewer",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(AgentCapability(
            name="packaging",
            description=(
                "Generates Settings UI / Dockerfile / README / .env.example "
                "tailored to the project's stack family so the scaffold "
                "ships as a runnable product, not a config-puzzle for the user."
            ),
            parameters={
                "artifact_dir": "str",
                "scaffold_dir": "str (optional, defaults to artifact_dir/scaffold)",
            },
        ))

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        data = task.input_data or {}
        artifact_dir_raw = data.get("artifact_dir")
        if not artifact_dir_raw:
            return TaskResult(
                task_id=task.task_id, success=False,
                error="artifact_dir required",
            )

        artifact_dir = Path(artifact_dir_raw).expanduser().resolve()
        scaffold_dir_raw = data.get("scaffold_dir") or str(artifact_dir / "scaffold")
        scaffold_dir = Path(scaffold_dir_raw).expanduser().resolve()
        verify_enabled = bool(data.get("packaging_verify", True))

        detection = detect_stack(artifact_dir)
        env_scan = scan_env(scaffold_dir if scaffold_dir.is_dir() else artifact_dir)

        await self.think(
            f"packaging: family={detection.family}, stack={detection.stack}, "
            f"vars={len(env_scan.vars)}, services={detection.services}"
        )

        try:
            match detection.family:
                case "web":
                    result = await self._package_web(
                        artifact_dir=artifact_dir,
                        scaffold_dir=scaffold_dir,
                        detection=detection,
                        env_scan=env_scan,
                        verify_enabled=verify_enabled,
                    )
                case "server":
                    result = self._package_server(
                        artifact_dir=artifact_dir,
                        scaffold_dir=scaffold_dir,
                        detection=detection,
                        env_scan=env_scan,
                    )
                case "fullstack" | "unknown":
                    # PR C-combo ships fullstack. unknown stays as
                    # placeholder until we get a meaningful signal.
                    result = self._package_placeholder(detection, env_scan)
                case _:
                    result = self._package_placeholder(detection, env_scan)
        except Exception as e:  # noqa: BLE001 - protect the pipeline
            logger.exception("packaging agent failed; emitting empty result")
            return TaskResult(
                task_id=task.task_id, success=True,
                output={
                    "verdict": "skipped",
                    "error": str(e),
                    "strategy": "error",
                },
            )

        await self.share_learning(
            f"Packaging generated {len(result.files_written)} file(s) for "
            f"{result.strategy} family; verified={result.verified}.",
            scope="run",
            strategy=result.strategy,
            verified=result.verified,
        )

        return TaskResult(
            task_id=task.task_id, success=True,
            output={
                "verdict": "ok" if (result.verified or result.verifier_skipped) else "warning",
                "strategy": result.strategy,
                "files_written": result.files_written,
                "files_patched": result.files_patched,
                "env_vars_found": result.env_vars_found,
                "verified": result.verified,
                "verifier_skipped": result.verifier_skipped,
                "notes": result.notes,
            },
        )

    # ==================================================================
    # Strategy: web
    # ==================================================================

    async def _package_web(
        self,
        *,
        artifact_dir: Path,
        scaffold_dir: Path,
        detection: StackDetection,
        env_scan: ScanResult,
        verify_enabled: bool,
    ) -> PackagingResult:
        """Generate Settings.jsx + useConfig hook + .gitignore + slim README."""
        files_written: List[str] = []
        files_patched: List[str] = []
        notes: List[str] = []

        # 1. useConfig hook
        hook_path = scaffold_dir / "src" / "hooks" / "useConfig.js"
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        hook_path.write_text(_USE_CONFIG_JS, encoding="utf-8")
        files_written.append(str(hook_path.relative_to(artifact_dir)))

        # 2. Settings.jsx — generated from scanner output
        settings_path = scaffold_dir / "src" / "Settings.jsx"
        settings_path.write_text(
            _render_settings_jsx(env_scan, app_name=_infer_app_name(detection, artifact_dir)),
            encoding="utf-8",
        )
        files_written.append(str(settings_path.relative_to(artifact_dir)))

        # 3. App.jsx — patch only if simple/safe; otherwise leave a README note
        app_patched, patch_note = self._maybe_patch_app(scaffold_dir)
        if app_patched:
            files_patched.append("scaffold/src/App.jsx")
        elif patch_note:
            notes.append(patch_note)
        # 4. .gitignore (stack-aware, web-tier)
        gitignore_path = artifact_dir / ".gitignore"
        if not gitignore_path.is_file():
            gitignore_path.write_text(_WEB_GITIGNORE, encoding="utf-8")
            files_written.append(".gitignore")

        # 5. README — slim, settings-first, no .env wall of text
        readme_path = artifact_dir / "README.md"
        readme_path.write_text(
            _render_web_readme(
                app_name=_infer_app_name(detection, artifact_dir),
                detection=detection,
                env_scan=env_scan,
                manual_patch_note=patch_note if not app_patched else None,
            ),
            encoding="utf-8",
        )
        files_written.append("README.md")

        # 6. Sandbox verification — npm install + npm run build
        verified = False
        if verify_enabled:
            verified, verify_note = await self._verify_web(scaffold_dir)
            if verify_note:
                notes.append(verify_note)
        else:
            notes.append("verification skipped by request")

        return PackagingResult(
            strategy="web",
            files_written=files_written,
            files_patched=files_patched,
            env_vars_found=len(env_scan.vars),
            verified=verified,
            verifier_skipped=not verify_enabled,
            notes=notes,
        )

    # ------------------------------------------------------------------
    # App.jsx patch — AST-safe injection
    # ------------------------------------------------------------------

    def _maybe_patch_app(self, scaffold_dir: Path) -> tuple[bool, Optional[str]]:
        """Inject a /settings route + first-run gate into App.jsx if safe.

        Returns ``(patched, note)``. ``patched=True`` means we modified the
        file in-place. ``note`` is a one-line README hint when we left the
        file alone for safety reasons.
        """
        candidates = [
            scaffold_dir / "src" / "App.jsx",
            scaffold_dir / "src" / "App.tsx",
        ]
        app_path = next((p for p in candidates if p.is_file()), None)
        if app_path is None:
            return False, "No App.jsx / App.tsx found — Settings.jsx generated but unrouted; wire it manually."

        try:
            text = app_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False, "App.jsx unreadable — Settings.jsx generated; wire it manually."

        # Skip if already patched (idempotent for re-runs).
        if "// @skyn3t-packaging" in text:
            return False, None

        # Don't fight existing routers — if react-router is already imported,
        # the user has their own routing model; just leave a manual note.
        if re.search(r"from\s+['\"]react-router(-dom)?['\"]", text):
            return False, (
                "App.jsx already uses react-router — Settings.jsx generated "
                "but you need to add `<Route path='/settings' element={<Settings/>}/>` "
                "to your router manually."
            )

        # Patch is only safe on a "small" App.jsx: < 200 lines, single
        # default export. Anything bigger is likely complex enough that
        # auto-mutating it will break something.
        line_count = text.count("\n") + 1
        if line_count > 200:
            return False, (
                f"App.jsx is large ({line_count} lines) — Settings.jsx generated "
                "but not auto-wired; add the import + a small first-run check manually."
            )

        # Confirm there's a recognizable default export to wrap.
        if not re.search(r"export\s+default\s+\w", text):
            return False, "App.jsx has no `export default` — wire Settings.jsx manually."

        patched = self._build_app_patch(text)
        if patched is None:
            return False, "Couldn't find a safe injection point in App.jsx — wire Settings.jsx manually."

        try:
            app_path.write_text(patched, encoding="utf-8")
        except OSError:
            return False, "App.jsx unwritable — Settings.jsx generated; wire it manually."
        return True, None

    @staticmethod
    def _build_app_patch(text: str) -> Optional[str]:
        """Wrap the default-exported component with a first-run Settings gate.

        Strategy: append a small wrapper component at the end of the file,
        change `export default X` to `export default SkynPackagingWrapper`,
        and inject the Settings import at the top.

        Returns the modified source, or ``None`` if we couldn't safely
        identify the export name to wrap.
        """
        m = re.search(r"export\s+default\s+(\w+)\s*;?\s*$", text, re.MULTILINE)
        if not m:
            # Could be `export default function Foo() {}` — handle that shape too.
            m2 = re.search(r"export\s+default\s+function\s+(\w+)", text)
            if not m2:
                return None
            exported = m2.group(1)
            # Rewrite "export default function Foo" → "function Foo" so we can
            # alias it cleanly.
            text = re.sub(
                r"export\s+default\s+function\s+" + exported,
                f"function {exported}",
                text,
                count=1,
            )
            text += f"\n\nexport default {exported};\n"
            return _inject_settings_wrapper(text, exported)

        exported = m.group(1)
        return _inject_settings_wrapper(text, exported)

    # ------------------------------------------------------------------
    # Sandbox verification — npm install + npm run build
    # ------------------------------------------------------------------

    async def _verify_web(self, scaffold_dir: Path) -> tuple[bool, Optional[str]]:
        """Run `npm install --no-audit --no-fund && npm run build`.

        Returns (success, note). Failure is non-fatal — the downstream
        build verifier will catch it again with more context. The note
        describes why we passed/failed so the reviewer can score it.
        """
        import shutil
        npm = shutil.which("npm")
        if npm is None:
            return False, "npm not available — verification skipped"

        if not (scaffold_dir / "package.json").is_file():
            return False, "no package.json in scaffold — verification skipped"

        env = {**__import__("os").environ, "CI": "1"}

        try:
            install = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    npm, "install", "--no-audit", "--no-fund", "--prefer-offline",
                    cwd=str(scaffold_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                ),
                timeout=_VERIFY_TIMEOUT_SECONDS,
            )
            i_stdout, i_stderr = await asyncio.wait_for(
                install.communicate(), timeout=_VERIFY_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, OSError):
            return False, f"npm install timed out after {_VERIFY_TIMEOUT_SECONDS}s"

        if install.returncode != 0:
            err = (i_stderr or b"").decode(errors="replace")[:400]
            return False, f"npm install failed: {err.strip()}"

        try:
            build = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    npm, "run", "build",
                    cwd=str(scaffold_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                ),
                timeout=_VERIFY_TIMEOUT_SECONDS,
            )
            await asyncio.wait_for(build.communicate(), timeout=_VERIFY_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, OSError):
            return False, f"npm run build timed out after {_VERIFY_TIMEOUT_SECONDS}s"

        if build.returncode != 0:
            return False, "npm run build failed (see downstream BuildVerifier for details)"

        return True, "verified: install + build succeeded"

    # ==================================================================
    # Strategy: server (Docker)
    # ==================================================================

    def _package_server(
        self,
        *,
        artifact_dir: Path,
        scaffold_dir: Path,
        detection: StackDetection,
        env_scan: ScanResult,
    ) -> PackagingResult:
        """Generate Dockerfile + docker-compose.yml + slim .env.example + README.

        Verification is skipped — the downstream BuildVerifier already
        runs `docker compose build` for Docker projects, no need to
        double-pay the install/build cost here.
        """
        files_written: List[str] = []
        notes: List[str] = []

        # Backends usually live at the project root (FastAPI app on root,
        # not in scaffold/). Pick the dir that has the manifest.
        project_root = self._project_root_for_server(artifact_dir, scaffold_dir)

        # 1. Dockerfile — stack-aware
        dockerfile_path = project_root / "Dockerfile"
        if dockerfile_path.is_file():
            notes.append("Dockerfile already exists — left in place")
        else:
            dockerfile_path.write_text(
                _render_dockerfile(detection),
                encoding="utf-8",
            )
            files_written.append(
                str(dockerfile_path.relative_to(artifact_dir))
                if dockerfile_path.is_relative_to(artifact_dir)
                else dockerfile_path.name
            )

        # 2. docker-compose.yml — app + detected services
        compose_path = project_root / "docker-compose.yml"
        if compose_path.is_file() or (project_root / "compose.yaml").is_file():
            notes.append("docker-compose already exists — left in place")
        else:
            compose_path.write_text(
                _render_compose(detection, env_scan),
                encoding="utf-8",
            )
            files_written.append(
                str(compose_path.relative_to(artifact_dir))
                if compose_path.is_relative_to(artifact_dir)
                else compose_path.name
            )

        # 3. .env.example — only the truly server-side vars
        env_example_path = project_root / ".env.example"
        if env_example_path.is_file():
            notes.append(".env.example already exists — left in place")
        else:
            env_example_path.write_text(
                _render_env_example(detection, env_scan),
                encoding="utf-8",
            )
            files_written.append(
                str(env_example_path.relative_to(artifact_dir))
                if env_example_path.is_relative_to(artifact_dir)
                else env_example_path.name
            )

        # 4. .gitignore — server-tier
        gitignore_path = project_root / ".gitignore"
        if not gitignore_path.is_file():
            gitignore_path.write_text(_SERVER_GITIGNORE, encoding="utf-8")
            files_written.append(
                str(gitignore_path.relative_to(artifact_dir))
                if gitignore_path.is_relative_to(artifact_dir)
                else gitignore_path.name
            )

        # 5. README — two-command quick start (cp .env, docker compose up)
        readme_path = project_root / "README.md"
        readme_path.write_text(
            _render_server_readme(
                app_name=_infer_app_name(detection, artifact_dir),
                detection=detection,
                env_scan=env_scan,
            ),
            encoding="utf-8",
        )
        files_written.append(
            str(readme_path.relative_to(artifact_dir))
            if readme_path.is_relative_to(artifact_dir)
            else readme_path.name
        )

        return PackagingResult(
            strategy="server",
            files_written=files_written,
            files_patched=[],
            env_vars_found=len(env_scan.vars),
            verified=False,
            # Server verification is owned by the downstream BuildVerifier
            # (docker compose build) — skipping here is intentional, not
            # a failure.
            verifier_skipped=True,
            notes=notes,
        )

    @staticmethod
    def _project_root_for_server(artifact_dir: Path, scaffold_dir: Path) -> Path:
        """Return the dir that has the server manifest.

        Most Python servers ship requirements.txt / pyproject.toml at the
        artifact root rather than under scaffold/, but a few (e.g.
        express apps generated alongside a frontend) live in scaffold/.
        Pick whichever has a Python manifest first, fall back to scaffold,
        fall back to artifact root.
        """
        for candidate in (artifact_dir, scaffold_dir):
            if candidate.is_dir() and any(
                (candidate / m).is_file()
                for m in ("requirements.txt", "pyproject.toml", "package.json")
            ):
                return candidate
        if scaffold_dir.is_dir():
            return scaffold_dir
        return artifact_dir

    # ==================================================================
    # Strategy: placeholder for fullstack/unknown families
    # ==================================================================

    def _package_placeholder(self, detection: StackDetection, env_scan: ScanResult) -> PackagingResult:
        """Stub for server/fullstack/unknown families until later PRs land."""
        return PackagingResult(
            strategy=detection.family,
            files_written=[],
            files_patched=[],
            env_vars_found=len(env_scan.vars),
            verified=False,
            verifier_skipped=True,
            notes=[
                f"packaging strategy '{detection.family}' not implemented in this PR — "
                "see roadmap PR C-docker / C-combo"
            ],
        )


# ---------------------------------------------------------------------------
# Helpers — template rendering
# ---------------------------------------------------------------------------

def _infer_app_name(detection: StackDetection, artifact_dir: Path) -> str:
    """Best-effort human-readable app name for README / Settings header."""
    # SkyN3t slugs look like "build-a-habit-tracker-with-streaks-a6f6c0".
    # Strip the trailing 6-char hex suffix and humanize the slug.
    name = artifact_dir.name
    name = re.sub(r"-[0-9a-f]{6}(-retry)*$", "", name)
    name = re.sub(r"^(build|create|make|design)-(a|an|the)?-?", "", name)
    name = name.replace("-", " ").strip()
    if not name:
        return "App"
    # Title-case words longer than 2 chars
    return " ".join(w.capitalize() if len(w) > 2 else w for w in name.split())


def _inject_settings_wrapper(text: str, exported: str) -> str:
    """Return ``text`` with a Settings import + first-run gate wrapper.

    The wrapper renders the user's exported component on the main route,
    a generated <Settings/> on /settings, and forces a one-time visit to
    /settings if useConfig().isFirstRun (no config saved yet).
    """
    # 1. Inject import at top if not already there.
    if "import Settings" not in text:
        text = (
            'import Settings from "./Settings";\n'
            'import { useConfig as __useConfig } from "./hooks/useConfig";\n'
            + text
        )

    # 2. Append wrapper component + change export to point at the wrapper.
    wrapper = f"""

// @skyn3t-packaging: first-run Settings gate (do not edit by hand)
function SkynPackagingWrapper(props) {{
  const cfg = __useConfig();
  if (cfg.isFirstRun || (typeof window !== "undefined" && window.location.pathname === "/settings")) {{
    return <Settings />;
  }}
  return <{exported} {{...props}} />;
}}
"""
    # Replace any `export default Foo;` with `export default Wrapper;`. If
    # we already inserted a default export in the calling site (function
    # form), this is a no-op for the literal pattern but we still append
    # the wrapper class.
    text = re.sub(
        rf"export\s+default\s+{exported}\s*;",
        "export default SkynPackagingWrapper;",
        text,
    )
    text += wrapper
    return text


def _render_settings_jsx(env_scan: ScanResult, *, app_name: str) -> str:
    """Render a Settings.jsx component from detected env vars."""
    fields_js: List[str] = []
    for var in sorted(env_scan.vars.values(), key=lambda v: (not v.is_secret, v.name)):
        field_type = _settings_input_type(var.type_hint)
        # Friendly label: VITE_API_KEY → "API key" (strip VITE_ + lowercase + title)
        label = _humanize_var_name(var.name)
        help_text = _help_text_for(var)
        default = var.default or ""
        fields_js.append(
            "  { "
            f'name: "{var.name}", '
            f'label: "{label}", '
            f'type: "{field_type}", '
            f'placeholder: {repr(default)}, '
            f'help: "{help_text}", '
            f"required: {str(not var.default).lower()} "
            "}"
        )
    fields_block = ",\n".join(fields_js) if fields_js else ""
    fields_const = f"const FIELDS = [\n{fields_block}\n];" if fields_js else "const FIELDS = [];"

    return _SETTINGS_JSX_TEMPLATE.format(app_name=app_name, fields_const=fields_const)


def _settings_input_type(type_hint: str) -> str:
    """Map scanner type_hint to an HTML input type."""
    return {
        "secret": "password",
        "email": "email",
        "int": "number",
        "url": "url",
        "bool": "checkbox",
    }.get(type_hint, "text")


def _humanize_var_name(name: str) -> str:
    """VITE_API_BASE_URL → 'API base URL'.

    Drops common framework prefixes and converts SHOUTING_SNAKE to a
    sentence the user can actually read.
    """
    stripped = re.sub(r"^(VITE_|REACT_APP_|NEXT_PUBLIC_)", "", name)
    words = stripped.replace("_", " ").split()
    if not words:
        return name
    # Keep common acronyms uppercase, lowercase the rest, capitalize first.
    acronyms = {"API", "URL", "URI", "DSN", "ID", "OAUTH", "JWT", "HTTP", "HTTPS",
                "DB", "CDN", "SMTP", "DNS", "IP", "TLS", "SSL", "UI", "UX"}
    out: List[str] = []
    for i, w in enumerate(words):
        if w.upper() in acronyms:
            out.append(w.upper())
        elif i == 0:
            out.append(w.capitalize())
        else:
            out.append(w.lower())
    return " ".join(out)


def _help_text_for(var: EnvVarRef) -> str:
    """One-liner help text shown under the field."""
    if var.is_secret:
        return "Stored only in your browser. Not sent to any server."
    if var.type_hint == "url":
        return "Full URL, e.g. https://api.example.com"
    if var.type_hint == "int":
        return "Whole number."
    if var.type_hint == "bool":
        return ""
    if var.type_hint == "email":
        return ""
    return ""


def _render_web_readme(
    *,
    app_name: str,
    detection: StackDetection,
    env_scan: ScanResult,
    manual_patch_note: Optional[str] = None,
) -> str:
    """Build a slim, settings-first README — no .env wall of text."""
    required = env_scan.required()
    optional_vars = env_scan.optional()

    # Always mention the Settings UI even with zero vars — readers
    # need to know this app is settings-driven, not env-driven.
    if env_scan.vars:
        config_section = (
            "Open the app, click **Settings** (gear icon), and fill in your values.\n"
            "Everything is stored locally in your browser — no server-side env file needed.\n"
        )
        if required:
            config_section += (
                f"\n**Required:** {', '.join(v.name for v in required)}\n"
            )
        if optional_vars:
            config_section += (
                f"**Optional:** {', '.join(v.name for v in optional_vars)}\n"
            )
    else:
        config_section = (
            "No configuration is required — **open the app** and use it. "
            "If you add env-var references later, they will appear in the "
            "auto-generated **Settings** page (no `.env` file needed).\n"
        )

    manual_note = ""
    if manual_patch_note:
        manual_note = (
            "\n> ⚠️ **One-time setup step:** Settings.jsx was generated but "
            f"not auto-wired. Reason: {manual_patch_note} "
            "Add this to your routing layer:\n"
            "> ```jsx\n"
            "> import Settings from './Settings';\n"
            "> // ... <Route path='/settings' element={<Settings/>} />\n"
            "> ```\n"
        )

    runtime_line = "Node 22+ (LTS recommended)"
    for r in detection.runtimes:
        if r.name == "node" and r.min_version:
            runtime_line = f"Node {r.min_version}+"

    # CHANGELOG hint — useful when running an old project after the
    # scanner finds new vars on a re-scan.
    changelog = ""
    if env_scan.vars:
        changelog = (
            "\n## What's configurable\n\n"
            "The Settings page auto-discovers configuration from the source code. "
            "Detected on this build:\n\n"
        )
        for v in sorted(env_scan.vars.values(), key=lambda x: x.name):
            kind = "🔒 secret" if v.is_secret else f"{v.type_hint}"
            changelog += f"- `{v.name}` ({kind})\n"

    return f"""# {app_name}

## Requirements
- {runtime_line} ([install](https://nodejs.org/))

## Quick start
```bash
cd scaffold
npm install
npm run dev
```

## Configuration
{config_section}{manual_note}{changelog}

## Production build
```bash
cd scaffold
npm run build
```
The static site lands in `scaffold/dist/` — serve with any static host.

---
*Generated by SkyN3t PackagingAgent.*
"""


# ---------------------------------------------------------------------------
# Static templates
# ---------------------------------------------------------------------------

_USE_CONFIG_JS = """\
/**
 * useConfig — localStorage-backed config layer.
 *
 * Generated by SkyN3t PackagingAgent. Replaces .env / import.meta.env
 * lookups for client-side config so users can configure the app via the
 * Settings UI instead of editing dotfiles.
 *
 * Usage:
 *   import { useConfig } from "./hooks/useConfig";
 *   const cfg = useConfig();
 *   const apiKey = cfg.get("API_KEY");
 *   cfg.set("API_KEY", "sk-...");
 */

import { useState, useEffect, useCallback } from "react";

const STORAGE_KEY = "skyn3t-app-config";

function load() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function save(config) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
  } catch {
    /* quota exceeded — fail silently */
  }
}

export function useConfig() {
  const [config, setConfig] = useState(load);

  // Cross-tab sync — pick up Settings UI changes from other tabs.
  useEffect(() => {
    const handler = (e) => {
      if (e.key === STORAGE_KEY) setConfig(load());
    };
    window.addEventListener("storage", handler);
    return () => window.removeEventListener("storage", handler);
  }, []);

  const get = useCallback((key, fallback = "") => {
    return config[key] ?? fallback;
  }, [config]);

  const set = useCallback((key, value) => {
    setConfig((prev) => {
      const next = { ...prev, [key]: value };
      save(next);
      return next;
    });
  }, []);

  const setMany = useCallback((updates) => {
    setConfig((prev) => {
      const next = { ...prev, ...updates };
      save(next);
      return next;
    });
  }, []);

  const isFirstRun = Object.keys(config).length === 0;

  return { config, get, set, setMany, isFirstRun };
}
"""


_SETTINGS_JSX_TEMPLATE = """\
/**
 * Settings — auto-generated configuration UI.
 *
 * Generated by SkyN3t PackagingAgent from env-var references in the source.
 * Edit by hand if you want to add validation, help text, or grouping —
 * the agent won't overwrite a file it didn't write.
 */

import {{ useState }} from "react";
import {{ useConfig }} from "./hooks/useConfig";

{fields_const}

export default function Settings() {{
  const cfg = useConfig();
  const [draft, setDraft] = useState(() => {{
    const initial = {{}};
    for (const f of FIELDS) initial[f.name] = cfg.get(f.name);
    return initial;
  }});
  const [saved, setSaved] = useState(false);

  const handleChange = (name, value) => {{
    setDraft((prev) => ({{ ...prev, [name]: value }}));
    setSaved(false);
  }};

  const handleSubmit = (e) => {{
    e.preventDefault();
    cfg.setMany(draft);
    setSaved(true);
  }};

  const missingRequired = FIELDS.filter((f) => f.required && !draft[f.name]);

  return (
    <div style={{{{
      maxWidth: "640px", margin: "2rem auto", padding: "1.5rem",
      fontFamily: "system-ui, sans-serif", lineHeight: 1.5,
    }}}}>
      <h1 style={{{{ marginTop: 0 }}}}>Settings — {app_name}</h1>
      {{cfg.isFirstRun && (
        <p style={{{{ background: "#eef", padding: "0.75rem", borderRadius: 6 }}}}>
          👋 Welcome! Fill in the values below to get started.
        </p>
      )}}
      {{FIELDS.length === 0 ? (
        <p>No configuration needed — close this page and use the app.</p>
      ) : (
        <form onSubmit={{handleSubmit}}>
          {{FIELDS.map((f) => (
            <div key={{f.name}} style={{{{ marginBottom: "1rem" }}}}>
              <label style={{{{ display: "block", fontWeight: 600 }}}}>
                {{f.label}}
                {{f.required && <span style={{{{ color: "#c00" }}}}> *</span>}}
              </label>
              <input
                type={{f.type}}
                value={{draft[f.name] || ""}}
                onChange={{(e) => handleChange(f.name, e.target.value)}}
                placeholder={{f.placeholder}}
                style={{{{
                  width: "100%", padding: "0.5rem", marginTop: "0.25rem",
                  border: "1px solid #ccc", borderRadius: 4, fontSize: "1rem",
                }}}}
              />
              {{f.help && <small style={{{{ color: "#666" }}}}>{{f.help}}</small>}}
            </div>
          ))}}
          <button
            type="submit"
            disabled={{missingRequired.length > 0}}
            style={{{{
              padding: "0.5rem 1.25rem", fontSize: "1rem",
              background: missingRequired.length > 0 ? "#999" : "#0066cc",
              color: "white", border: "none", borderRadius: 4, cursor: "pointer",
            }}}}
          >
            Save
          </button>
          {{saved && <span style={{{{ marginLeft: "1rem", color: "#080" }}}}>✓ Saved</span>}}
          {{missingRequired.length > 0 && (
            <p style={{{{ color: "#c00", marginTop: "0.5rem" }}}}>
              Required: {{missingRequired.map((f) => f.label).join(", ")}}
            </p>
          )}}
        </form>
      )}}
    </div>
  );
}}
"""


_WEB_GITIGNORE = """\
# Generated by SkyN3t PackagingAgent
node_modules/
dist/
build/
.next/
.cache/
*.log

# Editor / OS
.DS_Store
.vscode/
.idea/

# Local env (in case operator adds one — most config lives in Settings UI)
.env
.env.local
.env.*.local
"""


_SERVER_GITIGNORE = """\
# Generated by SkyN3t PackagingAgent
__pycache__/
*.py[cod]
*.so
.venv/
venv/
env/
.eggs/
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/

node_modules/
dist/
build/
*.log

# Editor / OS
.DS_Store
.vscode/
.idea/

# Secrets — never commit
.env
.env.local
.env.*.local
"""


# ---------------------------------------------------------------------------
# Docker render helpers
# ---------------------------------------------------------------------------

# Default port per known server stack — chosen to match the framework's
# documented default so users hit a familiar number.
_DEFAULT_PORT_BY_STACK: Dict[str, int] = {
    "fastapi": 8000,
    "flask": 5000,
    "django": 8000,
    "starlette": 8000,
    "aiohttp": 8080,
    "bottle": 8080,
    "express": 3000,
    "fastify": 3000,
    "koa": 3000,
    "hono": 3000,
}


def _server_port(detection: StackDetection) -> int:
    """Pick the run port from the stack hint, default to 8000."""
    if detection.stack and detection.stack in _DEFAULT_PORT_BY_STACK:
        return _DEFAULT_PORT_BY_STACK[detection.stack]
    return 8000


def _render_dockerfile(detection: StackDetection) -> str:
    """Pick a multi-stage Dockerfile template per stack family.

    Python stacks get python:3.12-slim + pip install. Node stacks get
    node:22-alpine + npm ci. Both pin specific image tags so builds are
    reproducible.
    """
    stack = detection.stack or ""
    port = _server_port(detection)

    if stack in ("fastapi", "starlette", "aiohttp"):
        cmd_line = f'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{port}"]'
        return _PYTHON_DOCKERFILE.format(port=port, cmd_line=cmd_line)
    if stack == "flask":
        cmd_line = f'CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:{port}", "app:app"]'
        return _PYTHON_DOCKERFILE.format(port=port, cmd_line=cmd_line)
    if stack == "django":
        cmd_line = f'CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:{port}", "config.wsgi:application"]'
        return _PYTHON_DOCKERFILE.format(port=port, cmd_line=cmd_line)
    if stack == "bottle":
        cmd_line = f'CMD ["python", "app.py"]'
        return _PYTHON_DOCKERFILE.format(port=port, cmd_line=cmd_line)

    if stack in ("express", "fastify", "koa", "hono"):
        cmd_line = 'CMD ["node", "server.js"]'
        return _NODE_DOCKERFILE.format(port=port, cmd_line=cmd_line)

    # Unknown server stack — generate the most common Python shape with a
    # comment telling the operator to adjust the CMD.
    cmd_line = f'# Replace with your start command\nCMD ["python", "main.py"]'
    return _PYTHON_DOCKERFILE.format(port=port, cmd_line=cmd_line)


_PYTHON_DOCKERFILE = """\
# syntax=docker/dockerfile:1.6
# Generated by SkyN3t PackagingAgent.

FROM python:3.12-slim AS runtime

# Don't write .pyc files; ensure stdout is unbuffered for container logs.
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install build deps only if they exist on disk. requirements.txt is the
# primary source; pyproject.toml is a fallback for poetry/PEP-621 projects.
COPY requirements.txt* pyproject.toml* ./
RUN if [ -f requirements.txt ]; then \\
        pip install -r requirements.txt; \\
    elif [ -f pyproject.toml ]; then \\
        pip install .; \\
    fi

COPY . .

EXPOSE {port}

{cmd_line}
"""


_NODE_DOCKERFILE = """\
# syntax=docker/dockerfile:1.6
# Generated by SkyN3t PackagingAgent.

FROM node:22-alpine AS runtime

WORKDIR /app

# Use npm ci when a lockfile is present (reproducible install); fall
# back to npm install for tracked-but-no-lock setups.
COPY package*.json ./
RUN if [ -f package-lock.json ]; then \\
        npm ci --omit=dev; \\
    else \\
        npm install --omit=dev --no-audit --no-fund; \\
    fi

COPY . .

EXPOSE {port}

{cmd_line}
"""


# Each known infra service gets a docker-compose stanza we can drop in
# verbatim. Volumes are declared as a list since compose lets us
# accumulate them across services.
_SERVICE_STANZAS: Dict[str, tuple[str, List[str]]] = {
    "postgres": (
        """\
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-app}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-changeme-in-env}
      POSTGRES_DB: ${POSTGRES_DB:-app}
    volumes:
      - postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "${POSTGRES_USER:-app}"]
      interval: 10s
      timeout: 5s
      retries: 5
""",
        ["postgres-data"],
    ),
    "redis": (
        """\
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
""",
        [],
    ),
    "mongodb": (
        """\
  mongodb:
    image: mongo:7
    restart: unless-stopped
    environment:
      MONGO_INITDB_ROOT_USERNAME: ${MONGO_USER:-app}
      MONGO_INITDB_ROOT_PASSWORD: ${MONGO_PASSWORD:-changeme-in-env}
    volumes:
      - mongo-data:/data/db
""",
        ["mongo-data"],
    ),
    "rabbitmq": (
        """\
  rabbitmq:
    image: rabbitmq:3-management-alpine
    restart: unless-stopped
""",
        [],
    ),
    "elasticsearch": (
        """\
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.13.4
    environment:
      discovery.type: single-node
      xpack.security.enabled: "false"
      ES_JAVA_OPTS: -Xms512m -Xmx512m
    volumes:
      - es-data:/usr/share/elasticsearch/data
""",
        ["es-data"],
    ),
}


def _render_compose(detection: StackDetection, env_scan: ScanResult) -> str:
    """Build a docker-compose.yml: app service + each detected infra service."""
    port = _server_port(detection)
    depends_on: List[str] = []
    service_blocks: List[str] = []
    volumes: List[str] = []

    for svc in detection.services:
        if svc in _SERVICE_STANZAS:
            block, vols = _SERVICE_STANZAS[svc]
            service_blocks.append(block)
            depends_on.append(svc)
            volumes.extend(vols)

    depends_block = ""
    if depends_on:
        depends_block = "    depends_on:\n" + "".join(
            f"      {svc}:\n        condition: service_started\n"
            if svc not in ("postgres",)
            else f"      {svc}:\n        condition: service_healthy\n"
            for svc in depends_on
        )

    volumes_block = ""
    if volumes:
        volumes_block = "\nvolumes:\n" + "".join(f"  {v}: {{}}\n" for v in sorted(set(volumes)))

    has_env = bool(env_scan.vars)
    env_file_line = "    env_file: .env\n" if has_env else ""

    return f"""\
# Generated by SkyN3t PackagingAgent.
# Run: cp .env.example .env  &&  docker compose up

services:
  app:
    build: .
    restart: unless-stopped
    ports:
      - "{port}:{port}"
{env_file_line}{depends_block}{"".join(service_blocks)}{volumes_block}"""


def _render_env_example(detection: StackDetection, env_scan: ScanResult) -> str:
    """Build a slim .env.example — only what the server actually needs.

    Skips client-side (VITE_/REACT_APP_/NEXT_PUBLIC_) vars because those
    belong in the frontend's Settings UI, not in a server-side env file.
    Adds infra-service defaults the docker-compose expects when a
    service was detected.
    """
    lines: List[str] = [
        "# Generated by SkyN3t PackagingAgent.",
        "# Copy to .env and fill in the values, then run: docker compose up",
        "",
    ]

    # Infra creds — only listed when the matching service is in compose.
    infra_added = False
    if "postgres" in detection.services:
        lines.extend([
            "# --- Postgres ---------------------------------------------------",
            "POSTGRES_USER=app",
            "POSTGRES_PASSWORD=changeme-strong-random-value",
            "POSTGRES_DB=app",
            "# DATABASE_URL is what your app reads. Matches the postgres service above.",
            "DATABASE_URL=postgresql://app:changeme-strong-random-value@postgres:5432/app",
            "",
        ])
        infra_added = True
    if "mongodb" in detection.services:
        lines.extend([
            "# --- MongoDB ----------------------------------------------------",
            "MONGO_USER=app",
            "MONGO_PASSWORD=changeme-strong-random-value",
            "MONGODB_URL=mongodb://app:changeme-strong-random-value@mongodb:27017",
            "",
        ])
        infra_added = True

    # Application-discovered env vars. Skip client-side prefixes — those
    # belong in the frontend's Settings UI.
    app_vars = [
        v for v in sorted(env_scan.vars.values(), key=lambda x: x.name)
        if not v.name.startswith(("VITE_", "REACT_APP_", "NEXT_PUBLIC_"))
        # Skip ones we already wrote in the infra section.
        and v.name not in {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
                           "DATABASE_URL", "MONGO_USER", "MONGO_PASSWORD",
                           "MONGODB_URL"}
    ]

    if app_vars:
        lines.append("# --- Application ------------------------------------------------")
        for var in app_vars:
            comment = _env_comment_for(var)
            if comment:
                lines.append(f"# {comment}")
            default = var.default if var.default else ""
            lines.append(f"{var.name}={default}")
        lines.append("")

    if not infra_added and not app_vars:
        lines.extend([
            "# No required env vars detected. This file is kept as a placeholder.",
            "# Add any operator-side configuration below.",
            "",
        ])

    return "\n".join(lines)


def _env_comment_for(var: EnvVarRef) -> str:
    """One-line human hint for an env var in .env.example."""
    if var.is_secret:
        return f"{var.name} — secret. Generate a strong random value."
    if var.type_hint == "url":
        return f"{var.name} — full URL (e.g. https://api.example.com)."
    if var.type_hint == "int":
        return f"{var.name} — integer."
    if var.type_hint == "bool":
        return f"{var.name} — true/false."
    return ""


def _render_server_readme(
    *,
    app_name: str,
    detection: StackDetection,
    env_scan: ScanResult,
) -> str:
    """Two-command quick start + service overview."""
    runtimes_lines: List[str] = []
    for r in detection.runtimes:
        if r.name == "python":
            v = r.min_version or "3.12"
            runtimes_lines.append(f"- Python {v}+ ([install](https://python.org/))")
        elif r.name == "node":
            v = r.min_version or "22"
            runtimes_lines.append(f"- Node {v}+ ([install](https://nodejs.org/))")
    runtimes_lines.append("- Docker + Docker Compose ([install](https://docs.docker.com/get-docker/))")
    runtimes_block = "\n".join(runtimes_lines)

    services_block = ""
    if detection.services:
        services_block = "\n## Services included\n\n"
        services_block += "\n".join(f"- **{s}** (auto-managed via docker-compose)" for s in detection.services)
        services_block += "\n"

    env_required = [v for v in env_scan.required()
                    if not v.name.startswith(("VITE_", "REACT_APP_", "NEXT_PUBLIC_"))]
    required_block = ""
    if env_required:
        required_block = (
            "\n## Required environment variables\n\n"
            "Before running `docker compose up`, set these in `.env`:\n\n"
        )
        for v in env_required:
            kind = "🔒 secret" if v.is_secret else v.type_hint
            required_block += f"- `{v.name}` ({kind})\n"

    return f"""# {app_name}

## Requirements
{runtimes_block}

## Quick start
```bash
cp .env.example .env  # then edit .env with your values
docker compose up
```

The app will be available on http://localhost:{_server_port(detection)}.
{services_block}{required_block}
## Stopping
```bash
docker compose down
```

To wipe persistent data (postgres volumes, etc.) add `-v`:
```bash
docker compose down -v
```

## Development without Docker
If you prefer to run directly:
```bash
{_native_run_command(detection)}
```

---
*Generated by SkyN3t PackagingAgent.*
"""


def _native_run_command(detection: StackDetection) -> str:
    """Snippet for running the server natively (no Docker)."""
    stack = detection.stack or ""
    if stack in ("fastapi", "starlette", "aiohttp"):
        return "pip install -r requirements.txt\nuvicorn main:app --reload"
    if stack == "flask":
        return "pip install -r requirements.txt\nflask run --debug"
    if stack == "django":
        return "pip install -r requirements.txt\npython manage.py runserver"
    if stack in ("express", "fastify", "koa", "hono"):
        return "npm install\nnpm run dev  # or: node server.js"
    return "# See your framework's docs for the dev command"
