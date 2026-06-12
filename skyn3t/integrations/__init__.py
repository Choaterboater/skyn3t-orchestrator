"""SkyN3t external service integrations."""

from skyn3t.integrations.discord_bot import DiscordBot
from skyn3t.integrations.email_agent import EmailAgent
from skyn3t.integrations.github_webhook import GitHubWebhookAgent
from skyn3t.integrations.github_webhook import router as github_webhook_router
from skyn3t.integrations.messaging import (
    FeishuChannel,
    GenericWebhookChannel,
    IMessageChannel,
    InboundMessage,
    MatrixChannel,
    MattermostChannel,
    MessagingChannel,
    MessagingRouter,
    MSTeamsChannel,
    SignalChannel,
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
    "FeishuChannel",
    "GenericWebhookChannel",
    "IMessageChannel",
    "InboundMessage",
    "MSTeamsChannel",
    "MatrixChannel",
    "MattermostChannel",
    "MessagingChannel",
    "MessagingRouter",
    "SignalChannel",
    "SlackBot",
    "TelegramChannel",
    "WhatsAppChannel",
    "get_default_router",
    "github_webhook_router",
    "telegram_webhook_router",
]

# ── Phase 5B new channel adapters (Hermes parity) ─────────────────────
#
# Each new channel lives in its own module and may depend on optional
# SDKs / be authored concurrently by other owners. Wrap every new
# import so a missing module or missing optional dep can never break
# ``import skyn3t.integrations`` — the channel simply isn't exported.

# Western: Home Assistant + SMS (this owner: CHANNELS_WESTERN_SMS).
try:
    from skyn3t.integrations.channel_homeassistant import HomeAssistantChannel

    __all__.append("HomeAssistantChannel")
except Exception:  # pragma: no cover - optional/degraded import
    HomeAssistantChannel = None  # type: ignore[assignment, misc]

try:
    from skyn3t.integrations.channel_sms import SmsChannel

    __all__.append("SmsChannel")
except Exception:  # pragma: no cover - optional/degraded import
    SmsChannel = None  # type: ignore[assignment, misc]

# Asia: DingTalk / WeCom / WeChat / Line / KakaoTalk (owner: CHANNELS_ASIA).
# These modules may not exist yet (concurrent authoring); guard each.
try:
    from skyn3t.integrations.channel_dingtalk import DingTalkChannel

    __all__.append("DingTalkChannel")
except Exception:  # pragma: no cover - optional/degraded import
    DingTalkChannel = None  # type: ignore[assignment, misc]

try:
    from skyn3t.integrations.channel_wecom import WeComChannel

    __all__.append("WeComChannel")
except Exception:  # pragma: no cover - optional/degraded import
    WeComChannel = None  # type: ignore[assignment, misc]

try:
    from skyn3t.integrations.channel_wechat import WeChatChannel

    __all__.append("WeChatChannel")
except Exception:  # pragma: no cover - optional/degraded import
    WeChatChannel = None  # type: ignore[assignment, misc]

try:
    from skyn3t.integrations.channel_line import LineChannel

    __all__.append("LineChannel")
except Exception:  # pragma: no cover - optional/degraded import
    LineChannel = None  # type: ignore[assignment, misc]

try:
    from skyn3t.integrations.channel_kakaotalk import KakaoTalkChannel

    __all__.append("KakaoTalkChannel")
except Exception:  # pragma: no cover - optional/degraded import
    KakaoTalkChannel = None  # type: ignore[assignment, misc]
