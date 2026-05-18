from __future__ import annotations

import pytest

from skyn3t.core.events import EventBus
from skyn3t.studio.runner import StudioRunner


@pytest.mark.asyncio
async def test_thread_update_skips_terminal_manifest_without_force(tmp_path, monkeypatch):
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    calls: list[tuple[str, str, str]] = []

    async def fake_post_to_thread(thread_id: str, content: str) -> None:
        calls.append(("discord", thread_id, content))

    async def fake_post_thread_reply(message_id: int, content: str) -> bool:
        calls.append(("telegram", str(message_id), content))
        return True

    monkeypatch.setattr("skyn3t.studio.notify_dispatcher.post_to_thread", fake_post_to_thread)
    monkeypatch.setattr(
        "skyn3t.integrations.telegram_dispatch.post_thread_reply",
        fake_post_thread_reply,
    )

    manifest = {
        "slug": "demo",
        "status": "needs_fixes",
        "completed_at": 123.0,
        "discord": {"thread_id": "thread-1"},
        "telegram": {"starter_message_id": 271},
    }

    await runner._post_discord_thread_update(manifest, "done already")

    assert calls == []


@pytest.mark.asyncio
async def test_thread_update_force_allows_terminal_completion_post(tmp_path, monkeypatch):
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    calls: list[tuple[str, str, str]] = []

    async def fake_post_to_thread(thread_id: str, content: str) -> None:
        calls.append(("discord", thread_id, content))

    async def fake_post_thread_reply(message_id: int, content: str) -> bool:
        calls.append(("telegram", str(message_id), content))
        return True

    monkeypatch.setattr("skyn3t.studio.notify_dispatcher.post_to_thread", fake_post_to_thread)
    monkeypatch.setattr(
        "skyn3t.integrations.telegram_dispatch.post_thread_reply",
        fake_post_thread_reply,
    )

    manifest = {
        "slug": "demo",
        "status": "needs_fixes",
        "completed_at": 123.0,
        "discord": {"thread_id": "thread-1"},
        "telegram": {"starter_message_id": 271},
    }

    await runner._post_discord_thread_update(manifest, "final status", force=True)

    assert calls == [
        ("discord", "thread-1", "final status"),
        ("telegram", "271", "final status"),
    ]


@pytest.mark.asyncio
async def test_thread_update_dedupes_repeated_content(tmp_path, monkeypatch):
    runner = StudioRunner(event_bus=EventBus(), projects_root=tmp_path / "projects")
    calls: list[tuple[str, str, str]] = []

    async def fake_post_to_thread(thread_id: str, content: str) -> None:
        calls.append(("discord", thread_id, content))

    async def fake_post_thread_reply(message_id: int, content: str) -> bool:
        calls.append(("telegram", str(message_id), content))
        return True

    monkeypatch.setattr("skyn3t.studio.notify_dispatcher.post_to_thread", fake_post_to_thread)
    monkeypatch.setattr(
        "skyn3t.integrations.telegram_dispatch.post_thread_reply",
        fake_post_thread_reply,
    )

    manifest = {
        "slug": "demo",
        "status": "running",
        "completed_at": None,
        "discord": {"thread_id": "thread-1"},
        "telegram": {"starter_message_id": 271},
    }

    await runner._post_discord_thread_update(manifest, "same update")
    await runner._post_discord_thread_update(manifest, "same update")

    assert calls == [
        ("discord", "thread-1", "same update"),
        ("telegram", "271", "same update"),
    ]
