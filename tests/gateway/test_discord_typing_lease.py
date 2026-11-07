"""Regression tests for Discord's persistent typing lease guard."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.discord import adapter as discord_mod
from plugins.platforms.discord.adapter import DiscordAdapter


def _adapter() -> DiscordAdapter:
    adapter = object.__new__(DiscordAdapter)
    adapter._client = SimpleNamespace(
        http=SimpleNamespace(request=AsyncMock(return_value=None))
    )
    adapter._typing_tasks = {}
    adapter._typing_lease = {}
    return adapter


@pytest.mark.asyncio
async def test_stale_typing_loop_self_terminates_and_cleans_state(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(discord_mod, "_TYPING_LOOP_STALE_SECS", 0.02)

    await adapter.send_typing("123")
    task = adapter._typing_tasks["123"]
    await asyncio.wait_for(task, timeout=0.5)

    adapter._client.http.request.assert_awaited_once()
    assert "123" not in adapter._typing_tasks
    assert "123" not in adapter._typing_lease


@pytest.mark.asyncio
async def test_refresh_reuses_task_and_stop_cleans_lease(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(discord_mod, "_TYPING_LOOP_STALE_SECS", 1.0)

    await adapter.send_typing("456")
    first_task = adapter._typing_tasks["456"]
    first_lease = adapter._typing_lease["456"]
    await asyncio.sleep(0)
    await adapter.send_typing("456")

    assert adapter._typing_tasks["456"] is first_task
    assert adapter._typing_lease["456"] >= first_lease

    await adapter.stop_typing("456")
    assert first_task.done()
    assert "456" not in adapter._typing_tasks
    assert "456" not in adapter._typing_lease


@pytest.mark.asyncio
async def test_stop_typing_does_not_hang_when_request_suppresses_cancel(monkeypatch):
    adapter = _adapter()
    started = asyncio.Event()
    release = asyncio.Event()

    async def stubborn_request(_route):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await release.wait()

    adapter._client.http.request = AsyncMock(side_effect=stubborn_request)
    monkeypatch.setattr(discord_mod, "_TYPING_STOP_TIMEOUT_SECS", 0.02)

    await adapter.send_typing("789")
    task = adapter._typing_tasks["789"]
    await asyncio.wait_for(started.wait(), timeout=0.5)
    await asyncio.wait_for(adapter.stop_typing("789"), timeout=0.2)

    assert not task.done()
    assert "789" not in adapter._typing_tasks
    assert "789" not in adapter._typing_lease

    release.set()
    await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_rate_limit_backoff_cannot_outlive_stale_lease(monkeypatch):
    adapter = _adapter()

    class RateLimited(Exception):
        retry_after = 5.0

    adapter._client.http.request = AsyncMock(side_effect=RateLimited())
    monkeypatch.setattr(discord_mod, "_TYPING_LOOP_STALE_SECS", 0.02)

    await adapter.send_typing("999")
    task = adapter._typing_tasks["999"]
    await asyncio.wait_for(task, timeout=0.5)

    adapter._client.http.request.assert_awaited_once()
    assert "999" not in adapter._typing_tasks
    assert "999" not in adapter._typing_lease