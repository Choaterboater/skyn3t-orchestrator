"""SkyN3t external service integrations."""

from skyn3t.integrations.discord_bot import DiscordBot
from skyn3t.integrations.email_agent import EmailAgent
from skyn3t.integrations.github_webhook import GitHubWebhookAgent
from skyn3t.integrations.github_webhook import router as github_webhook_router
from skyn3t.integrations.messaging import (
    InboundMessage,
    MatrixChannel,
    MessagingChannel,
    MessagingRouter,
    TelegramChannel,
    WhatsAppChannel,
    get_default_router,
)
from skyn3t.integrations.slack_bot import SlackBot
from skyn3t.integrations.telegram_webhook import router as telegram_webhook_router

__all__ = [
    "DiscordBot",
    "EmailAgent",
    "GitHubWebhookAgent",
    "InboundMessage",
    "MatrixChannel",
    "MessagingChannel",
    "MessagingRouter",
    "SlackBot",
    "TelegramChannel",
    "WhatsAppChannel",
    "get_default_router",
    "github_webhook_router",
    "telegram_webhook_router",
]
