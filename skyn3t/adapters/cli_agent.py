"""Base CLI agent implementation with sandboxed execution."""

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.security.sandbox import CLISandboxRunner, Sandbox, SandboxConfig


class CLIAgent(BaseAgent):
    """Base class for agents that invoke actual command-line tools.

    All CLI execution runs through the sandbox layer for isolation,
    resource limits, and path restrictions.
    """

    def __init__(
        self,
        name: str,
        agent_type: str,
        provider: str,
        event_bus: EventBus,
        command: str,
        config: Optional[Dict[str, Any]] = None,
        sandbox: Optional[Sandbox] = None,
    ):
        super().__init__(
            name=name,
            agent_type=agent_type,
            provider=provider,
            event_bus=event_bus,
            config=config,
        )
        self._command = command
        self.add_capability(
            AgentCapability(
                name="cli_execution",
                description=f"Execute tasks via the {command} CLI subprocess",
            )
        )
        # Build sandbox from config or use default
        sandbox_config = self._build_sandbox_config(config)
        self._sandbox = sandbox or Sandbox(config=sandbox_config)
        self._runner = CLISandboxRunner(sandbox=self._sandbox)

    def _build_sandbox_config(
        self, config: Optional[Dict[str, Any]]
    ) -> SandboxConfig:
        """Build a SandboxConfig from agent config dict."""
        cfg = config or {}
        default_dirs = [str(Path.cwd()), str(Path.home()), "/opt/homebrew"]
        allowed_dirs = cfg.get("allowed_dirs", default_dirs)
        return SandboxConfig(
            max_cpu_time=cfg.get("max_cpu_time", 60.0),
            max_memory_mb=cfg.get("max_memory_mb", 512),
            max_file_size_mb=cfg.get("max_file_size_mb", 128),
            max_open_files=cfg.get("max_open_files", 64),
            max_processes=cfg.get("max_processes", 32),
            allowed_dirs=[Path(d) for d in allowed_dirs],
            allow_network=cfg.get("allow_network", True),
            timeout_seconds=cfg.get("timeout_seconds", 300.0),
            capture_syscalls=cfg.get("capture_syscalls", False),
            cleanup_temp=cfg.get("cleanup_temp", True),
            keep_files_on_error=cfg.get("keep_files_on_error", False),
        )

    @property
    def command(self) -> str:
        """The CLI command to execute (e.g., 'claude')."""
        return self._command

    def build_args(self, task: TaskRequest) -> List[str]:
        """Build CLI arguments from task input. Override in subclasses."""
        args: List[str] = []
        prompt = task.input_data.get("message", task.description)
        if not prompt and task.title:
            prompt = task.title
        if prompt:
            args.append(prompt)
        return args

    async def run_cli(
        self,
        args: List[str],
        stdin: Optional[str] = None,
        timeout: int = 300,
        working_dir: Optional[Path] = None,
    ) -> tuple[int, str, str]:
        """Run the CLI command with given arguments through the sandbox.

        Returns:
            Tuple of (return_code, stdout, stderr)

        Raises:
            asyncio.TimeoutError: If the sandbox kills the process due to timeout.
        """
        rc, stdout, stderr = await self._runner.run(
            command=self.command,
            args=args,
            stdin=stdin,
            timeout=timeout,
            working_dir=working_dir,
        )
        if rc == -1 and "TIMEOUT" in stderr:
            raise asyncio.TimeoutError(stderr)
        return rc, stdout, stderr

    async def initialize(self) -> None:
        """Initialize the agent."""
        self.metadata["command"] = self.command
        self.metadata["initialized"] = True
        self.metadata["sandboxed"] = True

    async def health_check(self) -> bool:
        """Check if the CLI tool is available."""
        try:
            returncode, stdout, stderr = await self.run_cli(
                ["--version"], timeout=10
            )
            return returncode == 0
        except Exception:
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Get agent statistics with CLI indicator."""
        stats = super().get_stats()
        stats["cli_agent"] = True
        stats["cli_command"] = self._command
        stats["sandboxed"] = True
        return stats

    async def execute(self, task: TaskRequest) -> TaskResult:
        """Execute a task by shelling out to the CLI via the sandbox."""
        args = self.build_args(task)
        stdin = task.input_data.get("stdin")
        timeout = task.input_data.get("timeout", 300)
        working_dir = task.input_data.get("working_dir")
        if working_dir:
            working_dir = Path(working_dir)

        try:
            returncode, stdout, stderr = await self.run_cli(
                args, stdin=stdin, timeout=timeout, working_dir=working_dir
            )

            if returncode != 0:
                error_msg = f"CLI exited with code {returncode}"
                if stderr.strip():
                    error_msg += f": {stderr.strip()}"
                return TaskResult(
                    task_id=task.task_id,
                    success=False,
                    error=error_msg,
                    output={
                        "stdout": stdout,
                        "stderr": stderr,
                        "returncode": returncode,
                    },
                )

            return TaskResult(
                task_id=task.task_id,
                success=True,
                output={
                    "response": stdout,
                    "stderr": stderr,
                    "returncode": returncode,
                },
            )

        except asyncio.TimeoutError:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"CLI command timed out after {timeout}s",
            )
        except PermissionError as e:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"Sandbox permission denied: {e}",
            )
        except Exception as e:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=str(e),
            )
