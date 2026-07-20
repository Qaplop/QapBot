"""Tests for save_war_object (cache_manager) and war notifications — Phase 5 coverage push.

Covers:
- save_war_object inner helpers (simple_attack, find_best_opponent_attack,
  calculate_defensive_stars, simple_member, simple_badge, simple_clan) + outer flow (~292 lines)
- _get_players_needing_reminders (~91 lines)
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedImport=false, reportAttributeAccessIssue=false, reportReturnType=false, reportOptionalMemberAccess=false
# ruff: noqa: ANN001, ANN201
from __future__ import annotations
from typing import Any

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ===========================================================================
# Helpers
# ===========================================================================

def _obj(**kw):
    """Create a simple object with attributes from kwargs."""
    return type("obj", (object,), kw)()


def _make_war_obj(
    state="in_war",
    my_clan_tag="#MYCLAN1",
    my_clan_name="MyClan",
    enemy_clan_tag="#ENEMY1",
    enemy_clan_name="Enemy",
    members=None,
    enemy_members=None,
    team_size=15,
    attacks_per_member=2,
    start_time=None,
    end_time=None,
    war_type=None,
):
    """Build a fake war object matching coc.War interface."""
    if members is None:
        members = []
    if enemy_members is None:
        enemy_members = []

    my_clan = _obj(
        tag=my_clan_tag,
        name=my_clan_name,
        level=15,
        badge=None,
        stars=30,
        destruction=85.5,
        attack_wins=10,
        attacks_used=20,
        wins=None,
        members=members,
    )
    enemy = _obj(
        tag=enemy_clan_tag,
        name=enemy_clan_name,
        level=12,
        badge=None,
        stars=25,
        destruction=70.0,
        attack_wins=8,
        attacks_used=16,
        wins=None,
        members=enemy_members,
    )

    state_obj = _obj(name=state)

    return _obj(
        state=state_obj,
        clan=my_clan,
        opponent=enemy,
        team_size=team_size,
        attacks_per_member=attacks_per_member,
        start_time=start_time or "20250101T120000.000Z",
        end_time=end_time or "20250102T120000.000Z",
        preparation_start_time="20250101T000000.000Z",
        type=war_type,
        is_cwl=False,
        war_tag=None,
        league_group=None,
        attacks=[],
    )


def _make_member(tag="#P1", name="Player1", th=16, map_pos=1, attacks=None):
    return _obj(
        tag=tag,
        name=name,
        town_hall=th,
        map_position=map_pos,
        role=None,
        donated=None,
        received=None,
        attacks=attacks or [],
        previous_best_opponent_attack=None,
    )


def _make_attack(attacker="#A", defender="#D", stars=3, destruction=100.0, order=1):
    return _obj(
        attacker_tag=attacker,
        defender_tag=defender,
        stars=stars,
        destruction=destruction,
        order=order,
        duration=None,
        fresh=None,
    )


# ===========================================================================
# save_war_object — end to end
# ===========================================================================

class TestSaveWarObjectBasic:
    def _cm(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager()
        cm.db_manager = MagicMock()
        return cm

    def test_saves_json_to_temp(self, tmp_path, monkeypatch):
        cm = self._cm()
        war = _make_war_obj()
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "temp").mkdir(parents=True)
        (tmp_path / "data" / "archive").mkdir(parents=True)

        result = cm.save_war_object(war)
        assert result is True

        files = list((tmp_path / "data" / "temp").glob("shard_*/*.json"))
        assert len(files) == 1

        with open(files[0]) as f:
            data = json.load(f)
        assert data["state"] == "in_war"
        assert data["clan"]["tag"] == "#MYCLAN1"
        assert data["opponent"]["tag"] == "#ENEMY1"

    def test_invalid_clan_data_returns_false(self):
        cm = self._cm()
        war = _obj(state=_obj(name="in_war"), clan=None, opponent=None)
        result = cm.save_war_object(war)
        assert result is False

    def test_friendly_war_skipped(self):
        cm = self._cm()
        war = _make_war_obj(war_type="friendly")
        result = cm.save_war_object(war)
        assert result is False

    def test_swaps_clans_when_tracked_is_opponent(self, tmp_path, monkeypatch):
        cm = self._cm()
        war = _make_war_obj(my_clan_tag="#OTHER", enemy_clan_tag="#TRACKED")
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "temp").mkdir(parents=True)
        (tmp_path / "data" / "archive").mkdir(parents=True)

        result = cm.save_war_object(war, tracked_clan_tag="#TRACKED")
        assert result is True

        files = list((tmp_path / "data" / "temp").glob("shard_*/*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert data["clan"]["tag"] == "#TRACKED"
        assert data["opponent"]["tag"] == "#OTHER"


class TestSaveWarObjectMembers:
    def _cm(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager()
        cm.db_manager = MagicMock()
        return cm

    def test_serializes_members_with_attacks(self, tmp_path, monkeypatch):
        cm = self._cm()
        atk = _make_attack("#P1", "#E1", 3, 100.0, 1)
        member = _make_member(tag="#P1", attacks=[atk])
        enemy_member = _make_member(tag="#E1")
        war = _make_war_obj(members=[member], enemy_members=[enemy_member])

        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "temp").mkdir(parents=True)
        (tmp_path / "data" / "archive").mkdir(parents=True)

        result = cm.save_war_object(war)
        assert result is True

        files = list((tmp_path / "data" / "temp").glob("shard_*/*.json"))
        with open(files[0]) as f:
            data = json.load(f)

        clan_members = data["clan"]["members"]
        assert len(clan_members) == 1
        assert clan_members[0]["tag"] == "#P1"
        assert len(clan_members[0]["attacks"]) == 1
        assert clan_members[0]["attacks"][0]["stars"] == 3

    def test_defensive_stars_calculated(self, tmp_path, monkeypatch):
        cm = self._cm()
        # Enemy attacks our member
        enemy_atk = _make_attack("#E1", "#P1", 2, 60.0, 1)
        my_member = _make_member(tag="#P1")
        enemy_member = _make_member(tag="#E1", attacks=[enemy_atk])
        war = _make_war_obj(members=[my_member], enemy_members=[enemy_member])

        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "temp").mkdir(parents=True)
        (tmp_path / "data" / "archive").mkdir(parents=True)

        cm.save_war_object(war)

        files = list((tmp_path / "data" / "temp").glob("shard_*/*.json"))
        with open(files[0]) as f:
            data = json.load(f)

        my_p = data["clan"]["members"][0]
        assert my_p["opponentAttacks"] == 2  # Total defensive stars
        assert my_p["bestOpponentAttack"]["stars"] == 2


class TestSaveWarObjectBadge:
    def _cm(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager()
        cm.db_manager = MagicMock()
        return cm

    def test_badge_serialization(self, tmp_path, monkeypatch):
        cm = self._cm()
        badge = _obj(name="Shield", url="http://example.com", small="s", medium="m", large="l")
        war = _make_war_obj()
        war.clan.badge = badge

        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "temp").mkdir(parents=True)
        (tmp_path / "data" / "archive").mkdir(parents=True)

        cm.save_war_object(war)

        files = list((tmp_path / "data" / "temp").glob("shard_*/*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert data["clan"]["badge"]["name"] == "Shield"
        assert data["clan"]["badge"]["url"] == "http://example.com"


class TestSaveWarObjectWarEnded:
    def _cm(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager()
        cm.db_manager = MagicMock()
        return cm

    def test_old_war_ended_not_finalized_is_written(self, tmp_path, monkeypatch):
        cm = self._cm()
        # War ended 48 hours ago but is NOT in archive/history — must still be written
        end_time = datetime.now(timezone.utc) - timedelta(hours=48)
        war = _make_war_obj(state="war_ended", end_time=_obj(time=end_time))

        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "temp").mkdir(parents=True)
        (tmp_path / "data" / "archive").mkdir(parents=True)

        result = cm.save_war_object(war)
        assert result is True
        files = list((tmp_path / "data" / "temp").glob("shard_*/*.json"))
        assert len(files) == 1

    def test_stale_invalid_state_skipped(self, tmp_path, monkeypatch):
        cm = self._cm()
        # in_war state but end_time is 48h in the past — corrupt/stale API response
        end_time = datetime.now(timezone.utc) - timedelta(hours=48)
        war = _make_war_obj(state="in_war", end_time=_obj(time=end_time))

        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "temp").mkdir(parents=True)
        (tmp_path / "data" / "archive").mkdir(parents=True)

        result = cm.save_war_object(war)
        assert result is False

    def test_finalized_war_skipped(self, tmp_path, monkeypatch):
        cm = self._cm()
        # War ended 2 hours ago (within 24h threshold)
        end_time = datetime.now(timezone.utc) - timedelta(hours=2)
        start_time = datetime.now(timezone.utc) - timedelta(hours=26)
        war = _make_war_obj(
            state="war_ended",
            end_time=_obj(time=end_time),
            start_time=_obj(time=start_time),
        )

        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "temp").mkdir(parents=True)
        (tmp_path / "archive").mkdir(parents=True)

        # Create archive file to simulate already-archived (new naming with timestamp)
        import re as _re
        my_file_tag = _re.sub(r'[^A-Z0-9]', '', "#MYCLAN1".upper())
        enemy_file_tag = _re.sub(r'[^A-Z0-9]', '', "#ENEMY1".upper())
        war_ts = start_time.strftime("%Y%m%d%H%M")
        import QBcsvhandling
        archive_shard = tmp_path / QBcsvhandling.get_war_shard_dir(my_file_tag, "archive")
        archive_shard.mkdir(parents=True, exist_ok=True)
        archive_file = archive_shard / f"{my_file_tag}_{enemy_file_tag}_{war_ts}_war_data.json"
        archive_file.write_text("{}")

        # Mock _is_war_in_history to return True
        import QBhelperfunctions
        monkeypatch.setattr(QBhelperfunctions, "_is_war_in_history", lambda ct, wid: True)  # type: ignore[misc]

        # Mock DB state as war_ended so FINALIZED-WAR-SKIP fires
        cm.db_manager.get_war_summary_state_sync.return_value = "war_ended"  # type: ignore[union-attr]

        result = cm.save_war_object(war)
        assert result is False


class TestSaveWarObjectLeagueGroup:
    def _cm(self):
        from qapbot.cache_manager import CacheManager
        cm = CacheManager()
        cm.db_manager = MagicMock()
        return cm

    def test_league_group_serialized(self, tmp_path, monkeypatch):
        cm = self._cm()
        war = _make_war_obj()
        war.league_group = _obj(tag="#LG1", state=_obj(name="inWar"), season="2025-01")

        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "temp").mkdir(parents=True)
        (tmp_path / "data" / "archive").mkdir(parents=True)

        cm.save_war_object(war)

        files = list((tmp_path / "data" / "temp").glob("shard_*/*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert data["league_group"]["tag"] == "#LG1"
        assert data["league_group"]["season"] == "2025-01"


# ===========================================================================
# _get_players_needing_reminders
# ===========================================================================

class TestGetPlayersNeedingReminders:
    def _fn(self):
        from qapbot.war_notifications import _get_players_needing_reminders
        return _get_players_needing_reminders

    def _war_data(self, members=None, attacks_per_member=2):
        return {
            "clan": {
                "name": "TestClan",
                "members": members or [],
            },
            "opponent": {"name": "EnemyClan"},
            "attacks_per_member": attacks_per_member,
            "endTime": (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y%m%dT%H%M%S.000Z"),
        }

    def test_no_members(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 5.0)  # type: ignore[misc]
        result = self._fn()("#CLAN1", "WAR1", self._war_data())
        assert result == []

    def test_player_completed_all_attacks(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 5.0)  # type: ignore[misc]
        members = [{"tag": "#P1", "name": "P1", "attacks": [{"stars": 3}, {"stars": 2}]}]
        result = self._fn()("#CLAN1", "WAR1", self._war_data(members=members))
        assert result == []

    def test_unregistered_player_skipped(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 5.0)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_player_discord_id", lambda tag: None)  # type: ignore[misc]
        members = [{"tag": "#P1", "name": "P1", "attacks": []}]
        result = self._fn()("#CLAN1", "WAR1", self._war_data(members=members))
        assert result == []

    def test_war_type_mismatch_skipped(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 5.0)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_player_discord_id", lambda tag: "123")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._should_notify_for_war_type", lambda did, wd: False)  # type: ignore[misc]
        members = [{"tag": "#P1", "name": "P1", "attacks": []}]
        result = self._fn()("#CLAN1", "WAR1", self._war_data(members=members))
        assert result == []

    def test_already_notified_skipped(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 5.0)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_player_discord_id", lambda tag: "123")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._should_notify_for_war_type", lambda did, wd: True)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._should_send_notification", lambda wid, pt, did, hr: False)  # type: ignore[misc]
        members = [{"tag": "#P1", "name": "P1", "attacks": []}]
        result = self._fn()("#CLAN1", "WAR1", self._war_data(members=members))
        assert result == []

    def test_eligible_player_notified(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 5.0)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_player_discord_id", lambda tag: "123")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._should_notify_for_war_type", lambda did, wd: True)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._should_send_notification", lambda wid, pt, did, hr: True)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_buddy_watcher_discord_ids", lambda tag: [])  # type: ignore[misc]
        members = [{"tag": "#P1", "name": "P1", "attacks": [{"stars": 3}]}]
        result = self._fn()("#CLAN1", "WAR1", self._war_data(members=members))
        assert len(result) == 1
        assert result[0]["player_tag"] == "#P1"
        assert result[0]["attacks_remaining"] == 1
        assert result[0]["discord_id"] == "123"
        assert result[0]["is_buddy"] is False

    def test_buddy_watcher_notified(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 5.0)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_player_discord_id", lambda tag: "123")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._should_notify_for_war_type", lambda did, wd: True)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._should_send_notification", lambda wid, pt, did, hr: True)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_buddy_watcher_discord_ids", lambda tag: ["456"])  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._should_send_buddy_notification", lambda wid, pt, did, hr: True)  # type: ignore[misc]

        # Mock CACHE for buddy watcher
        cache = MagicMock()
        cache.user_accounts = {
            "456": {"notification_settings": {"war_reminders": True}},
        }
        monkeypatch.setattr("qapbot.war_notifications.CACHE", cache)

        members = [{"tag": "#P1", "name": "P1", "attacks": [{"stars": 3}]}]
        result = self._fn()("#CLAN1", "WAR1", self._war_data(members=members))
        # Should have both: the player notification and the buddy notification
        assert len(result) == 2
        buddy = [r for r in result if r["is_buddy"]]
        assert len(buddy) == 1
        assert buddy[0]["discord_id"] == "456"

    def test_hours_remaining_none_returns_empty(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: None)  # type: ignore[misc]
        members = [{"tag": "#P1", "name": "P1", "attacks": []}]
        result = self._fn()("#CLAN1", "WAR1", self._war_data(members=members))
        assert result == []

    def test_multiple_attacks_remaining(self, monkeypatch):
        monkeypatch.setattr("qapbot.war_notifications._get_hours_until_war_end", lambda wd: 3.0)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_player_discord_id", lambda tag: "789")  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._should_notify_for_war_type", lambda did, wd: True)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._should_send_notification", lambda wid, pt, did, hr: True)  # type: ignore[misc]
        monkeypatch.setattr("qapbot.war_notifications._get_buddy_watcher_discord_ids", lambda tag: [])  # type: ignore[misc]
        # No attacks used at all
        members = [{"tag": "#P2", "name": "P2", "attacks": []}]
        result = self._fn()("#CLAN1", "WAR2", self._war_data(members=members, attacks_per_member=2))
        assert len(result) == 1
        assert result[0]["attacks_remaining"] == 2
