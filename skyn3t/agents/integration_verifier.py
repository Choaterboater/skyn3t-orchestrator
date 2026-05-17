"""IntegrationContractVerifierAgent — does the frontend agree with the backend?

BuildVerifier checks syntax. BootVerifier checks the server starts.
This agent checks that every API route the frontend calls actually exists
on the backend and returns a plausible response.

Real failures we've shipped past both verifiers and the reviewer:

  - Frontend calls `fetch('/api/items')` but backend only mounts
    `/api/things`. The route path prefix is mismatched.
  - Frontend expects `{ items: [...] }` but backend returns `{ data: [...] }`.
  - Frontend calls `/api/config/test` but the config router only handles
    GET /:slug, not POST /:slug/test.

Every one of those is "file A expects one contract, file B implements another."
No single-file checker can catch them.

Output schema mirrors BuildVerifier/BootVerifier so the runner's fix-loop
and auto-retry hooks slot in unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from skyn3t.agents.boot_verifier import _named_export_mismatch_hint
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.agents.integration_verifier")

DEFAULT_BOOT_TIMEOUT = 45
DEFAULT_VERIFY_TIMEOUT = 15
DEFAULT_TOTAL_TIMEOUT = 120


@dataclass
class ProjectProbe:
    """What we figured out about the scaffold layout."""

    frontend_dir: Optional[Path] = None
    backend_dir: Optional[Path] = None
    backend_kind: str = "unknown"  # 'node-express' | 'python-fastapi' | 'python-flask' | 'unknown'
    backend_entry: str = ""
    backend_cwd: str = "."
    backend_port: int = 3100
    install_cmd: Optional[List[str]] = None
    boot_cmd: List[str] = field(default_factory=list)


@dataclass
class RouteIssue:
    """A single contract mismatch."""

    frontend_path: str
    method: str = "GET"
    issue: str = ""  # 'missing' | 'wrong_status' | 'not_json'
    backend_match: Optional[str] = None
    http_status: int = 0
    detail: str = ""


class IntegrationContractVerifierAgent(BaseAgent):
    """Verifies frontend-to-backend API contract."""

    def __init__(
        self,
        name: str = "integration_verifier",
        *,
        event_bus: Optional[EventBus] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="verifier",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="integration_verification",
                description=(
                    "Cross-checks frontend API calls against backend routes, "
                    "boots the server, and curls each route to confirm it responds."
                ),
                parameters={"scaffold_dir": "str"},
            )
        )
        cfg = config or {}
        self.boot_timeout = int(cfg.get("boot_timeout", DEFAULT_BOOT_TIMEOUT))
        self.verify_timeout = int(cfg.get("verify_timeout", DEFAULT_VERIFY_TIMEOUT))
        self.total_timeout = int(cfg.get("total_timeout", DEFAULT_TOTAL_TIMEOUT))

    async def initialize(self) -> None:
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(
        self, task: TaskRequest, stdin_data: str | None = None
    ) -> TaskResult:
        data = task.input_data or {}
        scaffold_dir_raw = (
            data.get("scaffold_dir")
            or (
                str(Path(data.get("artifact_dir", "")) / "scaffold")
                if data.get("artifact_dir")
                else None
            )
        )
        if not scaffold_dir_raw:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error="scaffold_dir required",
            )
        scaffold_dir = Path(scaffold_dir_raw).expanduser().resolve()
        if not scaffold_dir.exists() or not scaffold_dir.is_dir():
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"scaffold_dir does not exist: {scaffold_dir}",
            )

        probe = self._detect_project(scaffold_dir)
        await self.think(
            f"integration probe: frontend={probe.frontend_dir}, "
            f"backend={probe.backend_dir}, kind={probe.backend_kind}"
        )

        # Nothing to verify if there's no frontend or no backend.
        if probe.backend_kind == "unknown":
            return self._skip_output(
                scaffold_dir, "No backend detected — integration check not applicable."
            )
        if probe.frontend_dir is None:
            return self._skip_output(
                scaffold_dir, "No frontend detected — integration check not applicable."
            )
        if probe.backend_dir is None:
            return self._skip_output(
                scaffold_dir, "Backend kind detected but backend directory is missing."
            )

        # Extract routes from both sides.
        frontend_routes = self._extract_frontend_routes(probe.frontend_dir)
        backend_routes = self._extract_backend_routes(probe.backend_dir)

        await self.think(
            f"frontend calls: {len(frontend_routes)} unique paths; "
            f"backend routes: {len(backend_routes)} unique paths"
        )

        if not frontend_routes:
            return self._skip_output(
                scaffold_dir,
                "No API calls found in frontend — integration check not applicable.",
            )

        # Boot the backend so we can curl the routes.
        start = time.monotonic()
        actual_port = self._free_port(probe.backend_port)
        boot_env = self._build_boot_env(scaffold_dir, probe, actual_port)
        boot_cwd = scaffold_dir / probe.backend_cwd

        # Ensure a runnable .env exists (same logic as boot_verifier).
        self._ensure_runnable_env(scaffold_dir, probe)

        if probe.install_cmd:
            install_ok, _, install_err = await self._run_with_timeout(
                probe.install_cmd, boot_cwd, 240, env=os.environ.copy()
            )
            install_log = install_err or ""
            if not install_ok:
                relaxed_cmd = self._relaxed_install_cmd(probe.install_cmd)
                if relaxed_cmd is not None:
                    install_ok, install_out, install_err = await self._run_with_timeout(
                        relaxed_cmd, boot_cwd, 240, env=os.environ.copy()
                    )
                    install_log = (install_err or "") + "\n" + (install_out or "")
            if not install_ok:
                return self._fail_output(
                    probe,
                    scaffold_dir,
                    summary=f"npm install failed in {probe.backend_cwd}/ before integration test",
                    failure_hint=self._diagnose_install_failure(install_log),
                    frontend_routes=frontend_routes,
                    backend_routes=backend_routes,
                    issues=[],
                )

        if time.monotonic() - start > self.total_timeout:
            return self._fail_output(
                probe,
                scaffold_dir,
                summary="Integration verifier exceeded total time budget during install",
                failure_hint="Install phase is too slow. Reduce dependencies.",
                frontend_routes=frontend_routes,
                backend_routes=backend_routes,
                issues=[],
            )

        boot_ok, server_proc, boot_out, boot_err = await self._boot_and_wait(
            probe.boot_cmd, boot_cwd, boot_env, actual_port
        )
        if not boot_ok:
            await self._kill_proc(server_proc)
            return self._fail_output(
                probe,
                scaffold_dir,
                summary=f"Backend failed to boot within {self.boot_timeout}s",
                failure_hint=self._diagnose_boot_failure(boot_err, scaffold_dir, probe),
                frontend_routes=frontend_routes,
                backend_routes=backend_routes,
                issues=[],
            )

        # Server is running — verify every frontend-called route.
        try:
            issues = await self._verify_routes(
                frontend_routes, backend_routes, actual_port
            )
        finally:
            await self._kill_proc(server_proc)

        missing = [i for i in issues if i.issue == "missing"]
        wrong_status = [i for i in issues if i.issue == "wrong_status"]
        not_json = [i for i in issues if i.issue == "not_json"]

        if missing:
            summary = (
                f"Integration contract FAILED: {len(missing)} frontend route(s) "
                f"have no backend handler. "
                f"Missing: {', '.join(i.frontend_path for i in missing[:3])}"
                f"{' …' if len(missing) > 3 else ''}"
            )
            failure_hint = (
                "The frontend calls API routes that the backend does not implement. "
                f"Missing routes: {', '.join(i.frontend_path for i in missing)}. "
                f"Backend has: {', '.join(backend_routes[:10])}{' …' if len(backend_routes) > 10 else ''}. "
                "Add the missing Express/FastAPI route handlers or remove the frontend calls."
            )
            return self._fail_output(
                probe,
                scaffold_dir,
                summary=summary,
                failure_hint=failure_hint,
                frontend_routes=frontend_routes,
                backend_routes=backend_routes,
                issues=issues,
            )

        if wrong_status or not_json:
            summary = (
                f"Integration contract WARNINGS: "
                f"{len(wrong_status)} unexpected status code(s), "
                f"{len(not_json)} non-JSON response(s)."
            )
            # Warnings don't fail the build — they just get recorded.
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output={
                    "verdict": "yes",
                    "kind": probe.backend_kind,
                    "command": " ".join(probe.boot_cmd),
                    "port": actual_port,
                    "summary": summary,
                    "frontend_routes": frontend_routes,
                    "backend_routes": backend_routes,
                    "issues": [self._issue_to_dict(i) for i in issues],
                    "scaffold_dir": str(scaffold_dir),
                    "failure_hint": None,
                },
            )

        summary = (
            f"All {len(frontend_routes)} frontend API route(s) verified "
            f"against backend (HTTP 200+JSON)."
        )
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "verdict": "yes",
                "kind": probe.backend_kind,
                "command": " ".join(probe.boot_cmd),
                "port": actual_port,
                "summary": summary,
                "frontend_routes": frontend_routes,
                "backend_routes": backend_routes,
                "issues": [],
                "scaffold_dir": str(scaffold_dir),
                "failure_hint": None,
            },
        )

    # ── project detection ─────────────────────────────────────────

    def _detect_project(self, scaffold_dir: Path) -> ProjectProbe:
        probe = ProjectProbe()

        # Frontend detection: look for src/ with JSX/TSX, or any HTML/JS entry.
        for fe_dir in (scaffold_dir / "src", scaffold_dir / "app", scaffold_dir):
            if fe_dir.is_dir():
                # rglob() returns a generator (always truthy); take the
                # first hit to actually verify a JSX/TS file exists.
                has_jsx = any(
                    next(fe_dir.rglob(p), None) is not None
                    for p in ("*.jsx", "*.tsx", "*.js", "*.ts")
                )
                has_html = list(scaffold_dir.glob("index.html"))
                if has_jsx or has_html:
                    probe.frontend_dir = fe_dir if fe_dir != scaffold_dir else scaffold_dir
                    break

        # Backend detection: mirror boot_verifier logic.
        server_dir = scaffold_dir / "server"
        server_pkg = server_dir / "package.json"
        server_entry_candidates = [
            "index.js",
            "index.mjs",
            "index.cjs",
            "server.js",
            "app.js",
            "main.js",
        ]
        if server_dir.is_dir() and server_pkg.is_file():
            for entry in server_entry_candidates:
                p = server_dir / entry
                if p.is_file():
                    probe.backend_dir = server_dir
                    probe.backend_kind = "node-express"
                    probe.backend_entry = f"server/{entry}"
                    probe.backend_cwd = "server"
                    probe.backend_port = self._guess_port_from_files(server_dir) or 3100
                    probe.install_cmd = [
                        "npm",
                        "install",
                        "--silent",
                        "--no-audit",
                        "--no-fund",
                        "--prefer-offline",
                    ]
                    probe.boot_cmd = ["node", entry]
                    return probe

        # Top-level Node server.
        top_pkg = scaffold_dir / "package.json"
        if top_pkg.is_file():
            try:
                pkg = json.loads(top_pkg.read_text(encoding="utf-8"))
            except Exception:
                pkg = {}
            deps = (pkg.get("dependencies") or {}) if isinstance(pkg, dict) else {}
            is_server = any(
                k in deps
                for k in ("express", "fastify", "koa", "hapi", "polka")
            )
            if is_server:
                for entry in server_entry_candidates:
                    p = scaffold_dir / entry
                    if p.is_file():
                        probe.backend_dir = scaffold_dir
                        probe.backend_kind = "node-express"
                        probe.backend_entry = entry
                        probe.backend_cwd = "."
                        probe.backend_port = (
                            self._guess_port_from_files(scaffold_dir) or 3100
                        )
                        probe.install_cmd = [
                            "npm",
                            "install",
                            "--silent",
                            "--no-audit",
                            "--no-fund",
                            "--prefer-offline",
                        ]
                        probe.boot_cmd = ["node", entry]
                        return probe

        # Python backends.
        for py_entry, kind, port in (
            ("src/main.py", "python-fastapi", 8000),
            ("main.py", "python-fastapi", 8000),
            ("app.py", "python-flask", 5000),
        ):
            p = scaffold_dir / py_entry
            if p.is_file():
                probe.backend_dir = scaffold_dir
                probe.backend_kind = kind
                probe.backend_entry = py_entry
                probe.backend_cwd = "."
                probe.backend_port = port
                if kind == "python-fastapi":
                    module = py_entry.replace("/", ".").rsplit(".py", 1)[0]
                    probe.boot_cmd = [
                        "python",
                        "-m",
                        "uvicorn",
                        f"{module}:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ]
                else:
                    probe.boot_cmd = ["python", py_entry]
                req = scaffold_dir / "requirements.txt"
                probe.install_cmd = (
                    ["pip", "install", "-q", "-r", str(req)] if req.is_file() else None
                )
                return probe

        probe.backend_kind = "unknown"
        return probe

    # ── route extraction ──────────────────────────────────────────

    def _extract_frontend_routes(self, frontend_dir: Path) -> List[str]:
        """Scan JS/JSX/TS/TSX for fetch/axios calls to /api/... paths."""
        routes: set[str] = set()
        if not frontend_dir or not frontend_dir.is_dir():
            return []

        fetch_re = re.compile(
            r"""
            fetch\s*
            \(\s*
            (['"])(/api/[^'"]+)\1
            """,
            re.VERBOSE,
        )
        fetch_template_re = re.compile(
            r"""
            fetch\s*
            \(\s*
            `([^`]+)`
            """,
            re.VERBOSE,
        )
        axios_re = re.compile(
            r"""
            axios\.(get|post|put|delete|patch)\s*
            \(\s*
            (['"])(/api/[^'"]+)\2
            """,
            re.VERBOSE,
        )
        axios_template_re = re.compile(
            r"""
            axios\.(get|post|put|delete|patch)\s*
            \(\s*
            `([^`]+)`
            """,
            re.VERBOSE,
        )

        for ext in ("*.js", "*.jsx", "*.ts", "*.tsx"):
            for p in frontend_dir.rglob(ext):
                if "node_modules" in p.parts:
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for m in fetch_re.finditer(text):
                    path = self._normalize_frontend_path(m.group(2))
                    if not path:
                        continue
                    method = self._infer_fetch_method(text, m.end())
                    routes.add(self._format_frontend_route(method, path))
                for m in fetch_template_re.finditer(text):
                    path = self._normalize_frontend_path(m.group(1))
                    if not path:
                        continue
                    method = self._infer_fetch_method(text, m.end())
                    routes.add(self._format_frontend_route(method, path))
                for m in axios_re.finditer(text):
                    path = self._normalize_frontend_path(m.group(3))
                    if not path:
                        continue
                    routes.add(self._format_frontend_route(m.group(1), path))
                for m in axios_template_re.finditer(text):
                    path = self._normalize_frontend_path(m.group(2))
                    if not path:
                        continue
                    routes.add(self._format_frontend_route(m.group(1), path))
                for helper_name in self._detect_frontend_api_helpers(text):
                    helper_literal_re = re.compile(
                        rf"""
                        \b{re.escape(helper_name)}\s*
                        \(\s*
                        (['"])(/api/[^'"]+)\1
                        """,
                        re.VERBOSE,
                    )
                    helper_template_re = re.compile(
                        rf"""
                        \b{re.escape(helper_name)}\s*
                        \(\s*
                        `([^`]+)`
                        """,
                        re.VERBOSE,
                    )
                    for m in helper_literal_re.finditer(text):
                        path = self._normalize_frontend_path(m.group(2))
                        if not path:
                            continue
                        method = self._infer_callsite_method(text, m.end())
                        routes.add(self._format_frontend_route(method, path))
                    for m in helper_template_re.finditer(text):
                        path = self._normalize_frontend_path(m.group(1))
                        if not path:
                            continue
                        method = self._infer_callsite_method(text, m.end())
                        routes.add(self._format_frontend_route(method, path))

        return sorted(routes)

    @staticmethod
    def _format_frontend_route(method: str, path: str) -> str:
        return f"{str(method or 'GET').upper()} {path}"

    @staticmethod
    def _parse_frontend_route(route: str) -> Tuple[str, str]:
        method, _, path = str(route or "").partition(" ")
        if not path:
            return "GET", method
        return method.upper(), path

    @staticmethod
    def _infer_fetch_method(source: str, start_index: int) -> str:
        window = source[start_index:start_index + 1000]
        m = re.search(r"""\bmethod\s*:\s*['"]([A-Za-z]+)['"]""", window)
        if not m:
            return "GET"
        return m.group(1).upper()

    @staticmethod
    def _infer_callsite_method(source: str, start_index: int) -> str:
        window = source[start_index:start_index + 240]
        m = re.search(
            r"""
            ^\s*,\s*
            \{[\s\S]{0,180}?\bmethod\s*:\s*['"]([A-Za-z]+)['"]
            """,
            window,
            re.VERBOSE,
        )
        if not m:
            return "GET"
        return m.group(1).upper()

    def _normalize_frontend_path(self, path: str) -> str:
        """Strip base URL variables and replace template segments with :*."""
        path = str(path or "").strip()
        if not path:
            return ""
        # Strip ${API_BASE}, ${BASE_URL}, etc.
        if path.startswith("${") and "/api/" in path:
            path = path[path.find("/api/") :]
        # Replace ${var} and ${var.prop} with :*
        path = re.sub(r"\$\{[^}]+\}", ":*", path)
        # Collapse multiple slashes
        path = re.sub(r"/+", "/", path)
        normalized = path.rstrip("/")
        if not normalized:
            return ""
        if normalized != "/api" and not normalized.startswith("/api/"):
            return ""
        return normalized

    @staticmethod
    def _detect_frontend_api_helpers(source: str) -> List[str]:
        helper_names: set[str] = set()
        function_re = re.compile(
            r"""
            (?:async\s+)?function\s+
            ([A-Za-z_$][A-Za-z0-9_$]*)\s*
            \(\s*([A-Za-z_$][A-Za-z0-9_$]*)[^)]*\)
            """,
            re.VERBOSE,
        )
        arrow_re = re.compile(
            r"""
            (?:const|let|var)\s+
            ([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*
            (?:async\s*)?
            \(\s*([A-Za-z_$][A-Za-z0-9_$]*)[^)]*\)\s*=>\s*
            """,
            re.VERBOSE,
        )

        def _collect(matches: re.Pattern[str]) -> None:
            for match in matches.finditer(source):
                name = match.group(1)
                path_param = match.group(2)
                window = source[match.end():match.end() + 2000]
                if not name or not path_param or not window:
                    continue
                template_use = re.search(
                    rf"""
                    (?:fetch|axios\.(?:get|post|put|delete|patch))\s*
                    \(\s*
                    `[^`]*\$\{{[^}}]*\b{re.escape(path_param)}\b[^}}]*\}}[^`]*`
                    """,
                    window,
                    re.VERBOSE,
                )
                if template_use:
                    helper_names.add(name)

        _collect(function_re)
        _collect(arrow_re)
        return sorted(helper_names)

    def _extract_backend_routes(self, backend_dir: Path) -> List[str]:
        """Scan backend JS for Express route declarations.

        Composes ``app.use(prefix, router)`` mounts with the
        ``router.method(subpath, ...)`` declarations inside the imported
        router module. Without this, a scaffold using the standard
        Express prefix-mount pattern shows the prefix and inner
        subpaths as independent routes, which makes every legitimate
        frontend call look unhandled.
        """
        routes: set[str] = set()
        if not backend_dir or not backend_dir.is_dir():
            return []

        # Match: app.get('/api/x', ...), router.post('/api/x', ...), etc.
        # ``app.use`` is intentionally NOT captured here — it's a mount,
        # not a handler, and we collect it separately below for prefix
        # composition.
        method_re = re.compile(
            r"""
            (?:app|router)\.
            (get|post|put|delete|patch|all)\s*
            \(\s*
            (['"`])([^'"`]+)\2
            """,
            re.VERBOSE,
        )
        # Match: app.use('/api/foo', fooRouter)
        use_re = re.compile(
            r"""
            app\.use\s*\(\s*
            (['"`])(/[^'"`]+)\1\s*,\s*
            ([A-Za-z_$][A-Za-z0-9_$]*)
            (?:\.router)?
            """,
            re.VERBOSE,
        )
        # Match: import fooRouter from './routes/foo.js'  |  './routes/foo'
        # Match: const fooRouter = require('./routes/foo')
        import_re = re.compile(
            r"""
            (?:import\s+([A-Za-z_$][A-Za-z0-9_$]*)\s+from\s+
                (['"`])(\.\.?/[^'"`]+)\2
              |
              (?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*
                require\(\s*(['"`])(\.\.?/[^'"`]+)\5\s*\))
            """,
            re.VERBOSE,
        )
        load_required_router_re = re.compile(
            r"""
            (?:const|let|var)\s+
            ([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*
            (?:await\s+)?loadRequiredRouter\(\s*
            (['"`])(\.\.?/[^'"`]+)\2
            """,
            re.VERBOSE,
        )

        # Per-file scan: collect (file_path, methods, mounts, imports).
        file_routes: List[tuple[Path, list[tuple[str, str]]]] = []
        file_mounts: List[tuple[Path, list[tuple[str, str]]]] = []
        file_imports: List[tuple[Path, dict[str, str]]] = []
        for ext in ("*.js", "*.mjs", "*.cjs", "*.ts"):
            for p in backend_dir.rglob(ext):
                if "node_modules" in p.parts:
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                methods = [
                    (m.group(1).upper(), m.group(3))
                    for m in method_re.finditer(text)
                ]
                mounts = [
                    (m.group(2), m.group(3))  # (prefix, identifier)
                    for m in use_re.finditer(text)
                ]
                imports: dict[str, str] = {}
                for m in import_re.finditer(text):
                    name = m.group(1) or m.group(4)
                    spec = m.group(3) or m.group(6)
                    if name and spec:
                        imports[name] = spec
                for m in load_required_router_re.finditer(text):
                    name = m.group(1)
                    spec = m.group(3)
                    if name and spec:
                        imports[name] = spec
                file_routes.append((p, methods))
                file_mounts.append((p, mounts))
                file_imports.append((p, imports))

        # Resolve each mount's identifier → the file that defines it,
        # then prefix that file's method routes with the mount's prefix.
        def _resolve_import(host: Path, spec: str) -> Optional[Path]:
            base = (host.parent / spec).resolve()
            for cand in (
                base,
                base.with_suffix(".js"),
                base.with_suffix(".mjs"),
                base.with_suffix(".cjs"),
                base.with_suffix(".ts"),
                base / "index.js",
            ):
                if cand.exists() and cand.is_file():
                    return cand
            return None

        # Build map: target_file → list of (method, subpath)
        routes_by_file: dict[Path, list[tuple[str, str]]] = {}
        for p, methods in file_routes:
            if methods:
                routes_by_file[p.resolve()] = methods

        # Emit composed routes from app.use prefixes.
        mounted_files: set[Path] = set()
        for host, mounts in file_mounts:
            imports = dict(file_imports[file_mounts.index((host, mounts))][1])
            for prefix, identifier in mounts:
                spec = imports.get(identifier)
                if not spec:
                    # Mount without a resolvable import — record the prefix
                    # alone so a frontend hitting exactly /prefix still matches.
                    routes.add(f"USE {prefix}")
                    continue
                target = _resolve_import(host, spec)
                if not target:
                    routes.add(f"USE {prefix}")
                    continue
                mounted_files.add(target)
                inner = routes_by_file.get(target, [])
                for method, subpath in inner:
                    composed = prefix.rstrip("/") + (
                        "" if subpath == "/" else (
                            subpath if subpath.startswith("/") else "/" + subpath
                        )
                    )
                    routes.add(f"{method} {composed}")

        # Also emit any routes from files that weren't mounted (e.g. the
        # main server file declaring app.get(...) directly).
        for p, methods in file_routes:
            if p.resolve() in mounted_files:
                continue
            for method, path in methods:
                routes.add(f"{method} {path}")

        return sorted(routes)

    # ── route verification ────────────────────────────────────────

    async def _verify_routes(
        self,
        frontend_routes: List[str],
        backend_routes: List[str],
        port: int,
    ) -> List[RouteIssue]:
        """Curl each frontend route and compare against backend declarations."""
        issues: List[RouteIssue] = []

        # Parse backend routes into matchers.
        backend_matchers = [self._parse_backend_route(r) for r in backend_routes]

        for frontend_route in frontend_routes:
            method, fe_path = self._parse_frontend_route(frontend_route)
            matched = self._match_frontend_to_backend(fe_path, method, backend_matchers)

            if not matched:
                issues.append(
                    RouteIssue(
                        frontend_path=fe_path,
                        method=method,
                        issue="missing",
                        detail="No backend route handles this path",
                    )
                )
                continue

            # Actually curl the route.
            status, is_json, detail = await self._curl_route(fe_path, port, method)
            if status == 0:
                # Couldn't connect — server may have died.
                issues.append(
                    RouteIssue(
                        frontend_path=fe_path,
                        method=method,
                        issue="wrong_status",
                        backend_match=matched,
                        http_status=0,
                        detail="Could not connect to server",
                    )
                )
            elif status >= 500:
                issues.append(
                    RouteIssue(
                        frontend_path=fe_path,
                        method=method,
                        issue="wrong_status",
                        backend_match=matched,
                        http_status=status,
                        detail=f"Server error HTTP {status}",
                    )
                )
            elif status == 404:
                # Route not found at runtime even though static analysis
                # thought it matched. This happens with dynamic router
                # mounting where the prefix matches but the handler doesn't.
                issues.append(
                    RouteIssue(
                        frontend_path=fe_path,
                        method=method,
                        issue="missing" if self._runtime_404_is_missing(matched) else "wrong_status",
                        backend_match=matched,
                        http_status=404,
                        detail="Backend returned 404 at runtime",
                    )
                )
            elif not (200 <= status < 400):
                issues.append(
                    RouteIssue(
                        frontend_path=fe_path,
                        method=method,
                        issue="wrong_status",
                        backend_match=matched,
                        http_status=status,
                        detail=f"Unexpected HTTP {status}",
                    )
                )
            elif not is_json:
                # Some routes legitimately return HTML (e.g., /api/health).
                # Only flag as warning if it looks like a data endpoint.
                if self._looks_like_data_endpoint(fe_path):
                    issues.append(
                        RouteIssue(
                            frontend_path=fe_path,
                            method=method,
                            issue="not_json",
                            backend_match=matched,
                            http_status=status,
                            detail="Response is not JSON",
                        )
                    )

        return issues

    def _parse_backend_route(self, route: str) -> Tuple[str, str, bool]:
        """Parse 'GET /api/x' → (method, path, is_prefix).

        `app.use` routes are treated as prefix matchers.
        """
        parts = route.split(" ", 1)
        method = parts[0] if len(parts) > 1 else "ALL"
        path = parts[1] if len(parts) > 1 else route
        is_prefix = method in ("USE", "ALL")
        return method, path, is_prefix

    def _match_frontend_to_backend(
        self,
        fe_path: str,
        method: str,
        backend_matchers: List[Tuple[str, str, bool]],
    ) -> Optional[str]:
        """Return the most specific backend route that handles the frontend path."""
        fe_segments = fe_path.strip("/").split("/")
        frontend_method = str(method or "GET").upper()
        candidates: list[tuple[int, str]] = []
        for backend_method, be_path, is_prefix in backend_matchers:
            if backend_method not in {"ALL", "USE", frontend_method}:
                continue
            be_segments = be_path.strip("/").split("/")
            if is_prefix:
                # Prefix match: frontend path must start with backend path.
                if len(fe_segments) >= len(be_segments):
                    if all(
                        self._segment_match(fe_segments[i], be_segments[i])
                        for i in range(len(be_segments))
                    ):
                        candidates.append(
                            (
                                self._route_match_specificity(be_segments, is_prefix=True),
                                f"{backend_method} {be_path}",
                            )
                        )
            else:
                # Exact-length match.
                if len(fe_segments) != len(be_segments):
                    continue
                if all(
                    self._segment_match(fe_segments[i], be_segments[i])
                    for i in range(len(be_segments))
                ):
                    candidates.append(
                        (
                            self._route_match_specificity(be_segments, is_prefix=False),
                            f"{backend_method} {be_path}",
                        )
                    )
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    @staticmethod
    def _route_match_specificity(segments: List[str], *, is_prefix: bool) -> int:
        literal_segments = sum(1 for segment in segments if not segment.startswith(":") and segment != "*")
        wildcard_segments = len(segments) - literal_segments
        return (0 if is_prefix else 1000) + literal_segments * 10 - wildcard_segments

    @staticmethod
    def _runtime_404_is_missing(matched_route: str) -> bool:
        method, _, path = matched_route.partition(" ")
        return method in {"USE", "ALL"} or not path

    def _segment_match(self, fe_seg: str, be_seg: str) -> bool:
        """Match a frontend path segment against a backend segment.

        Backend segments like ':id', ':slug', or ':*' (from our normalization)
        are wildcards.
        """
        if be_seg.startswith(":"):
            return True
        if be_seg == "*":
            return True
        return fe_seg == be_seg

    def _looks_like_data_endpoint(self, path: str) -> bool:
        """Heuristic: does this path look like it should return JSON?"""
        data_keywords = ("queue", "items", "data", "list", "search", "config", "status")
        return any(kw in path.lower() for kw in data_keywords)

    async def _curl_route(
        self, path: str, port: int, method: str = "GET"
    ) -> Tuple[int, bool, str]:
        """Curl a single route. Returns (http_status, is_json, detail)."""
        url = f"http://127.0.0.1:{port}{path}"
        try:
            req = Request(
                url,
                method=method,
                headers={
                    "User-Agent": "skyn3t-integration-verifier",
                    "Accept": "application/json, text/plain, */*",
                },
            )
            loop = asyncio.get_event_loop()
            resp = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: urlopen(req, timeout=self.verify_timeout)
                ),
                timeout=self.verify_timeout + 1,
            )
            status = resp.getcode()
            content_type = resp.headers.get("Content-Type", "")
            is_json = "application/json" in content_type
            return status, is_json, ""
        except HTTPError as e:
            # HTTPError still has a response code.
            return e.code, False, str(e)
        except Exception as e:
            return 0, False, str(e)

    # ── subprocess helpers (mirrors boot_verifier) ────────────────

    async def _run_with_timeout(
        self,
        cmd: List[str],
        cwd: Path,
        timeout: int,
        env: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, OSError) as e:
            return False, "", f"failed to spawn {cmd[0]}: {e}"
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return False, "", f"{cmd[0]} exceeded {timeout}s timeout"
        out = stdout.decode(errors="replace") if stdout else ""
        err = stderr.decode(errors="replace") if stderr else ""
        return proc.returncode == 0, out, err

    async def _boot_and_wait(
        self,
        cmd: List[str],
        cwd: Path,
        env: Dict[str, str],
        port: int,
    ) -> Tuple[bool, Optional[asyncio.subprocess.Process], str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, OSError) as e:
            return False, None, "", f"failed to spawn {cmd[0]}: {e}"

        deadline = time.monotonic() + self.boot_timeout
        out_buf: List[str] = []
        err_buf: List[str] = []

        async def _drain() -> None:
            async def pull(stream, buf):
                while True:
                    line = await stream.readline()
                    if not line:
                        return
                    buf.append(line.decode(errors="replace"))

            await asyncio.gather(
                pull(proc.stdout, out_buf),
                pull(proc.stderr, err_buf),
                return_exceptions=True,
            )

        drainer = asyncio.create_task(_drain())
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                drainer.cancel()
                try:
                    await drainer
                except Exception:
                    pass
                return (
                    False,
                    None,
                    "".join(out_buf),
                    "".join(err_buf)
                    + f"\nserver exited with code {proc.returncode}",
                )
            if self._port_is_listening(port):
                return True, proc, "".join(out_buf), "".join(err_buf)
            await asyncio.sleep(0.3)

        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        drainer.cancel()
        try:
            await drainer
        except Exception:
            pass
        return (
            False,
            None,
            "".join(out_buf),
            "".join(err_buf) + f"\nserver did not bind port {port} within {self.boot_timeout}s",
        )

    async def _kill_proc(self, proc: Optional[asyncio.subprocess.Process]) -> None:
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return
        except asyncio.TimeoutError:
            pass
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass

    def _port_is_listening(self, port: int) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            return False
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _free_port(self, preferred: int) -> int:
        if not self._port_is_listening(preferred):
            return preferred
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])
        finally:
            s.close()

    def _build_boot_env(
        self, scaffold_dir: Path, probe: ProjectProbe, port: int
    ) -> Dict[str, str]:
        env = os.environ.copy()
        env["PORT"] = str(port)
        env["NODE_ENV"] = env.get("NODE_ENV", "development")
        return env

    def _ensure_runnable_env(self, scaffold_dir: Path, probe: ProjectProbe) -> None:
        run_cwd = scaffold_dir / probe.backend_cwd
        target_env = run_cwd / ".env"
        if target_env.is_file():
            return
        candidates = [run_cwd / ".env.example", scaffold_dir / ".env.example"]
        template: Optional[Path] = None
        for c in candidates:
            if c.is_file():
                template = c
                break
        if template is None:
            try:
                target_env.write_text("NODE_ENV=development\n", encoding="utf-8")
            except Exception:
                pass
            return
        try:
            raw = template.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        cleaned_lines: List[str] = []
        for line in raw.splitlines():
            if not line or line.startswith("#"):
                cleaned_lines.append(line)
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if "replace_with" in value.lower() or value == "" or value.startswith("<"):
                if key.endswith("_URL"):
                    value = "http://127.0.0.1:9"
                elif key.endswith(
                    ("_KEY", "_TOKEN", "_SECRET", "_PASS", "_PASSWORD")
                ):
                    value = "verifier_placeholder"
                elif key.endswith("_USER"):
                    value = "verifier"
                elif key.endswith("_PORT"):
                    value = "9"
                elif key.endswith("_HOST"):
                    value = "127.0.0.1"
                else:
                    value = "placeholder"
            cleaned_lines.append(f"{key}={value}")
        try:
            target_env.write_text("\n".join(cleaned_lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _guess_port_from_files(self, root: Path) -> Optional[int]:
        candidates: List[Path] = []
        for name in ("index.js", "server.js", "app.js", "index.mjs"):
            p = root / name
            if p.is_file():
                candidates.append(p)
        candidates.extend([root / ".env.example", root.parent / ".env.example"])
        port_re = re.compile(
            r"(?:PORT\s*[:=]\s*['\"]?(\d{4,5})|listen\s*\(\s*(\d{4,5}))",
            re.IGNORECASE,
        )
        for p in candidates:
            if not p.is_file():
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for m in port_re.finditer(txt):
                port_str = m.group(1) or m.group(2)
                if not port_str:
                    continue
                try:
                    port = int(port_str)
                    if 1024 < port < 65536:
                        return port
                except Exception:
                    continue
        return None

    # ── diagnostics ───────────────────────────────────────────────

    @staticmethod
    def _relaxed_install_cmd(cmd: List[str]) -> Optional[List[str]]:
        if not cmd or cmd[0] != "npm" or "install" not in cmd:
            return None
        trimmed = [part for part in cmd if part not in {"--silent", "--prefer-offline"}]
        return trimmed if trimmed != cmd else None

    def _diagnose_install_failure(self, install_log: str) -> str:
        s = install_log or ""
        sl = s.lower()
        if "ENOENT" in s:
            return (
                "npm install failed: a referenced file or directory is missing. "
                "Likely a workspace/path mismatch in package.json."
            )
        if "ETARGET" in s or "No matching version" in s:
            return (
                "npm install failed: a dependency version pin doesn't exist "
                "on the registry. Loosen the version range."
            )
        if "exceeded" in sl and "timeout" in sl:
            return (
                "npm install timed out in verifier. Dependencies may still be valid; "
                "retry without --silent/--prefer-offline or increase install timeout."
            )
        if not s.strip():
            return (
                "npm install failed but emitted no diagnostics. Retry with verbose "
                "install flags to surface the underlying npm error."
            )
        return (
            "npm install failed. Common cause: syntax error in package.json, "
            "or a postinstall script that itself fails."
        )

    def _diagnose_boot_failure(
        self, stderr: str, scaffold_dir: Path, probe: ProjectProbe
    ) -> str:
        s = stderr or ""
        if "require is not defined" in s or "Cannot use import statement" in s:
            return (
                "CJS/ESM module mismatch. Pick ONE module system for the whole server/ tree."
            )
        if "Cannot find module" in s or "MODULE_NOT_FOUND" in s:
            import re as _re

            m = _re.search(r"Cannot find module ['\"]([^'\"]+)['\"]", s)
            mod = m.group(1) if m else "<unknown>"
            if mod.startswith("."):
                return (
                    f"Server imports a local file ({mod}) that doesn't exist. "
                    "Likely a path typo."
                )
            return f"Missing npm dependency: {mod}. Add it to package.json."
        if "EADDRINUSE" in s or "address already in use" in s:
            return (
                "Port already in use. Fix: use `const port = Number(process.env.PORT) || 3100`."
            )
        if "Router.use() requires a middleware function" in s:
            return (
                "Express adapter export shape mismatch. "
                "Make sure adapters export a Router function."
            )
        named_export_hint = _named_export_mismatch_hint(s, scaffold_dir)
        if named_export_hint:
            return named_export_hint
        if "SyntaxError" in s:
            return (
                "Runtime syntax error (slipped past `node --check`). "
                "Common causes: unclosed template literal, mismatched quote."
            )
        meaningful = [
            ln
            for ln in s.splitlines()
            if ln.strip() and not ln.startswith(" ") and "node:internal" not in ln
        ]
        tail = " ".join(meaningful[-3:]) if meaningful else "(no diagnostic captured)"
        return f"Server failed to start. Last error: {tail}"

    # ── output helpers ────────────────────────────────────────────

    def _skip_output(self, scaffold_dir: Path, summary: str) -> TaskResult:
        return TaskResult(
            task_id="integration_verifier",
            success=True,
            output={
                "verdict": "skipped",
                "kind": "unknown",
                "command": None,
                "port": 0,
                "summary": summary,
                "frontend_routes": [],
                "backend_routes": [],
                "issues": [],
                "scaffold_dir": str(scaffold_dir),
                "failure_hint": None,
            },
        )

    def _fail_output(
        self,
        probe: ProjectProbe,
        scaffold_dir: Path,
        *,
        summary: str,
        failure_hint: str,
        frontend_routes: List[str],
        backend_routes: List[str],
        issues: List[RouteIssue],
    ) -> TaskResult:
        return TaskResult(
            task_id="integration_verifier",
            success=True,
            output={
                "verdict": "no",
                "kind": probe.backend_kind,
                "command": " ".join(probe.boot_cmd) if probe.boot_cmd else None,
                "port": probe.backend_port,
                "summary": summary,
                "frontend_routes": frontend_routes,
                "backend_routes": backend_routes,
                "issues": [self._issue_to_dict(i) for i in issues],
                "scaffold_dir": str(scaffold_dir),
                "failure_hint": failure_hint,
            },
        )

    def _issue_to_dict(self, issue: RouteIssue) -> Dict[str, Any]:
        return {
            "frontend_path": issue.frontend_path,
            "method": issue.method,
            "issue": issue.issue,
            "backend_match": issue.backend_match,
            "http_status": issue.http_status,
            "detail": issue.detail,
        }
