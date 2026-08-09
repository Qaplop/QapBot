"""Tests for qapbot.discord_health.bulk_sync_global_commands: the raw bulk-upsert helper
that preserves an existing Activities Entry Point command, which discord.py's own
CommandTree.sync()/clear_commands() know nothing about (see CWL_CLAN_CONFIG_ACTIVITY_PLAN.md
Phase D — omitting it from a global bulk overwrite makes Discord reject the whole request
with HTTP 400 / error code 50240 instead of just deleting it).
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from qapbot.discord_health import bulk_sync_global_commands


def _make_bot(existing_commands):
    bot = MagicMock()
    bot.application_id = 123
    bot.http.get_global_commands = AsyncMock(return_value=existing_commands)
    bot.http.bulk_upsert_global_commands = AsyncMock(return_value=[])
    return bot


@pytest.mark.asyncio
async def test_preserves_existing_entry_point_command():
    entry_point = {"id": "1", "type": 4, "name": "launch"}
    other_existing = {"id": "2", "type": 1, "name": "old-stale-command"}
    bot = _make_bot([entry_point, other_existing])
    tree_payload = [{"type": 1, "name": "ping"}]

    await bulk_sync_global_commands(bot, tree_payload)

    bot.http.bulk_upsert_global_commands.assert_awaited_once_with(
        123, payload=[{"type": 1, "name": "ping"}, entry_point]
    )


@pytest.mark.asyncio
async def test_no_entry_point_leaves_payload_unchanged():
    bot = _make_bot([{"id": "2", "type": 1, "name": "old-stale-command"}])
    tree_payload = [{"type": 1, "name": "ping"}]

    await bulk_sync_global_commands(bot, tree_payload)

    bot.http.bulk_upsert_global_commands.assert_awaited_once_with(123, payload=tree_payload)


@pytest.mark.asyncio
async def test_empty_payload_still_preserves_entry_point():
    """The clear-global-commands path (DEV app cleanup) passes an empty tree_payload —
    it must still keep the Entry Point command rather than deleting it."""
    entry_point = {"id": "1", "type": 4, "name": "launch"}
    bot = _make_bot([entry_point])

    await bulk_sync_global_commands(bot, [])

    bot.http.bulk_upsert_global_commands.assert_awaited_once_with(123, payload=[entry_point])
