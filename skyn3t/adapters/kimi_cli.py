"""Kimi CLI agent adapter."""

from typing import Any, Dict, Optional

from skyn3t.adapters.cli_agent import CLIAgent
from skyn3t.core.agent import AgentCapability, TaskRequest
from skyn3t.core.events import EventBus


class KimiCLIAgent(CLIAgent):
    """Agent powered by the Kimi CLI."""

    def __init__(
        self,
        name: str,
        event_bus: EventBus,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="llm",
            provider="kimi",
            event_bus=event_bus,
            command="kimi",
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="text_generation",
                description="Generate text responses using the Kimi CLI",
            )
        )
        self.add_capability(
            AgentCapability(
                name="code_generation",
                description="Generate and explain code with Kimi",
            )
        )
        self.add_capability(
            AgentCapability(
                name="system_design",
                description="Design system architecture and component structure",
            )
        )
        self.add_capability(
            AgentCapability(
                name="swarm_coordination",
                description="Coordinate multi-agent workflows and task decomposition",
            )
        )
        self.add_capability(
            AgentCapability(
                name="cli_kimi",
                description="Execute tasks via the kimi CLI subprocess",
            )
        )

    def build_args(self, task: TaskRequest) -> list[str]:
        """Build CLI args for `kimi --print -p <prompt>` with optional flags."""
        args = ["--print"]

        if "work_dir" in task.input_data:
            args.extend(["-w", str(task.input_data["work_dir"])])
        if "verbose" in task.input_data and task.input_data["verbose"]:
            args.append("--verbose")
        if "model" in task.input_data:
            args.extend(["-m", str(task.input_data["model"])])
        if "thinking" in task.input_data:
            if task.input_data["thinking"]:
                args.append("--thinking")
            else:
                args.append("--no-thinking")
        if "add_dir" in task.input_data:
            for d in task.input_data["add_dir"]:
                args.extend(["--add-dir", str(d)])
        if task.input_data.get("yolo"):
            args.append("-y")
        if task.input_data.get("quiet"):
            args.append("--quiet")

        # Prompt
        prompt = task.input_data.get("message", task.description)
        if not prompt and task.title:
            prompt = task.title
        if not prompt:
            prompt = "No prompt provided."

        args.extend(["-p", prompt])
        return args
