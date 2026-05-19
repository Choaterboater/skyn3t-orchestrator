# Telegram control surface — setup

A 100% mobile-friendly way to drive SkyN3t: kick off projects, watch stages stream in real time, approve / reject / view status, all from Telegram chat. No public URL, no tunnel, no webhooks — the bot opens an outbound long-poll connection to Telegram, so it works behind firewalls and on localhost.

## What you can do once it's wired up

- **DM the bot** in plain English: `build a homelab dashboard`, `status`, `approve`, `reject the palette is wrong`, `list`.
- **Slash commands**: `/build`, `/status`, `/approve`, `/reject`, `/list`, `/help`.
- **Tap-to-approve buttons** on every approval notification: ✅ Approve, ❌ Reject, 📊 Status, 🔗 Open dashboard.
- **Watch stages stream in real time** — each stage (brainstorm → research → architect → designer → code → reviewer) posts a reply in your project's chat thread when it starts and finishes.
- **Final score and verdict** land in the same thread when the project completes.

## Auth scope

Solo DM mode. The bot only responds to your Telegram user ID (`SKYN3T_TELEGRAM_USER_ID`). Anyone else who finds the bot will be silently ignored.

## One-time setup

### 1. Create the bot with BotFather

1. Open Telegram → search for `@BotFather` (the verified one with a blue check).
2. Send `/newbot`.
3. Pick a display name (e.g. `SkyN3t Studio`).
4. Pick a username — must end in `bot`, e.g. `your_skyn3t_bot`. Try variations if taken.
5. BotFather replies with your bot's token:
   ```
   Use this token to access the HTTP API:
   1234567890:AAH-xxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
6. Copy the token.

### 2. Get your Telegram user ID

1. In Telegram, search for `@userinfobot` (verified).
2. Send any message to start it. It immediately replies with your user ID (an integer like `123456789`).
3. Copy the ID.

### 3. Set env vars

Add to your `.env`:

```bash
SKYN3T_TELEGRAM_TOKEN=1234567890:AAH-xxxxxxxxxxxxxxxxxxxxxxxxxxxx
SKYN3T_TELEGRAM_USER_ID=123456789
```

### 4. Restart the SkyN3t server

```bash
uvicorn skyn3t.web.app:app --host 0.0.0.0 --port 6660
```

Watch the logs for `Telegram bot starting in background`. Within a few seconds the bot is connected.

### 5. Test it

Open your bot in Telegram (just search for the username you picked) and tap **Start** (or send `/start`). It should reply with the help text.

Then try:
- `build a small todo app`
- After ~5 minutes, an approval notification arrives with inline buttons
- Tap **✅ Approve** — pipeline resumes
- ~10 minutes later, the project completion + final score lands in the same chat

## DM-only by design

The current bot is solo-DM. To allow more users later, change `_is_authorized` in `skyn3t/integrations/telegram_bot.py` to check an allowlist. Or move to a group-chat model where any member of the group can issue commands.

## Why long-polling beats webhooks here

Telegram is one of the few platforms that gives you both options:

| | Webhook (requires public URL) | Long-poll (outbound only) |
|---|---|---|
| Needs Cloudflare Tunnel / ngrok? | Yes | No |
| Behind firewall? | Breaks | Works |
| Latency | ~50 ms | ~100 ms |
| Reliability | Discord-style fragile | Self-healing on disconnect |
| Setup time | 15 min | 30 sec |

We picked long-poll. Nothing in this setup requires a public hostname.

## Troubleshooting

**Bot is online but doesn't reply to me.**
- Check `SKYN3T_TELEGRAM_USER_ID` matches your actual ID exactly (from `@userinfobot`).
- The bot silently ignores unauthorized senders — that's by design.

**`/start` works but `build a thing` doesn't kick off a project.**
- The server logs should show what happened. Most likely the studio runner couldn't reach the LLM (missing `OPENAI_API_KEY` or similar).

**The bot doesn't start at server boot.**
- Both `SKYN3T_TELEGRAM_TOKEN` and `SKYN3T_TELEGRAM_USER_ID` must be set. Missing either → bot doesn't start, silently.

**I want to stop the bot.**
- Just unset the env var and restart the server. No cleanup needed on Telegram's side.

**I want to start over with a fresh bot.**
- DM `@BotFather` → `/deletebot` → pick the bot. Then re-run setup with a new bot.
