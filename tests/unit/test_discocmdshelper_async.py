"""Tests for QBdiscocmdshelper async helpers — Phase 4 coverage.

Covers: get_mode_autocomplete_choices, _delete_messages_by_filter.
"""
# pyright: reportPrivateUsage=false, reportUnusedImport=false, reportUnusedVariable=false
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from discord import app_commands


# ---------------------------------------------------------------------------
# get_mode_autocomplete_choices
# ---------------------------------------------------------------------------

class TestGetModeAutocompleteChoices:
    @pytest.mark.asyncio
    async def test_all_modes_returned(self):
        from qapbot.QBdiscocmdshelper import get_mode_autocomplete_choices

        with patch("qapbot.QBdiscocmdshelper.CACHE") as mock_cache:
            choices = await get_mode_autocomplete_choices("")
            assert len(choices) > 0
            assert all(isinstance(c, app_commands.Choice) for c in choices)

    @pytest.mark.asyncio
    async def test_filter_by_current(self):
        from qapbot.QBdiscocmdshelper import get_mode_autocomplete_choices

        with patch("qapbot.QBdiscocmdshelper.CACHE"):
            choices = await get_mode_autocomplete_choices("star")
            # Only modes containing 'star' should be returned
            for c in choices:
                assert "star" in c.value.lower()

    @pytest.mark.asyncio
    async def test_exclude_cwl_variants(self):
        from qapbot.QBdiscocmdshelper import get_mode_autocomplete_choices

        with patch("qapbot.QBdiscocmdshelper.CACHE"):
            choices = await get_mode_autocomplete_choices("", include_cwl_variants=False)
            for c in choices:
                assert not c.value.endswith("_cwl")

    @pytest.mark.asyncio
    async def test_include_cwl_variants(self):
        from qapbot.QBdiscocmdshelper import get_mode_autocomplete_choices

        with patch("qapbot.QBdiscocmdshelper.CACHE"):
            choices = await get_mode_autocomplete_choices("", include_cwl_variants=True)
            values = [c.value for c in choices]
            # At least some cwl modes should be present
            cwl_modes = [v for v in values if v.endswith("_cwl")]
            assert len(cwl_modes) >= 1

    @pytest.mark.asyncio
    async def test_max_choices_limit(self):
        from qapbot.QBdiscocmdshelper import get_mode_autocomplete_choices

        with patch("qapbot.QBdiscocmdshelper.CACHE"):
            choices = await get_mode_autocomplete_choices("", max_choices=3, include_cwl_variants=True)
            assert len(choices) <= 3

    @pytest.mark.asyncio
    async def test_subscribed_modes_only(self):
        from qapbot.QBdiscocmdshelper import get_mode_autocomplete_choices

        with patch("qapbot.cache_manager.CACHE") as mock_cache:
            mock_cache.get_channel_subscriptions.return_value = [
                {"clan_tag": "#TAG", "subscription_type": "stars"},
                {"clan_tag": "#TAG", "subscription_type": "attacks"},
                {"clan_tag": "#OTHER", "subscription_type": "missed"},
            ]
            choices = await get_mode_autocomplete_choices(
                "", subscribed_modes_only=True,
                clan_tag="#TAG", channel_id="123"
            )
            values = [c.value for c in choices]
            assert "stars" in values
            assert "attacks" in values
            assert "missed" not in values  # Different clan

    @pytest.mark.asyncio
    async def test_subscribed_modes_filter_current(self):
        from qapbot.QBdiscocmdshelper import get_mode_autocomplete_choices

        with patch("qapbot.cache_manager.CACHE") as mock_cache:
            mock_cache.get_channel_subscriptions.return_value = [
                {"clan_tag": "#T", "subscription_type": "stars"},
                {"clan_tag": "#T", "subscription_type": "attacks"},
            ]
            choices = await get_mode_autocomplete_choices(
                "att", subscribed_modes_only=True,
                clan_tag="#T", channel_id="1"
            )
            assert len(choices) == 1
            assert choices[0].value == "attacks"

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self):
        from qapbot.QBdiscocmdshelper import get_mode_autocomplete_choices

        with patch("qapbot.QBdiscocmdshelper.CACHE"):
            choices = await get_mode_autocomplete_choices("xyznonexistent")
            assert choices == []


# ---------------------------------------------------------------------------
# _delete_messages_by_filter
# ---------------------------------------------------------------------------

class TestDeleteMessagesByFilter:
    @pytest.mark.asyncio
    async def test_channel_not_found(self):
        from qapbot.QBdiscocmdshelper import _delete_messages_by_filter

        with patch("qapbot.QBdiscocmdshelper.CACHE") as mock_cache, \
             patch("QBcore.bot") as mock_bot:
            mock_bot.get_channel.return_value = None
            result = await _delete_messages_by_filter("999", lambda k, v: True, "test")
            assert result == 0

    @pytest.mark.asyncio
    async def test_deletes_matching_messages(self):
        from qapbot.QBdiscocmdshelper import _delete_messages_by_filter

        mock_channel = AsyncMock()
        mock_msg = AsyncMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_msg)

        with patch("qapbot.QBdiscocmdshelper.CACHE") as mock_cache, \
             patch("QBcore.bot") as mock_bot, \
             patch("qapbot.QBdiscocmdshelper.discord_retry", new_callable=AsyncMock):
            mock_bot.get_channel.return_value = mock_channel
            mock_cache.leaderboard_messages = {
                "key1": {"message_ids": "111,222", "channel_id": "999"},
                "key2": {"message_ids": "333", "channel_id": "999"},
            }
            mock_cache.delete_leaderboard_message = AsyncMock()

            result = await _delete_messages_by_filter(
                "999", lambda k, v: k == "key1", "test"
            )
            assert result == 1
            mock_cache.delete_leaderboard_message.assert_awaited_once_with("key1")

    @pytest.mark.asyncio
    async def test_empty_leaderboard_messages(self):
        from qapbot.QBdiscocmdshelper import _delete_messages_by_filter

        mock_channel = AsyncMock()

        with patch("qapbot.QBdiscocmdshelper.CACHE") as mock_cache, \
             patch("QBcore.bot") as mock_bot:
            mock_bot.get_channel.return_value = mock_channel
            mock_cache.leaderboard_messages = {}
            mock_cache.delete_leaderboard_message = AsyncMock()

            result = await _delete_messages_by_filter(
                "999", lambda k, v: True, "test"
            )
            assert result == 0

    @pytest.mark.asyncio
    async def test_message_deletion_error_continues(self):
        """Errors fetching/deleting messages shouldn't stop the process."""
        from qapbot.QBdiscocmdshelper import _delete_messages_by_filter

        mock_channel = AsyncMock()
        mock_channel.fetch_message = AsyncMock(side_effect=Exception("Not Found"))

        with patch("qapbot.QBdiscocmdshelper.CACHE") as mock_cache, \
             patch("QBcore.bot") as mock_bot:
            mock_bot.get_channel.return_value = mock_channel
            mock_cache.leaderboard_messages = {
                "key1": {"message_ids": "111"},
            }
            mock_cache.delete_leaderboard_message = AsyncMock()

            result = await _delete_messages_by_filter(
                "999", lambda k, v: True, "test"
            )
            # Key should still be deleted even though message deletion failed
            assert result == 1
