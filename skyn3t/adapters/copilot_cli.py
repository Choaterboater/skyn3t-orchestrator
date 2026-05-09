"""GitHub Copilot CLI agent adapter."""

from typing import Any, Dict, Optional

from skyn3t.adapters.cli_agent import CLIAgent
from skyn3t.core.agent import AgentCapability, TaskRequest
from skyn3t.core.events import EventBus


class CopilotCLIAgent(CLIAgent):
    """Agent powered by the GitHub Copilot CLI."""

    def __init__(
        self,
        name: str,
        event_bus: EventBus,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="code_assistant",
            provider="github_copilot",
            event_bus=event_bus,
            command="copilot",
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="code_completion",
                description="Complete code snippets via the Copilot CLI",
            )
        )
        self.add_capability(
            AgentCapability(
                name="code_review",
                description="Review code for bugs and style via Copilot CLI",
            )
        )
        self.add_capability(
            AgentCapability(
                name="test_generation",
                description="Generate unit tests via the Copilot CLI",
            )
        )
        self.add_capability(
            AgentCapability(
                name="documentation",
                description="Generate documentation via the Copilot CLI",
            )
        )
        self.add_capability(
            AgentCapability(
                name="cli_copilot",
                description="Execute tasks via the copilot CLI subprocess",
            )
        )

    def build_args(self, task: TaskRequest) -> list[str]:
        """Build CLI args for `copilot -p <prompt>` with optional flags."""
        args: list[str] = []

        if "effort" in task.input_data:
            args.extend(["--effort", str(task.input_data["effort"])])
        if "add_dir" in task.input_data:
            dirs = task.input_data["add_dir"]
            if isinstance(dirs, str):
                dirs = [dirs]
            for d in dirs:
                args.extend(["--add-dir", str(d)])
        if task.input_data.get("allow_all"):
            args.append("--allow-all")
        if task.input_data.get("allow_all_tools"):
            args.append("--allow-all-tools")
        if "model" in task.input_data:
            args.extend(["--model", str(task.input_data["model"])])

        # Prompt
        prompt = task.input_data.get("message", task.description)
        if not prompt and task.title:
            prompt = task.title
        if not prompt:
            prompt = "No prompt provided."

        args.extend(["-p", prompt])
        return args
