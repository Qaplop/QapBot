"""Tests for war_notifications deep coverage — Phase 3.

Covers: _get_players_with_attacks_remaining, _get_hours_until_war_end,
_get_war_id, _should_notify_for_war_type, _get_player_discord_id.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportReturnType=false, reportUnusedImport=false
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


def _mock_cache(**overrides) -> MagicMock:
    cache = MagicMock()
    cache.user_accounts = overrides.get("user_accounts", {})
    cache.notification_state = overrides.get("notification_state", {})
    return cache


# ---------------------------------------------------------------------------
# _get_hours_until_war_end
# ---------------------------------------------------------------------------

class TestGetHoursUntilWarEnd:
    def _fn(self):
        from qapbot.war_notifications import _get_hours_until_war_end
        return _get_hours_until_war_end

    def test_future_datetime_returns_correct_hours(self):
        """Datetime component is parsed; stale seconds_until is ignored."""
        import datetime as dt
        future = dt.datetime.utcnow() + dt.timedelta(hours=2)
        end_str = (
            f"<Timestamp time=datetime.datetime({future.year}, {future.month}, {future.day}, "
            f"{future.hour}, {future.minute}, {future.second}) seconds_until=99999>"
        )
        result = self._fn()({"end_time": end_str})
        assert result is not None
        assert 1.9 < result < 2.1

    def test_one_hour(self):
        wd = {"end_time": "<Timestamp seconds_until=3600>"}
        assert self._fn()(wd) == 1.0

    def test_no_seconds_until(self):
        wd = {"end_time": "<Timestamp time=datetime>"}
        assert self._fn()(wd) is None

    def test_empty_end_time(self):
        wd = {"end_time": ""}
        assert self._fn()(wd) is None

    def test_missing_end_time(self):
        wd = {}
        assert self._fn()(wd) is None

    def test_malformed_seconds(self):
        wd = {"end_time": "seconds_until=not_a_number>"}
        assert self._fn()(wd) is None


# ---------------------------------------------------------------------------
# _get_war_id
# ---------------------------------------------------------------------------

class TestGetWarId:
    def _fn(self):
        from qapbot.war_notifications import _get_war_id
        return _get_war_id

    def test_basic(self):
        wd = {"clan": {"tag": "#CLAN1"}, "opponent": {"tag": "#OPP1"}}
        result = self._fn()("#CLAN1", wd)
        assert "CLAN1" in result
        assert "OPP1" in result
        assert "#" not in result

    def test_strips_hash(self):
        wd = {"clan": {"tag": "#ABC"}, "opponent": {"tag": "#DEF"}}
        result = self._fn()("#ABC", wd)
        assert result == "ABC_DEF"

    def test_unknown_opponent(self):
        wd = {"clan": {"tag": "#ABC"}}
        result = self._fn()("#ABC", wd)
        assert result == "ABC_UNKNOWN"


# ---------------------------------------------------------------------------
# _get_players_with_attacks_remaining
# ---------------------------------------------------------------------------

class TestGetPlayersWithAttacksRemaining:
    def _fn(self):
        from qapbot.war_notifications import _get_players_with_attacks_remaining
        return _get_players_with_attacks_remaining

    def _war_data(self, members, attacks_per_member=2, seconds_until=7200):
        return {
            "clan": {"name": "TestClan", "members": members},
            "opponent": {"name": "EnemyClan"},
            "attacks_per_member": attacks_per_member,
            "end_time": f"<Timestamp seconds_until={seconds_until}>",
        }

    def test_player_with_remaining_attacks(self):
        members = [{"tag": "#P1", "name": "Alice", "attacks": [{}]}]  # 1 used, 1 remaining
        result = self._fn()(self._war_data(members))
        assert len(result) == 1
        assert result[0]["player_tag"] == "#P1"
        assert result[0]["attacks_remaining"] == 1
        assert result[0]["clan_name"] == "TestClan"
        assert result[0]["opponent_name"] == "EnemyClan"

    def test_all_attacks_used(self):
        members = [{"tag": "#P1", "name": "Alice", "attacks": [{}, {}]}]
        result = self._fn()(self._war_data(members))
        assert len(result) == 0

    def test_no_attacks_used(self):
        members = [{"tag": "#P1", "name": "Alice", "attacks": []}]
        result = self._fn()(self._war_data(members))
        assert len(result) == 1
        assert result[0]["attacks_remaining"] == 2

    def test_no_attacks_key(self):
        members = [{"tag": "#P1", "name": "Alice"}]
        result = self._fn()(self._war_data(members))
        assert len(result) == 1
        assert result[0]["attacks_remaining"] == 2

    def test_cwl_one_attack(self):
        members = [{"tag": "#P1", "name": "Alice", "attacks": []}]
        result = self._fn()(self._war_data(members, attacks_per_member=1))
        assert result[0]["attacks_remaining"] == 1

    def test_invalid_end_time_returns_empty(self):
        wd = {
            "clan": {"name": "TestClan", "members": [{"tag": "#P1", "name": "Alice"}]},
            "opponent": {"name": "Enemy"},
            "attacks_per_member": 2,
            "end_time": "invalid",
        }
        result = self._fn()(wd)
        assert result == []

    def test_multiple_players(self):
        members = [
            {"tag": "#P1", "name": "Alice", "attacks": [{}]},     # 1 remaining
            {"tag": "#P2", "name": "Bob", "attacks": [{}, {}]},    # 0 remaining
            {"tag": "#P3", "name": "Charlie", "attacks": []},      # 2 remaining
        ]
        result = self._fn()(self._war_data(members))
        tags = [r["player_tag"] for r in result]
        assert "#P1" in tags
        assert "#P2" not in tags
        assert "#P3" in tags

    def test_hours_remaining_in_result(self):
        members = [{"tag": "#P1", "name": "Alice", "attacks": []}]
        result = self._fn()(self._war_data(members, seconds_until=3600))
        assert result[0]["hours_remaining"] == 1.0


# ---------------------------------------------------------------------------
# _should_notify_for_war_type
# ---------------------------------------------------------------------------

class TestShouldNotifyForWarType:
    def _fn(self):
        from qapbot.war_notifications import _should_notify_for_war_type
        return _should_notify_for_war_type

    def test_all_wars_always_true(self, monkeypatch):
        cache = _mock_cache(user_accounts={
            "U1": {"notification_settings": {"notification_type": "all_wars"}}
        })
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        wd = {"attacks_per_member": 2}
        assert self._fn()("U1", wd) is True

    def test_cwl_only_with_cwl_war(self, monkeypatch):
        cache = _mock_cache(user_accounts={
            "U1": {"notification_settings": {"notification_type": "cwl_only"}}
        })
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        wd = {"type": "cwl", "is_cwl": True}
        assert self._fn()("U1", wd) is True

    def test_cwl_only_with_regular_war(self, monkeypatch):
        cache = _mock_cache(user_accounts={
            "U1": {"notification_settings": {"notification_type": "cwl_only"}}
        })
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        wd = {"type": "regular", "is_cwl": False}
        assert self._fn()("U1", wd) is False

    def test_no_preference_defaults_all_wars(self, monkeypatch):
        cache = _mock_cache(user_accounts={"U1": {"notification_settings": {}}})
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        wd = {"attacks_per_member": 2}
        assert self._fn()("U1", wd) is True

    def test_unknown_user_returns_false(self, monkeypatch):
        cache = _mock_cache(user_accounts={})
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)
        wd = {"attacks_per_member": 2}
        assert self._fn()("UNKNOWN", wd) is False


# ---------------------------------------------------------------------------
# _get_player_discord_id
# ---------------------------------------------------------------------------

class TestGetPlayerDiscordId:
    def _fn(self):
        from qapbot.war_notifications import _get_player_discord_id
        return _get_player_discord_id

    def test_finds_mapped_user(self, monkeypatch):
        import qapbot.war_notifications as wn
        monkeypatch.setattr(wn, "_notification_player_index", {"P1": "U1"})
        result = self._fn()("#P1")
        assert result == "U1"

    def test_war_reminders_disabled(self, monkeypatch):
        # war_reminders=False → player excluded from index at build time → None
        import qapbot.war_notifications as wn
        monkeypatch.setattr(wn, "_notification_player_index", {})
        result = self._fn()("#P1")
        assert result is None

    def test_no_notification_settings(self, monkeypatch):
        import qapbot.war_notifications as wn
        monkeypatch.setattr(wn, "_notification_player_index", {})
        result = self._fn()("#P1")
        assert result is None

    def test_player_not_found(self, monkeypatch):
        import qapbot.war_notifications as wn
        monkeypatch.setattr(wn, "_notification_player_index", {"OTHER": "U1"})
        result = self._fn()("#P1")
        assert result is None

    def test_empty_accounts(self, monkeypatch):
        import qapbot.war_notifications as wn
        monkeypatch.setattr(wn, "_notification_player_index", {})
        result = self._fn()("#P1")
        assert result is None
