from __future__ import annotations
# pyright: reportPrivateUsage=false

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest


class _FakeCache:
    def __init__(self) -> None:
        self.user_accounts: dict[str, dict[str, object]] = {}
        self.notification_state: dict[str, dict[str, object]] = {}
        self.persist_player_notification = AsyncMock()
        self.persist_channel_notification = AsyncMock()


def _identity_t(key: str, **kwargs: Any) -> str:
    _ = kwargs
    return key


@pytest.mark.smoke
def test_get_hours_until_war_end_parses_and_handles_invalid():
    import qapbot.war_notifications as wn

    assert wn._get_hours_until_war_end({"end_time": "<Timestamp seconds_until=7200>"}) == 2.0
    assert wn._get_hours_until_war_end({"end_time": "invalid"}) is None


@pytest.mark.smoke
def test_get_war_id_strips_hashes():
    import qapbot.war_notifications as wn

    war_id = wn._get_war_id("#CLAN01", {"opponent": {"tag": "#OPP01"}})
    assert war_id == "CLAN01_OPP01"


@pytest.mark.smoke
def test_should_notify_for_war_type_modes(monkeypatch: pytest.MonkeyPatch):
    import qapbot.war_notifications as wn

    cache = _FakeCache()
    cache.user_accounts = {
        "u1": {"notification_settings": {"notification_type": "all_wars"}},
        "u2": {"notification_settings": {"notification_type": "cwl_only"}},
        "u3": {"notification_settings": {"notification_type": "unknown"}},
    }
    monkeypatch.setattr(wn, "CACHE", cache)

    assert wn._should_notify_for_war_type("u1", {"type": "random"}) is True
    assert wn._should_notify_for_war_type("u2", {"type": "cwl", "is_cwl": False}) is True
    assert wn._should_notify_for_war_type("u2", {"type": "random", "is_cwl": True}) is True
    assert wn._should_notify_for_war_type("u2", {"type": "random", "is_cwl": False}) is False
    assert wn._should_notify_for_war_type("u3", {"type": "random"}) is True


@pytest.mark.smoke
def test_get_player_discord_id_and_already_notified(monkeypatch: pytest.MonkeyPatch):
    import qapbot.war_notifications as wn

    cache = _FakeCache()
    cache.notification_state = {"WAR1": {"notified_players": {"P1": {"x": 1}}}}
    monkeypatch.setattr(wn, "CACHE", cache)

    # P1 has war_reminders=True → in index; P2 has war_reminders=False → excluded from index
    monkeypatch.setattr(wn, "_notification_player_index", {"P1": "u1"})

    assert wn._get_player_discord_id("#P1") == "u1"
    assert wn._get_player_discord_id("#P2") is None
    assert wn._is_already_notified("WAR1", "#P1") is True
    assert wn._is_already_notified("WAR1", "#P9") is False


@pytest.mark.smoke
def test_should_send_notification_once_and_repeated(monkeypatch: pytest.MonkeyPatch):
    import qapbot.war_notifications as wn

    cache = _FakeCache()
    cache.user_accounts = {
        "u_once": {"notification_settings": {"notification_mode": "once", "hours_before_end": 6}},
        "u_rep": {"notification_settings": {"notification_mode": "repeated", "hours_before_end": 6}},
    }
    monkeypatch.setattr(wn, "CACHE", cache)

    # Once mode
    cache.notification_state = {"WAR1": {"notified_players": {}}}
    assert wn._should_send_notification("WAR1", "#P1", "u_once", 3.0) is True
    cache.notification_state = {"WAR1": {"notified_players": {"P1": {"notification_time": "2026-01-01 00:00:00"}}}}
    assert wn._should_send_notification("WAR1", "#P1", "u_once", 3.0) is False

    # Repeated mode: outside threshold => false
    cache.notification_state = {"WAR1": {"notified_players": {}}}
    assert wn._should_send_notification("WAR1", "#P1", "u_rep", 7.0) is False

    # Repeated mode: no previous notification => true
    assert wn._should_send_notification("WAR1", "#P1", "u_rep", 3.0) is True

    now = datetime.now()
    recent = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    old = (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

    cache.notification_state = {"WAR1": {"notified_players": {"P1": {"notification_time": recent}}}}
    assert wn._should_send_notification("WAR1", "#P1", "u_rep", 3.0) is False

    cache.notification_state = {"WAR1": {"notified_players": {"P1": {"notification_time": old}}}}
    assert wn._should_send_notification("WAR1", "#P1", "u_rep", 3.0) is True


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_record_notification_and_channel_notification(monkeypatch: pytest.MonkeyPatch):
    import qapbot.war_notifications as wn

    cache = _FakeCache()
    monkeypatch.setattr(wn, "CACHE", cache)

    await wn._record_notification(
        "WAR2",
        {
            "player_tag": "#P1",
            "player_name": "Alice",
            "discord_id": "u1",
            "attacks_remaining": 1,
        },
    )

    assert "WAR2" in cache.notification_state
    war2_state = cast(dict[str, Any], cache.notification_state["WAR2"])
    notified_players = cast(dict[str, Any], war2_state["notified_players"])
    assert "P1" in notified_players
    cache.persist_player_notification.assert_awaited_once_with("WAR2", "P1")

    await wn._record_channel_notification("WAR2", "guild1", "ClanA", "ClanB")

    assert wn._is_channel_notification_sent("WAR2", "guild1") is True
    cache.persist_channel_notification.assert_awaited_once_with("WAR2", "guild1")


@pytest.mark.smoke
def test_format_aggregated_message_contains_players(monkeypatch: pytest.MonkeyPatch):
    import qapbot.war_notifications as wn

    monkeypatch.setattr(wn, "t", _identity_t)

    msg = wn._format_aggregated_reminder_message(
        "Alex",
        [
            {
                "player_name": "Alice",
                "attacks_remaining": 1,
                "hours_remaining": 1.5,
                "clan_name": "ClanA",
                "opponent_name": "ClanB",
            },
            {
                "player_name": "Bob",
                "attacks_remaining": 2,
                "hours_remaining": 1.5,
                "clan_name": "ClanA",
                "opponent_name": "ClanB",
            },
        ],
        discord_user_id="u1",
        guild_id=1,
    )

    assert "Alice" in msg
    assert "Bob" in msg
    assert "ui_components.war_notification_dm.header" in msg


@pytest.mark.smoke
def test_get_active_wars_filters_by_state_and_time(monkeypatch: pytest.MonkeyPatch):
    """_get_active_wars uses CACHE.temp_war_metadata + temp_war_stats (zero I/O).
    Only in_war clans with positive hours remaining are returned.
    """
    import qapbot.war_notifications as wn

    from qapbot.cache_manager import CACHE
    original_metadata = CACHE.temp_war_metadata.copy()
    original_stats = CACHE.temp_war_stats.copy()
    original_in_war = CACHE.in_war_clan_tags.copy()

    # Clan1: active in_war with remaining time — should be returned.
    # Uses seconds_until-only fallback (no datetime component) so the fallback
    # path in _get_hours_until_war_end is exercised and returns a positive value.
    CACHE.temp_war_metadata["#CLAN1"] = {
        "state": "in_war",
        "start_time": "",
        "end_time": "<Timestamp seconds_until=3600>",
    }
    CACHE.temp_war_stats["#CLAN1"] = {
        "#MEM1": {
            "WarID": "OPP1_202604030000",
            "Max_Attacks": 2,
            "PlayerID": "#MEM1",
            "Player": "Player1",
            "Attacks": 0,
        }
    }

    # Clan2: war_ended — must be filtered out by state check.
    # It must NOT be in in_war_clan_tags (the set is the primary filter now).
    CACHE.temp_war_metadata["#CLAN2"] = {
        "state": "war_ended",
        "start_time": "",
        "end_time": "<Timestamp seconds_until=-10>",
    }
    CACHE.temp_war_stats["#CLAN2"] = {
        "#MEM2": {
            "WarID": "OPP2_202604030000",
            "Max_Attacks": 2,
            "PlayerID": "#MEM2",
            "Player": "Player2",
            "Attacks": 0,
        }
    }

    # _get_active_wars now iterates CACHE.in_war_clan_tags (O(n_active)) instead of
    # all temp_war_metadata entries. Only CLAN1 is in_war; CLAN2 is war_ended.
    CACHE.in_war_clan_tags = {"#CLAN1"}

    try:
        wars = wn._get_active_wars()
        assert len(wars) == 1
        assert wars[0][0] == "#CLAN1"
        assert wars[0][1] == "CLAN1_OPP1"   # "{CLAN_CLEAN}_{OPP_CLEAN}"
    finally:
        CACHE.temp_war_metadata = original_metadata
        CACHE.temp_war_stats = original_stats
        CACHE.in_war_clan_tags = original_in_war


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_unlinked_player_skipped_no_dm_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: discord_id='UNASSIGNED' must NOT reach _send_aggregated_war_reminder_dm.

    Before the fix, int('UNASSIGNED') raised ValueError 23 times during a single war
    because the player 'Conan2502' had no linked Discord account.
    """
    import qapbot.war_notifications as wn

    war_data: dict = {
        "state": "in_war",
        "end_time": "<Timestamp seconds_until=3600>",
        "clan": {"tag": "#CLAN1", "name": "ClanA"},
        "opponent": {"tag": "#OPP1", "name": "ClanB"},
    }

    # Player has no linked Discord account – sentinel value stored in DB
    unlinked_player = {
        "player_tag": "#P1",
        "player_name": "Conan2502",
        "discord_id": "UNASSIGNED",
        "attacks_remaining": 1,
        "hours_remaining": 1.0,
        "clan_name": "ClanA",
        "opponent_name": "ClanB",
        "is_buddy": False,
    }

    monkeypatch.setattr(wn, "_get_players_needing_reminders", lambda *_: [unlinked_player])
    monkeypatch.setattr(wn, "_get_players_with_attacks_remaining", lambda *_: [])
    monkeypatch.setattr(wn, "_send_channel_war_notification", AsyncMock(return_value=0))

    dm_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(wn, "_send_aggregated_war_reminder_dm", dm_mock)

    dm_sent, _ = await wn._process_war_for_notifications("#CLAN1", "CLAN1_OPP1", war_data)

    dm_mock.assert_not_called()
    assert dm_sent == 0, "No DMs should be sent for unlinked players"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_unlinked_player_does_not_block_valid_users(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression companion: a valid user in the same list still receives their DM."""
    import qapbot.war_notifications as wn

    war_data: dict = {
        "state": "in_war",
        "end_time": "<Timestamp seconds_until=3600>",
        "clan": {"tag": "#CLAN1", "name": "ClanA"},
        "opponent": {"tag": "#OPP1", "name": "ClanB"},
    }

    unlinked = {
        "player_tag": "#P1",
        "player_name": "Conan2502",
        "discord_id": "UNASSIGNED",
        "attacks_remaining": 1,
        "hours_remaining": 1.0,
        "clan_name": "ClanA",
        "opponent_name": "ClanB",
        "is_buddy": False,
    }
    linked = {
        "player_tag": "#P2",
        "player_name": "ValidPlayer",
        "discord_id": "123456789012345678",
        "attacks_remaining": 1,
        "hours_remaining": 1.0,
        "clan_name": "ClanA",
        "opponent_name": "ClanB",
        "is_buddy": False,
    }

    monkeypatch.setattr(wn, "_get_players_needing_reminders", lambda *_: [unlinked, linked])
    monkeypatch.setattr(wn, "_get_players_with_attacks_remaining", lambda *_: [])
    monkeypatch.setattr(wn, "_send_channel_war_notification", AsyncMock(return_value=0))

    record_mock = AsyncMock()
    monkeypatch.setattr(wn, "_record_notification", record_mock)

    dm_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(wn, "_send_aggregated_war_reminder_dm", dm_mock)

    # Stub CONFIG to avoid attribute errors on notification_batch_delay
    class _FakeConfig:
        notification_batch_delay = 0

    monkeypatch.setattr(wn, "CONFIG", _FakeConfig())

    dm_sent, _ = await wn._process_war_for_notifications("#CLAN1", "CLAN1_OPP1", war_data)

    # DM sent exactly once, only for the linked player
    dm_mock.assert_awaited_once()
    called_discord_id = dm_mock.call_args[0][0]
    assert called_discord_id == "123456789012345678"
    assert dm_sent == 1
