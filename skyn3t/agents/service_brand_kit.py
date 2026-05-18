"""Per-service brand-kit lookup.

When the brief names third-party services we're going to integrate
with, the LLM has no idea what those services actually LOOK like.
It defaults to "show data as JSON" because it has nothing else to
go on. Result: a homelab dashboard that says "Sonarr" in plain
text where Homarr/Heimdall would show Sonarr's actual logo and
blue color.

This module supplies the missing visual context. Each known service
gets a small kit: icon URL (simple-icons CDN), brand color, and a
WIDGET HINT — a one-line description of what UI shape a user
expects to see for that service. The brand kit is fed into
CodeAgent's prompt for visual files so the model can build
service-shaped widgets instead of generic JSON cards.

Coverage focus: homelab/media-stack services. Easy to extend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ServiceBrand:
    slug: str
    name: str           # display name
    icon_url: str       # CDN URL for an SVG logo
    color: str          # primary brand color, hex
    widget: str         # one-line widget shape hint for the UI
    category: str       # "media-server" / "downloader" / "audio" / etc.


# Icons from simple-icons.org — public-domain SVGs accessible at
# https://cdn.simpleicons.org/<slug>. Where simple-icons doesn't
# have a service, we fall back to dashboard-icons.dev which covers
# more homelab apps.
_DASH_ICONS = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/svg"
_SIMPLE = "https://cdn.simpleicons.org"


_BRAND_CATALOG: Dict[str, ServiceBrand] = {
    "sonarr": ServiceBrand(
        slug="sonarr", name="Sonarr",
        icon_url=f"{_DASH_ICONS}/sonarr.svg",
        color="#35C5F0",  # Sonarr cyan
        widget=(
            "Download-queue table: 5–8 rows max, columns = title, size, "
            "ETA, progress bar (horizontal, brand color), status. Truncate "
            "long titles with ellipsis. Empty state: 'No downloads queued.'"
        ),
        category="downloader",
    ),
    "radarr": ServiceBrand(
        slug="radarr", name="Radarr",
        icon_url=f"{_DASH_ICONS}/radarr.svg",
        color="#FFC230",  # Radarr yellow
        widget=(
            "Same shape as Sonarr (download queue table) but movies, not "
            "episodes. Columns: title, year, size, ETA, progress."
        ),
        category="downloader",
    ),
    "prowlarr": ServiceBrand(
        slug="prowlarr", name="Prowlarr",
        icon_url=f"{_DASH_ICONS}/prowlarr.svg",
        color="#E66E2D",  # Prowlarr orange
        widget=(
            "Indexer-status grid: small pills, one per indexer, each "
            "colored green/yellow/red for health. Below: total indexers "
            "enabled, queries today, fail-rate percentage. Compact."
        ),
        category="indexer",
    ),
    "qbittorrent": ServiceBrand(
        slug="qbittorrent", name="qBittorrent",
        icon_url=f"{_DASH_ICONS}/qbittorrent.svg",
        color="#406EBC",  # qBit blue
        widget=(
            "Active torrents list: rows of name + horizontal progress bar "
            "+ down/up speed + state pill. Header shows aggregate dl/ul "
            "kB/s, peers, ratio. Cap rows to 5–8."
        ),
        category="downloader",
    ),
    "emby": ServiceBrand(
        slug="emby", name="Emby",
        icon_url=f"{_DASH_ICONS}/emby.svg",
        color="#52B54B",  # Emby green
        widget=(
            "Now-playing tile: poster image (100×140 from "
            "/Items/{id}/Images/Primary), title + episode, user, device, "
            "transcode/direct-play badge, horizontal scrub bar showing "
            "position/runtime. Empty state: 'No active sessions.'"
        ),
        category="media-server",
    ),
    "jellyfin": ServiceBrand(
        slug="jellyfin", name="Jellyfin",
        icon_url=f"{_DASH_ICONS}/jellyfin.svg",
        color="#00A4DC",  # Jellyfin blue
        widget=(
            "Same shape as Emby (now-playing tile with poster + scrub) "
            "but pull from Jellyfin's /Sessions endpoint."
        ),
        category="media-server",
    ),
    "plex": ServiceBrand(
        slug="plex", name="Plex",
        icon_url=f"{_DASH_ICONS}/plex.svg",
        color="#E5A00D",  # Plex orange/yellow
        widget=(
            "Same shape as Emby/Jellyfin (now-playing with poster + scrub) "
            "from /status/sessions, /transcode/sessions."
        ),
        category="media-server",
    ),
    "sonos": ServiceBrand(
        slug="sonos", name="Sonos",
        icon_url=f"{_DASH_ICONS}/sonos.svg",
        color="#000000",  # Sonos uses black/white
        widget=(
            "Per-zone row: zone name, album art thumbnail (60×60), "
            "track title + artist, transport controls (play/pause/next/"
            "prev as icon buttons), volume slider. Multiple zones stack "
            "vertically. Empty state: 'No active zones.'"
        ),
        category="audio",
    ),
    "docker": ServiceBrand(
        slug="docker", name="Docker",
        icon_url=f"{_DASH_ICONS}/docker.svg",
        color="#2496ED",  # Docker blue
        widget=(
            "Container list: one row per container with name (truncated), "
            "image, state pill (running=green, exited=gray), CPU% gauge "
            "(thin horizontal bar), memory used / limit. 5–10 rows visible, "
            "scroll for more."
        ),
        category="infra",
    ),
    "home_assistant": ServiceBrand(
        slug="home_assistant", name="Home Assistant",
        icon_url=f"{_DASH_ICONS}/home-assistant.svg",
        color="#41BDF5",
        widget=(
            "Entity summary: counts per domain (lights X on, sensors Y, "
            "automations Z) as small stat tiles. Optionally a list of "
            "recently-changed entities below."
        ),
        category="iot",
    ),
    "pihole": ServiceBrand(
        slug="pihole", name="Pi-hole",
        icon_url=f"{_DASH_ICONS}/pi-hole.svg",
        color="#96060C",  # Pi-hole red
        widget=(
            "Big stat tiles: queries today, blocked today, block percentage, "
            "domains on blocklist. One sparkline of queries-per-minute for "
            "the last hour."
        ),
        category="network",
    ),
    "unifi": ServiceBrand(
        slug="unifi", name="UniFi",
        icon_url=f"{_DASH_ICONS}/unifi.svg",
        color="#0559C9",
        widget=(
            "Network summary: clients online, WAN status (up/down), AP "
            "count, total throughput dl/ul. List of recently-connected "
            "clients optional."
        ),
        category="network",
    ),
    "transmission": ServiceBrand(
        slug="transmission", name="Transmission",
        icon_url=f"{_DASH_ICONS}/transmission.svg",
        color="#D70008",
        widget=(
            "Same shape as qBittorrent (torrent list with progress bars + "
            "speed totals)."
        ),
        category="downloader",
    ),
    "sabnzbd": ServiceBrand(
        slug="sabnzbd", name="SABnzbd",
        icon_url=f"{_DASH_ICONS}/sabnzbd.svg",
        color="#FFBE2D",
        widget=(
            "Usenet queue: rows of name + size + ETA + progress, header "
            "shows speed and queue total. Same shape family as qBit."
        ),
        category="downloader",
    ),
    "overseerr": ServiceBrand(
        slug="overseerr", name="Overseerr",
        icon_url=f"{_DASH_ICONS}/overseerr.svg",
        color="#5460DF",
        widget=(
            "Request queue: pending requests with poster thumbnail, title, "
            "requester, status pill (pending/approved/available)."
        ),
        category="request",
    ),
    "tautulli": ServiceBrand(
        slug="tautulli", name="Tautulli",
        icon_url=f"{_DASH_ICONS}/tautulli.svg",
        color="#DBA827",
        widget=(
            "Plex activity dashboard: bandwidth tile, transcodes count, "
            "stream count by quality, recent history list."
        ),
        category="analytics",
    ),
    "nzbget": ServiceBrand(
        slug="nzbget", name="NZBGet",
        icon_url=f"{_DASH_ICONS}/nzbget.svg",
        color="#1FA465",
        widget=(
            "Usenet download queue: same shape family as SABnzbd."
        ),
        category="downloader",
    ),
}


def brand_for(slug: str) -> Optional[ServiceBrand]:
    """Return the brand kit for a service slug, or None if unknown."""
    if not slug:
        return None
    return _BRAND_CATALOG.get(slug.lower())


def brand_kit_markdown(slugs: List[str]) -> str:
    """Render a markdown block describing the visual shape per service.

    Designed to be appended to CodeAgent's prompt when writing
    visual files (App.jsx, components/*.jsx). Tells the model
    what icon, color, and widget shape each service expects so it
    builds product-shaped widgets, not generic JSON-dump cards.
    """
    if not slugs:
        return ""
    blocks: List[str] = ["## Service brand kit\n",
                         "These are the integrations the dashboard talks to. "
                         "Use the icon URL, brand color, and widget shape "
                         "per service — DO NOT render service data as a "
                         "generic JSON dump. Build the service-specific "
                         "widget shape described below.\n"]
    for slug in slugs:
        b = brand_for(slug)
        if b is None:
            continue
        blocks.append(
            f"### {b.name}\n"
            f"- icon: `{b.icon_url}` (SVG, render via `<img src=... height={{20}} />`)\n"
            f"- brand color: `{b.color}` (use for accents — progress bars, status dots, badges)\n"
            f"- category: {b.category}\n"
            f"- widget shape: {b.widget}\n"
        )
    return "\n".join(blocks) + "\n"


def known_slugs() -> List[str]:
    return sorted(_BRAND_CATALOG.keys())
