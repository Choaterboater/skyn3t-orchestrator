#!/usr/bin/env python3
"""Multi-agent round-robin conversation example.

This example demonstrates:
  - Claude CLI, Kimi CLI, and Copilot CLI agents
  - Round-robin conversation on a topic
  - Each agent responds to the previous agent's output
  - Using the orchestrator's run_conversation helper

Usage:
    python examples/multi_agent_chat.py
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path so imports work without package install
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skyn3t.adapters.claude_cli import ClaudeCLIAgent
from skyn3t.adapters.copilot_cli import CopilotCLIAgent
from skyn3t.adapters.kimi_cli import KimiCLIAgent

from skyn3t.core.agent import TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator


async def main():
    event_bus = EventBus()
    orchestrator = Orchestrator(event_bus)

    claude = ClaudeCLIAgent("claude", event_bus)
    kimi = KimiCLIAgent("kimi", event_bus)
    copilot = CopilotCLIAgent("copilot", event_bus)

    orchestrator.register_agent(claude)
    orchestrator.register_agent(kimi)
    orchestrator.register_agent(copilot)

    print("Registered agents:")
    for name in orchestrator.agents:
        print(f"  - {name}")

    # Mock executes to simulate responses
    async def mock_claude(task):
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "response": (
                    "Claude: To build a startup, start with a clear problem statement. "
                    "Validate the idea by talking to 10 potential customers before writing any code."
                )
            },
        )

    async def mock_kimi(task):
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "response": (
                    "Kimi: 我同意Claude的观点。此外，我建议组建一个互补技能的团队，"
                    "并采用精益创业方法来快速迭代。"
                )
            },
        )

    async def mock_copilot(task):
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "response": (
                    "Copilot: From a technical standpoint, choose boring technology for your MVP. "
                    "Focus on shipping fast and measuring user engagement metrics."
                )
            },
        )

    claude.execute = mock_claude
    kimi.execute = mock_kimi
    copilot.execute = mock_copilot

    topic = "How to build a startup"
    rounds = 2
    participants = ["claude", "kimi", "copilot"]

    print(f"\nTopic: {topic}")
    print(f"Participants: {', '.join(participants)}")
    print(f"Rounds: {rounds}\n")
    print("=" * 60)

    conversation = await orchestrator.run_conversation(
        initiator="claude",
        participants=participants,
        topic=topic,
        rounds=rounds,
    )

    for entry in conversation:
        print(f"\nRound {entry['round']} | {entry['agent']}")
        print(f"Input: {entry['input'][:120]}{'...' if len(entry['input']) > 120 else ''}")
        print(f"Response: {entry['response'][:200]}{'...' if len(entry['response']) > 200 else ''}")
        print(f"Success: {entry['success']}")

    print("\n" + "=" * 60)
    print(f"Conversation complete. Total exchanges: {len(conversation)}")
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
