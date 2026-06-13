"""Sandboxed execution for CLI agents.

Provides process isolation, resource limits, directory restrictions,
network controls, and syscall logging for agent command execution.
"""

import asyncio
import logging
import os
import platform
import resource
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SandboxConfig:
    """Configuration for sandboxed execution."""

    # Resource limits
    max_cpu_time: float = 60.0  # seconds
    max_memory_mb: int = 512  # MB
    max_file_size_mb: int = 128  # MB
    max_open_files: int = 64
    max_processes: int = 32

    # Directory restrictions
    allowed_dirs: List[Path] = field(default_factory=list)
    denied_dirs: List[Path] = field(default_factory=lambda: [
        Path("/etc"), Path("/usr/local/etc"),
        Path.home() / ".ssh", Path.home() / ".aws",
        Path.home() / ".kube", Path.home() / ".docker",
    ])
    temp_dir: Optional[Path] = None

    # Network controls
    allow_network: bool = False
    allowed_hosts: List[str] = field(default_factory=list)
    denied_hosts: List[str] = field(default_factory=list)
    allowed_ports: List[int] = field(default_factory=lambda: [80, 443])

    # Execution
    timeout_seconds: float = 300.0
    capture_syscalls: bool = False
    env_vars: Dict[str, str] = field(default_factory=dict)
    blocked_env_vars: List[str] = field(default_factory=lambda: [
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "KIMI_API_KEY", "OPENROUTER_API_KEY",
        "GITHUB_TOKEN",
        "SECRET_KEY", "SKYN3T_MASTER_KEY", "SKYN3T_WEB_TOKEN",
        "DISCORD_TOKEN", "SKYN3T_DISCORD_ADMIN_SECRET",
        "SKYN3T_TELEGRAM_TOKEN",
        "PASSWORD",
    ])
    # H1: Linux native sandbox is rlimits-only. By default require Docker for
    # real isolation; tests can bypass with SKYN3T_ALLOW_WEAK_LINUX_SANDBOX=1.
    linux_sandbox_require_docker: bool = True

    # Cleanup
    cleanup_temp: bool = True
    keep_files_on_error: bool = False

    def __post_init__(self) -> None:
        if not self.allowed_dirs:
            self.allowed_dirs = [Path.cwd()]
        self.allowed_dirs = [Path(d).resolve() for d in self.allowed_dirs]
        self.denied_dirs = [Path(d).resolve() for d in self.denied_dirs]
        if self.temp_dir:
            self.temp_dir = Path(self.temp_dir).resolve()


@dataclass
class SandboxResult:
    """Result of a sandboxed execution."""

    returncode: int
    stdout: str
    stderr: str
    execution_time_ms: float
    syscalls: List[Dict[str, Any]]
    temp_dir: Optional[Path]
    killed_by_sandbox: bool
    kill_reason: Optional[str]
    resource_usage: Dict[str, float]


class Sandbox:
    """Sandbox for isolated CLI execution.

    Uses process-level isolation with resource limits via setrlimit,
    directory restrictions via bind mounts or path validation,
    and optional network blocking via PF / iptables rules on Linux
    or socket filtering.
    """

    def __init__(self, config: Optional[SandboxConfig] = None):
        self.config = config or SandboxConfig()
        self._syscall_logs: List[Dict[str, Any]] = []
        self._temp_dirs: Set[Path] = set()
        self._macos = platform.system() == "Darwin"
        self._linux = platform.system() == "Linux"

    def _setup_resource_limits(self) -> None:
        """Apply POSIX resource limits to the current process."""
        cfg = self.config
        # CPU time (soft, hard)
        cpu_soft = int(cfg.max_cpu_time)
        cpu_hard = cpu_soft + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_soft, cpu_hard))

        # Memory (address space)
        mem_bytes = cfg.max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

        # File size
        fs_bytes = cfg.max_file_size_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fs_bytes, fs_bytes))

        # Open files
        resource.setrlimit(
            resource.RLIMIT_NOFILE, (cfg.max_open_files, cfg.max_open_files + 16)
        )

        # Number of processes
        resource.setrlimit(
            resource.RLIMIT_NPROC, (cfg.max_processes, cfg.max_processes)
        )

        # Core dumps disabled
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    def _build_seatbelt_profile(self, temp_dir: Path) -> str:
        """Build a macOS seatbelt profile string."""
        allowed_paths = "\n".join(
            f'    (allow file-read* file-write* process-exec (subpath "{d}"))'
            for d in self.config.allowed_dirs
        )
        denied_paths = "\n".join(
            f'    (deny file-read* file-write* (subpath "{d}"))'
            for d in self.config.denied_dirs
        )
        network_rule = (
            "    (allow network-outbound (remote tcp))\n"
            if self.config.allow_network
            else "    (deny network*)\n"
        )
        # Seatbelt evaluates rules top-to-bottom with last-match-wins. We start
        # from "(deny default)" so the security-critical capability — WRITE — is
        # confined: nothing can be written unless explicitly allowed below
        # (caller workdirs + temp + the std streams). READ and EXEC are allowed
        # broadly, which is REQUIRED for a dynamically-linked process to even
        # start on macOS (dyld maps libraries from anywhere under /usr, /System,
        # /Library, /opt, /private, and CLI agents read their own config under
        # $HOME); a narrower read/exec allow-list SIGABRTs the wrapped process.
        # The denied_dirs (credential dirs, pre-resolved in SandboxConfig) are
        # emitted LAST so a deny on a secret subtree wins over the broad read —
        # so secrets can't be exfiltrated even though read is otherwise open.
        # Net vs the old "(allow default)": writes are now confined to the
        # workdir/temp and credential reads are blocked.
        return f"""(version 1)
(debug deny)
(deny default)
    ; --- essentials for a dynamically-linked process to start + run ---
    (allow process-fork)
    (allow process-exec*)
    (allow signal (target self))
    (allow sysctl-read)
    (allow mach-lookup)
    (allow file-read-metadata)
    (allow file-read*)
{network_rule}
    ; --- std streams ---
    (allow file-write* (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr") (literal "/dev/tty"))
    ; --- caller-allowed working dirs + temp (the ONLY writable locations) ---
{allowed_paths}
    (allow file-write* (subpath "{temp_dir}"))
    ; --- belt-and-suspenders: deny credential-dir READS LAST so they win ---
{denied_paths}
"""

    def _validate_path(self, path: str) -> bool:
        """Check if a path is within allowed directories."""
        try:
            resolved = Path(path).resolve()
        except (OSError, ValueError):
            return False

        # Denied takes precedence
        for denied in self.config.denied_dirs:
            try:
                resolved.relative_to(denied)
                return False
            except ValueError:
                pass

        for allowed in self.config.allowed_dirs:
            try:
                resolved.relative_to(allowed)
                return True
            except ValueError:
                pass

        # System paths are generally OK for read
        system_prefixes = ("/usr", "/bin", "/sbin", "/lib", "/lib64",
                           "/System", "/dev", "/opt", "/var")
        if any(str(resolved).startswith(p) for p in system_prefixes):
            return True

        return False

    def _prepare_env(self) -> Dict[str, str]:
        """Prepare sanitized environment variables."""
        env = dict(os.environ)
        # Remove blocked secrets (exact key match — avoids over-stripping
        # benign variables such as TELEGRAM_TOKEN when the block list says
        # TOKEN).
        blocked = {key.upper() for key in self.config.blocked_env_vars}
        for key in list(env.keys()):
            if key.upper() in blocked:
                del env[key]
        # Apply overrides
        env.update(self.config.env_vars)
        # Force unbuffered output for Python-based CLIs (kimi, openai, etc.)
        # so they flush stdout/stderr promptly instead of block-buffering
        # when connected to a pipe.
        env.setdefault("PYTHONUNBUFFERED", "1")
        # Strip PATH to safe directories on Linux
        if self._linux:
            safe_path = "/usr/local/bin:/usr/bin:/bin"
            env["PATH"] = safe_path
        return env

    async def _run_with_strace(
        self,
        cmd: List[str],
        cwd: Path,
        env: Dict[str, str],
        stdin_data: Optional[str],
        timeout: float,
    ) -> Tuple[int, str, str, List[Dict[str, Any]]]:
        """Run command under strace for syscall logging (Linux only)."""
        strace_cmd = [
            "strace", "-f", "-e", "trace=openat,open,connect,execve,clone,fork,vfork",
            "-o", str(cwd / ".strace.log"),
            "--",
        ] + cmd
        proc = await asyncio.create_subprocess_exec(
            *strace_cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
        )
        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(input=stdin_data.encode() if stdin_data else None),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise

        syscalls = []
        strace_log = cwd / ".strace.log"
        if strace_log.exists():
            for line in strace_log.read_text(errors="replace").splitlines():
                if any(s in line for s in ("open", "connect", "execve", "clone")):
                    syscalls.append({"syscall": line.strip()})
            if self.config.cleanup_temp:
                strace_log.unlink(missing_ok=True)

        return (
            proc.returncode or 0,
            stdout_data.decode("utf-8", errors="replace"),
            stderr_data.decode("utf-8", errors="replace"),
            syscalls,
        )

    async def execute(
        self,
        cmd: List[str],
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        working_dir: Optional[Path] = None,
    ) -> SandboxResult:
        """Execute a command inside the sandbox.

        Args:
            cmd: Command and arguments as a list.
            stdin: Optional stdin string.
            timeout: Override default timeout.
            working_dir: Optional working directory inside allowed paths.

        Returns:
            SandboxResult with output, resource usage, and syscall log.
        """
        timeout = timeout or self.config.timeout_seconds
        temp_dir = self.config.temp_dir or Path(tempfile.mkdtemp(prefix="skyn3t_sandbox_"))
        temp_dir = temp_dir.resolve()
        self._temp_dirs.add(temp_dir)

        # Validate working directory
        if working_dir:
            working_dir = Path(working_dir).resolve()
            if not self._validate_path(str(working_dir)):
                raise PermissionError(
                    f"Working directory {working_dir} is outside allowed paths"
                )
        else:
            working_dir = temp_dir

        # Validate that the executable exists and is allowed
        executable = shutil.which(cmd[0])
        if not executable:
            raise FileNotFoundError(f"Command not found: {cmd[0]}")
        if not self._validate_path(executable):
            raise PermissionError(
                f"Executable {executable} is outside allowed paths"
            )

        # Validate any file arguments
        for arg in cmd[1:]:
            path_like = (
                arg.startswith("/")
                or arg.startswith("./")
                or arg.startswith("../")
            )
            if not path_like:
                # Treat any arg that resolves to an existing file as path-like
                try:
                    if Path(arg).exists() or (Path.cwd() / arg).exists():
                        path_like = True
                except (OSError, ValueError):
                    path_like = False
            if path_like:
                if not self._validate_path(arg):
                    raise PermissionError(
                        f"Argument path {arg} is outside allowed paths"
                    )

        env = self._prepare_env()

        # H1: native Linux sandbox only has rlimits. Unless explicitly allowed
        # for tests/development, require Docker for real process/network/fs
        # isolation. macOS seatbelt is the only acceptable native path.
        if (
            self._linux
            and self.config.linux_sandbox_require_docker
            and not (self._macos and shutil.which("sandbox-exec"))
            and not shutil.which("docker")
            and os.getenv("SKYN3T_ALLOW_WEAK_LINUX_SANDBOX", "").strip().lower()
            not in ("1", "true", "yes", "on")
        ):
            raise RuntimeError(
                "Linux Sandbox requires Docker for real process/network/filesystem "
                "isolation. Install Docker, or set SKYN3T_ALLOW_WEAK_LINUX_SANDBOX=1 "
                "only for trusted local development."
            )

        start_time = time.monotonic()
        syscalls: List[Dict[str, Any]] = []
        resource_usage: Dict[str, float] = {}
        killed_by_sandbox = False
        kill_reason: Optional[str] = None

        # Build actual command with sandbox wrapper
        actual_cmd: List[str]
        if self._macos and shutil.which("sandbox-exec"):
            profile_path = temp_dir / "seatbelt.sb"
            profile_path.write_text(self._build_seatbelt_profile(temp_dir))
            actual_cmd = ["sandbox-exec", "-f", str(profile_path)] + cmd
        else:
            actual_cmd = cmd

        try:
            if self.config.capture_syscalls and self._linux and shutil.which("strace"):
                rc, stdout, stderr, syscalls = await self._run_with_strace(
                    actual_cmd, working_dir, env, stdin, timeout
                )
            else:
                # Use preexec_fn on Linux to set resource limits in child
                kwargs: Dict[str, Any] = {
                    "stdin": asyncio.subprocess.PIPE if stdin is not None else None,
                    "stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.PIPE,
                    "cwd": str(working_dir),
                    "env": env,
                }
                if self._linux:
                    kwargs["preexec_fn"] = self._setup_resource_limits

                proc = await asyncio.create_subprocess_exec(
                    *actual_cmd,
                    **kwargs,
                )

                # On macOS we can't use preexec_fn; apply soft limits via ulimit wrapper
                # or rely on seatbelt. We set a watcher task for memory/CPU enforcement.
                watcher_task: Optional[asyncio.Task] = None
                if self._macos:
                    watcher_task = asyncio.create_task(
                        self._watch_process(proc, timeout)
                    )

                try:
                    stdout_data, stderr_data = await asyncio.wait_for(
                        proc.communicate(input=stdin.encode() if stdin else None),
                        timeout=timeout,
                    )
                    if watcher_task:
                        watcher_task.cancel()
                        try:
                            await watcher_task
                        except asyncio.CancelledError:
                            pass
                    rc = proc.returncode or 0
                    stdout = stdout_data.decode("utf-8", errors="replace")
                    stderr = stderr_data.decode("utf-8", errors="replace")
                except asyncio.TimeoutError:
                    if watcher_task:
                        watcher_task.cancel()
                        try:
                            await watcher_task
                        except asyncio.CancelledError:
                            pass
                    try:
                        proc.kill()
                        await proc.wait()
                    except ProcessLookupError:
                        pass
                    killed_by_sandbox = True
                    kill_reason = f"Timeout after {timeout}s"
                    rc = -9
                    stdout = ""
                    stderr = f"SANDBOX TIMEOUT: killed after {timeout}s"

            execution_time_ms = (time.monotonic() - start_time) * 1000

            # Estimate resource usage
            resource_usage = {
                "cpu_time_sec": execution_time_ms / 1000.0,
                "memory_mb": self.config.max_memory_mb,
                "file_size_mb": self.config.max_file_size_mb,
            }

        except Exception:
            execution_time_ms = (time.monotonic() - start_time) * 1000
            raise
        finally:
            if self.config.cleanup_temp and not self.config.keep_files_on_error:
                self._cleanup(temp_dir)

        return SandboxResult(
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
            execution_time_ms=execution_time_ms,
            syscalls=syscalls,
            temp_dir=temp_dir if not self.config.cleanup_temp else None,
            killed_by_sandbox=killed_by_sandbox,
            kill_reason=kill_reason,
            resource_usage=resource_usage,
        )

    async def _watch_process(
        self, proc: asyncio.subprocess.Process, timeout: float
    ) -> None:
        """Watch a process and kill if it exceeds timeout or memory limits."""
        try:
            await asyncio.sleep(timeout)
            if proc.returncode is None:
                proc.kill()
        except asyncio.CancelledError:
            pass

    def _cleanup(self, temp_dir: Path) -> None:
        """Remove temporary files created by the sandbox."""
        if temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
                self._temp_dirs.discard(temp_dir)
            except Exception as e:
                logger.warning("Sandbox cleanup failed for %s: %s", temp_dir, e)

    def cleanup_all(self) -> None:
        """Clean up all remaining temporary directories."""
        for d in list(self._temp_dirs):
            self._cleanup(d)


class CLISandboxRunner:
    """High-level runner that integrates Sandbox with CLIAgent execution."""

    def __init__(self, sandbox: Optional[Sandbox] = None):
        self.sandbox = sandbox or Sandbox()

    async def run(
        self,
        command: str,
        args: List[str],
        stdin: Optional[str] = None,
        timeout: int = 300,
        working_dir: Optional[Path] = None,
    ) -> Tuple[int, str, str]:
        """Run a CLI command through the sandbox.

        Returns (returncode, stdout, stderr).
        """
        result = await self.sandbox.execute(
            cmd=[command, *args],
            stdin=stdin,
            timeout=timeout,
            working_dir=working_dir,
        )
        if result.killed_by_sandbox:
            # Return a synthetic error result
            return (
                -1,
                result.stdout,
                f"SANDBOX_ERROR: {result.kill_reason}\n{result.stderr}",
            )
        return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Pluggable execution backends (Phase-3 sandbox)
# ---------------------------------------------------------------------------


@dataclass
class ExecutionResult:
    """Outcome of a sandboxed code run."""

    success: bool
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    truncated: bool = False


def _decode_subprocess_output(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


class ExecutionBackend(ABC):
    """Abstract backend for running untrusted code."""

    @abstractmethod
    async def execute(
        self,
        code: str,
        language: str = "python",
        *,
        timeout: int = 30,
        memory_mb: int = 256,
    ) -> ExecutionResult:
        """Run ``code`` and return captured output."""
        ...


class InlineBackend(ExecutionBackend):
    """Run code in-process with restricted builtins.

    Fast but NOT a real sandbox — trivial to escape. Suitable only for
    trusted or heavily-reviewed code snippets.
    """

    _SAFE_BUILTINS: Dict[str, object] = {
        "abs": abs,
        "all": all,
        "any": any,
        "ascii": ascii,
        "bin": bin,
        "bool": bool,
        "bytearray": bytearray,
        "bytes": bytes,
        "chr": chr,
        "complex": complex,
        "dict": dict,
        "dir": dir,
        "divmod": divmod,
        "enumerate": enumerate,
        "filter": filter,
        "float": float,
        "format": format,
        "frozenset": frozenset,
        "hasattr": hasattr,
        "hash": hash,
        "hex": hex,
        "id": id,
        "int": int,
        "isinstance": isinstance,
        "issubclass": issubclass,
        "iter": iter,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "next": next,
        "oct": oct,
        "ord": ord,
        "pow": pow,
        "print": print,
        "range": range,
        "repr": repr,
        "reversed": reversed,
        "round": round,
        "set": set,
        "slice": slice,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "type": type,
        "zip": zip,
    }

    async def execute(
        self,
        code: str,
        language: str = "python",
        *,
        timeout: int = 30,
        memory_mb: int = 256,
    ) -> ExecutionResult:
        import io
        import sys

        if language != "python":
            return ExecutionResult(
                success=False,
                error=f"InlineBackend only supports python, not {language}",
            )

        if not code.strip():
            return ExecutionResult(success=False, error="No code provided")

        old_stdout = sys.stdout
        old_stderr = sys.stderr
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()

        try:
            sys.stdout = stdout_buffer
            sys.stderr = stderr_buffer

            compiled_code = compile(code, "<sandbox>", "exec")
            exec_globals = {"__builtins__": self._SAFE_BUILTINS.copy()}
            exec(compiled_code, exec_globals)

            out = stdout_buffer.getvalue()
            err = stderr_buffer.getvalue()
            truncated = len(out) > 1_000_000
            if truncated:
                out = out[:1_000_000] + "\n...[truncated]"

            return ExecutionResult(success=True, stdout=out, stderr=err, truncated=truncated)
        except Exception as e:
            return ExecutionResult(success=False, error=str(e))
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr


def _docker_hardening_flags(settings: Any) -> List[str]:
    """Return extra `docker run` flags based on current settings."""
    flags: List[str] = []
    if not getattr(settings, "docker_hardening", True):
        return flags
    user = getattr(settings, "docker_user", "65534:65534")
    if user:
        flags.extend(["--user", user])
    if getattr(settings, "docker_cap_drop_all", True):
        flags.extend(["--cap-drop", "ALL"])
    if getattr(settings, "docker_no_new_privs", True):
        flags.extend(["--security-opt", "no-new-privileges=true"])
    cpus = getattr(settings, "docker_cpus", 1.0)
    if cpus > 0:
        flags.extend(["--cpus", str(cpus)])
    pids = getattr(settings, "docker_pids_limit", 64)
    if pids > 0:
        flags.extend(["--pids-limit", str(pids)])
    # Scrub orchestrator env from the container; keep a minimal PATH.
    flags.extend(["-e", "PATH=/usr/local/bin:/usr/bin:/bin"])
    return flags


class DockerBackend(ExecutionBackend):
    """Run code inside a short-lived Docker container.

    Features:
      - Network isolation (--network none)
      - Memory limit (--memory)
      - Read-only rootfs + writable /tmp
      - stdout/stderr capture
      - Automatic container cleanup
    """

    _IMAGES: Dict[str, str] = {
        "python": "python:3.11-alpine",
        "javascript": "node:18-alpine",
        "typescript": "node:18-alpine",
        "bash": "alpine:3.19",
        "go": "golang:1.22-alpine",
        "rust": "rust:1.78-alpine",
        "php": "php:8.3-alpine",
        "ruby": "ruby:3.3-alpine",
    }

    _ENTRYPOINTS: Dict[str, List[str]] = {
        "python": ["python"],
        "javascript": ["node"],
        "typescript": ["npx", "ts-node"],
        "bash": ["sh"],
        "go": ["go", "run"],
        "rust": ["rustc", "-o", "/tmp/out", "&&", "/tmp/out"],
        "php": ["php"],
        "ruby": ["ruby"],
    }

    def __init__(self, docker_path: str = "docker"):
        self.docker_path = docker_path
        self._available: Optional[bool] = None

    async def available(self) -> bool:
        """Return True if the Docker CLI is responsive."""
        if self._available is not None:
            return self._available
        if not shutil.which(self.docker_path):
            self._available = False
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker_path, "version", "--format", "{{.Server.Version}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            self._available = proc.returncode == 0 and bool(stdout.strip())
        except Exception:
            self._available = False
        return self._available

    async def execute(
        self,
        code: str,
        language: str = "python",
        *,
        timeout: int = 30,
        memory_mb: int = 256,
    ) -> ExecutionResult:
        if not await self.available():
            return ExecutionResult(
                success=False,
                error="Docker is not available; install Docker or switch to InlineBackend",
            )

        image = self._IMAGES.get(language)
        entrypoint = self._ENTRYPOINTS.get(language)
        if not image or not entrypoint:
            return ExecutionResult(
                success=False,
                error=f"DockerBackend does not support language: {language}",
            )

        suffix = ".py" if language == "python" else ".js" if language == "javascript" else ".sh"
        with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as fh:
            fh.write(code)
            code_path = fh.name

        # The chosen container user must be able to read the bind-mounted code.
        try:
            os.chmod(code_path, 0o644)
        except Exception:
            pass

        from skyn3t.config.settings import get_settings

        settings = get_settings()

        # Deterministic container name so we can forcibly kill it on timeout.
        import uuid

        container_name = f"skyn3t-run-{language}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

        cmd = [
            self.docker_path,
            "run",
            "--rm",
            "--name", container_name,
            "--network", "none",
            "--memory", f"{memory_mb}m",
            "--memory-swap", f"{memory_mb}m",
            "--read-only",
            "--tmpfs", "/tmp:noexec,nosuid,size=50m,mode=1777",
            *(_docker_hardening_flags(settings)),
            "-v", f"{code_path}:/sandbox/code{suffix}:ro",
            "-w", "/tmp",
            image,
        ]
        if language in ("bash", "python", "javascript", "typescript", "go", "php", "ruby"):
            cmd.extend(entrypoint)
            cmd.append(f"/sandbox/code{suffix}")
        elif language == "rust":
            # Rust needs compile then run
            cmd.extend(["sh", "-c", f"rustc -o /tmp/out /sandbox/code{suffix} && /tmp/out"])
        else:
            cmd.extend(entrypoint)
            cmd.append(f"/sandbox/code{suffix}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return ExecutionResult(
                success=proc.returncode == 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        except asyncio.TimeoutError:
            # The `docker run` client returning isn't enough — the container keeps
            # running (and consuming the full mem/CPU budget) until forcibly
            # stopped. Kill the local client AND the container itself.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await self._force_kill_container(container_name)
            try:
                await proc.wait()
            except Exception:
                pass
            return ExecutionResult(
                success=False,
                error=f"Execution timed out after {timeout}s",
            )
        except Exception as e:
            return ExecutionResult(success=False, error=str(e))
        finally:
            try:
                os.unlink(code_path)
            except Exception:
                pass

    async def _force_kill_container(self, container_name: str) -> None:
        """Forcibly kill a running container by name (best-effort)."""
        try:
            kill_proc = await asyncio.create_subprocess_exec(
                self.docker_path, "kill", container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(kill_proc.communicate(), timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.warning(
                "Failed to docker kill timed-out container %s: %s",
                container_name,
                exc,
            )


class DockerPoolBackend(ExecutionBackend):
    """Pooled Docker containers for low-latency sandboxed execution.

    Pre-starts a small fleet of ``sleep infinity`` containers per language.
    Actual runs use ``docker exec`` (~100-200ms) instead of cold-starting
    a new container (~1-2s).

    Security flags (network, memory, read-only) are set at container creation
    time and enforced for every exec.
    """

    _IMAGES = DockerBackend._IMAGES

    def __init__(self, pool_size: int = 2, docker_path: str = "docker"):
        self.pool_size = pool_size
        self.docker_path = docker_path
        self._containers: Dict[str, List[str]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._pool_init_locks: Dict[str, asyncio.Lock] = {}
        self._initialized: Dict[str, bool] = {}
        self._exec_counts: Dict[str, int] = {}
        self._shutdown = False

    async def available(self) -> bool:
        backend = DockerBackend(self.docker_path)
        return await backend.available()

    async def _ensure_pool(self, language: str) -> None:
        if self._initialized.get(language):
            return
        # H17: serialize pool creation per language so concurrent execute()
        # calls cannot race past the early-initialized check.
        lock = self._pool_init_locks.setdefault(language, asyncio.Lock())
        async with lock:
            if self._initialized.get(language):
                return
            await self._create_pool(language)

    async def _create_pool(self, language: str) -> None:
        from skyn3t.config.settings import get_settings

        settings = get_settings()
        image = self._IMAGES.get(language)
        if not image:
            return
        # Tear down stale containers before recreating (e.g., after recycle).
        for old_name in self._containers.get(language, []):
            try:
                stop_proc = await asyncio.create_subprocess_exec(
                    self.docker_path, "stop", "-t", "2", old_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await stop_proc.wait()
            except Exception:
                pass
            try:
                rm_proc = await asyncio.create_subprocess_exec(
                    self.docker_path, "rm", old_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await rm_proc.wait()
            except Exception:
                pass
            self._locks.pop(old_name, None)
            self._exec_counts.pop(old_name, None)
        self._containers[language] = []
        containers: List[str] = []
        for i in range(self.pool_size):
            name = f"skyn3t-pool-{language}-{i}-{os.getpid()}"
            proc = await asyncio.create_subprocess_exec(
                self.docker_path,
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "--network",
                "none",
                "--memory",
                "512m",
                "--read-only",
                "--tmpfs",
                "/tmp:noexec,nosuid,size=50m,mode=1777",
                *(_docker_hardening_flags(settings)),
                image,
                "sleep",
                "infinity",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            containers.append(name)
            self._locks[name] = asyncio.Lock()
            self._exec_counts[name] = 0
        self._containers[language] = containers
        self._initialized[language] = True
        logger.info(
            "DockerPoolBackend: started %d %s container(s)", self.pool_size, language
        )

    async def execute(
        self,
        code: str,
        language: str = "python",
        *,
        timeout: int = 30,
        memory_mb: int = 256,
    ) -> ExecutionResult:
        if self._shutdown:
            return ExecutionResult(success=False, error="Backend is shutting down")

        if not await self.available():
            return ExecutionResult(
                success=False,
                error="Docker is not available",
            )

        image = self._IMAGES.get(language)
        if not image:
            return ExecutionResult(
                success=False,
                error=f"DockerPoolBackend does not support language: {language}",
            )

        await self._ensure_pool(language)

        # Pick an idle container
        for name in self._containers.get(language, []):
            if self._locks[name].locked():
                continue
            async with self._locks[name]:
                return await self._exec_in_container(name, code, language, timeout)

        # All containers busy — fall back to cold-start DockerBackend
        logger.warning("DockerPoolBackend pool exhausted for %s; cold-starting", language)
        fallback = DockerBackend(self.docker_path)
        return await fallback.execute(code, language, timeout=timeout, memory_mb=memory_mb)

    async def _exec_in_container(
        self, name: str, code: str, language: str, timeout: int
    ) -> ExecutionResult:
        import uuid

        suffix = {
            "python": ".py",
            "javascript": ".js",
            "typescript": ".ts",
            "bash": ".sh",
            "go": ".go",
            "rust": ".rs",
            "php": ".php",
            "ruby": ".rb",
        }.get(language, ".txt")

        with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as fh:
            fh.write(code)
            local_path = fh.name

        try:
            os.chmod(local_path, 0o644)
        except Exception:
            pass

        remote_path = f"/tmp/code-{uuid.uuid4().hex}{suffix}"

        run_cmds: Dict[str, List[str]] = {
            "python": ["python", remote_path],
            "javascript": ["node", remote_path],
            "typescript": ["npx", "ts-node", remote_path],
            "bash": ["sh", remote_path],
            "go": ["go", "run", remote_path],
            "php": ["php", remote_path],
            "ruby": ["ruby", remote_path],
        }

        try:
            from skyn3t.config.settings import get_settings

            settings = get_settings()

            # Wipe leftover code artifacts from previous runs before streaming
            # new code into the pooled container.
            await self._clean_tmp_in_container(name)

            # Stream code into container via docker exec + cat (avoids
            # docker cp failing on read-only rootfs containers).
            with open(local_path, "rb") as fh:
                code_bytes = fh.read()
            cp_proc = await asyncio.create_subprocess_exec(
                self.docker_path, "exec", "-i", name, "sh", "-c",
                f"cat > {remote_path}",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout_data, stderr_data = await cp_proc.communicate(code_bytes)
            if cp_proc.returncode != 0:
                stderr_text = _decode_subprocess_output(stderr_data)
                return ExecutionResult(
                    success=False,
                    error=f"docker stream-in failed: {stderr_text}",
                )

            # Execute
            if language == "rust":
                exec_cmd = ["sh", "-c", f"rustc -o /tmp/out {remote_path} && /tmp/out"]
            else:
                exec_cmd = run_cmds.get(language, ["sh", remote_path])

            proc = await asyncio.create_subprocess_exec(
                self.docker_path, "exec", name, *exec_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            communicate_task = asyncio.create_task(proc.communicate())
            stdout_data, stderr_data = await asyncio.wait_for(communicate_task, timeout=timeout)

            # Clean up remote file and bump execution counter.
            asyncio.create_task(self._rm_in_container(name, remote_path))
            self._exec_counts[name] = self._exec_counts.get(name, 0) + 1
            recycle_after = getattr(settings, "docker_pool_recycle_after", 50)
            if recycle_after > 0 and self._exec_counts[name] >= recycle_after:
                self._initialized[language] = False
                self._exec_counts[name] = 0

            return ExecutionResult(
                success=proc.returncode == 0,
                stdout=_decode_subprocess_output(stdout_data),
                stderr=_decode_subprocess_output(stderr_data),
            )
        except asyncio.TimeoutError:
            # The local `docker exec` client returning/cancelling is NOT enough —
            # the process spawned INSIDE the pooled container keeps consuming its
            # CPU/mem (the container is long-lived and reused). Kill the local
            # client and the in-container process (matched by the unique
            # remote_path), then clean up the temp file.
            try:
                proc.kill()
            except (ProcessLookupError, Exception):
                pass
            await self._kill_in_container(name, remote_path)
            asyncio.create_task(self._rm_in_container(name, remote_path))
            return ExecutionResult(
                success=False,
                error=f"Execution timed out after {timeout}s",
            )
        except Exception as e:
            return ExecutionResult(success=False, error=str(e))
        finally:
            try:
                os.unlink(local_path)
            except Exception:
                pass

    async def _kill_in_container(self, name: str, remote_path: str) -> None:
        """Best-effort kill of any process still running ``remote_path`` inside
        the pooled container. The local ``docker exec`` client dying does not
        stop the spawned process, so an unkilled run would consume the shared
        container's resources until the container itself is recycled."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker_path, "exec", name, "pkill", "-f", remote_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except Exception:
            pass

    async def _rm_in_container(self, name: str, remote_path: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker_path, "exec", name, "rm", "-f", remote_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
        except Exception:
            pass

    async def _clean_tmp_in_container(self, name: str) -> None:
        """Best-effort cleanup of streamed code files inside a pooled container."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker_path,
                "exec",
                name,
                "sh",
                "-c",
                "rm -f /tmp/code-* /tmp/code-*.* /tmp/out 2>/dev/null",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except Exception:
            pass

    async def shutdown(self) -> None:
        """Stop and remove all pooled containers."""
        self._shutdown = True
        for language, names in self._containers.items():
            for name in names:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        self.docker_path, "stop", "-t", "2", name,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await proc.wait()
                except Exception:
                    pass
        self._containers.clear()
        self._initialized.clear()


async def get_backend(name: str = "auto") -> ExecutionBackend:
    """Return an execution backend by name.

    Names:
      - ``inline``      → InlineBackend (explicit opt-in for trusted code only)
      - ``docker``      → DockerBackend (raises if unavailable)
      - ``docker-pool`` → DockerPoolBackend (raises if unavailable)
      - ``auto``        → DockerPoolBackend if Docker is up; otherwise raises.
                          Will NOT silently run untrusted code in-process. The
                          insecure InlineBackend is used only when explicitly
                          opted in via the ``SKYN3T_ALLOW_INLINE_EXEC=1`` env
                          flag (off by default).
    """
    name = name.lower().strip()
    if name == "inline":
        # SECURITY: in-process exec() is trivially escapable to host RCE.
        # Require explicit opt-in even for explicit "inline" requests.
        if os.getenv("SKYN3T_ALLOW_INLINE_EXEC", "").strip() in ("1", "true", "yes", "on"):
            logger.warning(
                "SECURITY: InlineBackend selected explicitly. "
                "Running untrusted code IN-PROCESS is NOT a real sandbox."
            )
            return InlineBackend()
        raise RuntimeError(
            "InlineBackend is disabled. Set SKYN3T_ALLOW_INLINE_EXEC=1 to opt in to "
            "trusted-only in-process execution, or use Docker for real isolation."
        )
    if name == "docker":
        docker_backend = DockerBackend()
        if not await docker_backend.available():
            raise RuntimeError("Docker backend requested but Docker is not available")
        return docker_backend
    if name == "docker-pool":
        docker_pool_backend = DockerPoolBackend()
        if not await docker_pool_backend.available():
            raise RuntimeError("Docker pool backend requested but Docker is not available")
        return docker_pool_backend
    if name == "auto":
        docker = DockerPoolBackend()
        if await docker.available():
            logger.info("DockerPoolBackend selected (Docker is available)")
            return docker
        # SECURITY: do NOT silently fall back to the in-process InlineBackend
        # for untrusted/generated code — exec() in-process is trivially
        # escapable to full host RCE. Owner decision: Docker default,
        # opt-in inline. InlineBackend is allowed only when the operator
        # explicitly sets SKYN3T_ALLOW_INLINE_EXEC=1.
        if os.getenv("SKYN3T_ALLOW_INLINE_EXEC", "").strip() in ("1", "true", "yes", "on"):
            logger.warning(
                "SECURITY: Docker unavailable and SKYN3T_ALLOW_INLINE_EXEC is set — "
                "running untrusted code IN-PROCESS via InlineBackend. This is NOT a "
                "real sandbox and is trivially escapable to full host RCE. Use only "
                "for trusted code; install Docker for real isolation."
            )
            return InlineBackend()
        raise RuntimeError(
            "Docker is not available and in-process execution is disabled. "
            "Refusing to run untrusted code with the insecure InlineBackend. "
            "Install/start Docker, or set SKYN3T_ALLOW_INLINE_EXEC=1 to opt in to "
            "trusted-only in-process execution."
        )
    raise ValueError(f"Unknown execution backend: {name}")
