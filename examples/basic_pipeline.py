#!/usr/bin/env python3
"""Basic pipeline example: Write -> Review -> Add Tests.

This example demonstrates:
  - Creating an orchestrator
  - Registering Claude CLI, Kimi CLI, and Copilot CLI agents
  - Running a 3-stage pipeline with output forwarding
  - Printing results at each stage

Usage:
    python examples/basic_pipeline.py
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path so imports work without package install
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skyn3t.adapters.claude_cli import ClaudeCLIAgent
from skyn3t.adapters.copilot_cli import CopilotCLIAgent
from skyn3t.adapters.kimi_cli import KimiCLIAgent
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.core.pipeline import PipelineStage, create_pipeline


async def main():
    event_bus = EventBus()
    orchestrator = Orchestrator(event_bus)

    # Create CLI agents (they will be mocked in execution for the example)
    claude = ClaudeCLIAgent("claude_writer", event_bus)
    kimi = KimiCLIAgent("kimi_reviewer", event_bus)
    copilot = CopilotCLIAgent("copilot_tester", event_bus)

    # Register with orchestrator
    orchestrator.register_agent(claude)
    orchestrator.register_agent(kimi)
    orchestrator.register_agent(copilot)

    print("Registered agents:")
    for name, info in orchestrator.agent_registry.items():
        print(f"  - {name} ({info['provider']})")

    # Build a 3-stage pipeline
    pipeline = create_pipeline(
        name="write_review_test",
        agents=[claude, kimi, copilot],
        event_bus=event_bus,
        stage_names=["write_code", "review_code", "add_tests"],
    )

    print("\nRunning pipeline: Write a Python function -> Review it -> Add tests\n")

    # Mock execute to simulate CLI responses (remove in real usage)
    async def mock_claude(task):
        from skyn3t.core.agent import TaskResult
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"response": "def greet(name): return f'Hello, {name}!'"},
        )

    async def mock_kimi(task):
        from skyn3t.core.agent import TaskResult
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"response": "Review: Function is clean. Suggest adding type hints."},
        )

    async def mock_copilot(task):
        from skyn3t.core.agent import TaskResult
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "response": "import pytest\n\ndef test_greet():\n    assert greet('World') == 'Hello, World!'"
            },
        )

    claude.execute = mock_claude
    kimi.execute = mock_kimi
    copilot.execute = mock_copilot

    result = await pipeline.run(
        initial_input={"message": "Write a Python function that greets a user"}
    )

    # Print results at each stage
    for stage in result.stages:
        status = "OK" if stage["success"] else "FAIL"
        print(f"Stage {stage['stage']}: {stage['name']} [{status}]")
        print(f"  Agent : {stage['agent']}")
        response = stage["output"].get("response", "")
        print(f"  Output: {response[:200]}{'...' if len(response) > 200 else ''}")
        if stage["error"]:
            print(f"  Error : {stage['error']}")
        print()

    print(f"Pipeline '{pipeline.name}' completed: {'success' if result.success else 'failure'}")
    return 0 if result.success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
