"""Extended tests for qapbot/cache_manager.py — write-through methods & sync helpers.

Targets uncovered cache mutation methods: set_subscriptions_for_channel,
delete_subscriptions_for_channel, delete_subscriptions_for_guild,
set_leaderboard_message, delete_leaderboard_message,
set_clan_family, persist_clan_family, delete_clan_family,
set_user_account, persist_user, delete_user_account,
_calculate_subscription_status, get_clan_name, update_clan_subscription_status.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false
# pyright: reportOptionalMemberAccess=false, reportAttributeAccessIssue=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnusedImport=false
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict, List

import pytest

from qapbot.cache_manager import CacheManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cm_with_db() -> CacheManager:
    """Return a CacheManager with a mocked db_manager."""
    cm = CacheManager()
    cm.db_manager = MagicMock()
    # Make async methods return coroutines
    cm.db_manager.save_subscriptions_for_channel = AsyncMock()
    cm.db_manager.delete_subscriptions_for_channel = AsyncMock()
    cm.db_manager.delete_subscriptions_for_guild = AsyncMock()
    cm.db_manager.save_leaderboard_message = AsyncMock()
    cm.db_manager.delete_leaderboard_message = AsyncMock()
    cm.db_manager._ensure_clan_exists = AsyncMock()
    cm.db_manager.save_clan_family = AsyncMock()
    cm.db_manager.delete_clan_family = AsyncMock()
    cm.db_manager.save_user = AsyncMock()
    cm.db_manager.delete_user = AsyncMock()
    cm.db_manager.save_clan = AsyncMock()
    cm.db_manager.bulk_update_clan_subscription_statuses = AsyncMock()
    cm.db_manager.bulk_update_clan_track_war_updates = AsyncMock()
    return cm


# ---------------------------------------------------------------------------
# CacheManager.__init__
# ---------------------------------------------------------------------------

class TestCacheManagerInit:
    def test_starts_empty(self):
        cm = CacheManager()
        assert cm.clan_name_cache == {}
        assert cm.subscriptions == {}
        assert cm.leaderboard_messages == {}
        assert cm.clan_families == {}
        assert cm.user_accounts == {}
        assert cm.notification_state == {}
        assert cm.server_config == {}
        assert cm.temp_war_stats == {}
        assert cm.clan_history == {}
        assert cm.coc_client is None
        assert cm.db_manager is None

    def test_coc_clan_cache_backreference(self):
        cm = CacheManager()
        assert cm.coc_clan_cache.cache_manager is cm


# ---------------------------------------------------------------------------
# set_subscriptions_for_channel
# ---------------------------------------------------------------------------

class TestSetSubscriptionsForChannel:
    @pytest.mark.asyncio
    async def test_creates_guild_entry(self):
        cm = _cm_with_db()
        subs = [{"clan_tag": "#ABC", "subscription_type": "attack"}]
        await cm.set_subscriptions_for_channel("G1", "C1", subs)

        assert cm.subscriptions["G1"]["C1"] == subs
        cm.db_manager.save_subscriptions_for_channel.assert_awaited_once_with("G1", "C1", subs)

    @pytest.mark.asyncio
    async def test_appends_to_existing_guild(self):
        cm = _cm_with_db()
        cm.subscriptions["G1"] = {"C1": [{"clan_tag": "#A"}]}
        new_subs = [{"clan_tag": "#B"}]
        await cm.set_subscriptions_for_channel("G1", "C2", new_subs)

        assert "C1" in cm.subscriptions["G1"]
        assert cm.subscriptions["G1"]["C2"] == new_subs

    @pytest.mark.asyncio
    async def test_db_failure_raises(self):
        cm = _cm_with_db()
        cm.db_manager.save_subscriptions_for_channel = AsyncMock(side_effect=RuntimeError("DB fail"))
        with pytest.raises(RuntimeError):
            await cm.set_subscriptions_for_channel("G1", "C1", [])


# ---------------------------------------------------------------------------
# delete_subscriptions_for_channel
# ---------------------------------------------------------------------------

class TestDeleteSubscriptionsForChannel:
    @pytest.mark.asyncio
    async def test_removes_channel(self):
        cm = _cm_with_db()
        cm.subscriptions = {"G1": {"C1": [{"clan_tag": "#A"}], "C2": [{"clan_tag": "#B"}]}}
        await cm.delete_subscriptions_for_channel("G1", "C1")

        assert "C1" not in cm.subscriptions["G1"]
        assert "C2" in cm.subscriptions["G1"]

    @pytest.mark.asyncio
    async def test_removes_empty_guild(self):
        cm = _cm_with_db()
        cm.subscriptions = {"G1": {"C1": [{"clan_tag": "#A"}]}}
        await cm.delete_subscriptions_for_channel("G1", "C1")

        assert "G1" not in cm.subscriptions

    @pytest.mark.asyncio
    async def test_noop_for_missing_guild(self):
        cm = _cm_with_db()
        await cm.delete_subscriptions_for_channel("G1", "C1")
        cm.db_manager.delete_subscriptions_for_channel.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_noop_for_missing_channel(self):
        cm = _cm_with_db()
        cm.subscriptions = {"G1": {"C2": []}}
        await cm.delete_subscriptions_for_channel("G1", "C1")
        assert "C2" in cm.subscriptions["G1"]


# ---------------------------------------------------------------------------
# delete_subscriptions_for_guild
# ---------------------------------------------------------------------------

class TestDeleteSubscriptionsForGuild:
    @pytest.mark.asyncio
    async def test_removes_guild(self):
        cm = _cm_with_db()
        cm.subscriptions = {"G1": {"C1": []}, "G2": {"C2": []}}
        await cm.delete_subscriptions_for_guild("G1")

        assert "G1" not in cm.subscriptions
        assert "G2" in cm.subscriptions
        cm.db_manager.delete_subscriptions_for_guild.assert_awaited_once_with("G1")

    @pytest.mark.asyncio
    async def test_noop_for_missing_guild(self):
        cm = _cm_with_db()
        await cm.delete_subscriptions_for_guild("G1")
        cm.db_manager.delete_subscriptions_for_guild.assert_awaited_once()


# ---------------------------------------------------------------------------
# _calculate_subscription_status
# ---------------------------------------------------------------------------

class TestCalculateSubscriptionStatus:
    def test_direct_subscription(self):
        cm = CacheManager()
        cm.subscriptions = {"G1": {"C1": [{"clan_tag": "#CLAN"}]}}
        assert cm._calculate_subscription_status("#CLAN") is True

    def test_no_subscription(self):
        cm = CacheManager()
        cm.subscriptions = {"G1": {"C1": [{"clan_tag": "#OTHER"}]}}
        assert cm._calculate_subscription_status("#CLAN") is False

    def test_family_subscription(self):
        cm = CacheManager()
        cm.clan_families = {"#FAM1": {"name": "Family", "clans": ["#CLAN1", "#CLAN2"]}}
        cm.subscriptions = {"G1": {"C1": [{"clan_tag": "#FAM1"}]}}
        assert cm._calculate_subscription_status("#CLAN1") is True
        assert cm._calculate_subscription_status("#CLAN2") is True
        assert cm._calculate_subscription_status("#CLAN3") is False

    def test_empty_subscriptions(self):
        cm = CacheManager()
        assert cm._calculate_subscription_status("#CLAN") is False


# ---------------------------------------------------------------------------
# set_leaderboard_message
# ---------------------------------------------------------------------------

class TestSetLeaderboardMessage:
    @pytest.mark.asyncio
    async def test_sets_and_persists(self):
        cm = _cm_with_db()
        data = {
            "clan_tag": "#CLAN1",
            "channel_id": "123",
            "mode": "stars_01_2025",
            "message_ids": "999",
            "content_hash": "abc123",
        }
        await cm.set_leaderboard_message("KEY1", data)

        assert cm.leaderboard_messages["KEY1"] == data
        cm.db_manager._ensure_clan_exists.assert_awaited_once()
        cm.db_manager.save_leaderboard_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_channel_prefix_clan_tag_becomes_none(self):
        cm = _cm_with_db()
        data = {
            "clan_tag": "channel_123",
            "channel_id": "123",
            "mode": "playerlist",
            "message_ids": "999",
            "content_hash": "def456",
        }
        await cm.set_leaderboard_message("KEY2", data)
        # _ensure_clan_exists should NOT be called for channel_ prefix
        cm.db_manager._ensure_clan_exists.assert_not_awaited()


# ---------------------------------------------------------------------------
# delete_leaderboard_message
# ---------------------------------------------------------------------------

class TestDeleteLeaderboardMessage:
    @pytest.mark.asyncio
    async def test_deletes_existing(self):
        cm = _cm_with_db()
        cm.leaderboard_messages["KEY1"] = {"mode": "stars"}
        await cm.delete_leaderboard_message("KEY1")

        assert "KEY1" not in cm.leaderboard_messages
        cm.db_manager.delete_leaderboard_message.assert_awaited_once_with("KEY1")

    @pytest.mark.asyncio
    async def test_deletes_missing_key_no_error(self):
        cm = _cm_with_db()
        await cm.delete_leaderboard_message("KEY_MISSING")
        cm.db_manager.delete_leaderboard_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_db_failure_raises(self):
        cm = _cm_with_db()
        cm.db_manager.delete_leaderboard_message = AsyncMock(side_effect=RuntimeError("DB"))
        with pytest.raises(RuntimeError):
            await cm.delete_leaderboard_message("KEY1")


# ---------------------------------------------------------------------------
# set_clan_family / persist_clan_family / delete_clan_family
# ---------------------------------------------------------------------------

class TestClanFamilyOps:
    @pytest.mark.asyncio
    async def test_set_clan_family(self):
        cm = _cm_with_db()
        data = {"name": "TestFamily", "clans": ["#A", "#B"], "owned_by_guild": "G1"}
        await cm.set_clan_family("#FAM1", data)

        assert cm.clan_families["#FAM1"] == data
        cm.db_manager.save_clan_family.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_persist_clan_family(self):
        cm = _cm_with_db()
        cm.clan_families["#FAM1"] = {"name": "F", "clans": [], "owned_by_guild": "G1"}
        await cm.persist_clan_family("#FAM1")
        cm.db_manager.save_clan_family.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_persist_clan_family_missing_warns(self):
        cm = _cm_with_db()
        await cm.persist_clan_family("#NOPE")
        cm.db_manager.save_clan_family.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_clan_family(self):
        cm = _cm_with_db()
        cm.clan_families["#FAM1"] = {"name": "F"}
        await cm.delete_clan_family("#FAM1")

        assert "#FAM1" not in cm.clan_families
        cm.db_manager.delete_clan_family.assert_awaited_once_with("#FAM1")

    @pytest.mark.asyncio
    async def test_delete_clan_family_missing_still_deletes_db(self):
        cm = _cm_with_db()
        await cm.delete_clan_family("#FAM_MISSING")
        cm.db_manager.delete_clan_family.assert_awaited_once()


# ---------------------------------------------------------------------------
# set_user_account / persist_user / delete_user_account
# ---------------------------------------------------------------------------

class TestUserAccountOps:
    @pytest.mark.asyncio
    async def test_set_user_account_new(self):
        cm = _cm_with_db()
        data = {"display_name": "Alice", "players": []}
        await cm.set_user_account("100", data)

        assert cm.user_accounts["100"] == data
        cm.db_manager.save_user.assert_awaited_once_with("100", data)

    @pytest.mark.asyncio
    async def test_set_user_account_preserves_unknown_fields(self):
        """Pitfall 7: unknown keys must be preserved."""
        cm = _cm_with_db()
        cm.user_accounts["100"] = {
            "display_name": "Alice",
            "players": [],
            "custom_field": "keep_me",
        }
        new_data = {"display_name": "Alice2", "players": []}
        await cm.set_user_account("100", new_data)

        assert cm.user_accounts["100"]["custom_field"] == "keep_me"
        assert cm.user_accounts["100"]["display_name"] == "Alice2"

    @pytest.mark.asyncio
    async def test_persist_user(self):
        cm = _cm_with_db()
        cm.user_accounts["100"] = {"display_name": "Bob", "players": []}
        await cm.persist_user("100")
        cm.db_manager.save_user.assert_awaited_once_with("100", cm.user_accounts["100"])

    @pytest.mark.asyncio
    async def test_persist_user_missing_skips(self):
        cm = _cm_with_db()
        await cm.persist_user("MISSING")
        cm.db_manager.save_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_user_account(self):
        cm = _cm_with_db()
        cm.user_accounts["100"] = {"display_name": "X"}
        await cm.delete_user_account("100")

        assert "100" not in cm.user_accounts
        cm.db_manager.delete_user.assert_awaited_once_with("100")

    @pytest.mark.asyncio
    async def test_delete_user_account_missing_still_deletes_db(self):
        cm = _cm_with_db()
        await cm.delete_user_account("MISSING")
        cm.db_manager.delete_user.assert_awaited_once()


# ---------------------------------------------------------------------------
# get_clan_name
# ---------------------------------------------------------------------------

class TestGetClanName:
    def test_dict_format(self):
        cm = CacheManager()
        cm.clan_name_cache = {"#TAG": {"name": "MyClan", "has_active_subscriptions": True}}
        assert cm.get_clan_name("#TAG") == "MyClan"

    def test_missing_clan(self):
        cm = CacheManager()
        assert cm.get_clan_name("#MISSING") == "Unknown"

    def test_missing_clan_none_default(self):
        cm = CacheManager()
        assert cm.get_clan_name("#MISSING", default=None) is None

    def test_string_format_fallback(self):
        """Legacy string format should still work."""
        cm = CacheManager()
        cm.clan_name_cache = {"#TAG": "LegacyClan"}  # type: ignore
        assert cm.get_clan_name("#TAG") == "LegacyClan"

    def test_dict_missing_name_key(self):
        cm = CacheManager()
        cm.clan_name_cache = {"#TAG": {"has_active_subscriptions": True}}
        assert cm.get_clan_name("#TAG") == "Unknown"

    def test_empty_cache_logs_error(self):
        cm = CacheManager()
        cm.clan_name_cache = {}
        result = cm.get_clan_name("#TAG")
        assert result == "Unknown"


# ---------------------------------------------------------------------------
# update_clan_subscription_status
# ---------------------------------------------------------------------------

class TestUpdateClanSubscriptionStatus:
    @pytest.mark.asyncio
    async def test_updates_when_changed(self):
        cm = _cm_with_db()
        cm.db_manager.save_clan = AsyncMock()
        cm.clan_name_cache = {"#TAG": {"name": "C", "has_active_subscriptions": False}}
        cm.subscriptions = {"G1": {"C1": [{"clan_tag": "#TAG"}]}}
        # persist_clan calls db_manager internally
        with patch.object(cm, "persist_clan", new_callable=AsyncMock) as mock_persist:
            await cm.update_clan_subscription_status("#TAG")
            assert cm.clan_name_cache["#TAG"]["has_active_subscriptions"] is True
            mock_persist.assert_awaited_once_with("#TAG")

    @pytest.mark.asyncio
    async def test_no_update_when_unchanged(self):
        cm = _cm_with_db()
        cm.clan_name_cache = {"#TAG": {"name": "C", "has_active_subscriptions": True, "track_war_updates": True}}
        cm.subscriptions = {"G1": {"C1": [{"clan_tag": "#TAG"}]}}
        with patch.object(cm, "persist_clan", new_callable=AsyncMock) as mock_persist:
            await cm.update_clan_subscription_status("#TAG")
            mock_persist.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clan_not_in_cache(self):
        cm = _cm_with_db()
        cm.clan_name_cache = {}
        await cm.update_clan_subscription_status("#MISSING")  # should not raise

    @pytest.mark.asyncio
    async def test_non_dict_format_skips(self):
        cm = _cm_with_db()
        cm.clan_name_cache = {"#TAG": "OldFormat"}  # type: ignore
        await cm.update_clan_subscription_status("#TAG")  # should not raise


# ---------------------------------------------------------------------------
# update_all_clan_subscription_statuses
# ---------------------------------------------------------------------------

class TestUpdateAllClanSubscriptionStatuses:
    @pytest.mark.asyncio
    async def test_updates_changed_clans(self):
        cm = _cm_with_db()
        cm.clan_name_cache = {
            "#A": {"name": "A", "has_active_subscriptions": False},
            "#B": {"name": "B", "has_active_subscriptions": True},
        }
        cm.subscriptions = {"G1": {"C1": [{"clan_tag": "#A"}]}}
        await cm.update_all_clan_subscription_statuses()
        assert cm.clan_name_cache["#A"]["has_active_subscriptions"] is True
        assert cm.clan_name_cache["#B"]["has_active_subscriptions"] is False
        cm.db_manager.bulk_update_clan_subscription_statuses.assert_awaited_once()
        call_args = cm.db_manager.bulk_update_clan_subscription_statuses.call_args[0][0]
        assert set(call_args) == {(True, "#A"), (False, "#B")}

    @pytest.mark.asyncio
    async def test_no_changes_nothing_persisted(self):
        cm = _cm_with_db()
        cm.clan_name_cache = {
            "#A": {"name": "A", "has_active_subscriptions": False},
        }
        cm.subscriptions = {}
        await cm.update_all_clan_subscription_statuses()
        cm.db_manager.bulk_update_clan_subscription_statuses.assert_not_awaited()
