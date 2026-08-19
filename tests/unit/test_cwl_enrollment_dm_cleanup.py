"""Tests for cleanup_stale_cwl_enrollment_dms() (QBdiscocmdshelper_cwl.py, 2026-08-19 fix, live
bug report): "Delete Season" left every recipient's Confirm/Opt Out DM buttons sitting
live-looking in their DMs even after the underlying sign-up became invalid. This is the
best-effort Discord-API side of the fix — deletes the actual DM message for each ref, tolerating
every failure mode (DMs closed, bot blocked, message/user already gone, any other API hiccup)
without raising, since it always runs after the season itself is already deleted.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from qapbot.QBdiscocmdshelper_cwl import cleanup_stale_cwl_enrollment_dms


def _make_bot(user_by_id: dict) -> MagicMock:
    """user_by_id: {discord_id_int: user_mock_or_exception}. A value that's an Exception
    instance/class makes fetch_user raise it instead of returning a user."""
    bot = MagicMock()

    async def fetch_user(discord_id: int):
        entry = user_by_id[discord_id]
        if isinstance(entry, Exception) or (isinstance(entry, type) and issubclass(entry, Exception)):
            raise entry
        return entry

    bot.fetch_user = AsyncMock(side_effect=fetch_user)
    return bot


def _make_user(message_by_id: dict, dm_channel=None) -> MagicMock:
    """message_by_id: {message_id_int: message_mock_or_exception}."""
    channel = dm_channel or MagicMock()

    async def fetch_message(message_id: int):
        entry = message_by_id[message_id]
        if isinstance(entry, Exception) or (isinstance(entry, type) and issubclass(entry, Exception)):
            raise entry
        return entry

    channel.fetch_message = AsyncMock(side_effect=fetch_message)
    user = MagicMock()
    user.dm_channel = channel
    user.create_dm = AsyncMock(return_value=channel)
    return user


@pytest.mark.asyncio
async def test_deletes_each_referenced_dm_message():
    message = MagicMock()
    message.delete = AsyncMock()
    user = _make_user({111: message})
    bot = _make_bot({10: user})

    result = await cleanup_stale_cwl_enrollment_dms(
        bot, [{"player_tag": "#P1", "discord_id": "10", "message_id": "111", "channel_id": "222"}]
    )

    message.delete.assert_awaited_once()
    assert result == {"deleted": 1, "failed": 0}


@pytest.mark.asyncio
async def test_not_found_is_silently_skipped_not_a_failure():
    """The message (or the user/DM channel itself) is already gone — nothing to retract, and
    this is not something the admin needs to hear about as a failure."""
    user = _make_user({111: discord.NotFound(MagicMock(), "message")})
    bot = _make_bot({10: user})

    result = await cleanup_stale_cwl_enrollment_dms(
        bot, [{"player_tag": "#P1", "discord_id": "10", "message_id": "111", "channel_id": "222"}]
    )

    assert result == {"deleted": 0, "failed": 0}


@pytest.mark.asyncio
async def test_forbidden_counts_as_failed():
    """DMs closed / bot blocked since the message was sent."""
    user = _make_user({111: discord.Forbidden(MagicMock(), "blocked")})
    bot = _make_bot({10: user})

    result = await cleanup_stale_cwl_enrollment_dms(
        bot, [{"player_tag": "#P1", "discord_id": "10", "message_id": "111", "channel_id": "222"}]
    )

    assert result == {"deleted": 0, "failed": 1}


@pytest.mark.asyncio
async def test_one_recipients_failure_does_not_abort_the_rest():
    message_ok = MagicMock()
    message_ok.delete = AsyncMock()
    user_ok = _make_user({222: message_ok})
    user_forbidden = _make_user({111: discord.Forbidden(MagicMock(), "blocked")})
    bot = _make_bot({10: user_forbidden, 20: user_ok})

    result = await cleanup_stale_cwl_enrollment_dms(
        bot,
        [
            {"player_tag": "#P1", "discord_id": "10", "message_id": "111", "channel_id": "1"},
            {"player_tag": "#P2", "discord_id": "20", "message_id": "222", "channel_id": "2"},
        ],
    )

    message_ok.delete.assert_awaited_once()
    assert result == {"deleted": 1, "failed": 1}
