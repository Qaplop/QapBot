"""Tests for CacheManager methods — Phase 4 deep coverage.

Covers: get_temp_war_stats, set_temp_war_stats, delete_leaderboard_message,
ensure_user_metadata, set_subscriptions_for_channel, get_player.
"""
# pyright: reportUnknownMemberType=false, reportUnknownParameterType=false
# pyright: reportMissingParameterType=false, reportUnknownVariableType=false
# pyright: reportOptionalMemberAccess=false, reportAttributeAccessIssue=false
# pyright: reportUnknownLambdaType=false, reportUnknownArgumentType=false
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapbot.cache_manager import CacheManager


def _make_cm(**overrides):
    """Create a minimal CacheManager for testing."""
    cm = CacheManager.__new__(CacheManager)
    cm.user_accounts = overrides.get("user_accounts", {})
    cm.clan_name_cache = overrides.get("clan_name_cache", {})
    cm.clan_families = overrides.get("clan_families", {})
    cm.server_config = overrides.get("server_config", {})
    cm.notification_state = overrides.get("notification_state", {})
    cm.leaderboard_messages = overrides.get("leaderboard_messages", {})
    cm.temp_war_stats = overrides.get("temp_war_stats", {})
    cm.temp_war_metadata = overrides.get("temp_war_metadata", {})
    cm.temp_war_objects = overrides.get("temp_war_objects", {})
    cm.in_war_clan_tags = overrides.get("in_war_clan_tags", set())
    cm.subscriptions = overrides.get("subscriptions", {})
    cm.history_cache = {}
    cm.clan_history = {}
    cm.db_manager = AsyncMock()
    cm.persist_user = AsyncMock()
    cm.persist_clan = AsyncMock()
    cm.coc_client = None
    cm.update_user_metadata = AsyncMock()
    return cm


# ---------------------------------------------------------------------------
# get_temp_war_stats / set_temp_war_stats
# ---------------------------------------------------------------------------

class TestTempWarStats:
    def test_get_empty(self):
        cm = _make_cm()
        assert cm.get_temp_war_stats("#TAG") == {}

    def test_set_and_get(self):
        cm = _make_cm()
        stats = {"#P1": {"Stars": 3, "Attacks": 2}}
        cm.set_temp_war_stats("#TAG", stats)
        assert cm.get_temp_war_stats("#TAG") == stats

    def test_overwrite(self):
        cm = _make_cm()
        cm.set_temp_war_stats("#TAG", {"#P1": {"Stars": 1}})
        cm.set_temp_war_stats("#TAG", {"#P2": {"Stars": 5}})
        result = cm.get_temp_war_stats("#TAG")
        assert "#P1" not in result
        assert result["#P2"]["Stars"] == 5

    def test_multiple_clans_isolated(self):
        cm = _make_cm()
        cm.set_temp_war_stats("#A", {"data": "a"})
        cm.set_temp_war_stats("#B", {"data": "b"})
        assert cm.get_temp_war_stats("#A") == {"data": "a"}
        assert cm.get_temp_war_stats("#B") == {"data": "b"}

    def test_set_empty_clears(self):
        cm = _make_cm()
        cm.set_temp_war_stats("#TAG", {"#P1": {}})
        cm.set_temp_war_stats("#TAG", {})
        assert cm.get_temp_war_stats("#TAG") == {}


# ---------------------------------------------------------------------------
# delete_leaderboard_message
# ---------------------------------------------------------------------------

class TestDeleteLeaderboardMessage:
    @pytest.mark.asyncio
    async def test_deletes_from_cache_and_db(self):
        cm = _make_cm(leaderboard_messages={
            "key1": {"message_ids": "111", "channel_id": "c1"},
            "key2": {"message_ids": "222", "channel_id": "c2"},
        })
        await cm.delete_leaderboard_message("key1")
        assert "key1" not in cm.leaderboard_messages
        assert "key2" in cm.leaderboard_messages
        cm.db_manager.delete_leaderboard_message.assert_awaited_once_with("key1")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_key(self):
        cm = _make_cm(leaderboard_messages={})
        # Should not raise even if key not in local dict
        await cm.delete_leaderboard_message("nonexistent")
        cm.db_manager.delete_leaderboard_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_db_error_propagates(self):
        cm = _make_cm(leaderboard_messages={"key1": {}})
        cm.db_manager.delete_leaderboard_message.side_effect = Exception("DB error")
        with pytest.raises(Exception, match="DB error"):
            await cm.delete_leaderboard_message("key1")


# ---------------------------------------------------------------------------
# ensure_user_metadata
# ---------------------------------------------------------------------------

class TestEnsureUserMetadata:
    @pytest.mark.asyncio
    async def test_new_user_triggers_update(self):
        cm = _make_cm()
        cm.update_user_metadata = AsyncMock(side_effect=lambda uid: cm.user_accounts.__setitem__(uid, {"display_name": "New"}))
        result = await cm.ensure_user_metadata("U1")
        cm.update_user_metadata.assert_awaited_once_with("U1")
        assert result.get("display_name") == "New"

    @pytest.mark.asyncio
    async def test_existing_user_no_update(self):
        cm = _make_cm(user_accounts={
            "U1": {"display_name": "Existing", "user_language": "en"}
        })
        result = await cm.ensure_user_metadata("U1")
        cm.update_user_metadata.assert_not_awaited()
        assert result["display_name"] == "Existing"

    @pytest.mark.asyncio
    async def test_missing_display_name_triggers_update(self):
        cm = _make_cm(user_accounts={
            "U1": {"display_name": "", "user_language": "en"}
        })
        _ = await cm.ensure_user_metadata("U1")
        cm.update_user_metadata.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_language_triggers_update(self):
        cm = _make_cm(user_accounts={
            "U1": {"display_name": "Name"}  # No user_language
        })
        _ = await cm.ensure_user_metadata("U1")
        cm.update_user_metadata.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_converts_id_to_string(self):
        cm = _make_cm(user_accounts={
            "123": {"display_name": "Num", "user_language": "en"}
        })
        result = await cm.ensure_user_metadata(123)  # type: ignore
        assert result["display_name"] == "Num"

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        cm = _make_cm()
        cm.update_user_metadata = AsyncMock(side_effect=RuntimeError("fail"))
        result = await cm.ensure_user_metadata("U1")
        assert result == {}


# ---------------------------------------------------------------------------
# set_subscriptions_for_channel
# ---------------------------------------------------------------------------

class TestSetSubscriptionsForChannel:
    @pytest.mark.asyncio
    async def test_set_new_guild(self):
        cm = _make_cm()
        subs = [{"clan_tag": "#T", "subscription_type": "stars"}]
        await cm.set_subscriptions_for_channel("G1", "C1", subs)
        assert cm.subscriptions["G1"]["C1"] == subs
        cm.db_manager.save_subscriptions_for_channel.assert_awaited_once_with("G1", "C1", subs)

    @pytest.mark.asyncio
    async def test_update_existing_channel(self):
        cm = _make_cm(subscriptions={"G1": {"C1": [{"old": True}]}})
        new_subs = [{"clan_tag": "#T", "subscription_type": "attacks"}]
        await cm.set_subscriptions_for_channel("G1", "C1", new_subs)
        assert cm.subscriptions["G1"]["C1"] == new_subs

    @pytest.mark.asyncio
    async def test_db_error_propagates(self):
        cm = _make_cm()
        cm.db_manager.save_subscriptions_for_channel.side_effect = Exception("fail")
        with pytest.raises(Exception, match="fail"):
            await cm.set_subscriptions_for_channel("G1", "C1", [])


# ---------------------------------------------------------------------------
# get_player
# ---------------------------------------------------------------------------

class TestGetPlayer:
    @pytest.mark.asyncio
    async def test_invalid_tag_returns_none(self):
        cm = _make_cm()
        result = await cm.get_player("")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_coc_client_raises(self):
        cm = _make_cm()
        cm.coc_client = None
        # normalize_clan_tag returns a valid tag, but no client
        with patch("qapbot.cache_manager.coc_retry", new_callable=AsyncMock) as mock_retry:
            mock_retry.side_effect = RuntimeError("CoC API client not initialized")
            result = await cm.get_player("#VALIDTAG1")
            # Returns None on exception
            assert result is None

    @pytest.mark.asyncio
    async def test_successful_fetch(self):
        cm = _make_cm()
        cm.coc_client = MagicMock()
        mock_player = MagicMock()
        mock_player.tag = "#ABC12345"
        mock_player.name = "TestPlayer"

        with patch("qapbot.cache_manager.coc_retry", new_callable=AsyncMock) as mock_retry:
            mock_retry.return_value = mock_player
            result = await cm.get_player("#ABC12345")
            assert result == mock_player

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self):
        cm = _make_cm()
        cm.coc_client = MagicMock()
        with patch("qapbot.cache_manager.coc_retry", new_callable=AsyncMock) as mock_retry:
            mock_retry.side_effect = Exception("API error")
            result = await cm.get_player("#ABC12345")
            assert result is None
