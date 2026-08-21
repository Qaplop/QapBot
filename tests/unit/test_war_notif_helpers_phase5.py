"""Tests for war_notifications helper functions — Phase 5 coverage push.

Covers:
- _get_players_with_attacks_remaining (~40 lines)
- _should_notify_for_war_type (~20 lines)
- _should_send_buddy_notification (~25 lines)
- _record_buddy_notification (~15 lines)
- _format_aggregated_reminder_message (~70 lines)
- _send_channel_war_notification (~105 lines)
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# _get_players_with_attacks_remaining
# ---------------------------------------------------------------------------

class TestGetPlayersWithAttacksRemaining:
    def _fn(self):
        from qapbot.war_notifications import _get_players_with_attacks_remaining
        return _get_players_with_attacks_remaining

    def _war_data(self, members=None, attacks_per_member=2):
        return {
            "clan": {"name": "TestClan", "members": members or []},
            "opponent": {"name": "EnemyClan"},
            "attacks_per_member": attacks_per_member,
        }

    def test_no_hours_remaining(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: None)  # type: ignore[misc]
        result = self._fn()(self._war_data())
        assert result == []

    def test_no_members(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 5.0)  # type: ignore[misc]
        result = self._fn()(self._war_data())
        assert result == []

    def test_all_attacks_used(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 5.0)  # type: ignore[misc]
        members = [{"tag": "#P1", "name": "P1", "attacks": [{"stars": 3}, {"stars": 2}]}]
        result = self._fn()(self._war_data(members=members))
        assert result == []

    def test_partial_attacks_remaining(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 3.5)  # type: ignore[misc]
        members = [{"tag": "#P1", "name": "P1", "attacks": [{"stars": 3}]}]
        result = self._fn()(self._war_data(members=members))
        assert len(result) == 1
        assert result[0]["player_tag"] == "#P1"
        assert result[0]["attacks_remaining"] == 1
        assert result[0]["hours_remaining"] == 3.5
        assert result[0]["clan_name"] == "TestClan"
        assert result[0]["opponent_name"] == "EnemyClan"

    def test_no_attacks_used(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 1.0)  # type: ignore[misc]
        members = [{"tag": "#P2", "name": "P2", "attacks": []}]
        result = self._fn()(self._war_data(members=members, attacks_per_member=2))
        assert result[0]["attacks_remaining"] == 2

    def test_multiple_members_mixed(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 2.0)  # type: ignore[misc]
        members = [
            {"tag": "#P1", "name": "P1", "attacks": [{"stars": 3}, {"stars": 2}]},  # done
            {"tag": "#P2", "name": "P2", "attacks": [{"stars": 1}]},  # 1 remaining
            {"tag": "#P3", "name": "P3", "attacks": []},  # 2 remaining
        ]
        result = self._fn()(self._war_data(members=members))
        assert len(result) == 2
        tags = [r["player_tag"] for r in result]
        assert "#P2" in tags
        assert "#P3" in tags


# ---------------------------------------------------------------------------
# _should_notify_for_war_type
# ---------------------------------------------------------------------------

class TestShouldNotifyForWarType:
    def _fn(self):
        from qapbot.war_notifications import _should_notify_for_war_type
        return _should_notify_for_war_type

    def test_no_user_data(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        assert self._fn()("123", {}) is False

    def test_all_wars(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"123": {"notification_settings": {"notification_type": "all_wars"}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        assert self._fn()("123", {}) is True

    def test_cwl_only_with_cwl_war(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"123": {"notification_settings": {"notification_type": "cwl_only"}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        assert self._fn()("123", {"type": "cwl", "is_cwl": False}) is True

    def test_cwl_only_with_is_cwl_flag(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"123": {"notification_settings": {"notification_type": "cwl_only"}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        assert self._fn()("123", {"type": "regular", "is_cwl": True}) is True

    def test_cwl_only_with_regular_war(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"123": {"notification_settings": {"notification_type": "cwl_only"}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        assert self._fn()("123", {"type": "regular", "is_cwl": False}) is False

    def test_unknown_type_defaults_true(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"123": {"notification_settings": {"notification_type": "custom_unknown"}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        assert self._fn()("123", {}) is True

    def test_no_notification_settings(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"123": {}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        # Empty dict is falsy → returns False
        assert self._fn()("123", {}) is False


# ---------------------------------------------------------------------------
# _should_send_buddy_notification
# ---------------------------------------------------------------------------

class TestShouldSendBuddyNotification:
    def _fn(self):
        from qapbot.war_notifications import _should_send_buddy_notification
        return _should_send_buddy_notification

    def test_no_user_data(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        assert self._fn()("WAR1", "#P1", "456", 3.0) is False

    def test_hours_above_threshold(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"456": {"notification_settings": {"notification_mode": "once", "hours_before_end": 4}}}
        cache.notification_state = {}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        assert self._fn()("WAR1", "#P1", "456", 5.0) is False

    def test_once_mode_first_time(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"456": {"notification_settings": {"notification_mode": "once", "hours_before_end": 4}}}
        cache.notification_state = {}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        assert self._fn()("WAR1", "#P1", "456", 3.0) is True

    def test_once_mode_already_notified(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"456": {"notification_settings": {"notification_mode": "once", "hours_before_end": 4}}}
        cache.notification_state = {"WAR1": {"buddy_notifications": {"456_P1": {"notification_time": "2025-01-01 12:00:00"}}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        assert self._fn()("WAR1", "#P1", "456", 3.0) is False

    def test_repeated_mode_no_prior_state(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"456": {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 4}}}
        cache.notification_state = {}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        assert self._fn()("WAR1", "#P1", "456", 3.0) is True

    def test_repeated_mode_missing_time(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"456": {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 4}}}
        cache.notification_state = {"WAR1": {"buddy_notifications": {"456_P1": {}}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        assert self._fn()("WAR1", "#P1", "456", 3.0) is True

    def test_repeated_mode_interval_gt_2h(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"456": {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 10}}}
        last_time = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        cache.notification_state = {"WAR1": {"buddy_notifications": {"456_P1": {"notification_time": last_time}}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        # hours_remaining > 2, required_interval = 2.0, hours_since_last = 3 → True
        assert self._fn()("WAR1", "#P1", "456", 3.0) is True

    def test_repeated_mode_interval_lt_2h(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"456": {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 10}}}
        last_time = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        cache.notification_state = {"WAR1": {"buddy_notifications": {"456_P1": {"notification_time": last_time}}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        # hours_remaining > 2, required_interval = 2.0, hours_since_last = 0.5 → False
        assert self._fn()("WAR1", "#P1", "456", 3.0) is False

    def test_repeated_mode_interval_mid_range(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"456": {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 10}}}
        last_time = (datetime.now() - timedelta(hours=1, minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        cache.notification_state = {"WAR1": {"buddy_notifications": {"456_P1": {"notification_time": last_time}}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        # hours_remaining = 1.5 (>0.5, <=2), required_interval = 1.0, hours_since_last ≈ 1.17 → True
        assert self._fn()("WAR1", "#P1", "456", 1.5) is True

    def test_repeated_mode_interval_final_hour(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"456": {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 10}}}
        last_time = (datetime.now() - timedelta(minutes=35)).strftime("%Y-%m-%d %H:%M:%S")
        cache.notification_state = {"WAR1": {"buddy_notifications": {"456_P1": {"notification_time": last_time}}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        # hours_remaining = 0.3 (<=0.5), required_interval = 0.5, hours_since_last ≈ 0.58 → True
        assert self._fn()("WAR1", "#P1", "456", 0.3) is True

    def test_repeated_mode_parse_error(self, monkeypatch):
        cache = MagicMock()
        cache.user_accounts = {"456": {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 10}}}
        cache.notification_state = {"WAR1": {"buddy_notifications": {"456_P1": {"notification_time": "INVALID"}}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        # Parse error → returns True
        assert self._fn()("WAR1", "#P1", "456", 3.0) is True


# ---------------------------------------------------------------------------
# _record_buddy_notification
# ---------------------------------------------------------------------------

class TestRecordBuddyNotification:
    def _fn(self):
        from qapbot.war_notifications import _record_buddy_notification
        return _record_buddy_notification

    def test_new_war_id(self, monkeypatch):
        cache = MagicMock()
        cache.notification_state = {}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)

        self._fn()("WAR1", {"player_tag": "#P1", "player_name": "Player1", "discord_id": "456", "attacks_remaining": 1})
        state = cache.notification_state["WAR1"]["buddy_notifications"]["456_P1"]
        assert state["player_name"] == "Player1"
        assert state["watcher_discord_id"] == "456"
        assert state["attacks_remaining"] == 1

    def test_existing_war_no_buddy_key(self, monkeypatch):
        cache = MagicMock()
        cache.notification_state = {"WAR1": {"notified_players": {}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)

        self._fn()("WAR1", {"player_tag": "#P2", "player_name": "Player2", "discord_id": "789", "attacks_remaining": 2})
        assert "buddy_notifications" in cache.notification_state["WAR1"]
        assert "789_P2" in cache.notification_state["WAR1"]["buddy_notifications"]

    def test_existing_war_with_buddy_key(self, monkeypatch):
        cache = MagicMock()
        cache.notification_state = {"WAR1": {"notified_players": {}, "buddy_notifications": {"111_X1": {"old": True}}}}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)

        self._fn()("WAR1", {"player_tag": "#P3", "player_name": "Player3", "discord_id": "222", "attacks_remaining": 1})
        # Old entry preserved
        assert "111_X1" in cache.notification_state["WAR1"]["buddy_notifications"]
        # New entry added
        assert "222_P3" in cache.notification_state["WAR1"]["buddy_notifications"]


# ---------------------------------------------------------------------------
# _format_aggregated_reminder_message
# ---------------------------------------------------------------------------

class TestFormatAggregatedReminderMessage:
    def _fn(self):
        from qapbot.war_notifications import _format_aggregated_reminder_message
        return _format_aggregated_reminder_message

    def _player(self, tag="#P1", name="Player1", attacks=1, hours=3.5, is_buddy=False):
        return {
            "player_tag": tag,
            "player_name": name,
            "attacks_remaining": attacks,
            "clan_name": "TestClan",
            "opponent_name": "EnemyClan",
            "hours_remaining": hours,
            "is_buddy": is_buddy,
        }

    def test_single_own_account(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications.t", lambda key, **kw: key)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        result = self._fn()("Alex", [self._player()], "123", guild_id=1)
        assert "Player1" in result
        assert "ui_components.war_notification_dm.header" in result

    def test_time_format_minutes(self, monkeypatch):
        """hours < 1 should produce minutes-based time string."""
        captured = {}
        def fake_t(key, **kw):
            if "war_ends_minutes" in key:
                captured["minutes"] = kw.get("minutes")
            return key
        monkeypatch.setattr("qapbot.war_notifications.t", fake_t)
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        self._fn()("Alex", [self._player(hours=0.5)], "123", guild_id=1)
        assert captured["minutes"] == 30

    def test_time_format_hours(self, monkeypatch):
        """hours >= 1 should produce hours-based time string."""
        captured = {}
        def fake_t(key, **kw):
            if "war_ends_hours" in key:
                captured["hours"] = kw.get("hours")
            return key
        monkeypatch.setattr("qapbot.war_notifications.t", fake_t)
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        self._fn()("Alex", [self._player(hours=3.5)], "123", guild_id=1)
        assert captured["hours"] == "3.5"

    def test_single_attack_single_account(self, monkeypatch):
        captured = {}
        def fake_t(key, **kw):
            captured[key] = kw
            return key
        monkeypatch.setattr("qapbot.war_notifications.t", fake_t)
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        self._fn()("Alex", [self._player(attacks=1)], "123", guild_id=1)
        assert "ui_components.war_notification_dm.attacks_remaining_single" in captured

    def test_multiple_attacks_single_account(self, monkeypatch):
        captured = {}
        def fake_t(key, **kw):
            captured[key] = kw
            return key
        monkeypatch.setattr("qapbot.war_notifications.t", fake_t)
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        self._fn()("Alex", [self._player(attacks=2)], "123", guild_id=1)
        # total_attacks=2, account_count=1 → one of them is 1 → multiple_single
        assert "ui_components.war_notification_dm.attacks_remaining_multiple_single" in captured

    def test_multiple_attacks_multiple_accounts(self, monkeypatch):
        captured = {}
        def fake_t(key, **kw):
            captured[key] = kw
            return key
        monkeypatch.setattr("qapbot.war_notifications.t", fake_t)
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        players = [self._player(tag="#P1", attacks=2), self._player(tag="#P2", attacks=2)]
        self._fn()("Alex", players, "123", guild_id=1)
        # total_attacks=4, account_count=2 → multiple_multiple
        assert "ui_components.war_notification_dm.attacks_remaining_multiple_multiple" in captured

    def test_buddy_sections(self, monkeypatch):
        captured_keys = []
        def fake_t(key, **kw):
            captured_keys.append(key)
            return key
        monkeypatch.setattr("qapbot.war_notifications.t", fake_t)
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        players = [
            self._player(tag="#P1", is_buddy=False),
            self._player(tag="#P2", name="BuddyP", is_buddy=True),
        ]
        _result = self._fn()("Alex", players, "123", guild_id=1)
        assert "warnotifications.buddy_dm_own_section" in captured_keys
        assert "warnotifications.buddy_dm_section" in captured_keys

    def test_only_buddies(self, monkeypatch):
        captured_keys = []
        def fake_t(key, **kw):
            captured_keys.append(key)
            return key
        monkeypatch.setattr("qapbot.war_notifications.t", fake_t)
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        players = [self._player(tag="#P2", is_buddy=True)]
        _result = self._fn()("Alex", players, "123", guild_id=1)
        assert "warnotifications.buddy_dm_section" in captured_keys
        # own section should NOT appear
        assert "warnotifications.buddy_dm_own_section" not in captured_keys


# ---------------------------------------------------------------------------
# _send_channel_war_notification (async)
# ---------------------------------------------------------------------------

class TestSendChannelWarNotification:
    def _fn(self):
        from qapbot.war_notifications import _send_channel_war_notification
        return _send_channel_war_notification

    def _war_data(self):
        return {
            "clan": {"name": "TestClan"},
            "opponent": {"name": "EnemyClan"},
        }

    def _player(self, tag="#P1", name="P1", attacks=1):
        return {"player_tag": tag, "player_name": name, "attacks_remaining": attacks}

    @pytest.mark.asyncio
    async def test_empty_players_returns_zero(self):
        result = await self._fn()("#CLAN1", self._war_data(), [])
        assert result == 0

    @pytest.mark.asyncio
    async def test_no_hours_remaining_returns_zero(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: None)  # type: ignore[misc]
        result = await self._fn()("#CLAN1", self._war_data(), [self._player()])
        assert result == 0

    @pytest.mark.asyncio
    async def test_no_subscribed_guilds_returns_zero(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 3.0)  # type: ignore[misc]
        cache = MagicMock()
        cache.subscriptions = {}
        cache.clan_families = {}
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)
        result = await self._fn()("#CLAN1", self._war_data(), [self._player()])
        assert result == 0

    @pytest.mark.asyncio
    async def test_channel_disabled_returns_zero(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 0.5)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_war_id", lambda ct, wd: "W1")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._is_channel_notification_sent", lambda wid, gid: False)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications.t", lambda key, **kw: key)  # type: ignore[misc]

        cache = MagicMock()
        cache.subscriptions = {"111": {"ch1": [{"clan_tag": "#CLAN1"}]}}
        cache.clan_families = {}
        cache.server_config = {"111": {"channel_war_notifications_enabled": False}}
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)

        config = MagicMock()
        config.is_dev_mode = False
        monkeypatch.setattr("qapbot.war_notifications.CONFIG", config)

        result = await self._fn()("#CLAN1", self._war_data(), [self._player()])
        assert result == 0

    @pytest.mark.asyncio
    async def test_already_sent_skips(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 0.5)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_war_id", lambda ct, wd: "W1")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._is_channel_notification_sent", lambda wid, gid: True)  # type: ignore[misc]

        cache = MagicMock()
        cache.subscriptions = {"111": {"ch1": [{"clan_tag": "#CLAN1"}]}}
        cache.clan_families = {}
        cache.server_config = {"111": {"channel_war_notifications_enabled": True, "war_notification_channel_id": "999"}}
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)

        config = MagicMock()
        config.is_dev_mode = False
        monkeypatch.setattr("qapbot.war_notifications.CONFIG", config)

        result = await self._fn()("#CLAN1", self._war_data(), [self._player()])
        assert result == 0

    @pytest.mark.asyncio
    async def test_threshold_exceeded_skips(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 3.0)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_war_id", lambda ct, wd: "W1")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._is_channel_notification_sent", lambda wid, gid: False)  # type: ignore[misc]

        cache = MagicMock()
        cache.subscriptions = {"111": {"ch1": [{"clan_tag": "#CLAN1"}]}}
        cache.clan_families = {}
        cache.server_config = {"111": {
            "channel_war_notifications_enabled": True,
            "war_notification_channel_id": "999",
            "war_notification_threshold_hours": 1.0,  # threshold = 1h, but 3h remaining
        }}
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)

        config = MagicMock()
        config.is_dev_mode = False
        monkeypatch.setattr("qapbot.war_notifications.CONFIG", config)

        result = await self._fn()("#CLAN1", self._war_data(), [self._player()])
        assert result == 0

    @pytest.mark.asyncio
    async def test_successful_send(self, monkeypatch):
        import discord

        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 0.5)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_war_id", lambda ct, wd: "W1")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._is_channel_notification_sent", lambda wid, gid: False)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._record_channel_notification", AsyncMock())
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications.t", lambda key, **kw: key)  # type: ignore[misc]

        cache = MagicMock()
        cache.subscriptions = {"111": {"ch1": [{"clan_tag": "#CLAN1"}]}}
        cache.clan_families = {}
        cache.server_config = {"111": {
            "channel_war_notifications_enabled": True,
            "war_notification_channel_id": "999",
            "war_notification_threshold_hours": 1.0,
        }}
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)

        config = MagicMock()
        config.is_dev_mode = False
        monkeypatch.setattr("qapbot.war_notifications.CONFIG", config)

        channel = AsyncMock(spec=discord.TextChannel)
        bot = AsyncMock()
        bot.get_channel = MagicMock(return_value=channel)
        bot.fetch_channel = AsyncMock(return_value=channel)
        monkeypatch.setattr("QBcore.bot", bot)

        result = await self._fn()("#CLAN1", self._war_data(), [self._player()])
        assert result == 1
        channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dev_mode_filters_guild(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 0.5)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_war_id", lambda ct, wd: "W1")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._is_channel_notification_sent", lambda wid, gid: False)  # type: ignore[misc]

        cache = MagicMock()
        cache.subscriptions = {"111": {"ch1": [{"clan_tag": "#CLAN1"}]}}
        cache.clan_families = {}
        cache.server_config = {"111": {
            "channel_war_notifications_enabled": True,
            "war_notification_channel_id": "999",
            "war_notification_threshold_hours": 1.0,
        }}
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)

        config = MagicMock()
        config.is_dev_mode = True
        config.discord_guild_id = 999  # Different from "111"
        monkeypatch.setattr("qapbot.war_notifications.CONFIG", config)

        result = await self._fn()("#CLAN1", self._war_data(), [self._player()])
        assert result == 0

    @pytest.mark.asyncio
    async def test_fetch_channel_error_continues(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 0.5)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_war_id", lambda ct, wd: "W1")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._is_channel_notification_sent", lambda wid, gid: False)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications.t", lambda key, **kw: key)  # type: ignore[misc]

        cache = MagicMock()
        cache.subscriptions = {"111": {"ch1": [{"clan_tag": "#CLAN1"}]}}
        cache.clan_families = {}
        cache.server_config = {"111": {
            "channel_war_notifications_enabled": True,
            "war_notification_channel_id": "999",
            "war_notification_threshold_hours": 1.0,
        }}
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)

        config = MagicMock()
        config.is_dev_mode = False
        monkeypatch.setattr("qapbot.war_notifications.CONFIG", config)

        bot = AsyncMock()
        bot.get_channel = MagicMock(return_value=None)
        bot.fetch_channel = AsyncMock(side_effect=Exception("Not found"))
        monkeypatch.setattr("QBcore.bot", bot)

        result = await self._fn()("#CLAN1", self._war_data(), [self._player()])
        assert result == 0

    @pytest.mark.asyncio
    async def test_family_subscription_match(self, monkeypatch):
        import discord

        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 0.5)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_war_id", lambda ct, wd: "W1")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._is_channel_notification_sent", lambda wid, gid: False)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._record_channel_notification", AsyncMock())
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications.t", lambda key, **kw: key)  # type: ignore[misc]

        cache = MagicMock()
        # Subscribe to family, not directly to clan
        cache.subscriptions = {"111": {"ch1": [{"clan_tag": "FAMILY1"}]}}
        cache.clan_families = {"FAMILY1": {"clans": ["#CLAN1", "#CLAN2"]}}
        cache.server_config = {"111": {
            "channel_war_notifications_enabled": True,
            "war_notification_channel_id": "999",
            "war_notification_threshold_hours": 1.0,
        }}
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)

        config = MagicMock()
        config.is_dev_mode = False
        monkeypatch.setattr("qapbot.war_notifications.CONFIG", config)

        channel = AsyncMock(spec=discord.TextChannel)
        bot = AsyncMock()
        bot.get_channel = MagicMock(return_value=channel)
        bot.fetch_channel = AsyncMock(return_value=channel)
        monkeypatch.setattr("QBcore.bot", bot)

        result = await self._fn()("#CLAN1", self._war_data(), [self._player()])
        assert result == 1

    @pytest.mark.asyncio
    async def test_custodians_mentioned_when_configured(self, monkeypatch):
        import discord

        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 0.5)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_war_id", lambda ct, wd: "W1")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._is_channel_notification_sent", lambda wid, gid: False)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._record_channel_notification", AsyncMock())
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        # NOTE: _send_channel_war_notification does `from qapbot.i18n import t` locally (not just
        # at module scope), so a monkeypatch of `qapbot.war_notifications.t` gets shadowed by that
        # re-import on every call. Patching `qapbot.i18n.t` itself is what the local import
        # actually resolves against.
        monkeypatch.setattr("qapbot.i18n.t", lambda key, **kw: kw.get("mentions", key))  # type: ignore[misc]

        cache = MagicMock()
        cache.subscriptions = {"111": {"ch1": [{"clan_tag": "#CLAN1"}]}}
        cache.clan_families = {}
        cache.server_config = {"111": {
            "channel_war_notifications_enabled": True,
            "war_notification_channel_id": "999",
            "war_notification_threshold_hours": 1.0,
            "clan_custodians": {"#CLAN1": ["111", "222"]},
        }}
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)

        config = MagicMock()
        config.is_dev_mode = False
        monkeypatch.setattr("qapbot.war_notifications.CONFIG", config)

        channel = AsyncMock(spec=discord.TextChannel)
        bot = AsyncMock()
        bot.get_channel = MagicMock(return_value=channel)
        bot.fetch_channel = AsyncMock(return_value=channel)
        monkeypatch.setattr("QBcore.bot", bot)

        result = await self._fn()("#CLAN1", self._war_data(), [self._player()])
        assert result == 1
        channel.send.assert_awaited_once()
        _, kwargs = channel.send.call_args
        assert kwargs["content"] == "<@111> <@222>"
        assert kwargs["allowed_mentions"].users is True
        assert kwargs["allowed_mentions"].everyone is False
        assert kwargs["allowed_mentions"].roles is False

    @pytest.mark.asyncio
    async def test_no_content_when_no_custodians_configured(self, monkeypatch):
        import discord

        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 0.5)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_war_id", lambda ct, wd: "W1")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._is_channel_notification_sent", lambda wid, gid: False)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._record_channel_notification", AsyncMock())
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.i18n.t", lambda key, **kw: key)  # type: ignore[misc]

        cache = MagicMock()
        cache.subscriptions = {"111": {"ch1": [{"clan_tag": "#CLAN1"}]}}
        cache.clan_families = {}
        cache.server_config = {"111": {
            "channel_war_notifications_enabled": True,
            "war_notification_channel_id": "999",
            "war_notification_threshold_hours": 1.0,
        }}
        monkeypatch.setattr("qapbot.cache_manager.CACHE", cache)

        config = MagicMock()
        config.is_dev_mode = False
        monkeypatch.setattr("qapbot.war_notifications.CONFIG", config)

        channel = AsyncMock(spec=discord.TextChannel)
        bot = AsyncMock()
        bot.get_channel = MagicMock(return_value=channel)
        bot.fetch_channel = AsyncMock(return_value=channel)
        monkeypatch.setattr("QBcore.bot", bot)

        result = await self._fn()("#CLAN1", self._war_data(), [self._player()])
        assert result == 1
        channel.send.assert_awaited_once()
        _, kwargs = channel.send.call_args
        assert kwargs["content"] is None
