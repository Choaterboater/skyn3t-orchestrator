"""Anthropic Claude CLI agent adapter."""

from typing import Any, Dict, Optional

from skyn3t.adapters.cli_agent import CLIAgent
from skyn3t.core.agent import AgentCapability, TaskRequest
from skyn3t.core.events import EventBus


class ClaudeCLIAgent(CLIAgent):
    """Agent powered by the Anthropic Claude CLI."""

    def __init__(
        self,
        name: str,
        event_bus: EventBus,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="llm",
            provider="anthropic",
            event_bus=event_bus,
            command="claude",
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="text_generation",
                description="Generate text responses using the Claude CLI",
            )
        )
        self.add_capability(
            AgentCapability(
                name="code_generation",
                description="Generate and explain code with Claude",
            )
        )
        self.add_capability(
            AgentCapability(
                name="analysis",
                description="Analyze data, code, and documents with Claude",
            )
        )
        self.add_capability(
            AgentCapability(
                name="cli_claude",
                description="Execute tasks via the claude CLI subprocess",
            )
        )

    def build_args(self, task: TaskRequest) -> list[str]:
        """Build CLI args for `claude -p <prompt>` with optional flags."""
        args = ["-p"]

        # Optional flags
        if "allowed_tools" in task.input_data:
            args.extend(["--allowed-tools", str(task.input_data["allowed_tools"])])
        if "effort" in task.input_data:
            args.extend(["--effort", str(task.input_data["effort"])])
        if "system_prompt" in task.input_data:
            args.extend(["--system-prompt", str(task.input_data["system_prompt"])])
        if "model" in task.input_data:
            args.extend(["--model", str(task.input_data["model"])])
        if "output_format" in task.input_data:
            args.extend(["--output-format", str(task.input_data["output_format"])])
        if task.input_data.get("bare"):
            args.append("--bare")

        # Prompt (final positional arg)
        prompt = task.input_data.get("message", task.description)
        if not prompt and task.title:
            prompt = task.title
        if not prompt:
            prompt = "No prompt provided."

        args.append(prompt)
        return args
