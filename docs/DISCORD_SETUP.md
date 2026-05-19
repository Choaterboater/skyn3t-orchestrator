# Discord control surface — setup

SkyN3t can be driven entirely from Discord: start projects, check status,
approve or reject architecture from chat or via tap-to-approve buttons.

## What you can do once it's wired up

- **DM the bot** in plain English: `build a homelab dashboard`, `status canary-150`, `approve`, `reject canary-150 the palette is wrong`, `list`.
- **Slash commands**: `/skyn3t-start`, `/skyn3t-status`, `/skyn3t-approve`, `/skyn3t-reject`, `/skyn3t-list`.
- **Tap-to-approve buttons** in approval notifications: Approve, Reject (opens a feedback modal), and Open dashboard.

## Auth scope

Anyone in your Discord server can use the bot. The trust boundary is who you invite to the server. Per-user rate limit: 5 commands / minute.

## One-time setup

### 1. Create the Discord app

1. Go to <https://discord.com/developers/applications> and create an app.
2. Copy **Application ID** and **Public Key** from the General Information page.
3. Under **Bot**, create a bot user and copy the **Bot Token** (Reset Token if you forgot it).

### 2. Invite the bot

Under **OAuth2 → URL Generator**:

- Scopes: `bot`, `applications.commands`
- Bot Permissions: `Send Messages`, `Embed Links`, `Use Application Commands`

Visit the generated URL, pick your server, authorize.

### 3. Find the channel id (for outbound notifications)

In Discord, enable Developer Mode (Settings → Advanced → Developer Mode), right-click your target channel → **Copy Channel ID**.

### 4. Set environment variables

```bash
export SKYN3T_DISCORD_APP_ID=...           # from Application ID
export SKYN3T_DISCORD_PUBLIC_KEY=...       # from Public Key
export DISCORD_TOKEN=...                   # from Bot Token
export SKYN3T_DISCORD_CHANNEL_ID=...       # channel where approval notifications post
export SKYN3T_DISCORD_ADMIN_SECRET=...     # random string — gates /api/discord/register-commands
```

Add them to your `.env` if you use one. Restart the SkyN3t server.

### 5. Expose the interactions endpoint

Slash commands and button presses arrive as HTTPS webhooks; your server needs a public URL. For local testing:

```bash
# Cloudflare Tunnel (no signup)
cloudflared tunnel --url http://localhost:6660
# or ngrok
ngrok http 6660
```

Note the assigned `https://*.trycloudflare.com` (or `https://*.ngrok.io`) URL.

### 6. Configure the Interactions Endpoint URL

In the Discord developer portal, on your app's **General Information** page, set **Interactions Endpoint URL** to:

```
https://<your-tunnel-or-host>/api/discord/interactions
```

Discord verifies the endpoint by sending a signed `type: 1` PING. If your server is running, it returns `type: 1` (PONG) and Discord saves the URL. If verification fails, double-check that `SKYN3T_DISCORD_PUBLIC_KEY` matches the Public Key shown above the Interactions field.

### 7. Register the slash commands

One-shot, idempotent — safe to re-run any time:

```bash
curl -X POST \
  -H "X-Skyn3t-Admin: $SKYN3T_DISCORD_ADMIN_SECRET" \
  https://<your-host>/api/discord/register-commands
```

You should see a JSON response listing the five registered commands. They appear in your Discord client within a minute.

### 8. Verify

In your server, type `/skyn3t-` — autocomplete should show the five commands. DM the bot `help` and it should reply with the command list.

Run `/skyn3t-start brief:"build a small todo app"` and you should see a `🚀 Started ...` reply. ~5 minutes later, an embed with Approve / Reject buttons should land in the channel you configured.

## DM-only mode (no public URL)

If you can't expose a public URL yet, you can still ship Phase 1: DM the bot. The Gateway connection from bot → Discord is outbound, so DMs and `@mentions` work without an interactions endpoint. Slash commands and button presses require the endpoint.

## Health check

```bash
curl https://<your-host>/api/discord/status
```

Returns which of the four settings are configured and how many commands are registered.

## Tightening auth later

The current model is "anyone in the server." To restrict further, edit `data/approval_gates.json`:

```json
{
  "discord": {
    "allowed_user_ids": ["123456789012345678", "987654321098765432"]
  }
}
```

(This allowlist isn't wired into v1 — the file just documents the future shape.)
