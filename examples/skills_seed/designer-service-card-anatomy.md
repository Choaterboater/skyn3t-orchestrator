---
name: designer-service-card-anatomy
tags: [designer, code_agent, dashboard, ui-pattern, react, service-card]
success_count: 1
failure_count: 0
last_used_at: 1778600000.0
source: hand-curated:seed-skill
created_at: 1778600000.0
---

# Service-card anatomy for homelab / status dashboards

A service card on a homelab dashboard (Homarr / Heimdall / our scaffold)
has a CONSISTENT five-zone anatomy, not a free-form box. The LLM
defaults to "render data as JSON in a card." That ships as
unreadable. This is the right shape, top to bottom:

## Zones

1. **Header row** (height 36–44px, no border)
   - LEFT: 24×24 service icon (brand color or actual SVG logo) in a
     rounded square with 8% brand-color fill background.
   - LEFT-NEXT: service title (14px, font-weight 600). Below it: the
     host or url at 11px, color var(--text-dim).
   - RIGHT: status pill — `{dot} {label}` where dot is a 6px circle
     in green/amber/red, label is `Online`/`Warning`/`Offline` in
     10px uppercase tracking-wider. Pill background is the dot color
     at 12% opacity, border is the dot color at 24% opacity.

2. **Stat row** (height ~20px, just below header)
   - One line of CONCRETE service-specific facts, comma-separated:
     - Plex: `4 Libraries · 12.4K items`
     - Sonarr: `1 Missing · 24 monitored`
     - qBittorrent: `↓ 78.4 MB/s · ↑ 12.1 MB/s · 8 active`
     - Docker: `18 containers · 17 running · 1 stopped`
   - Tabular numbers (`font-variant-numeric: tabular-nums`).
   - 12px, color var(--text), NOT var(--text-dim). These are the
     important numbers.

3. **Sparkline / preview** (height 40–60px, optional but powerful)
   - A tiny SVG sparkline (recharts or hand-rolled `<polyline>`) of
     a relevant 1h time series in the service's brand color.
   - 1.5px stroke, no fill — minimal.
   - Falls back to a grayed "no recent data" line if no series.

4. **Action row** (height 28px, top border 1px var(--border))
   - LEFT: three small icon buttons — open (↗), refresh (⟳), settings (⚙)
   - Each is 14px icon in a 24×24 transparent button, hover fills with
     var(--panel-2). NO labels. Tooltip on hover.
   - RIGHT: `{N}{unit} ago` last-checked stamp, 10px, var(--text-dim).
     Examples: `2m ago`, `30s ago`, `Just now`.

5. **Edges + spacing**
   - Card: 12px border-radius, 16px padding, 1px var(--border).
   - Soft shadow `0 1px 3px rgba(0,0,0,0.3)`. On hover, border lightens
     and shadow grows slightly.
   - Background `var(--panel)` (one step lighter than the page bg).

## What NOT to do

- Don't dump JSON.stringify in the card. EVER. If the proxy returns
  raw data, transform it into the stat-row line at fetch time.
- Don't render service-specific fields the user doesn't care about
  (uuid, internal flags, full URLs). Only the headline numbers.
- Don't use rainbow per-card backgrounds. The brand color goes on the
  icon (8% fill), the sparkline (full color stroke), and the status
  dot. The card chrome stays neutral.

## Reference brands (use these exact colors)

- Sonarr `#35C5F0` (cyan), Radarr `#FFC230` (yellow)
- Prowlarr `#E66E2D` (orange), qBittorrent `#406EBC` (blue)
- Emby `#52B54B` (green), Jellyfin `#00A4DC` (blue)
- Plex `#E5A00D` (orange), Sonos `#000` (black on white)
- Docker `#2496ED` (blue), Pi-hole `#96060C` (red)
- Home Assistant `#41BDF5` (sky), UniFi `#0559C9` (deep blue)
