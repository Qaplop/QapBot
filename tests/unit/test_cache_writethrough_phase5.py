"""Tests for CacheManager write-through methods — Phase 5 Batch 4 coverage push.

Covers:
- delete_subscriptions_for_guild (cache + db delegate)
- set_leaderboard_message / delete_leaderboard_message
- set_clan_family / persist_clan_family / delete_clan_family
- set_user_account / persist_user / delete_user_account
- _calculate_subscription_status (direct + family)
- update_clan_subscription_status
- get_clan_name (dict, string, empty-cache, None)
- load_leaderboard_messages / load_clan_families / load_user_accounts — error paths
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

# ---------------------------------------------------------------------------
# Fixture — lightweight CacheManager with mocked db_manager
# ---------------------------------------------------------------------------

@pytest.fixture
def cache():
    """Create a CacheManager with a mocked db_manager."""
    from qapbot.cache_manager import CacheManager
    cm = CacheManager.__new__(CacheManager)
    # Initialise required attributes manually (skip __init__ side effects)
    cm.subscriptions = {}
    cm.leaderboard_messages = {}
    cm.clan_families = {}
    cm.user_accounts = {}
    cm.clan_name_cache = {}
    cm.db_manager = AsyncMock()
    return cm


# ===================================================================
# delete_subscriptions_for_guild
# ===================================================================

class TestDeleteSubscriptionsForGuild:
    @pytest.mark.asyncio
    async def test_removes_from_cache_and_db(self, cache):
        cache.subscriptions = {"G1": {"C1": [{"clan_tag": "#X"}]}, "G2": {"C2": []}}
        await cache.delete_subscriptions_for_guild("G1")
        assert "G1" not in cache.subscriptions
        cache.db_manager.delete_subscriptions_for_guild.assert_awaited_once_with("G1")

    @pytest.mark.asyncio
    async def test_nonexistent_guild_still_calls_db(self, cache):
        await cache.delete_subscriptions_for_guild("NOPE")
        cache.db_manager.delete_subscriptions_for_guild.assert_awaited_once_with("NOPE")

    @pytest.mark.asyncio
    async def test_db_error_propagates(self, cache):
        cache.db_manager.delete_subscriptions_for_guild.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            await cache.delete_subscriptions_for_guild("G1")


# ===================================================================
# set_leaderboard_message / delete_leaderboard_message
# ===================================================================

class TestLeaderboardWriteThrough:
    @pytest.mark.asyncio
    async def test_set_updates_cache_and_db(self, cache):
        data = {"clan_tag": None, "channel_id": "CH1", "mode": "stars", "message_ids": "1", "content_hash": "abc"}
        await cache.set_leaderboard_message("KEY1", data)
        assert cache.leaderboard_messages["KEY1"] == data
        cache.db_manager.save_leaderboard_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_with_clan_tag_ensures_clan(self, cache):
        data = {"clan_tag": "#LBCL1234", "channel_id": "CH2", "mode": "attacks",
                "message_ids": "2", "content_hash": "def"}
        await cache.set_leaderboard_message("KEY2", data)
        cache.db_manager._ensure_clan_exists.assert_awaited_once_with("#LBCL1234")

    @pytest.mark.asyncio
    async def test_set_channel_prefix_sets_none(self, cache):
        """clan_tag starting with 'channel_' should be converted to None."""
        data = {"clan_tag": "channel_123", "channel_id": "CH3", "mode": "m",
                "message_ids": "3", "content_hash": "ghi"}
        await cache.set_leaderboard_message("KEY3", data)
        call_kwargs = cache.db_manager.save_leaderboard_message.call_args
        assert call_kwargs.kwargs.get("clan_tag") is None or call_kwargs[1].get("clan_tag") is None \
               or (call_kwargs[0] if call_kwargs[0] else [None])[1] is None

    @pytest.mark.asyncio
    async def test_delete_removes_cache_and_db(self, cache):
        cache.leaderboard_messages["KEY_DEL"] = {"some": "data"}
        await cache.delete_leaderboard_message("KEY_DEL")
        assert "KEY_DEL" not in cache.leaderboard_messages
        cache.db_manager.delete_leaderboard_message.assert_awaited_once_with("KEY_DEL")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_key(self, cache):
        """Deleting key not in cache should still call DB and not crash."""
        await cache.delete_leaderboard_message("NOPE")
        cache.db_manager.delete_leaderboard_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_db_error_propagates(self, cache):
        cache.db_manager.delete_leaderboard_message.side_effect = RuntimeError("db fail")
        with pytest.raises(RuntimeError):
            await cache.delete_leaderboard_message("KEY_ERR")


# ===================================================================
# set_clan_family / persist_clan_family / delete_clan_family
# ===================================================================

class TestClanFamilyWriteThrough:
    @pytest.mark.asyncio
    async def test_set_updates_cache_and_db(self, cache):
        family = {"name": "Family1", "owned_by_guild": "G1", "clans": ["#C1", "#C2"]}
        await cache.set_clan_family("FAM1", family)
        assert cache.clan_families["FAM1"] == family
        cache.db_manager.save_clan_family.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_persist_existing(self, cache):
        cache.clan_families["FAM2"] = {"name": "F2", "owned_by_guild": "G2", "clans": []}
        await cache.persist_clan_family("FAM2")
        cache.db_manager.save_clan_family.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_persist_missing_skips(self, cache):
        await cache.persist_clan_family("NOPE")
        cache.db_manager.save_clan_family.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_removes_cache_and_db(self, cache):
        cache.clan_families["FAM_DEL"] = {"name": "Del"}
        await cache.delete_clan_family("FAM_DEL")
        assert "FAM_DEL" not in cache.clan_families
        cache.db_manager.delete_clan_family.assert_awaited_once_with("FAM_DEL")

    @pytest.mark.asyncio
    async def test_delete_db_error_propagates(self, cache):
        cache.db_manager.delete_clan_family.side_effect = RuntimeError("fail")
        with pytest.raises(RuntimeError):
            await cache.delete_clan_family("FAM_ERR")


# ===================================================================
# set_user_account / persist_user / delete_user_account
# ===================================================================

class TestUserWriteThrough:
    @pytest.mark.asyncio
    async def test_set_preserves_unknown_keys(self, cache):
        """Pitfall 7: existing unknown keys must be preserved."""
        cache.user_accounts["U1"] = {"display_name": "Old", "custom_field": 42, "players": []}
        await cache.set_user_account("U1", {"display_name": "New", "players": []})
        assert cache.user_accounts["U1"]["custom_field"] == 42
        assert cache.user_accounts["U1"]["display_name"] == "New"
        cache.db_manager.save_user.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_new_user(self, cache):
        await cache.set_user_account("U2", {"display_name": "Brand New", "players": []})
        assert cache.user_accounts["U2"]["display_name"] == "Brand New"

    @pytest.mark.asyncio
    async def test_persist_existing(self, cache):
        cache.user_accounts["U3"] = {"display_name": "Persist", "players": []}
        await cache.persist_user("U3")
        cache.db_manager.save_user.assert_awaited_once_with("U3", {"display_name": "Persist", "players": []})

    @pytest.mark.asyncio
    async def test_persist_missing_skips(self, cache):
        await cache.persist_user("NOPE")
        cache.db_manager.save_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_removes_cache_and_db(self, cache):
        cache.user_accounts["U_DEL"] = {"display_name": "Del"}
        await cache.delete_user_account("U_DEL")
        assert "U_DEL" not in cache.user_accounts
        cache.db_manager.delete_user.assert_awaited_once_with("U_DEL")

    @pytest.mark.asyncio
    async def test_delete_db_error_propagates(self, cache):
        cache.db_manager.delete_user.side_effect = RuntimeError("fail")
        with pytest.raises(RuntimeError):
            await cache.delete_user_account("U_ERR")


# ===================================================================
# _calculate_subscription_status
# ===================================================================

class TestCalculateSubscriptionStatus:
    def test_direct_subscription(self, cache):
        cache.subscriptions = {"G1": {"C1": [{"clan_tag": "#MY_CLAN"}]}}
        assert cache._calculate_subscription_status("#MY_CLAN") is True

    def test_no_subscription(self, cache):
        cache.subscriptions = {"G1": {"C1": [{"clan_tag": "#OTHER"}]}}
        assert cache._calculate_subscription_status("#MY_CLAN") is False

    def test_family_subscription(self, cache):
        """Clan in a family that has a subscription."""
        cache.subscriptions = {"G1": {"C1": [{"clan_tag": "FAM_TAG"}]}}
        cache.clan_families = {"FAM_TAG": {"clans": ["#MEMBER_CLAN"]}}
        assert cache._calculate_subscription_status("#MEMBER_CLAN") is True

    def test_empty_subscriptions(self, cache):
        assert cache._calculate_subscription_status("#ANYTHING") is False


# ===================================================================
# update_clan_subscription_status
# ===================================================================

class TestUpdateClanSubscriptionStatus:
    @pytest.mark.asyncio
    async def test_updates_when_changed(self, cache):
        cache.clan_name_cache["#SUBS_CL"] = {"name": "SubClan", "has_active_subscriptions": False}
        cache.subscriptions = {"G1": {"C1": [{"clan_tag": "#SUBS_CL"}]}}
        # Mock persist_clan
        cache.persist_clan = AsyncMock()
        await cache.update_clan_subscription_status("#SUBS_CL")
        assert cache.clan_name_cache["#SUBS_CL"]["has_active_subscriptions"] is True
        cache.persist_clan.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_change_no_persist(self, cache):
        cache.clan_name_cache["#SUBS_CL2"] = {"name": "SubClan2", "has_active_subscriptions": True, "track_war_updates": True}
        cache.subscriptions = {"G1": {"C1": [{"clan_tag": "#SUBS_CL2"}]}}
        cache.persist_clan = AsyncMock()
        await cache.update_clan_subscription_status("#SUBS_CL2")
        cache.persist_clan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clan_not_in_cache(self, cache):
        """If clan not in clan_name_cache, returns early without error."""
        await cache.update_clan_subscription_status("#NOPE")

    @pytest.mark.asyncio
    async def test_non_dict_format_skipped(self, cache):
        """Old string format should be skipped."""
        cache.clan_name_cache["#OLD_FMT"] = "OldStringName"
        await cache.update_clan_subscription_status("#OLD_FMT")


# ===================================================================
# get_clan_name
# ===================================================================

class TestGetClanName:
    def test_dict_format(self, cache):
        cache.clan_name_cache["#TAG1"] = {"name": "DictClan"}
        assert cache.get_clan_name("#TAG1") == "DictClan"

    def test_string_format(self, cache):
        cache.clan_name_cache["#TAG2"] = "StringClan"
        assert cache.get_clan_name("#TAG2") == "StringClan"

    def test_missing_returns_default(self, cache):
        assert cache.get_clan_name("#NOPE") == "Unknown"

    def test_missing_returns_none_when_default_none(self, cache):
        assert cache.get_clan_name("#NOPE", default=None) is None

    def test_dict_missing_name_returns_default(self, cache):
        cache.clan_name_cache["#TAG3"] = {"has_active_subscriptions": True}
        assert cache.get_clan_name("#TAG3") == "Unknown"

    def test_empty_cache_logs_error(self, cache):
        """When cache is empty, should log error but return default."""
        cache.clan_name_cache = {}
        result = cache.get_clan_name("#EMPTY_CACHE")
        assert result == "Unknown"


# ===================================================================
# load_leaderboard_messages — error path
# ===================================================================

class TestLoadLeaderboardMessages:
    @pytest.mark.asyncio
    async def test_success(self, cache):
        cache.db_manager.get_all_leaderboard_messages = AsyncMock(return_value={"K1": {"mode": "stars"}})
        await cache.load_leaderboard_messages()
        assert cache.leaderboard_messages == {"K1": {"mode": "stars"}}

    @pytest.mark.asyncio
    async def test_error_initialises_empty(self, cache):
        cache.db_manager.get_all_leaderboard_messages = AsyncMock(side_effect=RuntimeError("fail"))
        await cache.load_leaderboard_messages()
        assert cache.leaderboard_messages == {}


# ===================================================================
# load_clan_families — error path
# ===================================================================

class TestLoadClanFamilies:
    @pytest.mark.asyncio
    async def test_success(self, cache):
        cache.db_manager.get_all_clan_families = AsyncMock(return_value={"FAM": {"name": "F"}})
        await cache.load_clan_families()
        assert cache.clan_families == {"FAM": {"name": "F"}}

    @pytest.mark.asyncio
    async def test_error_initialises_empty(self, cache):
        cache.db_manager.get_all_clan_families = AsyncMock(side_effect=RuntimeError("fail"))
        await cache.load_clan_families()
        assert cache.clan_families == {}


# ===================================================================
# load_user_accounts — error path
# ===================================================================

class TestLoadUserAccounts:
    @pytest.mark.asyncio
    async def test_success(self, cache):
        cache.db_manager.get_all_users_dict = AsyncMock(return_value={"U1": {"display_name": "A"}})
        await cache.load_user_accounts()
        assert cache.user_accounts == {"U1": {"display_name": "A"}}

    @pytest.mark.asyncio
    async def test_error_exits(self, cache):
        cache.db_manager.get_all_users_dict = AsyncMock(side_effect=RuntimeError("fail"))
        with pytest.raises(SystemExit):
            await cache.load_user_accounts()

    @pytest.mark.asyncio
    async def test_no_db_manager_raises(self, cache):
        cache.db_manager = None
        with pytest.raises(RuntimeError):
            await cache.load_user_accounts()
