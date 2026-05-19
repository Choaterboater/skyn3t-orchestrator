#!/usr/bin/env python3
"""GitHub Explorer + CLI agents pipeline example.

This example demonstrates:
  - GitHub Explorer agent analyzes a repository
  - Claude CLI agent generates a README from the analysis
  - Kimi CLI agent translates the README to Chinese
  - Full pipeline execution with output forwarding

Usage:
    python examples/github_explorer_example.py
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path so imports work without package install
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skyn3t.adapters.claude_cli import ClaudeCLIAgent
from skyn3t.adapters.kimi_cli import KimiCLIAgent
from skyn3t.agents.github_explorer import GitHubExplorerAgent
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.core.pipeline import create_pipeline


async def main():
    event_bus = EventBus()
    orchestrator = Orchestrator(event_bus)

    # Create agents
    github = GitHubExplorerAgent("github_explorer", event_bus)
    claude = ClaudeCLIAgent("claude_readme", event_bus)
    kimi = KimiCLIAgent("kimi_translator", event_bus)

    orchestrator.register_agent(github)
    orchestrator.register_agent(claude)
    orchestrator.register_agent(kimi)

    print("Registered agents:")
    for name, info in orchestrator.agent_registry.items():
        print(f"  - {name} ({info['provider']})")

    # Build pipeline
    pipeline = create_pipeline(
        name="github_readme_translate",
        agents=[github, claude, kimi],
        event_bus=event_bus,
        stage_names=["analyze_repo", "generate_readme", "translate_to_chinese"],
    )

    print("\nRunning pipeline:")
    print("  1. GitHub Explorer analyzes a repo")
    print("  2. Claude CLI generates README from analysis")
    print("  3. Kimi CLI translates README to Chinese\n")

    # Mock executes to simulate real behavior without needing API keys
    async def mock_github(task):
        from skyn3t.core.agent import TaskResult
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "response": (
                    "Repo: skyn3t\n"
                    "Language: Python\n"
                    "Stars: 150\n"
                    "Description: Multi-agent orchestrator system.\n"
                    "Features: event-driven, self-healing, RAG pipeline"
                )
            },
        )

    async def mock_claude(task):
        from skyn3t.core.agent import TaskResult
        analysis = task.input_data.get("message", "")
        readme = f"""# SkyN3t

{analysis}

## Overview

SkyN3t is a powerful multi-agent orchestration platform.

## Getting Started

```bash
pip install -r requirements.txt
python -m skyn3t
```

## License

MIT
"""
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"response": readme},
        )

    async def mock_kimi(task):
        from skyn3t.core.agent import TaskResult
        readme = task.input_data.get("message", "")
        chinese = readme.replace("Overview", "概述").replace("Getting Started", "快速开始")
        chinese += "\n\n（由Kimi翻译）\n"
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"response": chinese},
        )

    github.execute = mock_github
    claude.execute = mock_claude
    kimi.execute = mock_kimi

    result = await pipeline.run(
        initial_input={"message": "Analyze skyn3t repo", "owner": "user", "repo": "skyn3t"}
    )

    for stage in result.stages:
        status = "OK" if stage["success"] else "FAIL"
        print(f"Stage {stage['stage']}: {stage['name']} [{status}]")
        print(f"  Agent : {stage['agent']}")
        response = stage["output"].get("response", "")
        print(f"  Output:\n{response[:400]}{'...' if len(response) > 400 else ''}")
        if stage["error"]:
            print(f"  Error : {stage['error']}")
        print()

    print(f"Pipeline '{pipeline.name}' completed: {'success' if result.success else 'failure'}")
    return 0 if result.success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
