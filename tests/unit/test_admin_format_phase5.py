"""Tests for admin command formatting and subscription listing — Phase 5 coverage push.

Covers:
- format_database_check_results (full — ~150 lines of format_war_status_details)
- list_all_subscriptions (async — ~125 lines)
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest


# ===========================================================================
# format_database_check_results — full coverage of war status details
# ===========================================================================

class TestFormatDatabaseCheckResultsFull:
    def _fn(self):
        from qapbot.QBdiscocmdshelper_admin_command import format_database_check_results
        return format_database_check_results

    def _base_results(self, **overrides):
        base = {
            "integrity_ok": True,
            "integrity_errors": [],
            "total_records": 100,
            "total_wars": 50,
            "total_clans": 5,
            "duplicate_wars": {},
            "missing_from_db": [],
            "missing_from_archive": [],
            "current_wars": [],
        }
        base.update(overrides)
        return base

    def test_healthy_db(self):
        result = self._fn()(self._base_results())
        assert "✅ OK" in result
        assert "100" in result
        assert "All checks passed" in result

    def test_integrity_failed_truncates_errors(self):
        errs = [f"Error {i}" for i in range(10)]
        result = self._fn()(self._base_results(integrity_ok=False, integrity_errors=errs))
        assert "❌ FAILED" in result
        # Should show max 5 errors
        assert "Error 0" in result
        assert "Error 4" in result

    def test_duplicate_wars_section(self):
        dups = {
            "#C1|OPP1": [("war_1", "2025-01-01"), ("war_2", "2025-01-02"), ("war_3", "2025-01-03"), ("war_4", "2025-01-04")],
        }
        result = self._fn()(self._base_results(duplicate_wars=dups))
        assert "⚠️" in result
        assert "1" in result  # 1 group
        assert "war_1" in result
        # Shows max 3 per group
        assert "war_3" in result

    def test_missing_from_db_section(self):
        missing = [
            ("file1.json", "#C1", "war_1"),
            ("file2.json", "#C2", "war_2"),
        ]
        result = self._fn()(self._base_results(missing_from_db=missing))
        assert "❌" in result
        assert "#C1" in result
        assert "war_1" in result

    def test_missing_from_archive_section(self):
        missing = [("#C1", "war_1"), ("#C2", "war_2")]
        result = self._fn()(self._base_results(missing_from_archive=missing))
        assert "⚠️" in result
        assert "#C1" in result

    def test_current_wars_preparation_phase(self):
        now = datetime.now(timezone.utc)
        wars = [{
            "clan_tag": "#C1",
            "clan_name": "TestClan",
            "war_state": "preparation",
            "start_time": now + timedelta(hours=5),
            "end_time": now + timedelta(hours=29),
        }]
        result = self._fn()(self._base_results(current_wars=wars))
        assert "Preparation Phase" in result
        assert "#C1" in result
        assert "TestClan" in result

    def test_current_wars_war_phase(self):
        now = datetime.now(timezone.utc)
        wars = [{
            "clan_tag": "#C2",
            "clan_name": "WarClan",
            "war_state": "inWar",
            "start_time": now - timedelta(hours=10),
            "end_time": now + timedelta(hours=14),
        }]
        result = self._fn()(self._base_results(current_wars=wars))
        assert "War Phase" in result
        assert "#C2" in result

    def test_current_wars_ended_phase(self):
        now = datetime.now(timezone.utc)
        wars = [{
            "clan_tag": "#C3",
            "clan_name": "OldClan",
            "war_state": "inWar",
            "start_time": now - timedelta(hours=48),
            "end_time": now - timedelta(hours=24),
        }]
        result = self._fn()(self._base_results(current_wars=wars))
        assert "ended" in result

    def test_current_wars_unknown_start_time(self):
        wars = [{
            "clan_tag": "#C4",
            "clan_name": "NoClock",
            "war_state": "preparation",
            "start_time": None,
            "end_time": None,
        }]
        result = self._fn()(self._base_results(current_wars=wars))
        assert "Unknown" in result

    def test_more_than_5_wars_shows_overflow(self):
        now = datetime.now(timezone.utc)
        wars = [
            {
                "clan_tag": f"#C{i}",
                "clan_name": f"Clan{i}",
                "war_state": "preparation",
                "start_time": now + timedelta(hours=i),
                "end_time": now + timedelta(hours=24 + i),
            }
            for i in range(8)
        ]
        result = self._fn()(self._base_results(current_wars=wars))
        assert "... and 3 more" in result

    def test_issues_found_message(self):
        result = self._fn()(self._base_results(integrity_ok=False, integrity_errors=["bad"]))
        assert "Issues found" in result

    def test_truncation_for_discord(self):
        dups = {f"#C{i}|OPP{i}": [(f"war_{j}", f"2025-01-{j:02d}") for j in range(10)] for i in range(20)}
        result = self._fn()(self._base_results(duplicate_wars=dups), max_length=500)
        assert len(result) <= 600


# ===========================================================================
# list_all_subscriptions
# ===========================================================================

class TestListAllSubscriptions:
    def _fn(self):
        from qapbot.QBdiscocmdshelper_admin_command import list_all_subscriptions
        return list_all_subscriptions

    def _make_bot(self, guilds=None, channels=None):
        bot = MagicMock()
        guild_map = guilds or {}
        channel_map = channels or {}
        bot.get_guild = lambda gid: guild_map.get(gid)  # type: ignore[misc]
        bot.get_channel = lambda cid: channel_map.get(cid)  # type: ignore[misc]
        return bot

    def _make_cache(self, subs=None, families=None, clan_names=None):
        cache = MagicMock()
        cache.subscriptions = subs or {}
        cache.clan_families = families or {}
        cache.clan_name_cache = clan_names or {}
        cache.get_clan_name = lambda tag, default="Unknown": (clan_names or {}).get(tag, default)  # type: ignore[misc]
        return cache

    @pytest.mark.asyncio
    async def test_no_subscriptions(self):
        bot = self._make_bot()
        cache = self._make_cache()
        result = await self._fn()(bot, cache)
        assert result == ["No active subscriptions found."]

    @pytest.mark.asyncio
    async def test_single_subscription(self):
        guild = MagicMock()
        guild.name = "TestGuild"
        channel = MagicMock()
        channel.name = "war-log"

        bot = self._make_bot(guilds={123: guild}, channels={456: channel})
        cache = self._make_cache(
            subs={"123": {"456": [{"clan_tag": "#CLAN1", "subscription_type": "war"}]}},
            clan_names={"#CLAN1": "MyClan"},
        )
        result = await self._fn()(bot, cache)
        combined = "\n".join(result)
        assert "TestGuild" in combined
        assert "MyClan" in combined

    @pytest.mark.asyncio
    async def test_playerregistration_skipped(self):
        guild = MagicMock()
        guild.name = "G"
        channel = MagicMock()
        channel.name = "reg"

        bot = self._make_bot(guilds={1: guild}, channels={2: channel})
        cache = self._make_cache(
            subs={"1": {"2": [
                {"clan_tag": "PLAYERREGISTRATION", "subscription_type": "playerregistration"},
                {"clan_tag": "#REAL", "subscription_type": "war"},
            ]}},
            clan_names={"#REAL": "RealClan"},
        )
        result = await self._fn()(bot, cache)
        combined = "\n".join(result)
        assert "PLAYERREGISTRATION" not in combined
        assert "RealClan" in combined

    @pytest.mark.asyncio
    async def test_family_subscription(self):
        guild = MagicMock()
        guild.name = "FamGuild"
        channel = MagicMock()
        channel.name = "fam-ch"

        bot = self._make_bot(guilds={10: guild}, channels={20: channel})
        cache = self._make_cache(
            subs={"10": {"20": [{"clan_tag": "FAM1", "subscription_type": "war"}]}},
            families={"FAM1": {"name": "The Family", "clans": ["#C1", "#C2"]}},
            clan_names={"#C1": "Clan1", "#C2": "Clan2"},
        )
        result = await self._fn()(bot, cache)
        combined = "\n".join(result)
        assert "The Family" in combined

    @pytest.mark.asyncio
    async def test_unsubscribed_clans_listed(self):
        guild = MagicMock()
        guild.name = "G"
        channel = MagicMock()
        channel.name = "ch"

        bot = self._make_bot(guilds={1: guild}, channels={2: channel})
        cache = self._make_cache(
            subs={"1": {"2": [{"clan_tag": "#SUB", "subscription_type": "war"}]}},
            clan_names={"#SUB": "Subscribed", "#UNSUB": "NotSubscribed"},
        )
        result = await self._fn()(bot, cache)
        combined = "\n".join(result)
        assert "NotSubscribed" in combined
        assert "Unsubscribed" in combined

    @pytest.mark.asyncio
    async def test_unknown_guild_and_channel(self):
        bot = self._make_bot()
        cache = self._make_cache(
            subs={"999": {"888": [{"clan_tag": "#X", "subscription_type": "war"}]}},
            clan_names={"#X": "XClan"},
        )
        result = await self._fn()(bot, cache)
        combined = "\n".join(result)
        assert "Unknown" in combined

    @pytest.mark.asyncio
    async def test_chunking_for_long_output(self):
        """Chunking splits across guilds, not within a single guild."""
        channels = {}
        guilds = {}
        subs_data = {}
        clan_names = {}
        for i in range(20):
            gid = 100 + i
            cid = 200 + i
            guilds[gid] = MagicMock(name=f"Guild{i}")
            guilds[gid].name = f"GuildNumber{i}"
            channels[cid] = MagicMock(name=f"ch{i}")
            channels[cid].name = f"channel-{i}"
            tag = f"#CLAN{i:04d}"
            subs_data[str(gid)] = {str(cid): [{"clan_tag": tag, "subscription_type": "war"}]}
            clan_names[tag] = f"ClanWithAVeryLongNameForTesting{i}"

        bot = self._make_bot(guilds=guilds, channels=channels)
        cache = self._make_cache(subs=subs_data, clan_names=clan_names)
        result = await self._fn()(bot, cache)
        assert len(result) >= 2  # Should be chunked across multiple messages
