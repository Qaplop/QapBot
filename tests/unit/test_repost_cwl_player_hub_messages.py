"""Repost lifecycle for the Player CWL Settings Hub (plans/cwl-personal-hub.md Phase 3).

No prior test coverage existed for repost_anchored_message()'s generic lifecycle at all (verified
by search — repost_cwl_management_messages/repost_playerregistration_messages have none either),
despite the plan text assuming there was some to extend. These are new tests written directly
against repost_cwl_player_hub_messages(), covering the three behaviors the plan named: delete on
disable, channel-migration only after a Discord-confirmed delete, and the cooldown-gated bump —
plus a plain fresh-post case.

QapBot's module-level code only configures logging and defines functions (the bot itself starts
under `if __name__ == "__main__"`), so importing it here is side-effect-light — same pattern
tests/unit/test_periodic_main_control.py already uses.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false
from __future__ import annotations

import dataclasses
import os
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
import QapBot  # noqa: E402
import QBcore  # noqa: E402
from qapbot.cache_manager import CACHE  # noqa: E402


def _fake_channel(*, guild_name: str = "TestGuild", last_message_id: int = None):
    """A MagicMock(spec=discord.TextChannel) so repost_anchored_message()'s isinstance() checks
    pass, with send()/fetch_message()/history() wired to plausible async behavior."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.guild = MagicMock()
    channel.guild.name = guild_name
    channel.name = "cwl-player-hub"

    new_message = MagicMock()
    new_message.id = 999999
    channel.send = AsyncMock(return_value=new_message)

    old_message = MagicMock()
    old_message.delete = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=old_message)

    async def _history(*_a, **_k):
        if last_message_id is not None:
            msg = MagicMock()
            msg.id = last_message_id
            yield msg

    channel.history = _history
    return channel


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch):
    """Every test gets a clean CACHE.server_config and a no-op persist (this is a control-flow
    test of the repost driver, not of the real DB write-through — Phase 1's own tests already
    cover that)."""
    monkeypatch.setattr(CACHE, "server_config", {})
    monkeypatch.setattr(CACHE, "persist_server_config", AsyncMock())
    # QapBot.py did `from qapbot.config import CONFIG` at module import time, so it holds its
    # own name binding — patching qapbot.config.CONFIG wouldn't reach it; QapBot's own binding
    # must be replaced directly. CONFIG is a frozen dataclass, hence dataclasses.replace().
    monkeypatch.setattr(QapBot, "CONFIG", dataclasses.replace(QapBot.CONFIG, is_dev_mode=False))


@pytest.mark.asyncio
async def test_disabled_hub_with_tracked_message_deletes_it(monkeypatch):
    channel = _fake_channel()
    fake_bot = MagicMock()
    fake_bot.get_channel = MagicMock(return_value=channel)
    monkeypatch.setattr(QBcore, "bot", fake_bot)

    CACHE.server_config["701"] = {
        "cwl_player_hub_message_enabled": False,
        "cwl_player_hub_channel_id": "123456",
        "cwl_player_hub_message_id": "555111",
    }

    await QapBot.repost_cwl_player_hub_messages()

    config = CACHE.server_config["701"]
    assert config["cwl_player_hub_message_id"] is None
    channel.fetch_message.assert_awaited_once_with(555111)


@pytest.mark.asyncio
async def test_disabled_hub_keeps_tracking_on_inconclusive_delete(monkeypatch):
    """An unconfirmed delete (channel lookup fails entirely) must not drop tracking — Pitfall 15:
    losing the message ID while the message might still be live is how a duplicate happens."""
    fake_bot = MagicMock()
    fake_bot.get_channel = MagicMock(return_value=None)
    fake_bot.fetch_channel = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
    monkeypatch.setattr(QBcore, "bot", fake_bot)

    CACHE.server_config["702"] = {
        "cwl_player_hub_message_enabled": False,
        "cwl_player_hub_channel_id": "123456",
        "cwl_player_hub_message_id": "555222",
    }

    await QapBot.repost_cwl_player_hub_messages()

    # persist_server_config must never have been called — tracking stays untouched in memory.
    CACHE.persist_server_config.assert_not_awaited()
    assert CACHE.server_config["702"]["cwl_player_hub_message_id"] == "555222"


@pytest.mark.asyncio
async def test_posts_a_fresh_message_when_none_tracked_yet(monkeypatch):
    channel = _fake_channel()
    fake_bot = MagicMock()
    fake_bot.get_channel = MagicMock(return_value=channel)
    monkeypatch.setattr(QBcore, "bot", fake_bot)

    CACHE.server_config["703"] = {
        "cwl_player_hub_message_enabled": True,
        "cwl_player_hub_channel_id": "123456",
        "cwl_player_hub_message_id": None,
    }

    await QapBot.repost_cwl_player_hub_messages()

    channel.send.assert_awaited_once()
    assert CACHE.server_config["703"]["cwl_player_hub_message_id"] == "999999"


@pytest.mark.asyncio
async def test_channel_migration_only_clears_tracking_after_confirmed_delete(monkeypatch):
    """Old channel != new channel: the old message must be confirmed-deleted from the OLD
    channel before message_id tracking is cleared (which is what lets a fresh post happen in
    the new channel on this same pass)."""
    old_channel = _fake_channel()
    new_channel = _fake_channel()

    def _get_channel(channel_id: int):
        return {111: old_channel, 222: new_channel}.get(channel_id)

    fake_bot = MagicMock()
    fake_bot.get_channel = MagicMock(side_effect=_get_channel)
    monkeypatch.setattr(QBcore, "bot", fake_bot)

    CACHE.server_config["704"] = {
        "cwl_player_hub_message_enabled": True,
        "cwl_player_hub_channel_id": "222",
        "_old_cwl_player_hub_channel_id": "111",
        "cwl_player_hub_message_id": "555333",
    }

    await QapBot.repost_cwl_player_hub_messages()

    old_channel.fetch_message.assert_awaited_once_with(555333)
    old_channel.fetch_message.return_value.delete.assert_awaited_once()
    # Old tracking cleared, and a fresh message posted to the NEW channel.
    assert CACHE.server_config["704"]["_old_cwl_player_hub_channel_id"] is None
    new_channel.send.assert_awaited_once()
    assert CACHE.server_config["704"]["cwl_player_hub_message_id"] == "999999"


@pytest.mark.asyncio
async def test_cooldown_gated_bump_skips_repost_within_the_window(monkeypatch):
    from datetime import datetime, timedelta, timezone

    channel = _fake_channel(last_message_id=555444)  # tracked message is NOT the newest either
    fake_bot = MagicMock()
    fake_bot.get_channel = MagicMock(return_value=channel)
    monkeypatch.setattr(QBcore, "bot", fake_bot)

    recent_bump = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    CACHE.server_config["705"] = {
        "cwl_player_hub_message_enabled": True,
        "cwl_player_hub_channel_id": "123456",
        "cwl_player_hub_message_id": "555444",
        "cwl_player_hub_message_last_bump_iso": recent_bump,
    }

    await QapBot.repost_cwl_player_hub_messages(only_if_not_bottom=True, bump_cooldown_seconds=300)

    channel.send.assert_not_awaited()
    channel.fetch_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_if_not_bottom_skips_when_tracked_message_is_already_newest(monkeypatch):
    channel = _fake_channel(last_message_id=555555)
    fake_bot = MagicMock()
    fake_bot.get_channel = MagicMock(return_value=channel)
    monkeypatch.setattr(QBcore, "bot", fake_bot)

    CACHE.server_config["706"] = {
        "cwl_player_hub_message_enabled": True,
        "cwl_player_hub_channel_id": "123456",
        "cwl_player_hub_message_id": "555555",  # matches the "newest" message from history()
    }

    await QapBot.repost_cwl_player_hub_messages(only_if_not_bottom=True, bump_cooldown_seconds=300)

    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_if_not_bottom_reposts_when_tracked_message_is_not_newest(monkeypatch):
    channel = _fake_channel(last_message_id=999000)  # some OTHER message is now newest
    fake_bot = MagicMock()
    fake_bot.get_channel = MagicMock(return_value=channel)
    monkeypatch.setattr(QBcore, "bot", fake_bot)

    CACHE.server_config["707"] = {
        "cwl_player_hub_message_enabled": True,
        "cwl_player_hub_channel_id": "123456",
        "cwl_player_hub_message_id": "555666",
    }

    await QapBot.repost_cwl_player_hub_messages(only_if_not_bottom=True, bump_cooldown_seconds=300)

    channel.send.assert_awaited_once()
