"""Slack bot integration for SkyN3t."""

import logging
import os
import re
from typing import Any, Dict, Optional

from skyn3t.core.events import EventBus

logger = logging.getLogger("skyn3t.integrations.slack_bot")


class SlackBot:
    """Slack bot that routes messages to SkyN3t agents."""

    def __init__(self, event_bus: EventBus, bot_token: Optional[str] = None):
        self.event_bus = event_bus
        self.bot_token = bot_token or os.getenv("SLACK_BOT_TOKEN")
        self.app_token = os.getenv("SLACK_APP_TOKEN")
        self.client = None
        self._running = False
        # Resolved at initialize() via auth.test(); used to strip mentions of
        # *this* bot (event.bot_id is the *sender*, not us, so it can't be used
        # to strip our own @mention).
        self._self_user_id: Optional[str] = None

    async def initialize(self) -> None:
        """Initialize Slack connection."""
        if not self.bot_token:
            raise ValueError("SLACK_BOT_TOKEN not configured")

        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
            from slack_sdk.web.async_client import AsyncWebClient

            self.web_client = AsyncWebClient(token=self.bot_token)
            # Resolve our own user_id so _handle_event can strip mentions of us.
            try:
                auth = await self.web_client.auth_test()
                self._self_user_id = auth.get("user_id")
            except Exception as exc:
                logger.warning("Slack auth.test failed: %s", exc)
            if self.app_token:
                self.socket_client = SocketModeClient(
                    app_token=self.app_token, web_client=self.web_client
                )
                self.socket_client.socket_mode_request_listeners.append(
                    self._handle_socket_event
                )
            self._running = True
        except ImportError:
            raise ImportError("slack-sdk not installed. Run: pip install slack-sdk")

    async def start(self) -> None:
        """Start the Slack bot."""
        if not self._running:
            await self.initialize()

        if hasattr(self, "socket_client"):
            await self.socket_client.connect()
            print("Slack bot connected via Socket Mode")
        else:
            print("Slack bot initialized (polling mode)")

    async def stop(self) -> None:
        """Stop the Slack bot."""
        self._running = False
        if hasattr(self, "socket_client"):
            await self.socket_client.close()

    async def _handle_socket_event(
        self, client: Any, req: Any
    ) -> None:
        """Handle incoming Slack events."""
        if req.type == "events_api":
            event = req.payload.get("event", {})
            await self._handle_event(event)
            await client.send_socket_mode_response(
                {
                    "envelope_id": req.envelope_id,
                    "payload": {"ok": True},
                }
            )

    async def _handle_event(self, event: Dict[str, Any]) -> None:
        """Process a Slack event."""
        event_type = event.get("type")

        if event_type == "app_mention":
            raw_text = event.get("text", "")
            # Strip the mention of *our* user_id (resolved via auth.test in
            # initialize). event.get('bot_id') is the *sender's* bot id, so
            # using it here was a no-op and the mention text was never removed.
            if self._self_user_id:
                raw_text = raw_text.replace(f"<@{self._self_user_id}>", "")
            else:
                # Fallback: drop any leading user mention.
                raw_text = re.sub(r"^\s*<@[A-Z0-9]+>\s*", "", raw_text)
            text = raw_text.strip()
            channel = str(event.get("channel") or "")
            thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
            await self._process_message(text, channel, thread_ts)

        elif event_type == "message" and not event.get("bot_id"):
            text = event.get("text", "")
            channel = str(event.get("channel") or "")
            thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
            # Only respond to DMs
            if event.get("channel_type") == "im":
                await self._process_message(text, channel, thread_ts)

    async def _process_message(
        self, text: str, channel: str, thread_ts: str
    ) -> None:
        """Process a user message and route to agents."""
        # Create a task for the orchestrator
        from skyn3t.core.events import Event, EventType

        self.event_bus.publish(
            Event(
                event_type=EventType.TASK_CREATED,
                source="slack_bot",
                payload={
                    "message": text,
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "platform": "slack",
                },
            )
        )

        # Send acknowledgment
        if hasattr(self, "web_client"):
            await self.web_client.chat_postMessage(
                channel=channel,
                text=f"🤖 Processing: _{text[:100]}..._",
                thread_ts=thread_ts,
            )
