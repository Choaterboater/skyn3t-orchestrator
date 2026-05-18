"""OpenAI agent adapter.

The `openai` CLI tool exists (`openai api chat.completions.create`) but does not
provide a simple single-prompt interface like Claude/Kimi/Copilot. For robustness
and feature parity we keep the API-based implementation, placed in this file as
requested.
"""

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

from skyn3t.config.settings import get_settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus


class OpenAIAgent(BaseAgent):
    """Agent powered by OpenAI's GPT models (API-based)."""

    MAX_HISTORY_MESSAGES = 50

    def __init__(
        self,
        name: str,
        event_bus: EventBus,
        model: str = "gpt-4-turbo-preview",
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="llm",
            provider="openai",
            event_bus=event_bus,
            config=config,
        )
        self.model = model
        self.client: Any = None
        self.conversation_history: List[Dict[str, str]] = []
        self.add_capability(
            AgentCapability(
                name="text_generation",
                description="Generate text responses using GPT",
                parameters={"model": model, "max_tokens": 4096},
            )
        )
        self.add_capability(
            AgentCapability(
                name="code_generation",
                description="Generate and explain code",
                parameters={"model": model, "languages": "any"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="analysis",
                description="Analyze data, code, and documents",
            )
        )

    async def initialize(self) -> None:
        """Initialize OpenAI client."""
        try:
            import openai

            settings = get_settings()
            api_key = settings.openai_api_key or os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OpenAI API key not configured")

            self.client = openai.AsyncOpenAI(api_key=api_key)
            self.metadata["model"] = self.model
            self.metadata["initialized"] = True
        except ImportError:
            raise ImportError("openai package not installed. Run: pip install openai")

    # Health-check cache: don't hammer models.list() (a paid call) on every
    # 30s monitor tick — cache the last result for 5 minutes.
    _HEALTH_TTL_SECONDS = 300
    _health_cached_at: float = 0.0
    _health_cached_value: bool = False

    async def health_check(self) -> bool:
        """Check if OpenAI API is accessible (cached + timeout-bounded)."""
        if not self.client:
            return False
        now = time.monotonic()
        if now - self._health_cached_at < self._HEALTH_TTL_SECONDS:
            return self._health_cached_value
        try:
            await asyncio.wait_for(self.client.models.list(), timeout=5.0)
            self._health_cached_value = True
        except Exception:
            self._health_cached_value = False
        self._health_cached_at = now
        return self._health_cached_value

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        """Execute a task using OpenAI."""
        if not self.client:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error="OpenAI client not initialized",
            )

        try:
            messages: List[Dict[str, str]] = []

            # System prompt
            system_prompt = task.input_data.get(
                "system_prompt",
                "You are a helpful AI assistant agent in the SkyN3t orchestrator system. "
                "You work alongside other specialized agents to accomplish complex tasks.",
            )
            messages.append({"role": "system", "content": system_prompt})

            # Conversation history
            history = task.input_data.get("conversation_history", [])
            for entry in history:
                messages.append({
                    "role": "user" if entry.get("agent") != self.name else "assistant",
                    "content": entry.get("content", ""),
                })

            # Current task
            content = task.input_data.get("message", task.description)
            if not content and task.title:
                content = task.title

            messages.append({"role": "user", "content": content})

            # Call OpenAI
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=task.input_data.get("max_tokens", 4096),
                temperature=task.input_data.get("temperature", 0.7),
                tools=task.input_data.get("tools"),
                tool_choice=task.input_data.get("tool_choice"),
            )

            result_content = response.choices[0].message.content or ""

            # Update conversation history (bounded)
            self.conversation_history.append({"role": "user", "content": content})
            self.conversation_history.append({"role": "assistant", "content": result_content})
            if len(self.conversation_history) > self.MAX_HISTORY_MESSAGES:
                self.conversation_history = self.conversation_history[-self.MAX_HISTORY_MESSAGES:]

            return TaskResult(
                task_id=task.task_id,
                success=True,
                output={
                    "response": result_content,
                    "model": self.model,
                    "usage": {
                        "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                        "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                        "total_tokens": response.usage.total_tokens if response.usage else 0,
                    },
                },
            )

        except Exception as e:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=str(e),
            )
