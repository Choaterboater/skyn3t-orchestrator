"""Preset Studio briefs — shared by the web dashboard and CLI."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

STUDIO_EXAMPLES: List[Dict[str, Any]] = [
    {
        "id": "redesign-dashboard",
        "title": "Redesign this dashboard",
        "subtitle": "Sweep across the UI to refine spacing, typography, and color",
        "icon": "fa-palette",
        "template": "frontend_redesign",
        "brief": (
            "Redesign skyn3t/web/dashboard.html — refine spacing, typography hierarchy, "
            "and visual consistency. Polish forms, cards, and the swarm map. Keep all "
            "DOM IDs and JS handlers intact."
        ),
    },
    {
        "id": "choatelab-homelab",
        "title": "ChoateLab homelab dashboard",
        "subtitle": "Homarr-style Docker dashboard · UI config · port 6969",
        "icon": "fa-server",
        "template": "auto",
        "brief": (
            "Build ChoateLab — a self-hosted homelab dashboard inspired by Homarr but "
            "easier to configure entirely from the UI (no env-file editing).\n\n"
            "PRODUCT (MVP v1 — must work):\n"
            "- Brand name ChoateLab everywhere (logo text must say ChoateLab)\n"
            "- Service grid with cards for Plex, Sonarr, Radarr, qBittorrent, Pi-hole, "
            "Portainer, Uptime Kuma, Home Assistant\n"
            "- Each card: brand icon, status pill, open link, last-checked stamp\n"
            "- Click card → right-side drawer with stats and actions\n"
            "- Settings: add/edit services (URL, API key), test connection, persist to disk\n"
            "- Integration picker: prebuilt templates + custom API entries\n"
            "- Cmd+K palette, KPI strip (services online), dark theme + light toggle\n\n"
            "TECH (non-negotiable — all artifacts must agree):\n"
            "- React 18 + Vite + Tailwind in scaffold/\n"
            "- Node 20 + Express in server/ with JSON or sqlite config store\n"
            "- docker-compose.yml exposes host port 6969 (NOT 3000 or 5173)\n"
            "- Dockerfile uses node:20-alpine — NOT Python\n"
            "- npm run dev AND docker compose up must succeed\n"
            "- Full runnable scaffold/ source — NOT docs-only output\n\n"
            "DO NOT: skip CodeAgent, Python Dockerfile, or contradictory stack files."
        ),
    },
    {
        "id": "habit-tracker",
        "title": "Build a habit tracker app",
        "subtitle": "Today-first React app · localStorage · streaks · dark theme",
        "icon": "fa-circle-check",
        "template": "auto",
        "brief": (
            "Build a personal daily habit tracker as a single-page web app.\n\n"
            "PRODUCT (what users want):\n"
            "- Today-first UX: show what is left today; unchecked habits on top\n"
            "- One-tap daily check-in per habit (toggle on/off)\n"
            "- Streaks with forgiving logic (yesterday still counts until checked today)\n"
            "- Starter suggestions on empty state (Move, Read, Water, Sleep, etc.)\n"
            "- Celebrate milestones at 3/7/14/30 days; message when all habits done\n"
            "- Block duplicate habit names and names under 2 characters\n\n"
            "TECH (non-negotiable):\n"
            "- React 18 + Vite + Tailwind CSS only\n"
            "- Proper tailwind.config.js, postcss.config.js, src/index.css with @tailwind; "
            "import index.css from main.jsx\n"
            "- localStorage only — NO backend, NO Express/FastAPI, NO database, "
            "NO Docker, NO docker-compose\n"
            "- Dark theme default + light mode toggle (class strategy)\n"
            "- Brand: purple/teal accents on #1A1A2E background\n"
            "- npm run dev and npm run build must succeed\n\n"
            "DO NOT: server folder, Tailwind CDN injection, conflicting stack in "
            "architecture vs code, or split data models between App and hooks."
        ),
    },
    {
        "id": "marketing-launch",
        "title": "Marketing campaign for a SaaS launch",
        "subtitle": "Positioning + channel plan + landing copy + checklist",
        "icon": "fa-bullhorn",
        "template": "auto",
        "brief": (
            "Build a launch campaign for a new AI-powered code reviewer tool. Audience: "
            "senior engineers and engineering managers. Channels: Hacker News, X/Twitter, "
            "dev podcasts. Include positioning, channel plan, and a launch-day checklist."
        ),
    },
    {
        "id": "brand-kit",
        "title": "Generate a brand kit",
        "subtitle": "Palette + typography + voice + logo concepts",
        "icon": "fa-paintbrush",
        "template": "brand_kit",
        "brief": (
            "Create a brand kit for an open-source dev tool called 'Skyn3t' — autonomous "
            "multi-agent orchestration. Aesthetic: military HUD meets modern dev tool. "
            "Dark, technical, slightly menacing but trustworthy."
        ),
    },
    {
        "id": "ingest-repo",
        "title": "Ingest a GitHub repo into RAG",
        "subtitle": "Pull docs from any repo so the swarm can reference it",
        "icon": "fa-database",
        "template": "auto",
        "brief": (
            "Ingest the GitHub repo openai/openai-cookbook into our RAG. Pull README, "
            "examples/, and docs. Tag as kind=reference. Then summarize what topics were covered."
        ),
    },
    {
        "id": "audit-codebase",
        "title": "Audit this codebase",
        "subtitle": "Surface risks, dead code, and improvement priorities",
        "icon": "fa-magnifying-glass",
        "template": "auto",
        "brief": (
            "Audit the skyn3t/ Python package. Identify dead code, unused imports, modules "
            "that have grown too large, and files with high failure rates from the recent "
            "project history. Produce review.md with prioritized recommendations."
        ),
    },
    {
        "id": "business-plan",
        "title": "Write a business plan",
        "subtitle": "Market scan + revenue model + 10-slide pitch outline",
        "icon": "fa-chart-line",
        "template": "business_plan",
        "brief": (
            "A B2B AI-powered scheduling assistant for sales teams that reads CRM context "
            "and proposes meeting times. Subscription model. Target: mid-market SaaS sales "
            "leaders. Produce market scan, business model, and a 10-slide pitch."
        ),
    },
]


def list_studio_examples() -> List[Dict[str, Any]]:
    """Return preset Studio briefs for the dashboard and CLI."""
    return list(STUDIO_EXAMPLES)


def get_studio_example(example_id: str) -> Optional[Dict[str, Any]]:
    """Look up a preset by id."""
    needle = str(example_id or "").strip().lower()
    if not needle:
        return None
    for item in STUDIO_EXAMPLES:
        if str(item.get("id") or "").lower() == needle:
            return dict(item)
    return None
