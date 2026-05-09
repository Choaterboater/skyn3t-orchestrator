"""Discord bot integration."""

import asyncio
import os
from typing import Any, Dict, Optional

from skyn3t.config.settings import get_settings
from skyn3t.core.events import Event, EventBus, EventType


class DiscordBot:
    """Discord bot that routes messages to SkyN3t agents."""

    def __init__(self, event_bus: EventBus, token: Optional[str] = None):
        self.event_bus = event_bus
        self.token = token or os.getenv("DISCORD_TOKEN")
        self._client = None
        self._running = False

    async def initialize(self) -> None:
        """Initialize Discord client."""
        if not self.token:
            raise ValueError("DISCORD_TOKEN not configured")

        try:
            import discord

            intents = discord.Intents.default()
            intents.message_content = True

            outer_self = self

            class Skyn3tDiscordClient(discord.Client):
                def __init__(inner_self, event_bus, **kwargs):
                    super().__init__(**kwargs)
                    inner_self.event_bus = event_bus

                async def on_ready(inner_self):
                    print(f"Discord bot logged in as {inner_self.user}")

                async def on_message(inner_self, message):
                    try:
                        if message.author == inner_self.user:
                            return

                        if inner_self.user.mentioned_in(message):
                            text = message.content.replace(f"<@{inner_self.user.id}>", "").strip()
                            await outer_self._process_message(text, str(message.channel.id), str(message.id))

                        elif isinstance(message.channel, discord.DMChannel):
                            await outer_self._process_message(message.content, str(message.channel.id), str(message.id))
                    except Exception as e:
                        print(f"Discord on_message error: {e}")

            self._client = Skyn3tDiscordClient(
                event_bus=self.event_bus,
                intents=intents,
            )
            self._running = True
        except ImportError:
            raise ImportError("discord.py not installed. Run: pip install discord.py")

    async def start(self) -> None:
        """Start the Discord bot."""
        if not self._running:
            await self.initialize()
        await self._client.start(self.token)

    async def stop(self) -> None:
        """Stop the Discord bot."""
        if self._client:
            await self._client.close()
        self._running = False

    async def _process_message(self, text: str, channel_id: str, message_id: str) -> None:
        """Process a Discord message."""
        self.event_bus.publish(
            Event(
                event_type=EventType.TASK_CREATED,
                source="discord_bot",
                payload={
                    "message": text,
                    "channel_id": channel_id,
                    "message_id": message_id,
                    "platform": "discord",
                },
            )
        )
