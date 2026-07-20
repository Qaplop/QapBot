from __future__ import annotations
# pyright: reportPrivateUsage=false

import re
from datetime import datetime, timedelta
from typing import Any, Dict

import pytest


class _FakeCache:
    def __init__(self) -> None:
        self.clan_name_cache: Dict[str, Dict[str, str]] = {}
        self.clan_families: Dict[str, Dict[str, Any]] = {}
        self.subscriptions: Dict[str, Dict[str, list[Dict[str, Any]]]] = {}
        self.server_config: Dict[str, Dict[str, list[str]]] = {}
        self.user_accounts: Dict[str, Dict[str, Any]] = {}
        self._temp_war_stats: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._history: Dict[str, list[Dict[str, Any]]] = {}

    def get_clan_name(self, clan_tag: str, default: str = "Unknown") -> str:
        return self.clan_name_cache.get(clan_tag, {}).get("name", default)

    def get_temp_war_stats(self, clan_tag: str) -> Dict[str, Dict[str, Any]]:
        return self._temp_war_stats.get(clan_tag, {})

    def get_clan_history(self, clan_tag: str) -> list[Dict[str, Any]]:
        return self._history.get(clan_tag, [])


@pytest.mark.smoke
def test_generate_family_tag_format() -> None:
    from qapbot.QBdiscocmdshelper import generate_family_tag

    tag = generate_family_tag("Alpha Family", ["#AAA", "#BBB"])

    assert re.fullmatch(r"#[A-Z0-9]{10}", tag)


@pytest.mark.smoke
def test_normalize_family_tag_handles_hash_o_replacement_and_invalid() -> None:
    from qapbot.QBdiscocmdshelper import normalize_family_tag

    assert normalize_family_tag("3457o457b9") == "#34570457B9"
    assert normalize_family_tag("#abc123") is None


@pytest.mark.smoke
def test_build_autocomplete_choices_filters_and_limits() -> None:
    from qapbot.QBdiscocmdshelper import _build_autocomplete_choices

    items = [("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")]
    choices = _build_autocomplete_choices(items, current="a", max_choices=1)

    assert len(choices) == 1
    assert choices[0].value == "a"
    assert "Alpha" in choices[0].name


@pytest.mark.smoke
def test_display_name_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper

    cache = _FakeCache()
    cache.clan_name_cache = {"#C1": {"name": "Clan One"}}
    cache.clan_families = {"#FAM0000001": {"name": "Fam One", "clans": ["#C1"]}}
    monkeypatch.setattr(helper, "CACHE", cache)

    assert helper.get_clan_display_name("#C1") == "Clan One (#C1)"
    assert helper.get_family_display_name("#FAM0000001") == "Fam One (#FAM0000001)"
    assert helper.get_family_display_name("#UNKNOWN000") == "#UNKNOWN000"
    assert helper.get_clan_or_family_display_name("#FAM0000001") == "Fam One (#FAM0000001)"
    assert helper.get_clan_or_family_display_name("#C1") == "Clan One (#C1)"


@pytest.mark.smoke
def test_is_already_subscribed_matches_mode_and_year() -> None:
    from qapbot.QBdiscocmdshelper import is_already_subscribed

    subs: list[dict[str, Any]] = [
        {"clan_tag": "#C1", "subscription_type": "attack", "year": None},
        {},
        {"clan_tag": "#C1", "subscription_type": "defense", "year": "current"},
    ]

    assert is_already_subscribed(subs, "#C1", "attack", None) is True
    assert is_already_subscribed(subs, "#C1", "attack", "current") is False


@pytest.mark.smoke
def test_get_guild_subscribed_clans_expands_families(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper

    cache = _FakeCache()
    cache.clan_families = {
        "#FAM0000001": {"name": "Family", "clans": ["#C1", "#C2"]},
    }
    cache.subscriptions = {
        "123": {
            "10": [
                {"clan_tag": "PLAYERREGISTRATION", "subscription_type": "playerregistration"},
                {"clan_tag": "#FAM0000001", "subscription_type": "attack"},
                {"clan_tag": "#C2", "subscription_type": "defense"},
            ]
        }
    }
    monkeypatch.setattr(helper, "CACHE", cache)

    out = helper.get_guild_subscribed_clans(123)

    assert out == ["#C1", "#C2"]


@pytest.mark.smoke
def test_get_guild_clans_including_member_config_combines_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper

    cache = _FakeCache()
    cache.clan_families = {
        "#FAM0000001": {"name": "Family", "clans": ["#C1", "#C2"]},
        "#FAM0000002": {"name": "Family2", "clans": ["#C3"]},
    }
    cache.subscriptions = {"321": {"20": [{"clan_tag": "#C1", "subscription_type": "attack"}]}}
    cache.server_config = {
        "321": {
            "member_families": ["#FAM0000002"],
            "member_clans": ["#C4"],
        }
    }
    monkeypatch.setattr(helper, "CACHE", cache)

    out = helper.get_guild_clans_including_member_config(321)

    assert out == ["#C1", "#C2", "#C3", "#C4"]


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_resolve_clan_or_family_tag_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper

    cache = _FakeCache()
    cache.clan_families = {"#FAM0000001": {"name": "Family", "clans": ["#C1"]}}
    cache.clan_name_cache = {"#C1": {"name": "Clan One"}}
    monkeypatch.setattr(helper, "CACHE", cache)

    ok1, tag1, is_family1 = await helper.resolve_clan_or_family_tag("#FAM0000001")
    ok2, tag2, is_family2 = await helper.resolve_clan_or_family_tag("Clan One")
    ok3, tag3, is_family3 = await helper.resolve_clan_or_family_tag("Nope")

    assert (ok1, tag1, is_family1) == (True, "#FAM0000001", True)
    assert (ok2, tag2, is_family2) == (True, "#C1", False)
    assert (ok3, tag3, is_family3) == (False, None, False)


@pytest.mark.smoke
def test_get_user_player_handles_invalid_players_shape() -> None:
    from qapbot.QBdiscocmdshelper import get_user_player

    assert get_user_player({}, "#P1") is None
    assert get_user_player({"players": "bad"}, "#P1") is None
    assert get_user_player({"players": [{"player_tag": "#P1", "player_name": "A"}]}, "#P1") == {
        "player_tag": "#P1",
        "player_name": "A",
    }


@pytest.mark.smoke
def test_get_registered_player_ids_ignores_unassigned(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper

    cache = _FakeCache()
    cache.user_accounts = {
        "111": {"players": [{"player_tag": "#P1"}, {"player_tag": "#P2"}]},
        "UNASSIGNED": {"players": [{"player_tag": "#P9"}]},
    }
    monkeypatch.setattr(helper, "CACHE", cache)

    out = helper._get_registered_player_ids()

    assert out == {"#P1", "#P2"}


@pytest.mark.smoke
def test_calculate_activity_score_counts_recent_history_and_temp(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.QBdiscocmdshelper as helper

    cache = _FakeCache()
    now = datetime.now()
    recent = (now - timedelta(days=3)).isoformat()
    old = (now - timedelta(days=80)).isoformat()

    cache._history = {
        "#C1": [
            {"PlayerID": "#P1", "Date": recent, "Attacks": 2},
            {"PlayerID": "#P1", "Date": old, "Attacks": 5},
            {"PlayerID": "#P2", "Date": recent, "Attacks": 9},
            {"PlayerID": "#P1", "Date": "bad-date", "Attacks": 7},
        ]
    }
    cache._temp_war_stats = {"#C1": {"#P1": {"Attacks": 1}}}
    monkeypatch.setattr(helper, "CACHE", cache)

    score = helper._calculate_activity_score("#P1", ["#C1"])

    assert score == 3
