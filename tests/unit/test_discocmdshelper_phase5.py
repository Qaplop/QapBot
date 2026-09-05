"""Tests for QBdiscocmdshelper — Phase 5 coverage push.

Targets EASY and MEDIUM functions with highest coverage impact:
- is_player_in_member_clans
- get_guild_subscribed_clans
- get_guild_clans_including_member_config
- _gather_players_from_cache
- _get_registered_player_ids
- _calculate_activity_score
- get_playerregistration_message
- format_notification_settings
- get_most_active_clan_for_guild
- _split_content_into_embeds
- _split_content_into_two_embeds
- set_primary_account
- unlink_player
- restore_player_from_unassigned
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cache(**overrides) -> MagicMock:
    cache = MagicMock()
    cache.server_config = overrides.get("server_config", {})
    cache.clan_families = overrides.get("clan_families", {})
    cache.subscriptions = overrides.get("subscriptions", {})
    cache.user_accounts = overrides.get("user_accounts", {})
    cache.get_temp_war_stats = MagicMock(return_value=overrides.get("temp_stats", {}))
    cache.get_clan_history = MagicMock(return_value=overrides.get("clan_history", []))
    cache.get_all_subscriptions_flat = MagicMock(return_value=overrides.get("subs_flat", {}))
    cache.persist_user = AsyncMock()
    cache.delete_user_account = AsyncMock()
    cache.get_clan_name = MagicMock(side_effect=lambda tag, default="": default)  # type: ignore[misc]
    return cache


# ===========================================================================
# is_player_in_member_clans
# ===========================================================================

class TestIsPlayerInMemberClans:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import is_player_in_member_clans
        return is_player_in_member_clans

    def test_none_clan_tag_returns_false(self, monkeypatch):
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", _make_cache())
        assert self._fn()(None, 123) is False

    def test_empty_clan_tag_returns_false(self, monkeypatch):
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", _make_cache())
        assert self._fn()("", 123) is False

    def test_direct_member_clan(self, monkeypatch):
        cache = _make_cache(server_config={"123": {"member_clans": ["#ABC12345"]}})
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#ABC12345", 123) is True

    def test_not_in_member_clans(self, monkeypatch):
        cache = _make_cache(server_config={"123": {"member_clans": ["#OTHER1234"]}})
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#ABC12345", 123) is False

    def test_via_member_family(self, monkeypatch):
        cache = _make_cache(
            server_config={"123": {"member_clans": [], "member_families": ["fam1"]}},
            clan_families={"fam1": {"clans": ["#ABC12345", "#DEF67890"]}},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#DEF67890", 123) is True

    def test_family_no_match(self, monkeypatch):
        cache = _make_cache(
            server_config={"123": {"member_clans": [], "member_families": ["fam1"]}},
            clan_families={"fam1": {"clans": ["#OTHER1234"]}},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#ABC12345", 123) is False

    def test_no_config_for_guild(self, monkeypatch):
        cache = _make_cache(server_config={})
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#ABC12345", 999) is False


# ===========================================================================
# get_guild_subscribed_clans
# ===========================================================================

class TestGetGuildSubscribedClans:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import get_guild_subscribed_clans
        return get_guild_subscribed_clans

    def test_empty_subscriptions(self, monkeypatch):
        cache = _make_cache(subscriptions={})
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()(123) == []

    def test_direct_clan_sub(self, monkeypatch):
        cache = _make_cache(
            subscriptions={"123": {"ch1": [{"clan_tag": "#CLAN1234", "subscription_type": "war"}]}},
            clan_families={},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()(123) == ["#CLAN1234"]

    def test_skips_playerregistration(self, monkeypatch):
        cache = _make_cache(
            subscriptions={"123": {"ch1": [
                {"clan_tag": "#CLAN1234", "subscription_type": "war"},
                {"clan_tag": "#REG12345", "subscription_type": "playerregistration"},
            ]}},
            clan_families={},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = self._fn()(123)
        assert "#REG12345" not in result
        assert "#CLAN1234" in result

    def test_family_expansion(self, monkeypatch):
        cache = _make_cache(
            subscriptions={"123": {"ch1": [{"clan_tag": "fam1", "subscription_type": "war"}]}},
            clan_families={"fam1": {"clans": ["#CLAN1234", "#CLAN5678"]}},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = self._fn()(123)
        assert "#CLAN1234" in result
        assert "#CLAN5678" in result

    def test_clan_in_family_expands_siblings(self, monkeypatch):
        cache = _make_cache(
            subscriptions={"123": {"ch1": [{"clan_tag": "#CLAN1234", "subscription_type": "war"}]}},
            clan_families={"fam1": {"clans": ["#CLAN1234", "#SIBLING12"]}},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = self._fn()(123)
        assert "#CLAN1234" in result
        assert "#SIBLING12" in result

    def test_skips_empty_subs(self, monkeypatch):
        cache = _make_cache(
            subscriptions={"123": {"ch1": [None, {}, {"clan_tag": "#CLAN1234", "subscription_type": "war"}]}},
            clan_families={},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = self._fn()(123)
        assert result == ["#CLAN1234"]

    def test_sorted_output(self, monkeypatch):
        cache = _make_cache(
            subscriptions={"123": {"ch1": [
                {"clan_tag": "#ZZZ123456", "subscription_type": "war"},
                {"clan_tag": "#AAA123456", "subscription_type": "war"},
            ]}},
            clan_families={},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = self._fn()(123)
        assert result == ["#AAA123456", "#ZZZ123456"]


# ===========================================================================
# get_guild_clans_including_member_config
# ===========================================================================

class TestGetGuildClansIncludingMemberConfig:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import get_guild_clans_including_member_config
        return get_guild_clans_including_member_config

    def test_includes_subscribed_and_member_clans(self, monkeypatch):
        cache = _make_cache(
            subscriptions={"123": {"ch1": [{"clan_tag": "#SUB12345", "subscription_type": "war"}]}},
            server_config={"123": {"member_clans": ["#MEM12345"], "member_families": []}},
            clan_families={},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = self._fn()(123)
        assert "#SUB12345" in result
        assert "#MEM12345" in result

    def test_includes_member_family_clans(self, monkeypatch):
        cache = _make_cache(
            subscriptions={"123": {}},
            server_config={"123": {"member_clans": [], "member_families": ["fam1"]}},
            clan_families={"fam1": {"clans": ["#FAM12345"]}},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = self._fn()(123)
        assert "#FAM12345" in result

    def test_deduplicates(self, monkeypatch):
        cache = _make_cache(
            subscriptions={"123": {"ch1": [{"clan_tag": "#DUP123456", "subscription_type": "war"}]}},
            server_config={"123": {"member_clans": ["#DUP123456"], "member_families": []}},
            clan_families={},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = self._fn()(123)
        assert result.count("#DUP123456") == 1


# ===========================================================================
# _gather_players_from_cache
# ===========================================================================

class TestGatherPlayersFromCache:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import _gather_players_from_cache
        return _gather_players_from_cache

    def test_gathers_from_temp_stats(self, monkeypatch):
        cache = _make_cache(temp_stats={"#P1": {"Player": "Alice"}, "#P2": {"Player": "Bob"}})
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = {}
        self._fn()("#CLAN1234", result)
        assert result == {"#P1": "Alice", "#P2": "Bob"}

    def test_gathers_from_history(self, monkeypatch):
        cache = _make_cache(
            temp_stats={},
            clan_history=[{"PlayerID": "#P1", "Player": "Alice"}],
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = {}
        self._fn()("#CLAN1234", result)
        assert result == {"#P1": "Alice"}

    def test_temp_has_priority(self, monkeypatch):
        cache = _make_cache(
            temp_stats={"#P1": {"Player": "NewName"}},
            clan_history=[{"PlayerID": "#P1", "Player": "OldName"}],
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = {}
        self._fn()("#CLAN1234", result)
        assert result["#P1"] == "NewName"

    def test_skips_empty_pids(self, monkeypatch):
        cache = _make_cache(
            temp_stats={"": {"Player": "Ghost"}, "#P1": {"Player": "Real"}},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = {}
        self._fn()("#CLAN1234", result)
        assert "" not in result
        assert result == {"#P1": "Real"}

    def test_handles_non_dict_history_rows(self, monkeypatch):
        cache = _make_cache(
            temp_stats={},
            clan_history=["bad_row", None, {"PlayerID": "#P1", "Player": "Ok"}],
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = {}
        self._fn()("#CLAN1234", result)
        assert result == {"#P1": "Ok"}


# ===========================================================================
# _get_registered_player_ids
# ===========================================================================

class TestGetRegisteredPlayerIds:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import _get_registered_player_ids
        return _get_registered_player_ids

    def test_returns_all_player_tags(self, monkeypatch):
        cache = _make_cache(user_accounts={
            "U1": {"players": [{"player_tag": "#P1"}, {"player_tag": "#P2"}]},
            "U2": {"players": [{"player_tag": "#P3"}]},
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()() == {"#P1", "#P2", "#P3"}

    def test_skips_unassigned(self, monkeypatch):
        cache = _make_cache(user_accounts={
            "UNASSIGNED": {"players": [{"player_tag": "#P1"}]},
            "U1": {"players": [{"player_tag": "#P2"}]},
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()() == {"#P2"}

    def test_skips_empty_entries(self, monkeypatch):
        cache = _make_cache(user_accounts={
            "U1": None,
            "U2": {"players": [{"player_tag": "#P1"}]},
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()() == {"#P1"}

    def test_skips_empty_tags(self, monkeypatch):
        cache = _make_cache(user_accounts={
            "U1": {"players": [{"player_tag": ""}, None, {"player_tag": "#P1"}]},
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()() == {"#P1"}

    def test_empty_accounts(self, monkeypatch):
        cache = _make_cache(user_accounts={})
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()() == set()


# ===========================================================================
# _calculate_activity_score
# ===========================================================================

class TestCalculateActivityScore:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import _calculate_activity_score
        return _calculate_activity_score

    def test_counts_recent_history_attacks(self, monkeypatch):
        recent = (datetime.now() - timedelta(days=10)).isoformat()
        cache = _make_cache(
            clan_history=[
                {"PlayerID": "#P1", "Date": recent, "Attacks": 2},
            ],
            temp_stats={},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#P1", ["#CLAN1234"]) == 2

    def test_ignores_old_history(self, monkeypatch):
        old = (datetime.now() - timedelta(days=90)).isoformat()
        cache = _make_cache(
            clan_history=[
                {"PlayerID": "#P1", "Date": old, "Attacks": 5},
            ],
            temp_stats={},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#P1", ["#CLAN1234"]) == 0

    def test_adds_temp_attacks(self, monkeypatch):
        cache = _make_cache(
            clan_history=[],
            temp_stats={"#P1": {"Attacks": 3}},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#P1", ["#CLAN1234"]) == 3

    def test_combines_history_and_temp(self, monkeypatch):
        recent = (datetime.now() - timedelta(days=5)).isoformat()
        cache = _make_cache(
            clan_history=[{"PlayerID": "#P1", "Date": recent, "Attacks": 2}],
            temp_stats={"#P1": {"Attacks": 1}},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#P1", ["#CLAN1234"]) == 3

    def test_handles_bad_dates(self, monkeypatch):
        cache = _make_cache(
            clan_history=[
                {"PlayerID": "#P1", "Date": "not-a-date", "Attacks": 5},
            ],
            temp_stats={},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#P1", ["#CLAN1234"]) == 0

    def test_handles_non_dict_rows(self, monkeypatch):
        cache = _make_cache(clan_history=["bad", None], temp_stats={})
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#P1", ["#CLAN1234"]) == 0

    def test_no_matching_player(self, monkeypatch):
        recent = (datetime.now() - timedelta(days=5)).isoformat()
        cache = _make_cache(
            clan_history=[{"PlayerID": "#OTHER1234", "Date": recent, "Attacks": 10}],
            temp_stats={},
        )
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#P1", ["#CLAN1234"]) == 0

    def test_multiple_clans(self, monkeypatch):
        """Activity across multiple clans is summed."""
        recent = (datetime.now() - timedelta(days=5)).isoformat()
        cache = _make_cache()
        call_count = [0]
        def mock_history(tag):
            call_count[0] += 1
            if tag == "#C1":
                return [{"PlayerID": "#P1", "Date": recent, "Attacks": 2}]
            elif tag == "#C2":
                return [{"PlayerID": "#P1", "Date": recent, "Attacks": 3}]
            return []
        cache.get_clan_history = MagicMock(side_effect=mock_history)

        def mock_temp(tag):
            return {}
        cache.get_temp_war_stats = MagicMock(side_effect=mock_temp)
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#P1", ["#C1", "#C2"]) == 5


# ===========================================================================
# get_playerregistration_message
# ===========================================================================

class TestGetPlayerregistrationMessage:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import get_playerregistration_message
        return get_playerregistration_message

    def test_returns_formatted_string(self, monkeypatch):
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", _make_cache())
        result = self._fn()("TestServer", guild_id=123)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_bold_title(self, monkeypatch):
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", _make_cache())
        result = self._fn()("TestServer", guild_id=123)
        assert "**" in result


# ===========================================================================
# format_notification_settings
# ===========================================================================

class TestFormatNotificationSettings:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import format_notification_settings
        return format_notification_settings

    def test_basic_format(self, monkeypatch):
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", _make_cache())
        user_data = {
            "notification_settings": {"war_reminders": True, "notification_mode": "repeated", "notification_type": "all_wars", "hours_before_end": 4},
            "user_language": "en",
            "players": [{"player_name": "TestPlayer", "player_tag": "#TP123456", "verified": True}],
        }
        result = self._fn()(user_data, "DisplayName", guild_id=123)
        assert "DisplayName" in result
        assert "✅" in result

    def test_disabled_notifications(self, monkeypatch):
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", _make_cache())
        user_data = {
            "notification_settings": {"war_reminders": False},
            "user_language": "en",
            "players": [],
        }
        result = self._fn()(user_data, "User", guild_id=123)
        assert "❌" in result

    def test_cwl_only_type(self, monkeypatch):
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", _make_cache())
        user_data = {
            "notification_settings": {"war_reminders": True, "notification_type": "cwl_only", "notification_mode": "once", "hours_before_end": 2},
            "user_language": "en",
            "players": [],
        }
        result = self._fn()(user_data, "User", guild_id=123)
        assert isinstance(result, str)

    def test_locked_language(self, monkeypatch):
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", _make_cache())
        user_data = {
            "notification_settings": {"war_reminders": True},
            "user_language": "de",
            "user_language_locked": True,
            "players": [],
        }
        result = self._fn()(user_data, "User", guild_id=123)
        assert isinstance(result, str)

    def test_with_buddies(self, monkeypatch):
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", _make_cache())
        user_data = {
            "notification_settings": {"war_reminders": True},
            "user_language": "en",
            "players": [{"player_name": "Me", "player_tag": "#ME123456", "verified": True}],
            "watched_players": [{"player_name": "Buddy", "player_tag": "#BUD12345"}],
        }
        result = self._fn()(user_data, "User", guild_id=123)
        assert "👥" in result

    def test_no_players_no_buddies(self, monkeypatch):
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", _make_cache())
        user_data = {
            "notification_settings": {},
            "players": [],
            "watched_players": [],
        }
        result = self._fn()(user_data, "Nobody", guild_id=123)
        assert "Nobody" in result

    def test_unknown_mode_fallback(self, monkeypatch):
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", _make_cache())
        user_data = {
            "notification_settings": {"war_reminders": True, "notification_mode": "unknown_mode"},
            "user_language": "en",
            "players": [],
        }
        result = self._fn()(user_data, "User", guild_id=123)
        assert "unknown_mode" in result


# ===========================================================================
# get_most_active_clan_for_guild
# ===========================================================================

class TestGetMostActiveClanForGuild:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import get_most_active_clan_for_guild
        return get_most_active_clan_for_guild

    def test_empty_clan_tags(self, monkeypatch):
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", _make_cache())
        assert self._fn()(123, []) == ""

    def test_returns_most_subscribed(self, monkeypatch):
        cache = _make_cache(subs_flat={
            "ch1": [{"clan_tag": "#A1234567"}, {"clan_tag": "#A1234567"}, {"clan_tag": "#B1234567"}],
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        # Need to also patch inside the function since it re-imports
        with patch("qapbot.QBdiscocmdshelper.CACHE", cache):
            result = self._fn()(123, ["#A1234567", "#B1234567"])
            assert result == "#A1234567"

    def test_no_subscriptions_returns_first(self, monkeypatch):
        cache = _make_cache(subs_flat={})
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        with patch("qapbot.cache_manager.CACHE", cache):
            result = self._fn()(123, ["#FIRST1234", "#SECOND123"])
            assert result == "#FIRST1234"


# ===========================================================================
# _split_content_into_embeds
# ===========================================================================

class TestSplitContentIntoEmbeds:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import _split_content_into_embeds
        return _split_content_into_embeds

    def test_single_embed_short_content(self):
        result = self._fn()("Clan", "#TAG", "Header\n", ["Line1", "Line2"])
        assert len(result) == 1
        assert result[0].description is not None
        assert "Line1" in result[0].description
        assert "Line2" in result[0].description

    def test_splits_on_max_length(self):
        long_lines = [f"Line {i} " + "x" * 200 for i in range(30)]
        result = self._fn()("Clan", "#TAG", "H\n", long_lines, max_length=500)
        assert len(result) > 1
        for embed in result:
            assert embed.description is not None
            assert len(embed.description) <= 600  # Some tolerance for last addition

    def test_empty_content_returns_no_content(self):
        result = self._fn()("Clan", "#TAG", "", [])
        assert len(result) == 1

    def test_author_set_on_all_embeds(self):
        long_lines = [f"Line {i} " + "x" * 200 for i in range(30)]
        result = self._fn()("MyClan", "#T1", "H\n", long_lines, max_length=500)
        for embed in result:
            assert embed.author.name is not None
            assert "MyClan" in embed.author.name


# ===========================================================================
# _split_content_into_two_embeds
# ===========================================================================

class TestSplitContentIntoTwoEmbeds:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import _split_content_into_two_embeds
        return _split_content_into_two_embeds

    def test_short_content_single_embed(self):
        result = self._fn()("Clan", "#TAG", "Header\n", ["Short line"])
        assert len(result) == 1

    def test_long_content_two_embeds(self):
        long_lines = [f"Line {i} " + "x" * 200 for i in range(30)]
        result = self._fn()("Clan", "#TAG", "H\n", long_lines)
        assert len(result) == 2
        assert result[0].description is not None
        assert len(result[0].description) <= 4096
        assert result[1].description is not None
        assert len(result[1].description) <= 4096

    def test_first_embed_has_author(self):
        result = self._fn()("MyClan", "#TAG", "H\n", ["Line"])
        assert result[0].author.name is not None
        assert "MyClan" in result[0].author.name

    def test_second_embed_never_exceeds_4096_with_lopsided_content(self):
        """tracker item #0032 (live bug): switching /clan management to the war notification
        tab raised Discord's 400 "embeds.1.description: Must be 4096 or fewer in length".
        Root cause -- the first embed filled up to ~4096 and then ALL remaining content was
        dumped into a single unbounded second embed with no length check at all. That's only
        safe when the leftover happens to be small; a big gap between one content unit's size
        and the embed's remaining headroom (e.g. one line barely fits, forcing everything after
        it into embed #2) can leave far more than 4096 chars in the "remainder." Every returned
        embed must stay within Discord's 4096-char limit regardless of how unevenly
        `content_lines` sizes are distributed -- even when that means returning more than two."""
        lines = ["a" * 3500] + ["b" * 1000] * 5  # first line alone nearly fills embed 1;
        # the old code would then dump all 5000+ remaining chars into embed 2 unbounded.
        result = self._fn()("Clan", "#TAG", "H\n", lines)
        assert len(result) >= 2
        for embed in result:
            assert embed.description is not None
            assert len(embed.description) <= 4096

    def test_no_content_loss_with_lopsided_content(self):
        lines = ["a" * 3500] + [f"b{i}" * 500 for i in range(5)]
        result = self._fn()("Clan", "#TAG", "H\n", lines)
        combined = "\n".join(e.description for e in result if e.description)
        for line in lines:
            assert line in combined


# ===========================================================================
# _format_clan_management_roles -- orphaned clan_roles pruning (tracker #0030)
# ===========================================================================

class TestFormatClanManagementRolesPruning:
    @pytest.mark.asyncio
    async def test_orphaned_clan_role_entry_is_pruned_and_hidden(self, monkeypatch):
        """A clan removed from its clan family (the reported "StayUndefeated" case) kept
        showing up in Server-Rollen verwalten -> Clan-Rollen forever, because config's
        clan_roles entries were never pruned once a clan stopped being covered by the guild's
        member_clans/member_families -- even once the underlying Discord role was itself
        deleted (shown as "deleted" but the clan line stayed). Pruning must key off actual
        current clan coverage, not just whether the stored role id still resolves."""
        from qapbot.QBdiscocmdshelper import _format_clan_management_roles

        cache = _make_cache(
            server_config={
                "1": {
                    "clan_role_enabled": True,
                    "member_clans": [],
                    "member_families": ["FAM1"],
                    "clan_roles": {
                        "#COVERED123": "111",   # still in FAM1 -> kept and displayed
                        "#ORPHANED12": "222",   # no longer in FAM1 or member_clans -> pruned
                    },
                },
            },
            clan_families={"FAM1": {"name": "Stay-Family", "clans": ["#COVERED123"]}},
        )
        cache.persist_server_config = AsyncMock()

        guild = MagicMock()
        guild.id = 1
        covered_role = MagicMock()
        covered_role.mention = "<@&111>"
        guild.get_role = MagicMock(side_effect=lambda rid: covered_role if rid == 111 else None)

        with patch("qapbot.cache_manager.CACHE", cache), patch("qapbot.QBdiscocmdshelper.CACHE", cache):
            embed, _, _, _ = await _format_clan_management_roles(guild)

        clan_roles_field_value = embed.fields[-1].value or ""
        assert "ORPHANED12" not in clan_roles_field_value
        assert covered_role.mention in clan_roles_field_value
        assert "#ORPHANED12" not in cache.server_config["1"]["clan_roles"]
        assert cache.server_config["1"]["clan_roles"]["#COVERED123"] == "111"
        cache.persist_server_config.assert_awaited_once_with("1")

    @pytest.mark.asyncio
    async def test_no_pruning_or_persist_when_all_entries_still_covered(self, monkeypatch):
        """No orphaned entries -> no unnecessary persist_server_config write."""
        from qapbot.QBdiscocmdshelper import _format_clan_management_roles

        cache = _make_cache(
            server_config={
                "1": {
                    "clan_role_enabled": True,
                    "member_clans": ["#COVERED123"],
                    "member_families": [],
                    "clan_roles": {"#COVERED123": "111"},
                },
            },
            clan_families={},
        )
        cache.persist_server_config = AsyncMock()

        guild = MagicMock()
        guild.id = 1
        covered_role = MagicMock()
        covered_role.mention = "<@&111>"
        guild.get_role = MagicMock(return_value=covered_role)

        with patch("qapbot.cache_manager.CACHE", cache), patch("qapbot.QBdiscocmdshelper.CACHE", cache):
            embed, _, _, _ = await _format_clan_management_roles(guild)

        assert covered_role.mention in embed.fields[-1].value
        assert "#COVERED123" in cache.server_config["1"]["clan_roles"]
        cache.persist_server_config.assert_not_awaited()


# ===========================================================================
# _format_clan_management_notifications -- lopsided single user (tracker #0032,
# reopened: still reproducible after the first #0032 fix)
# ===========================================================================

class TestFormatClanManagementNotificationsLopsidedUser:
    @pytest.mark.asyncio
    async def test_user_with_many_linked_players_never_produces_oversized_embed(self, monkeypatch):
        """The first #0032 fix made _split_content_into_two_embeds/_split_content_into_embeds
        guarantee every embed stays <=4096 chars -- but only because each entry it receives in
        content_lines is assumed small. _format_clan_management_notifications violated that by
        joining one whole Discord user's header + every one of their linked players into a
        single content_lines entry. A user linked to many players (routine on a multi-clan
        family guild, e.g. "Stay") can make that one entry alone exceed 4096 chars, which no
        general-purpose line splitter can subdivide -- reproducing Discord's 400 "description:
        Must be 4096 or fewer" (this time as embeds.0, since the oversized unit is now the
        first user section instead of the old unbounded remainder). Fix: emit each user's
        header and player lines as separate content_lines entries so the splitter can always
        break between them."""
        from qapbot.QBdiscocmdshelper import _format_clan_management_notifications

        clan_tag = "#CLAN00001"
        num_players = 80  # enough linked players on one user to exceed 4096 chars alone
        member_tags = [f"#P{i:08d}" for i in range(num_players)]

        cache = _make_cache(
            server_config={
                "1": {
                    "channel_war_notifications_enabled": False,
                    "war_notification_channel_id": None,
                    "clan_custodians": {},
                },
            },
            user_accounts={
                # Sorted first (by user_id) so its oversized section lands as embeds[0].
                "100000000000000001": {
                    "display_name": "HeavyLinkedUser",
                    "notification_settings": {"war_reminders": True, "notification_mode": "repeated", "notification_type": "all_wars"},
                    "players": [
                        {
                            "player_tag": tag,
                            "player_name": f"Player{i}",
                            "verified": True,
                            "th_level": 15,
                            "current_clan_tag": clan_tag,
                        }
                        for i, tag in enumerate(member_tags)
                    ],
                },
            },
        )
        cache.coc_clan_cache = MagicMock()
        cache.coc_clan_cache.get_clan = AsyncMock(
            return_value=SimpleNamespace(members=[SimpleNamespace(tag=tag) for tag in member_tags])
        )
        cache.fetch_and_update_player_info = AsyncMock(return_value=None)

        guild = MagicMock()
        guild.id = 1

        with patch("qapbot.cache_manager.CACHE", cache), patch("qapbot.QBdiscocmdshelper.CACHE", cache):
            main_embed, unlinked_embed, _, _ = await _format_clan_management_notifications(clan_tag, guild)

        all_embeds = [main_embed]
        if isinstance(unlinked_embed, list):
            all_embeds.extend(unlinked_embed)
        elif unlinked_embed is not None:
            all_embeds.append(unlinked_embed)

        assert all_embeds, "expected at least one embed"
        for embed in all_embeds:
            assert embed.description is not None
            assert len(embed.description) <= 4096


# ===========================================================================
# set_primary_account
# ===========================================================================

class TestSetPrimaryAccount:
    @pytest.mark.asyncio
    async def test_sets_primary(self, monkeypatch):
        from qapbot.QBdiscocmdshelper import set_primary_account
        cache = _make_cache(user_accounts={
            "U1": {"players": [
                {"player_tag": "#P1", "is_primary": True},
                {"player_tag": "#P2", "is_primary": False},
            ]},
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = await set_primary_account("U1", "#P2")
        assert result is True
        assert cache.user_accounts["U1"]["players"][1]["is_primary"] is True
        assert cache.user_accounts["U1"]["players"][0]["is_primary"] is False
        cache.persist_user.assert_awaited_once_with("U1")

    @pytest.mark.asyncio
    async def test_already_primary_returns_false(self, monkeypatch):
        from qapbot.QBdiscocmdshelper import set_primary_account
        cache = _make_cache(user_accounts={
            "U1": {"players": [{"player_tag": "#P1", "is_primary": True}]},
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert await set_primary_account("U1", "#P1") is False

    @pytest.mark.asyncio
    async def test_user_not_found(self, monkeypatch):
        from qapbot.QBdiscocmdshelper import set_primary_account
        cache = _make_cache(user_accounts={})
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert await set_primary_account("U1", "#P1") is False

    @pytest.mark.asyncio
    async def test_player_not_found(self, monkeypatch):
        from qapbot.QBdiscocmdshelper import set_primary_account
        cache = _make_cache(user_accounts={
            "U1": {"players": [{"player_tag": "#OTHER1234"}]},
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert await set_primary_account("U1", "#P1") is False

    @pytest.mark.asyncio
    async def test_invalid_players_type(self, monkeypatch):
        from qapbot.QBdiscocmdshelper import set_primary_account
        cache = _make_cache(user_accounts={"U1": {"players": "not_a_list"}})
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert await set_primary_account("U1", "#P1") is False


# ===========================================================================
# unlink_player
# ===========================================================================

class TestUnlinkPlayer:
    @pytest.mark.asyncio
    async def test_unlinks_and_moves_to_unassigned(self, monkeypatch):
        from qapbot.QBdiscocmdshelper import unlink_player
        cache = _make_cache(user_accounts={
            "U1": {"players": [
                {"player_tag": "#P1", "is_primary": True, "player_name": "Test"},
                {"player_tag": "#P2", "is_primary": False},
            ]},
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = await unlink_player("U1", "#P1")
        assert result is True
        # P1 removed from user
        remaining_tags = [p["player_tag"] for p in cache.user_accounts["U1"]["players"]]
        assert "#P1" not in remaining_tags
        # P1 moved to UNASSIGNED
        unassigned_tags = [p["player_tag"] for p in cache.user_accounts["UNASSIGNED"]["players"]]
        assert "#P1" in unassigned_tags
        # Primary flag stripped
        unassigned_p1 = next(p for p in cache.user_accounts["UNASSIGNED"]["players"] if p["player_tag"] == "#P1")
        assert unassigned_p1["is_primary"] is False

    @pytest.mark.asyncio
    async def test_user_not_found(self, monkeypatch):
        from qapbot.QBdiscocmdshelper import unlink_player
        cache = _make_cache(user_accounts={})
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert await unlink_player("U1", "#P1") is False

    @pytest.mark.asyncio
    async def test_player_not_found(self, monkeypatch):
        from qapbot.QBdiscocmdshelper import unlink_player
        cache = _make_cache(user_accounts={
            "U1": {"players": [{"player_tag": "#OTHER1234"}]},
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert await unlink_player("U1", "#P1") is False

    @pytest.mark.asyncio
    async def test_already_in_unassigned_not_duplicated(self, monkeypatch):
        from qapbot.QBdiscocmdshelper import unlink_player
        cache = _make_cache(user_accounts={
            "U1": {"players": [{"player_tag": "#P1", "is_primary": False}]},
            "UNASSIGNED": {"display_name": "UNASSIGNED", "players": [{"player_tag": "#P1"}]},
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        result = await unlink_player("U1", "#P1")
        assert result is True
        # Not duplicated in UNASSIGNED
        unassigned_p1s = [p for p in cache.user_accounts["UNASSIGNED"]["players"] if p.get("player_tag") == "#P1"]
        assert len(unassigned_p1s) == 1


# ===========================================================================
# restore_player_from_unassigned
# ===========================================================================

class TestRestorePlayerFromUnassigned:
    @pytest.mark.asyncio
    async def test_restores_player(self, monkeypatch):
        from qapbot.QBdiscocmdshelper import restore_player_from_unassigned
        player = {"player_tag": "#P1", "player_name": "Test", "verified": True}
        cache = _make_cache(user_accounts={
            "U1": {"players": [], "display_name": "User1"},
            "UNASSIGNED": {"display_name": "UNASSIGNED", "players": [player]},
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        user_entry = cache.user_accounts["U1"]
        restored, data, _msg = await restore_player_from_unassigned(user_entry, "#P1", "User1")
        assert restored is True
        assert data is not None
        assert data["verified"] is False  # Always reset on restore
        assert "#P1" in [p["player_tag"] for p in user_entry["players"]]

    @pytest.mark.asyncio
    async def test_not_in_unassigned(self, monkeypatch):
        from qapbot.QBdiscocmdshelper import restore_player_from_unassigned
        cache = _make_cache(user_accounts={
            "U1": {"players": []},
            "UNASSIGNED": {"display_name": "UNASSIGNED", "players": []},
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        restored, data, _msg = await restore_player_from_unassigned(cache.user_accounts["U1"], "#P1", "User1")
        assert restored is False
        assert data is None

    @pytest.mark.asyncio
    async def test_no_unassigned_entry(self, monkeypatch):
        from qapbot.QBdiscocmdshelper import restore_player_from_unassigned
        cache = _make_cache(user_accounts={"U1": {"players": []}})
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        restored, _data, _msg = await restore_player_from_unassigned(cache.user_accounts["U1"], "#P1", "User1")
        assert restored is False

    @pytest.mark.asyncio
    async def test_cleans_up_empty_unassigned(self, monkeypatch):
        from qapbot.QBdiscocmdshelper import restore_player_from_unassigned
        player = {"player_tag": "#P1", "player_name": "Test", "verified": False}
        cache = _make_cache(user_accounts={
            "U1": {"players": [], "display_name": "User1"},
            "UNASSIGNED": {"display_name": "UNASSIGNED", "players": [player]},
        })
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        await restore_player_from_unassigned(cache.user_accounts["U1"], "#P1", "User1")
        # UNASSIGNED should be removed since it's now empty
        assert "UNASSIGNED" not in cache.user_accounts
        cache.delete_user_account.assert_awaited_once_with("UNASSIGNED")
