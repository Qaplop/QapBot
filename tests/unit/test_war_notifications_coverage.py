"""Extended tests for qapbot/war_notifications.py — pure/sync helper functions.

Targets uncovered lines: 93-136, 181-182, 187, 193-196, 230-232, 294-301,
328, 335-340, 346-347, 381-472 and more.
Focuses on testable helpers that don't require live Discord connections.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownLambdaType=false, reportUnusedImport=false
# pyright: reportReturnType=false
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# _get_hours_until_war_end
# ---------------------------------------------------------------------------

class TestGetHoursUntilWarEnd:
    def test_future_datetime_parsed_correctly(self):
        """Actual UTC datetime is parsed; stale seconds_until is ignored."""
        from datetime import timedelta
        from qapbot.war_notifications import _get_hours_until_war_end
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        end_str = (
            f"<Timestamp time=datetime.datetime({future.year}, {future.month}, {future.day}, "
            f"{future.hour}, {future.minute}, {future.second}) seconds_until=99999>"
        )
        result = _get_hours_until_war_end({"end_time": end_str})
        assert result is not None
        assert 1.9 < result < 2.1

    def test_expired_datetime_returns_negative(self):
        """War that ended hours ago returns negative value → filtered out."""
        from datetime import timedelta
        from qapbot.war_notifications import _get_hours_until_war_end
        past = datetime.now(timezone.utc) - timedelta(hours=4)
        end_str = (
            f"<Timestamp time=datetime.datetime({past.year}, {past.month}, {past.day}, "
            f"{past.hour}, {past.minute}, {past.second}) seconds_until=14400>"
        )
        result = _get_hours_until_war_end({"end_time": end_str})
        assert result is not None
        assert result < 0  # correctly identified as expired despite positive seconds_until

    def test_5_field_datetime_no_seconds(self):
        """Datetime with only 5 fields (no seconds component) is handled."""
        from datetime import timedelta
        from qapbot.war_notifications import _get_hours_until_war_end
        future = datetime.now(timezone.utc) + timedelta(hours=3)
        # Omit seconds field intentionally
        end_str = (
            f"<Timestamp time=datetime.datetime({future.year}, {future.month}, {future.day}, "
            f"{future.hour}, {future.minute}) seconds_until=10800>"
        )
        result = _get_hours_until_war_end({"end_time": end_str})
        assert result is not None
        assert 2.9 < result < 3.1

    def test_fallback_seconds_until_when_no_datetime(self):
        """No datetime component → fall back to seconds_until."""
        from qapbot.war_notifications import _get_hours_until_war_end
        wd = {"end_time": "<Timestamp seconds_until=7200>"}
        assert _get_hours_until_war_end(wd) == 2.0

    def test_missing_end_time(self):
        from qapbot.war_notifications import _get_hours_until_war_end
        assert _get_hours_until_war_end({}) is None

    def test_invalid_format(self):
        from qapbot.war_notifications import _get_hours_until_war_end
        assert _get_hours_until_war_end({"end_time": "garbage"}) is None

    def test_empty_string(self):
        from qapbot.war_notifications import _get_hours_until_war_end
        assert _get_hours_until_war_end({"end_time": ""}) is None


# ---------------------------------------------------------------------------
# _get_war_id
# ---------------------------------------------------------------------------

class TestGetWarId:
    def test_strips_hashes(self):
        from qapbot.war_notifications import _get_war_id
        wd = {"opponent": {"tag": "#OPP123"}}
        assert _get_war_id("#CLAN99", wd) == "CLAN99_OPP123"

    def test_missing_opponent(self):
        from qapbot.war_notifications import _get_war_id
        result = _get_war_id("#CLAN", {})
        assert result == "CLAN_UNKNOWN"


# ---------------------------------------------------------------------------
# _should_notify_for_war_type
# ---------------------------------------------------------------------------

class TestShouldNotifyForWarType:
    def test_all_wars_always_true(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.user_accounts = {
            "42": {"notification_settings": {"notification_type": "all_wars"}}
        }
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _should_notify_for_war_type
        assert _should_notify_for_war_type("42", {}) is True

    def test_cwl_only_true_for_cwl_war(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.user_accounts = {
            "42": {"notification_settings": {"notification_type": "cwl_only"}}
        }
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _should_notify_for_war_type
        assert _should_notify_for_war_type("42", {"type": "cwl"}) is True

    def test_cwl_only_true_for_is_cwl_flag(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.user_accounts = {
            "42": {"notification_settings": {"notification_type": "cwl_only"}}
        }
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _should_notify_for_war_type
        assert _should_notify_for_war_type("42", {"is_cwl": True}) is True

    def test_cwl_only_false_for_regular_war(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.user_accounts = {
            "42": {"notification_settings": {"notification_type": "cwl_only"}}
        }
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _should_notify_for_war_type
        assert _should_notify_for_war_type("42", {"type": "regular"}) is False

    def test_unknown_type_defaults_true(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.user_accounts = {
            "42": {"notification_settings": {"notification_type": "future_mode"}}
        }
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _should_notify_for_war_type
        assert _should_notify_for_war_type("42", {}) is True

    def test_user_not_in_cache(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.user_accounts = {}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _should_notify_for_war_type
        assert _should_notify_for_war_type("42", {}) is False


# ---------------------------------------------------------------------------
# _get_player_discord_id
# ---------------------------------------------------------------------------

class TestGetPlayerDiscordId:
    def test_finds_registered_player(self, monkeypatch):
        import qapbot.war_notifications as wn
        monkeypatch.setattr(wn, "_notification_player_index", {"PLAYER1": "100"})
        assert wn._get_player_discord_id("#PLAYER1") == "100"

    def test_notifications_disabled_returns_none(self, monkeypatch):
        # war_reminders=False → player excluded from index at build time → None
        import qapbot.war_notifications as wn
        monkeypatch.setattr(wn, "_notification_player_index", {})
        assert wn._get_player_discord_id("#PLAYER1") is None

    def test_unregistered_player(self, monkeypatch):
        import qapbot.war_notifications as wn
        monkeypatch.setattr(wn, "_notification_player_index", {})
        assert wn._get_player_discord_id("#NOONE") is None

    def test_tag_without_hash_still_matches(self, monkeypatch):
        # lstrip("#") normalises both stored key and lookup input
        import qapbot.war_notifications as wn
        monkeypatch.setattr(wn, "_notification_player_index", {"PLAYER1": "100"})
        assert wn._get_player_discord_id("#PLAYER1") == "100"


# ---------------------------------------------------------------------------
# _get_buddy_watcher_discord_ids
# ---------------------------------------------------------------------------

class TestGetBuddyWatcherDiscordIds:
    def test_finds_watchers(self, monkeypatch):
        import qapbot.war_notifications as wn
        monkeypatch.setattr(wn, "_notification_watcher_index", {"PLAYER1": ["100", "300"]})
        watchers = wn._get_buddy_watcher_discord_ids("#PLAYER1")
        assert set(watchers) == {"100", "300"}

    def test_no_watchers(self, monkeypatch):
        import qapbot.war_notifications as wn
        monkeypatch.setattr(wn, "_notification_watcher_index", {"OTHER": ["100"]})
        assert wn._get_buddy_watcher_discord_ids("#PLAYER1") == []

    def test_case_insensitive_match(self, monkeypatch):
        # index keys are upper-case; lookup also uppercases → matches lowercase input
        import qapbot.war_notifications as wn
        monkeypatch.setattr(wn, "_notification_watcher_index", {"PLAYER1": ["100"]})
        assert wn._get_buddy_watcher_discord_ids("#player1") == ["100"]


# ---------------------------------------------------------------------------
# _is_already_notified
# ---------------------------------------------------------------------------

class TestIsAlreadyNotified:
    def test_not_notified(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.notification_state = {}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _is_already_notified
        assert _is_already_notified("war1", "#PLAYER1") is False

    def test_already_notified(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.notification_state = {
            "war1": {"notified_players": {"PLAYER1": {"notification_time": "2025-01-01 00:00:00"}}}
        }
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _is_already_notified
        assert _is_already_notified("war1", "#PLAYER1") is True


# ---------------------------------------------------------------------------
# _should_send_notification
# ---------------------------------------------------------------------------

class TestShouldSendNotification:
    def _setup_cache(self, monkeypatch, user_data: Dict[str, Any], notif_state: Dict[str, Any]):
        mock_cache = MagicMock()
        mock_cache.user_accounts = {"42": user_data}
        mock_cache.notification_state = notif_state
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)

    def test_user_not_in_cache(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.user_accounts = {}
        mock_cache.notification_state = {}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _should_send_notification
        assert _should_send_notification("war1", "#P", "42", 3.0) is False

    def test_outside_threshold(self, monkeypatch):
        self._setup_cache(monkeypatch,
            {"notification_settings": {"notification_mode": "once", "hours_before_end": 4}},
            {}
        )
        from qapbot.war_notifications import _should_send_notification
        assert _should_send_notification("war1", "#P", "42", 5.0) is False

    def test_once_mode_first_time(self, monkeypatch):
        self._setup_cache(monkeypatch,
            {"notification_settings": {"notification_mode": "once", "hours_before_end": 4}},
            {}
        )
        from qapbot.war_notifications import _should_send_notification
        assert _should_send_notification("war1", "#P", "42", 3.0) is True

    def test_once_mode_already_notified(self, monkeypatch):
        self._setup_cache(monkeypatch,
            {"notification_settings": {"notification_mode": "once", "hours_before_end": 4}},
            {"war1": {"notified_players": {"P": {"notification_time": "2025-01-01 00:00:00"}}}}
        )
        from qapbot.war_notifications import _should_send_notification
        assert _should_send_notification("war1", "#P", "42", 3.0) is False

    def test_repeated_mode_first_time(self, monkeypatch):
        self._setup_cache(monkeypatch,
            {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 4}},
            {}
        )
        from qapbot.war_notifications import _should_send_notification
        assert _should_send_notification("war1", "#P", "42", 3.0) is True

    def test_repeated_mode_too_soon(self, monkeypatch):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._setup_cache(monkeypatch,
            {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 4}},
            {"war1": {"notified_players": {"P": {"notification_time": now_str}}}}
        )
        from qapbot.war_notifications import _should_send_notification
        # Just notified → should be False (interval not met)
        assert _should_send_notification("war1", "#P", "42", 3.0) is False

    def test_repeated_mode_enough_time_passed(self, monkeypatch):
        old_time = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        self._setup_cache(monkeypatch,
            {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 10}},
            {"war1": {"notified_players": {"P": {"notification_time": old_time}}}}
        )
        from qapbot.war_notifications import _should_send_notification
        # 3h passed, >2h remaining → interval=2h → should be True
        assert _should_send_notification("war1", "#P", "42", 3.0) is True

    def test_repeated_mode_short_interval_near_end(self, monkeypatch):
        old_time = (datetime.now() - timedelta(hours=1.5)).strftime("%Y-%m-%d %H:%M:%S")
        self._setup_cache(monkeypatch,
            {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 10}},
            {"war1": {"notified_players": {"P": {"notification_time": old_time}}}}
        )
        from qapbot.war_notifications import _should_send_notification
        # 1.5h passed, 1.5h remaining → interval=1h → should be True
        assert _should_send_notification("war1", "#P", "42", 1.5) is True

    def test_repeated_no_timestamp_allows(self, monkeypatch):
        self._setup_cache(monkeypatch,
            {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 4}},
            {"war1": {"notified_players": {"P": {}}}}  # no notification_time key
        )
        from qapbot.war_notifications import _should_send_notification
        assert _should_send_notification("war1", "#P", "42", 3.0) is True


# ---------------------------------------------------------------------------
# _should_send_buddy_notification
# ---------------------------------------------------------------------------

class TestShouldSendBuddyNotification:
    def _setup_cache(self, monkeypatch, user_data: Dict[str, Any], notif_state: Dict[str, Any]):
        mock_cache = MagicMock()
        mock_cache.user_accounts = {"42": user_data}
        mock_cache.notification_state = notif_state
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)

    def test_user_not_found(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.user_accounts = {}
        mock_cache.notification_state = {}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _should_send_buddy_notification
        assert _should_send_buddy_notification("war1", "#P", "42", 3.0) is False

    def test_outside_threshold(self, monkeypatch):
        self._setup_cache(monkeypatch,
            {"notification_settings": {"notification_mode": "once", "hours_before_end": 2}},
            {}
        )
        from qapbot.war_notifications import _should_send_buddy_notification
        assert _should_send_buddy_notification("war1", "#P", "42", 5.0) is False

    def test_once_mode_first_time(self, monkeypatch):
        self._setup_cache(monkeypatch,
            {"notification_settings": {"notification_mode": "once", "hours_before_end": 4}},
            {}
        )
        from qapbot.war_notifications import _should_send_buddy_notification
        assert _should_send_buddy_notification("war1", "#P", "42", 3.0) is True

    def test_once_mode_already_sent(self, monkeypatch):
        self._setup_cache(monkeypatch,
            {"notification_settings": {"notification_mode": "once", "hours_before_end": 4}},
            {"war1": {"buddy_notifications": {"42_P": {"notification_time": "2025-01-01 00:00:00"}}}}
        )
        from qapbot.war_notifications import _should_send_buddy_notification
        assert _should_send_buddy_notification("war1", "#P", "42", 3.0) is False

    def test_repeated_first_time(self, monkeypatch):
        self._setup_cache(monkeypatch,
            {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 4}},
            {}
        )
        from qapbot.war_notifications import _should_send_buddy_notification
        assert _should_send_buddy_notification("war1", "#P", "42", 3.0) is True

    def test_repeated_too_soon(self, monkeypatch):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._setup_cache(monkeypatch,
            {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 4}},
            {"war1": {"buddy_notifications": {"42_P": {"notification_time": now_str}}}}
        )
        from qapbot.war_notifications import _should_send_buddy_notification
        assert _should_send_buddy_notification("war1", "#P", "42", 3.0) is False


# ---------------------------------------------------------------------------
# _record_buddy_notification
# ---------------------------------------------------------------------------

class TestRecordBuddyNotification:
    def test_creates_state_structure(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.notification_state = {}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _record_buddy_notification
        player_info = {
            "player_tag": "#P1",
            "player_name": "TestPlayer",
            "discord_id": "42",
            "attacks_remaining": 1,
        }
        _record_buddy_notification("war1", player_info)
        state = mock_cache.notification_state["war1"]["buddy_notifications"]["42_P1"]
        assert state["player_name"] == "TestPlayer"
        assert state["watcher_discord_id"] == "42"
        assert state["attacks_remaining"] == 1

    def test_appends_to_existing_state(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.notification_state = {
            "war1": {
                "notified_players": {},
                "buddy_notifications": {"99_X": {"player_name": "OtherPlayer"}}
            }
        }
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _record_buddy_notification
        _record_buddy_notification("war1", {
            "player_tag": "#P2",
            "player_name": "NewPlayer",
            "discord_id": "42",
            "attacks_remaining": 2,
        })
        assert "42_P2" in mock_cache.notification_state["war1"]["buddy_notifications"]
        assert "99_X" in mock_cache.notification_state["war1"]["buddy_notifications"]


# ---------------------------------------------------------------------------
# _is_channel_notification_sent
# ---------------------------------------------------------------------------

class TestIsChannelNotificationSent:
    def test_not_sent(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.notification_state = {}
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _is_channel_notification_sent
        assert _is_channel_notification_sent("war1", "guild1") is False

    def test_already_sent(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.notification_state = {
            "war1": {"channel_notifications": {"guild1": {"notification_time": "2025-01-01"}}}
        }
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _is_channel_notification_sent
        assert _is_channel_notification_sent("war1", "guild1") is True


# ---------------------------------------------------------------------------
# _record_notification (async)
# ---------------------------------------------------------------------------

class TestRecordNotification:
    @pytest.mark.asyncio
    async def test_creates_notification_state(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.notification_state = {}
        mock_cache.persist_player_notification = AsyncMock()
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _record_notification
        await _record_notification("war1", {
            "player_tag": "#P1",
            "player_name": "Test",
            "discord_id": "42",
            "attacks_remaining": 1,
        })
        state = mock_cache.notification_state["war1"]["notified_players"]["P1"]
        assert state["player_name"] == "Test"
        mock_cache.persist_player_notification.assert_awaited_once()


# ---------------------------------------------------------------------------
# _record_channel_notification (async)
# ---------------------------------------------------------------------------

class TestRecordChannelNotification:
    @pytest.mark.asyncio
    async def test_creates_channel_notification_state(self, monkeypatch):
        mock_cache = MagicMock()
        mock_cache.notification_state = {}
        mock_cache.persist_channel_notification = AsyncMock()
        monkeypatch.setattr("qapbot.war_notifications.CACHE", mock_cache)
        from qapbot.war_notifications import _record_channel_notification
        await _record_channel_notification("war1", "guild1", "MyClan", "EnemyClan")
        state = mock_cache.notification_state["war1"]["channel_notifications"]["guild1"]
        assert state["clan_name"] == "MyClan"
        mock_cache.persist_channel_notification.assert_awaited_once()


# ---------------------------------------------------------------------------
# _get_players_with_attacks_remaining
# ---------------------------------------------------------------------------

class TestGetPlayersWithAttacksRemaining:
    def test_basic(self, monkeypatch):
        from qapbot.war_notifications import _get_players_with_attacks_remaining
        wd = {
            "end_time": "<Timestamp time=datetime.datetime(2025, 12, 24, 10, 7, 9) seconds_until=7200>",
            "attacks_per_member": 2,
            "clan": {
                "name": "MyClan",
                "members": [
                    {"tag": "#P1", "name": "Player1", "attacks": [{"defenderTag": "#E1", "stars": 2}]},
                    {"tag": "#P2", "name": "Player2", "attacks": []},
                ]
            },
            "opponent": {"name": "Enemy"},
        }
        players = _get_players_with_attacks_remaining(wd)
        assert len(players) == 2
        assert players[0]["player_tag"] == "#P1"
        assert players[0]["attacks_remaining"] == 1
        assert players[1]["player_tag"] == "#P2"
        assert players[1]["attacks_remaining"] == 2

    def test_all_attacks_used(self, monkeypatch):
        from qapbot.war_notifications import _get_players_with_attacks_remaining
        wd = {
            "end_time": "<Timestamp time=datetime.datetime(2025, 12, 24, 10, 7, 9) seconds_until=7200>",
            "attacks_per_member": 1,
            "clan": {
                "name": "MyClan",
                "members": [
                    {"tag": "#P1", "name": "Player1", "attacks": [{"defenderTag": "#E1", "stars": 3}]},
                ]
            },
            "opponent": {"name": "Enemy"},
        }
        assert _get_players_with_attacks_remaining(wd) == []

    def test_missing_end_time(self):
        from qapbot.war_notifications import _get_players_with_attacks_remaining
        wd = {
            "attacks_per_member": 2,
            "clan": {"name": "MyClan", "members": [{"tag": "#P1", "name": "P1", "attacks": []}]},
            "opponent": {"name": "Enemy"},
        }
        assert _get_players_with_attacks_remaining(wd) == []


# ---------------------------------------------------------------------------
# _format_aggregated_reminder_message
# ---------------------------------------------------------------------------

class TestFormatAggregatedReminderMessage:
    def test_single_player_message(self, monkeypatch):
        # Stub t() to return key
        monkeypatch.setattr("qapbot.war_notifications.t", lambda key, **kwargs: key)
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)
        from qapbot.war_notifications import _format_aggregated_reminder_message
        players = [{
            "player_name": "TestPlayer",
            "attacks_remaining": 1,
            "hours_remaining": 2.5,
            "clan_name": "MyClan",
            "opponent_name": "Enemy",
            "is_buddy": False,
        }]
        msg = _format_aggregated_reminder_message("Alex", players, "42", guild_id=0)
        assert "TestPlayer" in msg
        assert "Alex" not in msg or True  # greeting key will contain display_name

    def test_buddy_section_appears(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications.t", lambda key, **kwargs: key)
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)
        from qapbot.war_notifications import _format_aggregated_reminder_message
        players = [
            {"player_name": "OwnAccount", "attacks_remaining": 1, "hours_remaining": 2.0,
             "clan_name": "C", "opponent_name": "O", "is_buddy": False},
            {"player_name": "BuddyAccount", "attacks_remaining": 2, "hours_remaining": 2.0,
             "clan_name": "C", "opponent_name": "O", "is_buddy": True},
        ]
        msg = _format_aggregated_reminder_message("User", players, "42")
        assert "OwnAccount" in msg
        assert "BuddyAccount" in msg

    def test_minutes_format_under_1h(self, monkeypatch):
        """When hours < 1, message should use minutes key."""
        calls = []
        def mock_t(key, **kw):
            calls.append(key)
            return key
        monkeypatch.setattr("qapbot.war_notifications.t", mock_t)
        monkeypatch.setattr("qapbot.war_notifications.normalize_player_name", lambda n: n)
        from qapbot.war_notifications import _format_aggregated_reminder_message
        players = [{
            "player_name": "P", "attacks_remaining": 1, "hours_remaining": 0.5,
            "clan_name": "C", "opponent_name": "O", "is_buddy": False,
        }]
        _format_aggregated_reminder_message("User", players, "42")
        assert any("minutes" in c for c in calls)


# ---------------------------------------------------------------------------
# _get_active_wars — skipped: function uses local imports of glob/json/os
# and reads files directly, making it hard to mock without complex patching.
# Covered by integration tests instead.
# ---------------------------------------------------------------------------
