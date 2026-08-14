"""Tests for cache_manager.update_user_metadata + coc_cache._update_clan_metadata — Phase 5 coverage push.

Covers:
- update_user_metadata (~50 lines)
- _update_clan_metadata (~45 lines)
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import discord


# ---------------------------------------------------------------------------
# update_user_metadata
# ---------------------------------------------------------------------------

class TestUpdateUserMetadata:
    def _cm(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager()
        cm.user_accounts = {}
        cm.users_loaded = True  # tests exercise post-startup behavior; pre-load gate tested in test_cache_manager.py
        cm.persist_user = AsyncMock()
        return cm

    @pytest.mark.asyncio
    async def test_new_user_created(self, monkeypatch):
        cm = self._cm()
        user_obj = MagicMock(spec=discord.User)
        user_obj.display_name = "TestUser"

        bot = AsyncMock()
        monkeypatch.setattr("QBcore.bot", bot)

        result = await cm.update_user_metadata("12345", user_obj=user_obj)
        assert result is True
        assert "12345" in cm.user_accounts
        assert cm.user_accounts["12345"]["display_name"] == "TestUser"
        cm.persist_user.assert_awaited_once_with("12345")

    @pytest.mark.asyncio
    async def test_display_name_updated(self, monkeypatch):
        cm = self._cm()
        cm.user_accounts["12345"] = {"display_name": "OldName", "user_language": "en", "players": []}
        user_obj = MagicMock(spec=discord.User)
        user_obj.display_name = "NewName"

        bot = AsyncMock()
        monkeypatch.setattr("QBcore.bot", bot)

        result = await cm.update_user_metadata("12345", user_obj=user_obj)
        assert result is True
        assert cm.user_accounts["12345"]["display_name"] == "NewName"

    @pytest.mark.asyncio
    async def test_no_change_no_persist(self, monkeypatch):
        cm = self._cm()
        cm.user_accounts["12345"] = {
            "display_name": "SameName",
            "user_language": "en",
            "notification_settings": {"notification_mode": "repeated", "notification_type": "all_wars", "hours_before_end": 4, "war_reminders": True},
            "players": [],
        }
        user_obj = MagicMock(spec=discord.User)
        user_obj.display_name = "SameName"

        bot = AsyncMock()
        monkeypatch.setattr("QBcore.bot", bot)

        result = await cm.update_user_metadata("12345", user_obj=user_obj)
        assert result is False
        cm.persist_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_locale_updates_language(self, monkeypatch):
        cm = self._cm()
        cm.user_accounts["12345"] = {"display_name": "User", "user_language": "en", "players": []}
        interaction = MagicMock()
        interaction.user = MagicMock(spec=discord.User)
        interaction.user.display_name = "User"
        interaction.locale = "de-DE"

        bot = AsyncMock()
        monkeypatch.setattr("QBcore.bot", bot)

        result = await cm.update_user_metadata("12345", interaction=interaction)
        assert result is True
        assert cm.user_accounts["12345"]["user_language"] == "de"

    @pytest.mark.asyncio
    async def test_locked_language_not_updated(self, monkeypatch):
        cm = self._cm()
        cm.user_accounts["12345"] = {"display_name": "User", "user_language": "en", "user_language_locked": True, "players": []}
        interaction = MagicMock()
        interaction.user = MagicMock(spec=discord.User)
        interaction.user.display_name = "User"
        interaction.locale = "de-DE"

        bot = AsyncMock()
        monkeypatch.setattr("QBcore.bot", bot)

        result = await cm.update_user_metadata("12345", interaction=interaction)
        assert result is False
        assert cm.user_accounts["12345"]["user_language"] == "en"

    @pytest.mark.asyncio
    async def test_fetch_user_when_no_obj(self, monkeypatch):
        cm = self._cm()
        user_obj = MagicMock(spec=discord.User)
        user_obj.display_name = "FetchedUser"

        bot = AsyncMock()
        bot.fetch_user = AsyncMock(return_value=user_obj)
        monkeypatch.setattr("QBcore.bot", bot)

        result = await cm.update_user_metadata("12345")
        assert result is True
        bot.fetch_user.assert_awaited_once_with(12345)

    @pytest.mark.asyncio
    async def test_not_found_raises(self, monkeypatch):
        cm = self._cm()
        bot = AsyncMock()
        bot.fetch_user = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
        monkeypatch.setattr("QBcore.bot", bot)

        with pytest.raises(discord.NotFound):
            await cm.update_user_metadata("12345")

    @pytest.mark.asyncio
    async def test_generic_error_returns_false(self, monkeypatch):
        cm = self._cm()
        bot = AsyncMock()
        bot.fetch_user = AsyncMock(side_effect=RuntimeError("Something went wrong"))
        monkeypatch.setattr("QBcore.bot", bot)

        result = await cm.update_user_metadata("12345")
        assert result is False


# ---------------------------------------------------------------------------
# _update_clan_metadata
# ---------------------------------------------------------------------------

class TestUpdateClanMetadata:
    def _coc_cache(self):
        from qapbot.coc_cache import CoCClanCache
        cc = CoCClanCache.__new__(CoCClanCache)
        cc.cache_manager = MagicMock()
        cc.cache_manager.clan_name_cache = {}
        cc.cache_manager.persist_clan = AsyncMock()
        cc.cache_manager.server_config = {}
        cc._cache = {}
        cc._refreshing = set()
        cc._update_warlog_status = AsyncMock()
        cc.update_player_info_in_user_accounts = AsyncMock()
        cc._schedule_role_sync_for_clan = MagicMock()
        return cc

    def _clan_obj(self, tag="#CLAN1", name="TestClan"):
        obj = MagicMock()
        obj.tag = tag
        obj.name = name
        obj.members = []
        obj.public_war_log = True
        obj.war_league = None  # Explicitly None so war_league dirty-tracking is a no-op
        return obj

    @pytest.mark.asyncio
    async def test_new_clan_added(self):
        cc = self._coc_cache()
        now = datetime.now(timezone.utc)
        await cc._update_clan_metadata(self._clan_obj(), now)
        assert "#CLAN1" in cc.cache_manager.clan_name_cache
        assert cc.cache_manager.clan_name_cache["#CLAN1"]["name"] == "TestClan"
        cc.cache_manager.persist_clan.assert_awaited()

    @pytest.mark.asyncio
    async def test_non_dict_format_overwritten(self):
        cc = self._coc_cache()
        cc.cache_manager.clan_name_cache["#CLAN1"] = "just_a_string"  # type: ignore[assignment]
        now = datetime.now(timezone.utc)
        await cc._update_clan_metadata(self._clan_obj(), now)
        assert isinstance(cc.cache_manager.clan_name_cache["#CLAN1"], dict)
        cc.cache_manager.persist_clan.assert_awaited()

    @pytest.mark.asyncio
    async def test_name_change_triggers_persist(self):
        cc = self._coc_cache()
        cc.cache_manager.clan_name_cache["#CLAN1"] = {
            "name": "OldName",
            "has_active_subscriptions": False,
            "last_war_update": None,
            "warlog_is_public": True,
            "last_checked_via_api": datetime.now(timezone.utc).isoformat(),
        }
        now = datetime.now(timezone.utc)
        await cc._update_clan_metadata(self._clan_obj(name="NewName"), now)
        assert cc.cache_manager.clan_name_cache["#CLAN1"]["name"] == "NewName"
        cc.cache_manager.persist_clan.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_change_no_persist_within_hour(self):
        cc = self._coc_cache()
        now = datetime.now(timezone.utc)
        cc.cache_manager.clan_name_cache["#CLAN1"] = {
            "name": "TestClan",
            "has_active_subscriptions": False,
            "last_war_update": None,
            "warlog_is_public": True,
            "last_checked_via_api": (now - timedelta(minutes=30)).isoformat(),
        }
        await cc._update_clan_metadata(self._clan_obj(), now)
        cc.cache_manager.persist_clan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_timestamp_triggers_persist(self):
        cc = self._coc_cache()
        now = datetime.now(timezone.utc)
        cc.cache_manager.clan_name_cache["#CLAN1"] = {
            "name": "TestClan",
            "has_active_subscriptions": False,
            "last_war_update": None,
            "warlog_is_public": True,
            "last_checked_via_api": (now - timedelta(hours=2)).isoformat(),
        }
        await cc._update_clan_metadata(self._clan_obj(), now)
        cc.cache_manager.persist_clan.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_timestamp_triggers_persist(self):
        cc = self._coc_cache()
        now = datetime.now(timezone.utc)
        cc.cache_manager.clan_name_cache["#CLAN1"] = {
            "name": "TestClan",
            "has_active_subscriptions": False,
            "last_war_update": None,
            "warlog_is_public": True,
        }
        await cc._update_clan_metadata(self._clan_obj(), now)
        cc.cache_manager.persist_clan.assert_awaited()

    @pytest.mark.asyncio
    async def test_invalid_timestamp_triggers_persist(self):
        cc = self._coc_cache()
        now = datetime.now(timezone.utc)
        cc.cache_manager.clan_name_cache["#CLAN1"] = {
            "name": "TestClan",
            "has_active_subscriptions": False,
            "last_war_update": None,
            "warlog_is_public": True,
            "last_checked_via_api": "NOT_A_DATE",
        }
        await cc._update_clan_metadata(self._clan_obj(), now)
        cc.cache_manager.persist_clan.assert_awaited()

    @pytest.mark.asyncio
    async def test_delegates_to_warlog_and_player_update_when_subscribed(self):
        """2026-08-14: update_player_info_in_user_accounts must only run for clans a guild
        actually configured (has_active_subscriptions — member_clans/member_families/channel
        subscriptions, see update_all_clan_subscription_statuses()) — not every clan this
        process's shared get_clan() cache happens to touch (e.g. CWL opponents). Confirmed live
        on PROD: without this gate, the O(len(user_accounts)) scan inside
        update_player_info_in_user_accounts ran on every one of ~380K cached clans instead of
        just the guild's own member clans, both polluting user_players with non-member accounts
        and making every clan fetch drastically slower."""
        cc = self._coc_cache()
        cc.cache_manager.clan_name_cache["#CLAN1"] = {
            "name": "TestClan",
            "has_active_subscriptions": True,
            "last_war_update": None,
            "warlog_is_public": True,
            "last_checked_via_api": datetime.now(timezone.utc).isoformat(),
        }
        now = datetime.now(timezone.utc)
        clan = self._clan_obj()
        await cc._update_clan_metadata(clan, now)
        cc._update_warlog_status.assert_awaited_once()
        cc.update_player_info_in_user_accounts.assert_awaited_once()
        cc._schedule_role_sync_for_clan.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_player_update_when_not_subscribed(self):
        """A clan with no guild configuration behind it (has_active_subscriptions=False —
        e.g. a CWL opponent or family-harvested clan this process merely happened to see)
        must NOT trigger update_player_info_in_user_accounts. Covers both a never-before-seen
        clan (defaults has_active_subscriptions=False on creation) and an existing,
        never-subscribed one."""
        cc = self._coc_cache()
        now = datetime.now(timezone.utc)
        clan = self._clan_obj()
        await cc._update_clan_metadata(clan, now)  # brand-new clan — defaults to False
        cc._update_warlog_status.assert_awaited_once()
        cc.update_player_info_in_user_accounts.assert_not_awaited()
        cc._schedule_role_sync_for_clan.assert_called_once()
