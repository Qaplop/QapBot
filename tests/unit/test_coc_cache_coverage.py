"""Extended tests for qapbot/coc_cache.py — sync helpers & cache lifecycle.

Targets uncovered lines for: invalidate(), clear_expired(), get_stats(),
get_memory_usage_mb(), TTL initialization, _update_warlog_status,
_update_clan_metadata (dirty tracking), update_player_info_in_user_accounts.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false
# pyright: reportUnusedImport=false
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapbot.coc_cache import CoCClanCache


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestCoCClanCacheInit:
    def test_default_ttls(self):
        c = CoCClanCache()
        assert c.soft_ttl_seconds == 280
        assert c.hard_ttl_seconds == 600

    def test_custom_ttls(self):
        c = CoCClanCache(soft_ttl_seconds=60, hard_ttl_seconds=120)
        assert c.soft_ttl_seconds == 60
        assert c.hard_ttl_seconds == 120

    def test_cache_starts_empty(self):
        c = CoCClanCache()
        assert c.cache == {}
        assert c._refreshing == set()
        assert c.cache_manager is None


# ---------------------------------------------------------------------------
# invalidate
# ---------------------------------------------------------------------------

class TestInvalidate:
    def test_removes_existing_entry(self):
        c = CoCClanCache()
        c.cache["#TAG1"] = {"data": "x", "timestamp": datetime.now(timezone.utc)}
        c.invalidate("#TAG1")
        assert "#TAG1" not in c.cache

    def test_noop_for_missing_entry(self):
        c = CoCClanCache()
        c.invalidate("#NOPE")  # should not raise
        assert c.cache == {}


# ---------------------------------------------------------------------------
# clear_expired
# ---------------------------------------------------------------------------

class TestClearExpired:
    def test_removes_expired(self):
        c = CoCClanCache(hard_ttl_seconds=60)
        old = datetime.now(timezone.utc) - timedelta(seconds=120)
        c.cache["#OLD"] = {"data": "x", "timestamp": old}
        c.cache["#NEW"] = {"data": "y", "timestamp": datetime.now(timezone.utc)}
        removed = c.clear_expired()
        assert removed == 1
        assert "#OLD" not in c.cache
        assert "#NEW" in c.cache

    def test_returns_zero_when_nothing_expired(self):
        c = CoCClanCache(hard_ttl_seconds=600)
        c.cache["#A"] = {"data": "z", "timestamp": datetime.now(timezone.utc)}
        assert c.clear_expired() == 0

    def test_empty_cache(self):
        c = CoCClanCache()
        assert c.clear_expired() == 0


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------

class TestGetStats:
    def test_empty_cache(self):
        c = CoCClanCache()
        stats = c.get_stats()
        assert stats == {"size": 0, "oldest_age_seconds": 0, "newest_age_seconds": 0}

    def test_single_entry(self):
        c = CoCClanCache(hard_ttl_seconds=600)
        c.cache["#A"] = {"data": "x", "timestamp": datetime.now(timezone.utc) - timedelta(seconds=30)}
        stats = c.get_stats()
        assert stats["size"] == 1
        assert stats["oldest_age_seconds"] == pytest.approx(30, abs=2)
        assert stats["newest_age_seconds"] == pytest.approx(30, abs=2)
        assert stats["ttl_seconds"] == 600

    def test_multiple_entries(self):
        c = CoCClanCache(hard_ttl_seconds=300)
        now = datetime.now(timezone.utc)
        c.cache["#A"] = {"data": "x", "timestamp": now - timedelta(seconds=10)}
        c.cache["#B"] = {"data": "y", "timestamp": now - timedelta(seconds=100)}
        stats = c.get_stats()
        assert stats["size"] == 2
        assert stats["oldest_age_seconds"] > stats["newest_age_seconds"]


# ---------------------------------------------------------------------------
# get_memory_usage_mb
# ---------------------------------------------------------------------------

class TestGetMemoryUsageMb:
    def test_empty_cache(self):
        c = CoCClanCache()
        assert c.get_memory_usage_mb() == 0.0

    def test_nonzero_for_populated_cache(self):
        c = CoCClanCache()
        clan_mock = MagicMock()
        clan_mock.members = [MagicMock()]
        c.cache["#A"] = {"data": clan_mock, "timestamp": datetime.now(timezone.utc)}
        usage = c.get_memory_usage_mb()
        assert usage > 0.0


# ---------------------------------------------------------------------------
# get_clan — TTL logic
# ---------------------------------------------------------------------------

class TestGetClanTTL:
    @pytest.mark.asyncio
    async def test_raises_without_coc_client(self):
        c = CoCClanCache()
        c.cache_manager = None
        from qapbot.exceptions import CacheError
        with pytest.raises(CacheError):
            await c.get_clan("#TAG")

    @pytest.mark.asyncio
    async def test_raises_with_null_coc_client(self):
        c = CoCClanCache()
        c.cache_manager = MagicMock()
        c.cache_manager.coc_client = None
        from qapbot.exceptions import CacheError
        with pytest.raises(CacheError):
            await c.get_clan("#TAG")

    @pytest.mark.asyncio
    async def test_fresh_returns_cached(self):
        """Fresh entry (age < soft_ttl) should be returned immediately, no API call."""
        c = CoCClanCache(soft_ttl_seconds=300, hard_ttl_seconds=600)
        c.cache_manager = MagicMock()
        c.cache_manager.coc_client = MagicMock()

        sentinel = object()
        c.cache["#TAG"] = {"data": sentinel, "timestamp": datetime.now(timezone.utc)}
        result = await c.get_clan("#TAG")
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_stale_returns_cached_and_schedules_refresh(self):
        """Stale entry (soft < age < hard) returns cached and schedules background refresh."""
        c = CoCClanCache(soft_ttl_seconds=10, hard_ttl_seconds=600)
        c.cache_manager = MagicMock()
        c.cache_manager.coc_client = MagicMock()

        sentinel = object()
        c.cache["#TAG"] = {
            "data": sentinel,
            "timestamp": datetime.now(timezone.utc) - timedelta(seconds=60),
        }
        c._schedule_background_refresh = MagicMock()
        result = await c.get_clan("#TAG")
        assert result is sentinel
        c._schedule_background_refresh.assert_called_once_with("#TAG")


# ---------------------------------------------------------------------------
# _schedule_background_refresh dedup
# ---------------------------------------------------------------------------

class TestScheduleBackgroundRefresh:
    def test_dedup_prevents_double_refresh(self):
        c = CoCClanCache()
        c._refreshing.add("#TAG")
        with patch("asyncio.create_task") as ct:
            c._schedule_background_refresh("#TAG")
            ct.assert_not_called()

    def test_first_call_creates_task(self):
        c = CoCClanCache()
        with patch("asyncio.create_task") as ct:
            c._schedule_background_refresh("#TAG")
            assert "#TAG" in c._refreshing
            # Close the unawaited coroutine to suppress RuntimeWarning
            if ct.call_args:
                ct.call_args[0][0].close()


# ---------------------------------------------------------------------------
# _update_warlog_status
# ---------------------------------------------------------------------------

class TestUpdateWarlogStatus:
    @pytest.mark.asyncio
    async def test_no_cache_manager(self):
        c = CoCClanCache()
        c.cache_manager = None
        clan_mock = MagicMock()
        # Should not raise
        await c._update_warlog_status(clan_mock)

    @pytest.mark.asyncio
    async def test_clan_not_in_cache(self):
        c = CoCClanCache()
        c.cache_manager = MagicMock()
        c.cache_manager.clan_name_cache = {}
        clan_mock = MagicMock()
        clan_mock.tag = "#MISSING"
        await c._update_warlog_status(clan_mock)  # should not raise

    @pytest.mark.asyncio
    async def test_non_dict_format_skips(self):
        c = CoCClanCache()
        c.cache_manager = MagicMock()
        c.cache_manager.clan_name_cache = {"#TAG": "not_a_dict"}
        clan_mock = MagicMock()
        clan_mock.tag = "#TAG"
        await c._update_warlog_status(clan_mock)

    @pytest.mark.asyncio
    async def test_updates_when_status_changed(self):
        c = CoCClanCache()
        c.cache_manager = MagicMock()
        c.cache_manager.persist_clan = AsyncMock()
        c.cache_manager.clan_name_cache = {
            "#TAG": {"name": "Test", "warlog_is_public": True}
        }
        clan_mock = MagicMock()
        clan_mock.tag = "#TAG"
        clan_mock.is_war_log_public = False
        await c._update_warlog_status(clan_mock)
        assert c.cache_manager.clan_name_cache["#TAG"]["warlog_is_public"] is False
        c.cache_manager.persist_clan.assert_awaited_once_with("#TAG")

    @pytest.mark.asyncio
    async def test_no_persist_when_unchanged(self):
        c = CoCClanCache()
        c.cache_manager = MagicMock()
        c.cache_manager.persist_clan = AsyncMock()
        c.cache_manager.clan_name_cache = {
            "#TAG": {"name": "Test", "warlog_is_public": True}
        }
        clan_mock = MagicMock()
        clan_mock.tag = "#TAG"
        clan_mock.is_war_log_public = True
        await c._update_warlog_status(clan_mock)
        c.cache_manager.persist_clan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_public_update_even_when_cache_is_false(self):
        """Clan endpoint says is_war_log_public=True but we must NOT trust it.
        The war fetch success handler is the authority for marking public.
        This prevents the flip-flop where get_clan says True but get_current_war
        immediately throws PrivateWarLog in the same cycle."""
        c = CoCClanCache()
        c.cache_manager = MagicMock()
        c.cache_manager.persist_clan = AsyncMock()
        c.cache_manager.clan_name_cache = {
            "#TAG": {"name": "Test", "warlog_is_public": False}  # currently private
        }
        clan_mock = MagicMock()
        clan_mock.tag = "#TAG"
        clan_mock.is_war_log_public = True  # clan endpoint claims public
        await c._update_warlog_status(clan_mock)
        # Must NOT update to True – war fetch success handler owns this direction
        assert c.cache_manager.clan_name_cache["#TAG"]["warlog_is_public"] is False
        c.cache_manager.persist_clan.assert_not_awaited()


# ---------------------------------------------------------------------------
# update_player_info_in_user_accounts
# ---------------------------------------------------------------------------

class TestUpdatePlayerInfo:
    def _make_member(self, tag: str, th: int = 15, clan_tag: str = "#CLAN1", role_name: str = "member"):
        m = MagicMock()
        m.tag = tag
        m.name = tag  # use tag as player name in tests
        m.town_hall = th
        role_mock = MagicMock()
        role_mock.name = role_name
        m.role = role_mock
        return m

    @pytest.mark.asyncio
    async def test_updates_th_level(self):
        c = CoCClanCache()
        cm = MagicMock()
        cm.persist_user = AsyncMock()
        cm.user_accounts = {
            "100": {"players": [{"player_tag": "#P1", "player_name": "#P1", "th_level": 14, "current_clan_tag": "#CLAN1"}]}
        }
        clan_mock = MagicMock()
        clan_mock.tag = "#CLAN1"
        clan_mock.members = [self._make_member("#P1", th=16)]

        await c.update_player_info_in_user_accounts(clan_mock, cm)
        assert cm.user_accounts["100"]["players"][0]["th_level"] == 16
        cm.persist_user.assert_awaited_once_with("100")

    @pytest.mark.asyncio
    async def test_updates_clan_tag(self):
        c = CoCClanCache()
        cm = MagicMock()
        cm.persist_user = AsyncMock()
        cm.user_accounts = {
            "100": {"players": [{"player_tag": "#P1", "player_name": "#P1", "th_level": 15, "current_clan_tag": "#OLD"}]}
        }
        clan_mock = MagicMock()
        clan_mock.tag = "#NEW"
        clan_mock.members = [self._make_member("#P1", th=15)]

        await c.update_player_info_in_user_accounts(clan_mock, cm)
        assert cm.user_accounts["100"]["players"][0]["current_clan_tag"] == "#NEW"
        cm.persist_user.assert_awaited()

    @pytest.mark.asyncio
    async def test_updates_coc_role(self):
        c = CoCClanCache()
        cm = MagicMock()
        cm.persist_user = AsyncMock()
        cm.user_accounts = {
            "100": {"players": [
                {"player_tag": "#P1", "player_name": "#P1", "th_level": 15, "current_clan_tag": "#CLAN1", "coc_role": "member"}
            ]}
        }
        clan_mock = MagicMock()
        clan_mock.tag = "#CLAN1"
        clan_mock.members = [self._make_member("#P1", role_name="elder")]

        await c.update_player_info_in_user_accounts(clan_mock, cm)
        assert cm.user_accounts["100"]["players"][0]["coc_role"] == "elder"

    @pytest.mark.asyncio
    async def test_co_leader_mapping(self):
        c = CoCClanCache()
        cm = MagicMock()
        cm.persist_user = AsyncMock()
        cm.user_accounts = {
            "100": {"players": [
                {"player_tag": "#P1", "player_name": "#P1", "th_level": 15, "current_clan_tag": "#CLAN1", "coc_role": "member"}
            ]}
        }
        clan_mock = MagicMock()
        clan_mock.tag = "#CLAN1"
        clan_mock.members = [self._make_member("#P1", role_name="co_leader")]

        await c.update_player_info_in_user_accounts(clan_mock, cm)
        assert cm.user_accounts["100"]["players"][0]["coc_role"] == "coLeader"

    @pytest.mark.asyncio
    async def test_detects_departures(self):
        """Player was in clan but is no longer in member list → clear current_clan_tag."""
        c = CoCClanCache()
        cm = MagicMock()
        cm.persist_user = AsyncMock()
        cm.user_accounts = {
            "100": {"players": [
                {"player_tag": "#P1", "player_name": "#P1", "th_level": 15, "current_clan_tag": "#CLAN1"}
            ]}
        }
        clan_mock = MagicMock()
        clan_mock.tag = "#CLAN1"
        clan_mock.members = []  # P1 is no longer in clan

        await c.update_player_info_in_user_accounts(clan_mock, cm)
        assert cm.user_accounts["100"]["players"][0]["current_clan_tag"] is None
        cm.persist_user.assert_awaited()

    @pytest.mark.asyncio
    async def test_skips_unassigned(self):
        c = CoCClanCache()
        cm = MagicMock()
        cm.persist_user = AsyncMock()
        cm.user_accounts = {
            "UNASSIGNED": {"players": [{"player_tag": "#P1", "player_name": "#P1", "th_level": 10}]},
            "100": {"players": [{"player_tag": "#P2", "player_name": "#P2", "th_level": 14, "current_clan_tag": "#CLAN1"}]},
        }
        clan_mock = MagicMock()
        clan_mock.tag = "#CLAN1"
        clan_mock.members = [self._make_member("#P2", th=15)]

        await c.update_player_info_in_user_accounts(clan_mock, cm)
        # Only user "100" should be persisted
        cm.persist_user.assert_awaited_once_with("100")

    @pytest.mark.asyncio
    async def test_no_changes_no_persist(self):
        c = CoCClanCache()
        cm = MagicMock()
        cm.persist_user = AsyncMock()
        cm.player_name_index = {}
        cm.db_manager = None
        cm.user_accounts = {
            "100": {"players": [
                {"player_tag": "#P1", "player_name": "#P1", "th_level": 15, "current_clan_tag": "#CLAN1", "coc_role": "member"}
            ]}
        }
        clan_mock = MagicMock()
        clan_mock.tag = "#CLAN1"
        clan_mock.members = [self._make_member("#P1", th=15, role_name="member")]

        await c.update_player_info_in_user_accounts(clan_mock, cm)
        cm.persist_user.assert_not_awaited()


# ---------------------------------------------------------------------------
# _update_clan_metadata — league gate + track_war_updates ratchet + is_deleted
# ---------------------------------------------------------------------------

class TestUpdateClanMetadataLeagueGate:
    """Covers the _WAR_UPDATE_LEAGUES gate, the one-way track_war_updates
    promotion ratchet, and the is_deleted clear-on-success behavior."""

    def _make_clan(self, tag: str, name: str = "Clan", war_league: str | None = None,
                   warlog_public: bool = True):
        clan = MagicMock()
        clan.tag = tag
        clan.name = name
        if war_league is None:
            clan.war_league = None
        else:
            wl = MagicMock()
            wl.name = war_league
            clan.war_league = wl
        clan.is_war_log_public = warlog_public
        return clan

    def _make_cache(self):
        c = CoCClanCache()
        cm = MagicMock()
        cm.persist_clan = AsyncMock()
        cm.clan_name_cache = {}
        c.cache_manager = cm
        return c, cm

    @pytest.mark.asyncio
    async def test_new_clan_master3_tracked(self):
        c, cm = self._make_cache()
        clan = self._make_clan("#NEW", war_league="Master League III")
        await c._update_clan_metadata(clan, datetime.now(timezone.utc))
        assert cm.clan_name_cache["#NEW"]["track_war_updates"] is True
        assert cm.clan_name_cache["#NEW"]["is_deleted"] is False

    @pytest.mark.asyncio
    async def test_new_clan_crystal_not_tracked(self):
        c, cm = self._make_cache()
        clan = self._make_clan("#NEW", war_league="Crystal League I")
        await c._update_clan_metadata(clan, datetime.now(timezone.utc))
        assert cm.clan_name_cache["#NEW"]["track_war_updates"] is False

    @pytest.mark.asyncio
    async def test_new_clan_unknown_league_not_tracked(self):
        c, cm = self._make_cache()
        clan = self._make_clan("#NEW", war_league=None)
        await c._update_clan_metadata(clan, datetime.now(timezone.utc))
        assert cm.clan_name_cache["#NEW"]["track_war_updates"] is False

    @pytest.mark.asyncio
    async def test_promotion_ratchet_passive_clan_flips_true(self):
        """Passive (no subs, track=False) clan promoted into Master III → True."""
        c, cm = self._make_cache()
        cm.clan_name_cache = {
            "#P": {
                "name": "Clan", "has_active_subscriptions": False,
                "warlog_is_public": True, "war_league": "Crystal League I",
                "track_war_updates": False, "is_deleted": False,
                "last_checked_via_api": datetime.now(timezone.utc).isoformat(),
            }
        }
        clan = self._make_clan("#P", war_league="Master League III")
        await c._update_clan_metadata(clan, datetime.now(timezone.utc))
        assert cm.clan_name_cache["#P"]["track_war_updates"] is True

    @pytest.mark.asyncio
    async def test_demotion_reverts_track_for_non_subscribed(self):
        """Non-subscribed track=True clan demoted to Crystal → track_war_updates reverts to False."""
        c, cm = self._make_cache()
        cm.clan_name_cache = {
            "#T": {
                "name": "Clan", "has_active_subscriptions": False,
                "warlog_is_public": True, "war_league": "Master League III",
                "track_war_updates": True, "is_deleted": False,
                "last_checked_via_api": datetime.now(timezone.utc).isoformat(),
            }
        }
        clan = self._make_clan("#T", war_league="Crystal League I")
        await c._update_clan_metadata(clan, datetime.now(timezone.utc))
        assert cm.clan_name_cache["#T"]["track_war_updates"] is False

    @pytest.mark.asyncio
    async def test_demotion_deferred_when_clan_has_in_progress_season_data(self):
        """A clan that would otherwise be demoted must stay tracked if it
        already has archived data for its current in-progress CWL season —
        demoting now would silence polling for the remaining rounds and
        permanently freeze an incomplete season on record (mirrors the
        cache_manager._sync_group_track_war_updates mid-season guard for this
        older, separate demotion path)."""
        c, cm = self._make_cache()
        cm.db_manager = AsyncMock()
        cm.db_manager.clan_has_in_progress_cwl_data = AsyncMock(return_value=True)
        cm.clan_name_cache = {
            "#T": {
                "name": "Clan", "has_active_subscriptions": False,
                "warlog_is_public": True, "war_league": "Master League III",
                "track_war_updates": True, "is_deleted": False,
                "last_checked_via_api": datetime.now(timezone.utc).isoformat(),
            }
        }
        clan = self._make_clan("#T", war_league="Crystal League I")
        await c._update_clan_metadata(clan, datetime.now(timezone.utc))

        cm.db_manager.clan_has_in_progress_cwl_data.assert_awaited_once_with("#T")
        # war_league is still corrected...
        assert cm.clan_name_cache["#T"]["war_league"] == "Crystal League I"
        # ...but track_war_updates is deferred, not flipped.
        assert cm.clan_name_cache["#T"]["track_war_updates"] is True

    @pytest.mark.asyncio
    async def test_demotion_proceeds_when_no_in_progress_season_data(self):
        """Same shape as the deferral test above, but with no in-progress season
        data on file — the ordinary demotion must proceed exactly as before
        this guard existed."""
        c, cm = self._make_cache()
        cm.db_manager = AsyncMock()
        cm.db_manager.clan_has_in_progress_cwl_data = AsyncMock(return_value=False)
        cm.clan_name_cache = {
            "#T": {
                "name": "Clan", "has_active_subscriptions": False,
                "warlog_is_public": True, "war_league": "Master League III",
                "track_war_updates": True, "is_deleted": False,
                "last_checked_via_api": datetime.now(timezone.utc).isoformat(),
            }
        }
        clan = self._make_clan("#T", war_league="Crystal League I")
        await c._update_clan_metadata(clan, datetime.now(timezone.utc))

        cm.db_manager.clan_has_in_progress_cwl_data.assert_awaited_once_with("#T")
        assert cm.clan_name_cache["#T"]["track_war_updates"] is False

    @pytest.mark.asyncio
    async def test_subscribed_clan_immune_to_demotion(self):
        """Subscribed clan demoted to Crystal keeps track_war_updates = True."""
        c, cm = self._make_cache()
        cm.clan_name_cache = {
            "#S": {
                "name": "Clan", "has_active_subscriptions": True,
                "warlog_is_public": True, "war_league": "Master League III",
                "track_war_updates": True, "is_deleted": False,
                "last_checked_via_api": datetime.now(timezone.utc).isoformat(),
            }
        }
        clan = self._make_clan("#S", war_league="Crystal League I")
        await c._update_clan_metadata(clan, datetime.now(timezone.utc))
        assert cm.clan_name_cache["#S"]["track_war_updates"] is True

    @pytest.mark.asyncio
    async def test_is_deleted_cleared_on_success(self):
        """A successful get_clan response clears a previously set is_deleted flag."""
        c, cm = self._make_cache()
        cm.clan_name_cache = {
            "#D": {
                "name": "Clan", "has_active_subscriptions": False,
                "warlog_is_public": True, "war_league": "Master League III",
                "track_war_updates": True, "is_deleted": True,
                "last_checked_via_api": datetime.now(timezone.utc).isoformat(),
            }
        }
        clan = self._make_clan("#D", war_league="Master League III")
        await c._update_clan_metadata(clan, datetime.now(timezone.utc))
        assert cm.clan_name_cache["#D"]["is_deleted"] is False
        cm.persist_clan.assert_awaited()
