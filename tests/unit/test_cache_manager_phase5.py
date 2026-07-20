"""Tests for cache_manager.py — Phase 5 coverage push.

Covers ALL remaining untested methods:
- load_* methods (clan_name_cache, subscriptions, leaderboard, families, users, notification, server_config)
- set_server_config, persist_server_config
- persist_player_notification, persist_channel_notification
- persist_clan, delete_clan_from_cache
- validate_clan_cache_consistency
- get_channel_subscriptions, get_all_subscriptions_flat
- set_channel_subscriptions, remove_channel_subscriptions
- get_current_war_data
- get_current_war_from_api, get_league_war
- load_all
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false, reportCallIssue=false
from __future__ import annotations

import json
import os
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cm():
    """Create a CacheManager instance with a mocked db_manager."""
    from qapbot.cache_manager import CacheManager
    cm = CacheManager()
    cm.db_manager = MagicMock()
    # Make all db_manager methods async
    cm.db_manager.get_all_clans_dict = AsyncMock(return_value={})
    cm.db_manager.get_all_subscriptions_for_cache = AsyncMock(return_value={})
    cm.db_manager.get_all_leaderboard_messages = AsyncMock(return_value={})
    cm.db_manager.get_all_clan_families_dict = AsyncMock(return_value={})
    cm.db_manager.get_all_clan_families = AsyncMock(return_value={})
    cm.db_manager.get_all_users_dict = AsyncMock(return_value={})
    cm.db_manager.load_notification_state = AsyncMock(return_value={})
    cm.db_manager.get_all_guild_configs_dict = AsyncMock(return_value={})
    cm.db_manager.save_guild_config = AsyncMock()
    cm.db_manager.save_clan = AsyncMock()
    cm.db_manager.delete_clan = AsyncMock()
    cm.db_manager.save_player_notification = AsyncMock()
    cm.db_manager.save_channel_notification = AsyncMock()
    cm.db_manager.save_subscription = AsyncMock()
    cm.db_manager.delete_subscription = AsyncMock()
    cm.db_manager.delete_subscriptions_for_channel = AsyncMock()
    return cm


# ===========================================================================
# load_clan_name_cache
# ===========================================================================

class TestLoadClanNameCache:
    @pytest.mark.asyncio
    async def test_loads_from_db(self):
        cm = _make_cm()
        cm.db_manager.get_all_clans_dict = AsyncMock(return_value={"#C1": {"name": "Clan1"}})
        await cm.load_clan_name_cache()
        assert cm.clan_name_cache == {"#C1": {"name": "Clan1"}}

    @pytest.mark.asyncio
    async def test_no_db_raises_runtime(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager()
        with pytest.raises(RuntimeError, match="Database manager not initialized"):
            await cm.load_clan_name_cache()

    @pytest.mark.asyncio
    async def test_db_error_raises_system_exit(self):
        cm = _make_cm()
        cm.db_manager.get_all_clans_dict = AsyncMock(side_effect=Exception("db error"))
        with pytest.raises(SystemExit):
            await cm.load_clan_name_cache()


# ===========================================================================
# load_subscriptions
# ===========================================================================

class TestLoadSubscriptions:
    @pytest.mark.asyncio
    async def test_loads_from_db(self):
        cm = _make_cm()
        cm.db_manager.get_all_subscriptions_for_cache = AsyncMock(return_value={"G1": {"ch1": [{"clan_tag": "#C1"}]}})
        await cm.load_subscriptions()
        assert "G1" in cm.subscriptions

    @pytest.mark.asyncio
    async def test_db_error_defaults_empty(self):
        cm = _make_cm()
        cm.db_manager.get_all_subscriptions_for_cache = AsyncMock(side_effect=Exception("fail"))
        await cm.load_subscriptions()
        assert cm.subscriptions == {}


# ===========================================================================
# load_leaderboard_messages
# ===========================================================================

class TestLoadLeaderboardMessages:
    @pytest.mark.asyncio
    async def test_loads_from_db(self):
        cm = _make_cm()
        cm.db_manager.get_all_leaderboard_messages = AsyncMock(return_value={"key": "data"})
        await cm.load_leaderboard_messages()
        assert cm.leaderboard_messages == {"key": "data"}


# ===========================================================================
# load_clan_families
# ===========================================================================

class TestLoadClanFamilies:
    @pytest.mark.asyncio
    async def test_loads_from_db(self):
        cm = _make_cm()
        cm.db_manager.get_all_clan_families = AsyncMock(return_value={"fam1": {"clans": ["#C1"]}})
        await cm.load_clan_families()
        assert cm.clan_families == {"fam1": {"clans": ["#C1"]}}


# ===========================================================================
# load_user_accounts
# ===========================================================================

class TestLoadUserAccounts:
    @pytest.mark.asyncio
    async def test_loads_from_db(self):
        cm = _make_cm()
        cm.db_manager.get_all_users_dict = AsyncMock(return_value={"U1": {"display_name": "Test"}})
        await cm.load_user_accounts()
        assert cm.user_accounts == {"U1": {"display_name": "Test"}}

    @pytest.mark.asyncio
    async def test_no_db_raises_runtime(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager()
        with pytest.raises(RuntimeError):
            await cm.load_user_accounts()

    @pytest.mark.asyncio
    async def test_db_error_raises_system_exit(self):
        cm = _make_cm()
        cm.db_manager.get_all_users_dict = AsyncMock(side_effect=Exception("fail"))
        with pytest.raises(SystemExit):
            await cm.load_user_accounts()


# ===========================================================================
# load_notification_state
# ===========================================================================

class TestLoadNotificationState:
    @pytest.mark.asyncio
    async def test_loads_from_db(self):
        cm = _make_cm()
        cm.db_manager.load_notification_state = AsyncMock(return_value={"war1": {"notified_players": {}}})
        await cm.load_notification_state()
        assert "war1" in cm.notification_state

    @pytest.mark.asyncio
    async def test_no_db_raises_runtime(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager()
        with pytest.raises(RuntimeError):
            await cm.load_notification_state()

    @pytest.mark.asyncio
    async def test_db_error_raises_system_exit(self):
        cm = _make_cm()
        cm.db_manager.load_notification_state = AsyncMock(side_effect=Exception("fail"))
        with pytest.raises(SystemExit):
            await cm.load_notification_state()


# ===========================================================================
# load_server_config
# ===========================================================================

class TestLoadServerConfig:
    @pytest.mark.asyncio
    async def test_loads_from_db(self):
        cm = _make_cm()
        cm.db_manager.get_all_guild_configs_dict = AsyncMock(return_value={"G1": {"member_clans": ["#C1"]}})
        await cm.load_server_config()
        assert cm.server_config == {"G1": {"member_clans": ["#C1"]}}

    @pytest.mark.asyncio
    async def test_no_db_raises_runtime(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager()
        with pytest.raises(RuntimeError):
            await cm.load_server_config()

    @pytest.mark.asyncio
    async def test_db_error_raises_system_exit(self):
        cm = _make_cm()
        cm.db_manager.get_all_guild_configs_dict = AsyncMock(side_effect=Exception("fail"))
        with pytest.raises(SystemExit):
            await cm.load_server_config()


# ===========================================================================
# set_server_config / persist_server_config
# ===========================================================================

class TestSetServerConfig:
    @pytest.mark.asyncio
    async def test_writes_through(self):
        cm = _make_cm()
        await cm.set_server_config("G1", {"member_clans": ["#C1"]})
        assert cm.server_config["G1"] == {"member_clans": ["#C1"]}
        cm.db_manager.save_guild_config.assert_awaited_once_with("G1", {"member_clans": ["#C1"]})

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        cm = _make_cm()
        cm.db_manager.save_guild_config = AsyncMock(side_effect=Exception("fail"))
        with pytest.raises(Exception):
            await cm.set_server_config("G1", {})


class TestPersistServerConfig:
    @pytest.mark.asyncio
    async def test_persists_existing(self):
        cm = _make_cm()
        cm.server_config["G1"] = {"member_clans": []}
        await cm.persist_server_config("G1")
        cm.db_manager.save_guild_config.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_warns_missing(self):
        cm = _make_cm()
        await cm.persist_server_config("MISSING")
        cm.db_manager.save_guild_config.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        cm = _make_cm()
        cm.server_config["G1"] = {}
        cm.db_manager.save_guild_config = AsyncMock(side_effect=Exception("fail"))
        with pytest.raises(Exception):
            await cm.persist_server_config("G1")


# ===========================================================================
# persist_player_notification
# ===========================================================================

class TestPersistPlayerNotification:
    @pytest.mark.asyncio
    async def test_persists_existing(self):
        cm = _make_cm()
        cm.notification_state = {
            "war1": {"notified_players": {
                "#P1": {"player_name": "Alice", "discord_id": "U1", "notification_time": "2025-01-01", "attacks_remaining": 2}
            }}
        }
        await cm.persist_player_notification("war1", "#P1")
        cm.db_manager.save_player_notification.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_warns_missing_war(self):
        cm = _make_cm()
        cm.notification_state = {}
        await cm.persist_player_notification("war1", "#P1")
        cm.db_manager.save_player_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_warns_missing_player(self):
        cm = _make_cm()
        cm.notification_state = {"war1": {"notified_players": {}}}
        await cm.persist_player_notification("war1", "#P1")
        cm.db_manager.save_player_notification.assert_not_awaited()


# ===========================================================================
# persist_channel_notification
# ===========================================================================

class TestPersistChannelNotification:
    @pytest.mark.asyncio
    async def test_persists_existing(self):
        cm = _make_cm()
        cm.notification_state = {
            "war1": {"channel_notifications": {
                "G1": {"notification_time": "2025-01-01", "clan_name": "TestClan", "opponent_name": "Enemy"}
            }}
        }
        await cm.persist_channel_notification("war1", "G1")
        cm.db_manager.save_channel_notification.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_warns_missing(self):
        cm = _make_cm()
        cm.notification_state = {}
        await cm.persist_channel_notification("war1", "G1")
        cm.db_manager.save_channel_notification.assert_not_awaited()


# ===========================================================================
# persist_clan / delete_clan_from_cache
# ===========================================================================

class TestPersistClan:
    @pytest.mark.asyncio
    async def test_persists_dict_data(self):
        cm = _make_cm()
        cm.clan_name_cache = {"#C1": {"name": "Test", "has_active_subscriptions": True, "warlog_is_public": True}}
        await cm.persist_clan("#C1")
        cm.db_manager.save_clan.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_warns_missing(self):
        cm = _make_cm()
        cm.clan_name_cache = {}
        await cm.persist_clan("#C1")
        cm.db_manager.save_clan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_warns_non_dict(self):
        cm = _make_cm()
        cm.clan_name_cache = {"#C1": "just_a_string"}
        await cm.persist_clan("#C1")
        cm.db_manager.save_clan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        cm = _make_cm()
        cm.clan_name_cache = {"#C1": {"name": "Test"}}
        cm.db_manager.save_clan = AsyncMock(side_effect=Exception("fail"))
        with pytest.raises(Exception):
            await cm.persist_clan("#C1")


class TestDeleteClanFromCache:
    @pytest.mark.asyncio
    async def test_deletes_and_persists(self):
        cm = _make_cm()
        cm.clan_name_cache = {"#C1": {"name": "Test"}}
        await cm.delete_clan_from_cache("#C1")
        assert "#C1" not in cm.clan_name_cache
        cm.db_manager.delete_clan.assert_awaited_once_with("#C1")

    @pytest.mark.asyncio
    async def test_not_in_cache_still_deletes_db(self):
        cm = _make_cm()
        cm.clan_name_cache = {}
        await cm.delete_clan_from_cache("#C1")
        cm.db_manager.delete_clan.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        cm = _make_cm()
        cm.clan_name_cache = {}
        cm.db_manager.delete_clan = AsyncMock(side_effect=Exception("fail"))
        with pytest.raises(Exception):
            await cm.delete_clan_from_cache("#C1")


# ===========================================================================
# validate_clan_cache_consistency
# ===========================================================================

class TestValidateClanCacheConsistency:
    @pytest.mark.asyncio
    async def test_no_missing_clans(self):
        cm = _make_cm()
        cm.server_config = {"G1": {"member_clans": ["#C1"]}}
        cm.clan_name_cache = {"#C1": {"name": "Test"}}
        await cm.validate_clan_cache_consistency()
        cm.db_manager.save_clan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_adds_missing_clans(self):
        cm = _make_cm()
        cm.server_config = {"G1": {"member_clans": ["#MISSING"]}}
        cm.clan_name_cache = {}
        await cm.validate_clan_cache_consistency()
        assert "#MISSING" in cm.clan_name_cache
        assert cm.clan_name_cache["#MISSING"]["name"] == "Unknown (auto-added)"
        cm.db_manager.save_clan.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_multiple_guilds_missing(self):
        cm = _make_cm()
        cm.server_config = {
            "G1": {"member_clans": ["#M1"]},
            "G2": {"member_clans": ["#M2"]},
        }
        cm.clan_name_cache = {}
        await cm.validate_clan_cache_consistency()
        assert "#M1" in cm.clan_name_cache
        assert "#M2" in cm.clan_name_cache


# ===========================================================================
# get_channel_subscriptions / get_all_subscriptions_flat
# ===========================================================================

class TestGetChannelSubscriptions:
    def test_finds_channel(self):
        cm = _make_cm()
        cm.subscriptions = {"G1": {"ch1": [{"clan_tag": "#C1"}], "ch2": [{"clan_tag": "#C2"}]}}
        result = cm.get_channel_subscriptions("ch1")
        assert result == [{"clan_tag": "#C1"}]

    def test_channel_not_found(self):
        cm = _make_cm()
        cm.subscriptions = {"G1": {"ch1": []}}
        assert cm.get_channel_subscriptions("ch99") == []

    def test_empty_subscriptions(self):
        cm = _make_cm()
        cm.subscriptions = {}
        assert cm.get_channel_subscriptions("ch1") == []


class TestGetAllSubscriptionsFlat:
    def test_flattens(self):
        cm = _make_cm()
        cm.subscriptions = {
            "G1": {"ch1": [{"a": 1}], "ch2": [{"b": 2}]},
            "G2": {"ch3": [{"c": 3}]},
        }
        result = cm.get_all_subscriptions_flat()
        assert result == {"ch1": [{"a": 1}], "ch2": [{"b": 2}], "ch3": [{"c": 3}]}

    def test_empty(self):
        cm = _make_cm()
        cm.subscriptions = {}
        assert cm.get_all_subscriptions_flat() == {}


# ===========================================================================
# set_channel_subscriptions / remove_channel_subscriptions
# ===========================================================================

class TestSetChannelSubscriptions:
    @pytest.mark.asyncio
    async def test_delegates_to_set_subscriptions_for_channel(self):
        cm = _make_cm()
        cm.set_subscriptions_for_channel = AsyncMock()
        await cm.set_channel_subscriptions("G1", "ch1", [{"clan_tag": "#C1"}])
        cm.set_subscriptions_for_channel.assert_awaited_once_with("G1", "ch1", [{"clan_tag": "#C1"}])


class TestRemoveChannelSubscriptions:
    @pytest.mark.asyncio
    async def test_removes_existing(self):
        cm = _make_cm()
        cm.subscriptions = {"G1": {"ch1": [{"clan_tag": "#C1"}]}}
        cm.delete_subscriptions_for_channel = AsyncMock()
        result = await cm.remove_channel_subscriptions("ch1")
        assert result is True
        cm.delete_subscriptions_for_channel.assert_awaited_once_with("G1", "ch1")

    @pytest.mark.asyncio
    async def test_not_found_returns_false(self):
        cm = _make_cm()
        cm.subscriptions = {}
        result = await cm.remove_channel_subscriptions("ch99")
        assert result is False


# ===========================================================================
# get_current_war_data (sync with file I/O)
# ===========================================================================

class TestGetCurrentWarData:
    def test_no_files_returns_none(self):
        cm = _make_cm()
        with patch("qapbot.cache_manager.glob.glob", return_value=[]):
            assert cm.get_current_war_data("#L2J0C0PY") is None

    def test_loads_latest_file(self):
        cm = _make_cm()
        war_data = {"state": "inWar", "clan": {"tag": "#C1"}}
        with patch("qapbot.cache_manager.glob.glob", return_value=["file1.json"]):
            with patch("qapbot.cache_manager.os.path.getmtime", return_value=1000):
                with patch("builtins.open", mock_open(read_data=json.dumps(war_data))):
                    result = cm.get_current_war_data("#C1")
                    assert result == war_data

    def test_corrupted_file_returns_none(self):
        cm = _make_cm()
        with patch("qapbot.cache_manager.glob.glob", return_value=["bad.json"]):
            with patch("qapbot.cache_manager.os.path.getmtime", return_value=1000):
                with patch("builtins.open", mock_open(read_data="not json")):
                    assert cm.get_current_war_data("#C1") is None


# ===========================================================================
# get_current_war_from_api / get_league_war
# ===========================================================================

class TestGetCurrentWarFromApi:
    @pytest.mark.asyncio
    async def test_calls_coc_retry(self):
        cm = _make_cm()
        cm.coc_client = MagicMock()
        war = MagicMock()
        with patch("qapbot.cache_manager.coc_retry", new_callable=AsyncMock, return_value=war) as mock_retry:
            result = await cm.get_current_war_from_api("#C1")
            assert result == war
            mock_retry.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_client_raises(self):
        cm = _make_cm()
        cm.coc_client = None
        with pytest.raises(RuntimeError, match="CoC API client not initialized"):
            await cm.get_current_war_from_api("#C1")


class TestGetLeagueWar:
    @pytest.mark.asyncio
    async def test_calls_coc_retry(self):
        cm = _make_cm()
        cm.coc_client = MagicMock()
        war = MagicMock()
        with patch("qapbot.cache_manager.coc_retry", new_callable=AsyncMock, return_value=war):
            result = await cm.get_league_war("#WAR123")
            assert result == war

    @pytest.mark.asyncio
    async def test_no_client_raises(self):
        cm = _make_cm()
        cm.coc_client = None
        with pytest.raises(RuntimeError):
            await cm.get_league_war("#WAR123")


# ===========================================================================
# load_all
# ===========================================================================

class TestLoadAll:
    @pytest.mark.asyncio
    async def test_calls_all_loaders(self):
        cm = _make_cm()
        cm.load_clan_name_cache = AsyncMock()
        cm.load_subscriptions = AsyncMock()
        cm.load_leaderboard_messages = AsyncMock()
        cm.load_clan_families = AsyncMock()
        cm.load_user_accounts = AsyncMock()
        cm.load_notification_state = AsyncMock()
        cm.load_server_config = AsyncMock()
        cm.validate_clan_cache_consistency = AsyncMock()
        cm.load_all_temp_war_stats = MagicMock()
        await cm.load_all()
        cm.load_clan_name_cache.assert_awaited_once()
        cm.load_subscriptions.assert_awaited_once()
        cm.load_leaderboard_messages.assert_awaited_once()
        cm.load_clan_families.assert_awaited_once()
        cm.load_user_accounts.assert_awaited_once()
        cm.load_notification_state.assert_awaited_once()
        cm.load_server_config.assert_awaited_once()
        cm.validate_clan_cache_consistency.assert_awaited_once()
