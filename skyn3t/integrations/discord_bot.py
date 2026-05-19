"""Discord bot integration.

Two roles:

1. Outbound notifications (approval gate posts here).
2. Inbound control surface — DMs and ``@mentions`` get routed through
   ``skyn3t.integrations.discord_commands.handle_dm`` so the user can
   start projects, approve, reject, or list directly from chat.

Button-press interactions arrive via Discord's HTTP webhook (see
``/api/discord/interactions`` in ``web/app.py``), not the Gateway, so
the bot itself doesn't need an ``on_interaction`` handler — Discord
handles the routing.
"""

import logging
import os
from typing import Any, Optional

from skyn3t.core.events import Event, EventBus, EventType

logger = logging.getLogger(__name__)


class DiscordBot:
    """Discord bot that routes messages to SkyN3t agents."""

    def __init__(
        self,
        event_bus: EventBus,
        token: Optional[str] = None,
        studio_runner: Optional[Any] = None,
    ):
        self.event_bus = event_bus
        self.token = token or os.getenv("DISCORD_TOKEN")
        self.studio_runner = studio_runner
        self._client: Any = None
        self._running = False

    async def initialize(self) -> None:
        """Initialize Discord client."""
        if not self.token:
            raise ValueError("DISCORD_TOKEN not configured")

        try:
            import discord

            intents = discord.Intents.default()
            intents.message_content = True
            intents.dm_messages = True

            outer_self = self

            class Skyn3tDiscordClient(discord.Client):
                def __init__(inner_self, event_bus, **kwargs):
                    super().__init__(**kwargs)
                    inner_self.event_bus = event_bus

                async def on_ready(inner_self):
                    logger.info("Discord bot logged in as %s", inner_self.user)

                async def on_message(inner_self, message):
                    try:
                        if message.author == inner_self.user:
                            return

                        is_dm = isinstance(message.channel, discord.DMChannel)
                        is_mention = inner_self.user.mentioned_in(message)
                        if not (is_dm or is_mention):
                            return

                        text = message.content
                        if is_mention:
                            text = text.replace(f"<@{inner_self.user.id}>", "").strip()

                        await outer_self._handle_inbound_text(text, message)
                    except Exception:
                        logger.exception("Discord on_message error")

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

    async def _handle_inbound_text(self, text: str, message: Any) -> None:
        """Route a DM or @mention. If a studio runner is wired, treat the
        message as a studio intent (start/status/approve/reject/list);
        otherwise fall back to publishing a TASK_CREATED event so other
        agents can consume it.
        """
        user_id = str(getattr(message.author, "id", ""))
        if self.studio_runner is not None:
            try:
                from skyn3t.integrations.discord_commands import handle_dm
                reply = await handle_dm(text, user_id, self.studio_runner)
            except Exception:
                logger.exception("Discord studio dispatch failed")
                reply = "Sorry, something went wrong handling that message."
            try:
                await message.channel.send(reply)
            except Exception:
                logger.exception("Discord reply failed")
            return

        # Fallback: no runner wired → original TASK_CREATED behavior.
        await self._process_message(text, str(message.channel.id), str(message.id))

    async def _process_message(self, text: str, channel_id: str, message_id: str) -> None:
        """Process a Discord message (fallback / non-studio path)."""
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
