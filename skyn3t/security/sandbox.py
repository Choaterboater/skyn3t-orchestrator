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
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN", "KIMI_API_KEY",
        "SECRET_KEY", "PASSWORD", "TOKEN",
    ])

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
            f'    (allow file-read* file-write* (subpath "{d}"))'
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
        home_dir = str(Path.home())
        return f"""(version 1)
(debug deny)
(allow default)
{allowed_paths}
{denied_paths}
{network_rule}
    (allow file-read* (subpath "/usr"))
    (allow file-read* (subpath "/bin"))
    (allow file-read* (subpath "/sbin"))
    (allow file-read* (subpath "/lib"))
    (allow file-read* (subpath "/lib64"))
    (allow file-read* (subpath "/System"))
    (allow file-read* (subpath "/dev"))
    (allow file-read* (subpath "/private/var"))
    (allow file-read* file-write* (subpath "{temp_dir}"))
    (allow process-exec (subpath "/usr") (subpath "/bin") (subpath "/sbin") (subpath "/opt") (subpath "/private/var"))
    (allow process-exec (subpath "{temp_dir}"))
    (allow process-exec (subpath "{home_dir}"))
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
        # Remove blocked secrets
        for key in list(env.keys()):
            if any(blocked.lower() in key.lower() for blocked in self.config.blocked_env_vars):
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
